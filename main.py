import os
import io
import sys
import contextlib
import requests
import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import pytz

# ================= 0. 推送配置函数 =================
def send_pushplus(title, content):
    token = os.environ.get('PUSH_TOKEN')
    if not token:
        print("⚠️ 未检测到 PUSH_TOKEN，跳过推送。")
        return

    url = 'http://www.pushplus.plus/send'
    # 微信显示优化: 换行符替换
    content = content.replace('\n', '\n\n') 
    
    data = {
        "token": token,
        "title": title,
        "content": content,
        "template": "markdown"
    }
    
    try:
        response = requests.post(url, json=data)
        if response.status_code == 200:
            print("✅ 推送发送成功！")
        else:
            print(f"❌ 推送发送失败: {response.text}")
    except Exception as e:
        print(f"❌ 推送请求出错: {e}")

# ================= 1. 策略逻辑封装 =================
def run_strategy_logic():
    # ------------------ 配置参数 ------------------
    symbol_1x = 'QQQ'   
    symbol_2x = 'QLD'   
    symbol_3x = 'TQQQ'
    symbol_spx = 'SPY'
    indicator_asset = '^NDX'
    vix_asset = '^VIX'

    # 🔥 核心参数 (AI 优化版)
    ma_window = 170      
    rsi_window = 14
    rsi_buy_3x = 65      
    rsi_sell_3x = 80     
    bear_buffer = 0.01   
    bull_buffer = 0.0    
    vix_threshold = 40.0 
    transaction_cost = 0.001 

    etf_map = {1: symbol_1x, 2: symbol_2x, 3: symbol_3x}
    name_map = {1: f'{symbol_1x} (1x 防守)', 2: f'{symbol_2x} (2x 常态)', 3: f'{symbol_3x} (3x 进攻)'}

    # ------------------ 市场状态 ------------------
    ny_tz = pytz.timezone('America/New_York')
    now_ny = datetime.now(ny_tz)
    is_market_open = False
    if 0 <= now_ny.weekday() <= 4:
        if (now_ny.hour > 9 or (now_ny.hour == 9 and now_ny.minute >= 30)) and now_ny.hour < 16:
            is_market_open = True

    print(f"## 📅 时间: {now_ny.strftime('%Y-%m-%d %H:%M')} (美东)")
    if is_market_open: print("**🔔 状态: 美股【交易中】**")
    else: print("**💤 状态: 美股【已收盘/盘前】**")

    # ------------------ 数据获取 ------------------
    try:
        print("⏳ 正在下载数据 (Max)...")
        # 改为 max 以确保计算 10 年数据
        raw_data = yf.download(
            [symbol_1x, symbol_2x, symbol_3x, symbol_spx, indicator_asset, vix_asset], 
            period="max", interval="1d", auto_adjust=False, progress=False
        )
        
        if isinstance(raw_data.columns, pd.MultiIndex):
            adj = raw_data['Adj Close']
            data = adj[[symbol_1x, symbol_2x, symbol_3x, symbol_spx, indicator_asset]].ffill().dropna()
            vix_data = adj[vix_asset].reindex(data.index).ffill().fillna(0)
        else:
            # 容错单列
            data = raw_data['Adj Close'].ffill().dropna()
            vix_data = pd.Series(0, index=data.index)
            
        # 截取最近 10 年 + buffer (用于计算)
        # 至少保留 3000 天用于计算指标和回测
        if len(data) > 3000:
            data = data.iloc[-3000:]
            vix_data = vix_data.iloc[-3000:]

    except Exception as e:
        print(f"❌ 数据下载失败: {e}")
        return 

    if not data.empty:
        # ------------------ 策略计算 ------------------
        sma = data[indicator_asset].rolling(window=ma_window).mean()
        delta = data[indicator_asset].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=rsi_window).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=rsi_window).mean().replace(0, 1e-10)
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))

        signals = [] 
        current_state = 2 

        for i in range(len(data)):
            price = data[indicator_asset].iloc[i]
            ma = sma.iloc[i]
            r = rsi.iloc[i]
            vix = vix_data.iloc[i]
            
            if pd.isna(ma): 
                signals.append(2)
                continue

            if vix > vix_threshold:
                current_state = 1
            elif price < ma * (1 - bear_buffer):
                current_state = 1 
            else:
                if current_state == 1:
                    if price > ma * (1 + bull_buffer):
                        current_state = 2
                else:
                    if r > rsi_sell_3x:
                        current_state = 2 
                    elif r < rsi_buy_3x:
                        current_state = 3 
            signals.append(current_state)

        # ------------------ 收益回测 ------------------
        ret_1x = data[symbol_1x].pct_change().fillna(0)
        ret_2x = data[symbol_2x].pct_change().fillna(0)
        ret_3x = data[symbol_3x].pct_change().fillna(0)
        ret_spx = data[symbol_spx].pct_change().fillna(0)
        
        pos_series = pd.Series(signals, index=data.index).shift(1).fillna(2)
        strat_daily_ret = pd.Series(0.0, index=data.index)
        strat_daily_ret[pos_series == 1] = ret_1x
        strat_daily_ret[pos_series == 2] = ret_2x
        strat_daily_ret[pos_series == 3] = ret_3x
        strat_daily_ret -= ((pos_series != pos_series.shift(1)).astype(int) * transaction_cost)
        
        strat_cum = (1 + strat_daily_ret).cumprod()
        bench_cum_1x = (1 + ret_1x).cumprod()
        bench_cum_2x = (1 + ret_2x).cumprod()
        bench_cum_3x = (1 + ret_3x).cumprod()
        bench_cum_spx = (1 + ret_spx).cumprod()

        def get_period_return(cum_series, days_lookback):
            # 只有当数据长度足够时才计算
            if len(cum_series) <= days_lookback: return None 
            
            # 使用真实交易日计算 (美股一年约252天)
            # days_lookback 是日历天数，转换为交易日索引
            # 简单估算：日历天数 * (252/365)
            # 为了更精准，直接用 date search
            target_date = cum_series.index[-1] - timedelta(days=days_lookback)
            
            # 确保 target_date 不早于数据起始日期
            if target_date < cum_series.index[0]:
                target_date = cum_series.index[0]
            
            idx = cum_series.index.searchsorted(target_date)
            # 如果 idx 越界，修正
            if idx >= len(cum_series): idx = len(cum_series) - 1
            
            return (cum_series.iloc[-1] / cum_series.iloc[idx]) - 1

        # ------------------ 调仓记录 ------------------
        switch_history = []
        temp_signal = signals[-1]
        temp_end_idx = len(signals) - 1
        for i in range(len(signals) - 2, -1, -1):
            if signals[i] != temp_signal:
                switch_history.append({
                    'date': data.index[i+1].strftime('%Y-%m-%d'),
                    'action': f"{etf_map[signals[i]]} -> {etf_map[temp_signal]}",
                    'days': temp_end_idx - i
                })
                temp_signal = signals[i]
                temp_end_idx = i
            if len(switch_history) >= 5: break
        
        # 持仓统计
        last_signal = signals[-1]
        sig_prev = signals[-2]
        current_held_days = 0
        for i in range(len(signals) - 2, -1, -1):
            if signals[i] == last_signal: current_held_days += 1
            else: break
        current_held_days += 1

        # ------------------ 输出看板 ------------------
        price_now = data[indicator_asset].iloc[-1]
        ma_now = sma.iloc[-1]
        rsi_now = rsi.iloc[-1]
        vix_now = vix_data.iloc[-1]

        print("\n" + "---")
        print(f"### 📊 策略决策看板 (AI Optimized)")
        
        print(f"**【1. 市场体检】**")
        print(f"- 纳指价格: `{price_now:.2f}` (MA{ma_window}: `{ma_now:.2f}`)")
        
        if price_now < ma_now * (1 - bear_buffer): status = "❌ 熊市 (破位)"
        elif price_now < ma_now: status = "⚠️ 震荡 (均下)"
        else: status = "✅ 牛市 (均上)"
        print(f"- 趋势状态: {status}")
        
        rsi_desc = "⚪ 震荡"
        if rsi_now < rsi_buy_3x: rsi_desc = "🔵 机会"
        elif rsi_now > rsi_sell_3x: rsi_desc = "🔴 过热"
        print(f"- RSI(14): `{rsi_now:.2f}` {rsi_desc}")
        
        vix_icon = "🟢" if vix_now < 30 else "🔴" if vix_now > vix_threshold else "🟡"
        print(f"- VIX恐慌: `{vix_now:.2f}` {vix_icon} (熔断: {vix_threshold})")

        print(f"\n**【2. 历史业绩PK】**")
        # 格式化字符串，确保对齐 (Markdown表格)
        print("| 区间 | 策略 | QQQ | QLD | TQQQ | SPY |")
        print("|---|---|---|---|---|---|")
        
        periods = {
            '近1周': 7, '近1月': 30, '近3月': 90, 
            '近6月': 180, '近1年': 365, '近3年': 1095, '近5年': 1825,
            '近10年': 3650
        }
        
        for label, days in periods.items():
            s = get_period_return(strat_cum, days)
            b1 = get_period_return(bench_cum_1x, days)
            b2 = get_period_return(bench_cum_2x, days)
            b3 = get_period_return(bench_cum_3x, days)
            spx = get_period_return(bench_cum_spx, days)
            
            def fmt(val): 
                return f"{val*100:.1f}%" if val is not None else "N/A"
            
            icon = "🔥" if (s is not None and b2 is not None and s > b2) else " " 
            print(f"| {label} | {icon}{fmt(s)} | {fmt(b1)} | {fmt(b2)} | {fmt(b3)} | {fmt(spx)} |")

        print(f"\n**【3. 最近5次调仓】**")
        print("| 日期 | 操作方向 | 之前持有 |")
        print("|---|---|---|")
        for item in switch_history:
            print(f"| {item['date']} | {item['action']} | {item['days']}天 |")

        print(f"\n**【4. 分年度详细战报 (过去10年)】**")
        print("| 年份 | 策略 | QQQ | QLD | TQQQ | 评价 |")
        print("|---|---|---|---|---|---|")
        
        df_perf = pd.DataFrame({'S':strat_daily_ret, 'Q':ret_1x, '2x':ret_2x, '3x':ret_3x})
        # 只取最近10年
        current_year = datetime.now().year
        start_year = current_year - 10
        df_perf = df_perf[df_perf.index.year >= start_year]

        for year in sorted(df_perf.index.year.unique()):
            sub = df_perf[df_perf.index.year == year]
            y_s = (1 + sub['S']).prod() - 1
            y_q = (1 + sub['Q']).prod() - 1
            y_2x = (1 + sub['2x']).prod() - 1
            y_3x = (1 + sub['3x']).prod() - 1
            
            tag = ""
            if y_s > y_3x: tag = "🔥完胜"
            elif y_3x < -0.2 and y_s > y_3x + 0.15: tag = "🛡️避险"
            elif y_s < y_2x: tag = "⚠️跑输"
            
            print(f"| {year} | {y_s*100:.1f}% | {y_q*100:.1f}% | {y_2x*100:.1f}% | {y_3x*100:.1f}% | {tag} |")

        print(f"\n### 📢 【今日行动指南】")
        print(f"- 当前持有: **{name_map[last_signal]}**")
        print(f"- 持仓时间: `{current_held_days}` 个交易日")

        if last_signal == sig_prev:
            print(f"\n#### 🔒 锁仓不动 (HOLD)")
            print(f"策略建议继续持有 **{etf_map[last_signal]}**。")
        else:
            print(f"\n#### ⚡⚡⚡ 调仓信号 (ACTION) ⚡⚡⚡")
            print(f"- 昨日持有: {etf_map[sig_prev]}")
            print(f"- 今日目标: **{etf_map[last_signal]}**")
            print(f"\n👉 **请立即卖出 {etf_map[sig_prev]}，全仓买入 {etf_map[last_signal]}**")

    else:
        print("❌ 错误: 无法获取数据")

# ================= 2. 主执行入口 =================
if __name__ == "__main__":
    output_buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(output_buffer):
            run_strategy_logic()
    except Exception as e:
        output_buffer.write(f"\n\n❌ 程序运行严重错误: {str(e)}")

    final_output = output_buffer.getvalue()
    print(final_output)

    current_date = datetime.now(pytz.timezone('America/New_York')).strftime('%Y-%m-%d')
    title = f"纳指策略日报 ({current_date})"
    
    send_pushplus(title, final_output)
