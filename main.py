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
import traceback

# ================= 0. 推送配置函数 =================
def send_pushplus(title, content):
    token = os.environ.get('PUSH_TOKEN')
    if not token:
        print("⚠️ 未检测到 PUSH_TOKEN，跳过推送。")
        return

    url = 'http://www.pushplus.plus/send'
    # 简单处理换行，保证Markdown在微信显示好看
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
    indicator_asset = '^NDX' # 纳指100指数
    vix_asset = '^VIX'       # 恐慌指数

    # 🔥 核心参数
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

    # ------------------ 数据获取 (修复增强版) ------------------
    try:
        print("⏳ 正在下载数据 (Max)...")
        tickers = [symbol_1x, symbol_2x, symbol_3x, symbol_spx, indicator_asset, vix_asset]
        
        # 批量下载
        raw_data = yf.download(
            tickers, 
            period="max", interval="1d", auto_adjust=False, progress=False
        )
        
        # 🛠️ 兼容性处理：提取收盘价
        # yfinance 新版返回的是 (Price, Ticker) 的多层索引
        adj_close = pd.DataFrame()
        
        if isinstance(raw_data.columns, pd.MultiIndex):
            # 优先尝试获取 Adj Close，没有则获取 Close
            try:
                adj_close = raw_data['Adj Close']
            except KeyError:
                print("⚠️ 未找到 Adj Close，降级使用 Close")
                adj_close = raw_data['Close']
        else:
            # 旧版兼容
            adj_close = raw_data['Adj Close'] if 'Adj Close' in raw_data else raw_data['Close']

        # 🔍 诊断信息 (打印到日志，方便Github Action排查)
        print("\n🔍 数据完整性自检:")
        download_success = True
        for t in tickers:
            if t not in adj_close.columns:
                print(f"   ❌ 失败: [{t}] 列不存在")
                download_success = False
            else:
                count = adj_close[t].dropna().shape[0]
                if count < 10:
                    print(f"   ❌ 失败: [{t}] 数据量过少 ({count}行)")
                    download_success = False
                else:
                    print(f"   ✅ 成功: [{t}] 获取 {count} 行")

        # 🛑 核心资产检查 (VIX除外)
        core_assets = [symbol_1x, symbol_2x, symbol_3x, symbol_spx, indicator_asset]
        for asset in core_assets:
            if asset not in adj_close.columns or adj_close[asset].dropna().empty:
                print(f"\n❌ 严重错误: 核心资产 [{asset}] 下载失败，策略无法运行。")
                return

        # 🧹 数据清洗
        # 1. 提取核心数据并清洗
        data = adj_close[core_assets].ffill().dropna()
        
        # 2. VIX 单独处理 (容错：如果VIX没下下来，默认补0，不阻断策略)
        if vix_asset in adj_close.columns:
            vix_data = adj_close[vix_asset].reindex(data.index).ffill().fillna(0)
        else:
            print(f"⚠️ 警告: [{vix_asset}] 缺失，VIX风控将失效 (默认为0)。")
            vix_data = pd.Series(0, index=data.index)

        print(f"📊 最终有效交易天数: {len(data)}")
        
        if len(data) < 200:
            print("❌ 数据清洗后长度不足200天，无法计算长周期均线。")
            return

    except Exception as e:
        print(f"❌ 数据处理发生异常: {e}")
        traceback.print_exc()
        return 

    # ================= 策略逻辑开始 =================
    if not data.empty:
        # 1. 指标计算
        sma = data[indicator_asset].rolling(window=ma_window).mean()
        delta = data[indicator_asset].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=rsi_window).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=rsi_window).mean().replace(0, 1e-10)
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))

        signals = [] 
        current_state = 2 

        # 2. 信号生成循环
        for i in range(len(data)):
            price = data[indicator_asset].iloc[i]
            ma = sma.iloc[i]
            r = rsi.iloc[i]
            vix = vix_data.iloc[i]
            
            # MA无效时保持常态
            if pd.isna(ma): 
                signals.append(2)
                continue

            # 状态机逻辑
            if vix > vix_threshold:
                current_state = 1 # 恐慌 -> 防守
            elif price < ma * (1 - bear_buffer):
                current_state = 1 # 熊市 -> 防守
            else:
                if current_state == 1:
                    # 熊转牛/震荡: 需要站稳缓冲带
                    if price > ma * (1 + bull_buffer):
                        current_state = 2
                else:
                    # 牛市/震荡内部切换
                    if r > rsi_sell_3x:
                        current_state = 2 # 超买 -> 降杠杆
                    elif r < rsi_buy_3x:
                        current_state = 3 # 超卖 -> 上杠杆
            signals.append(current_state)

        # 3. 收益回测
        ret_1x = data[symbol_1x].pct_change().fillna(0)
        ret_2x = data[symbol_2x].pct_change().fillna(0)
        ret_3x = data[symbol_3x].pct_change().fillna(0)
        ret_spx = data[symbol_spx].pct_change().fillna(0)
        
        pos_series = pd.Series(signals, index=data.index).shift(1).fillna(2)
        strat_daily_ret = pd.Series(0.0, index=data.index)
        strat_daily_ret[pos_series == 1] = ret_1x
        strat_daily_ret[pos_series == 2] = ret_2x
        strat_daily_ret[pos_series == 3] = ret_3x
        
        # 扣除调仓成本
        trades = (pos_series != pos_series.shift(1)).astype(int)
        strat_daily_ret -= (trades * transaction_cost)
        
        # 累计净值
        strat_cum = (1 + strat_daily_ret).cumprod()
        bench_cum_1x = (1 + ret_1x).cumprod()
        bench_cum_2x = (1 + ret_2x).cumprod()
        bench_cum_3x = (1 + ret_3x).cumprod()
        bench_cum_spx = (1 + ret_spx).cumprod()

        # 辅助函数: 计算区间收益
        def get_period_return(cum_series, days_lookback):
            if len(cum_series) == 0: return 0.0
            target_date = cum_series.index[-1] - timedelta(days=days_lookback)
            if target_date < cum_series.index[0]:
                start_val = cum_series.iloc[0]
            else:
                # 找最近的交易日
                idx = cum_series.index.searchsorted(target_date)
                if idx >= len(cum_series): idx = len(cum_series) - 1
                start_val = cum_series.iloc[idx]
            return (cum_series.iloc[-1] / start_val) - 1

        # ------------------ 调仓记录 ------------------
        switch_history = []
        temp_signal = signals[-1]
        temp_end_idx = len(signals) - 1
        # 倒序查找最近5次
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
        
        # 计算当前持仓天数
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
        print("| 区间    | 策略    | QQQ    | QLD    | TQQQ   | SPY    |")
        print("|-------|-------|-------|-------|-------|-------|")
        
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
                if val is None: return "N/A"
                return f"{val*100:.1f}%"
            
            icon = "🔥" if (s is not None and b2 is not None and s > b2) else " "
            print(f"| {label:<5} | {icon}{fmt(s):<6} | {fmt(b1):<6} | {fmt(b2):<6} | {fmt(b3):<6} | {fmt(spx):<6} |")

        print(f"\n**【3. 最近5次调仓】**")
        print("| 日期       | 操作方向         | 之前持有 |")
        print("|------------|----------------|---------|")
        for item in switch_history:
            print(f"| {item['date']} | {item['action']:<14} | {item['days']}天   |")

        print(f"\n**【4. 分年度详细战报 (过去10年)】**")
        print("| 年份 | 策略    | QQQ    | QLD    | TQQQ   | 评价   |")
        print("|-----|-------|-------|-------|-------|-------|")
        
        df_perf = pd.DataFrame({'S':strat_daily_ret, 'Q':ret_1x, '2x':ret_2x, '3x':ret_3x})
        current_year = datetime.now().year
        start_year = current_year - 10
        df_perf = df_perf[df_perf.index.year >= start_year]

        for year in sorted(df_perf.index.year.unique()):
            sub = df_perf[df_perf.index.year == year]
            y_s = (1 + sub['S']).prod() - 1
            y_q = (1 + sub['Q']).prod() - 1
            y_2x = (1 + sub['2x']).prod() - 1
            y_3x = (1 + sub['3x']).prod() - 1
            
            tag = "✅达标" 
            if y_s > y_3x: tag = "🔥完胜"
            elif y_3x < -0.2 and y_s > y_3x + 0.15: tag = "🛡️避险"
            elif y_s < y_2x: tag = "⚠️跑输"
            elif y_s > y_2x and y_s < y_3x: tag = "✅不错"
            
            print(f"| {year} | {y_s*100:<6.1f}% | {y_q*100:<6.1f}% | {y_2x*100:<6.1f}% | {y_3x*100:<6.1f}% | {tag:<4} |")

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
        print("❌ 错误: 有效数据为空")

# ================= 2. 主执行入口 =================
if __name__ == "__main__":
    output_buffer = io.StringIO()
    try:
        # 捕获 print 输出
        with contextlib.redirect_stdout(output_buffer):
            run_strategy_logic()
    except Exception as e:
        output_buffer.write(f"\n\n❌ 程序主逻辑崩溃: {str(e)}\n")
        traceback.print_exc(file=output_buffer)

    final_output = output_buffer.getvalue()
    print(final_output) # 在控制台打印一遍，方便看 Log

    # 发送推送
    current_date = datetime.now(pytz.timezone('America/New_York')).strftime('%Y-%m-%d')
    title = f"纳指策略日报 ({current_date})"
    
    send_pushplus(title, final_output)
