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
    """
    发送推送到微信 (使用 PushPlus 服务)
    """
    token = os.environ.get('PUSH_TOKEN') # 从环境变量获取 Token
    if not token:
        print("⚠️ 未检测到 PUSH_TOKEN，跳过推送。")
        return

    url = 'http://www.pushplus.plus/send'
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
    # ------------------ 配置参数 (AI 优化版) ------------------
    symbol_1x = 'QQQ'   
    symbol_2x = 'QLD'   
    symbol_3x = 'TQQQ'
    symbol_spx = 'SPY'
    indicator_asset = '^NDX'
    vix_asset = '^VIX' # 新增恐慌指数

    # 🔥 核心参数 (基于 Alpha/Sharpe 最优解)
    # 优化结论: MA 170 | Buy 65 | Sell 80 | VIX 40 | BearBuf 1% | BullBuf 0%
    ma_window = 170      
    rsi_window = 14
    
    rsi_buy_3x = 65      # 积极抄底 (原50)
    rsi_sell_3x = 80     # 让利润奔跑 (原75)
    
    # 缓冲区设置
    bear_buffer = 0.01   # 1% 缓冲 (防假摔，跌破均线1%才离场)
    bull_buffer = 0.0    # 0% 缓冲 (立刻追，站上均线即买入)
    
    # 风控熔断
    vix_threshold = 40.0 # 极度恐慌熔断线

    transaction_cost = 0.001 

    etf_map = {1: symbol_1x, 2: symbol_2x, 3: symbol_3x}
    name_map = {1: f'{symbol_1x} (1x 防守)', 2: f'{symbol_2x} (2x 常态)', 3: f'{symbol_3x} (3x 进攻)'}

    # ------------------ 市场状态检测 ------------------
    ny_tz = pytz.timezone('America/New_York')
    now_ny = datetime.now(ny_tz)
    is_market_open = False

    if 0 <= now_ny.weekday() <= 4:
        if (now_ny.hour > 9 or (now_ny.hour == 9 and now_ny.minute >= 30)) and now_ny.hour < 16:
            is_market_open = True

    print(f"## 📅 时间: {now_ny.strftime('%Y-%m-%d %H:%M')} (美东)")
    if is_market_open:
        print("**🔔 状态: 美股【交易中】**")
    else:
        print("**💤 状态: 美股【已收盘/盘前】**")

    # ------------------ 数据获取 ------------------
    try:
        # 下载数据 (包含 VIX)
        tickers = [symbol_1x, symbol_2x, symbol_3x, symbol_spx, indicator_asset, vix_asset]
        print("⏳ 正在下载数据...")
        raw_data = yf.download(
            tickers, 
            period="10y", interval="1d", auto_adjust=False, progress=False
        )
        
        # 数据清洗与提取
        if isinstance(raw_data.columns, pd.MultiIndex):
            # 提取价格数据 (Adj Close)
            adj_close = raw_data['Adj Close']
            data = adj_close[[symbol_1x, symbol_2x, symbol_3x, symbol_spx, indicator_asset]].ffill().dropna()
            # 提取 VIX 数据并对齐索引
            vix_data = adj_close[vix_asset].reindex(data.index).ffill().fillna(0)
        else:
            # 容错处理 (单列情况)
            data = raw_data['Adj Close'].ffill().dropna()
            # 如果下载失败，生成全0的VIX防止报错
            vix_data = pd.Series(0, index=data.index)
            
    except Exception as e:
        print(f"❌ 数据下载失败: {e}")
        return 

    if not data.empty:
        # ------------------ 指标计算 & 信号重建 ------------------
        # 1. 均线
        sma = data[indicator_asset].rolling(window=ma_window).mean()
        
        # 2. RSI
        delta = data[indicator_asset].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=rsi_window).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=rsi_window).mean().replace(0, 1e-10)
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))

        signals = [] 
        current_state = 2 

        # ------------------ 策略核心循环 ------------------
        for i in range(len(data)):
            price = data[indicator_asset].iloc[i]
            ma = sma.iloc[i]
            r = rsi.iloc[i]
            vix = vix_data.iloc[i]
            
            if pd.isna(ma): 
                signals.append(2)
                continue

            # 优先级 1: VIX 熔断 (极度恐慌时强制防守)
            if vix > vix_threshold:
                current_state = 1
            
            # 优先级 2: 均线大势 (熊市防守)
            # 使用 bear_buffer (0.01): 只有跌破均线 1% 才离场
            elif price < ma * (1 - bear_buffer):
                current_state = 1 
                
            # 优先级 3: 牛市/震荡逻辑
            else:
                if current_state == 1:
                    # 刚从熊市回来，使用 bull_buffer (0.0): 站上均线立刻买
                    if price > ma * (1 + bull_buffer):
                        current_state = 2
                else:
                    # 已经在牛市，RSI 择时
                    if r > rsi_sell_3x:
                        current_state = 2 # 超买，降杠杆
                    elif r < rsi_buy_3x:
                        current_state = 3 # 抄底，上杠杆
            
            signals.append(current_state)

        # ------------------ 业绩回溯计算 ------------------
        ret_1x = data[symbol_1x].pct_change().fillna(0)
        ret_2x = data[symbol_2x].pct_change().fillna(0)
        ret_3x = data[symbol_3x].pct_change().fillna(0)
        ret_spx = data[symbol_spx].pct_change().fillna(0)
        
        pos_series = pd.Series(signals, index=data.index).shift(1).fillna(2)
        strat_daily_ret = pd.Series(0.0, index=data.index)
        strat_daily_ret[pos_series == 1] = ret_1x
        strat_daily_ret[pos_series == 2] = ret_2x
        strat_daily_ret[pos_series == 3] = ret_3x
        
        # 扣除滑点
        trades = (pos_series != pos_series.shift(1)).astype(int)
        strat_daily_ret -= (trades * transaction_cost)
        
        # 累计净值
        strat_cum = (1 + strat_daily_ret).cumprod()
        bench_cum_1x = (1 + ret_1x).cumprod()
        bench_cum_2x = (1 + ret_2x).cumprod()
        bench_cum_3x = (1 + ret_3x).cumprod()
        bench_cum_spx = (1 + ret_spx).cumprod()

        def get_period_return(cum_series, days_lookback):
            if len(cum_series) < days_lookback: return 0.0
            target_date = cum_series.index[-1] - timedelta(days=days_lookback)
            # 寻找最近的交易日索引
            idx = cum_series.index.searchsorted(target_date)
            if idx >= len(cum_series): idx = len(cum_series) - 1
            return (cum_series.iloc[-1] / cum_series.iloc[idx]) - 1

        # ------------------ 历史切换记录 ------------------
        switch_history = []
        temp_signal = signals[-1]
        temp_end_idx = len(signals) - 1
        
        for i in range(len(signals) - 2, -1, -1):
            if signals[i] != temp_signal:
                prev_sig = signals[i]
                curr_sig = temp_signal
                switch_date = data.index[i+1]
                hold_days = temp_end_idx - i 
                
                switch_history.append({
                    'date': switch_date.strftime('%Y-%m-%d'),
                    'action': f"{etf_map[prev_sig]} -> {etf_map[curr_sig]}",
                    'days': hold_days
                })
                
                temp_signal = prev_sig
                temp_end_idx = i
            if len(switch_history) >= 5:
                break
        
        # ------------------ 当前持仓统计 ------------------
        last_signal = signals[-1]
        sig_prev = signals[-2]
        
        current_held_days = 0
        for i in range(len(signals) - 2, -1, -1):
            if signals[i] == last_signal:
                current_held_days += 1
            else:
                break
        current_held_days += 1

        price_now = data[indicator_asset].iloc[-1]
        ma_now = sma.iloc[-1]
        rsi_now = rsi.iloc[-1]
        vix_now = vix_data.iloc[-1]

        # ------------------ 输出看板 (Markdown格式) ------------------
        print("\n" + "---")
        print(f"### 📊 策略决策看板 (AI Optimized)")
        
        # 模块 A: 市场体检
        print(f"**【1. 市场体检】**")
        print(f"- 纳指价格: `{price_now:.2f}` (MA{ma_window}: `{ma_now:.2f}`)")
        
        # 趋势描述
        if price_now < ma_now * (1 - bear_buffer): 
            trend_status = "❌ 熊市 (跌破缓冲线)"
        elif price_now < ma_now:
            trend_status = "⚠️ 震荡 (均线下方但未破防)"
        else: 
            trend_status = "✅ 牛市 (均线上方)"
        print(f"- 趋势状态: {trend_status}")
        
        # RSI 描述
        rsi_desc = "⚪ 震荡区"
        if rsi_now < rsi_buy_3x: rsi_desc = "🔵 机会区 (回调)"
        elif rsi_now > rsi_sell_3x: rsi_desc = "🔴 风险区 (过热)"
        print(f"- RSI(14): `{rsi_now:.2f}` {rsi_desc}")
        
        # VIX 描述
        vix_icon = "🟢" if vix_now < 30 else "🔴" if vix_now > vix_threshold else "🟡"
        print(f"- VIX恐慌: `{vix_now:.2f}` {vix_icon} (熔断线: {vix_threshold})")

        # 模块 B: 业绩PK (含10年)
        print(f"\n**【2. 历史业绩PK】**")
        print("| 区间 | 策略 | QQQ | QLD | TQQQ | SPY |")
        print("|---|---|---|---|---|---|")
        
        periods = {
            '近1周': 7, '近1月': 30, '近3月': 90, 
            '近6月': 180, '近1年': 365, '近3年': 1095, '近5年': 1825,
            '近10年': 3650
        }
        
        for label, days in periods.items():
            s_ret = get_period_return(strat_cum, days)
            b1_ret = get_period_return(bench_cum_1x, days)
            b2_ret = get_period_return(bench_cum_2x, days)
            b3_ret = get_period_return(bench_cum_3x, days)
            spx_ret = get_period_return(bench_cum_spx, days)
            
            # 如果数据长度不足，显示 N/A
            if len(data) < days * 0.6: # 简单判断
                print(f"| {label} | N/A | ... | ... | ... | ... |")
            else:
                icon = "🔥" if s_ret > b2_ret else " " 
                print(f"| {label} | {icon}{s_ret*100:.1f}% | {b1_ret*100:.1f}% | {b2_ret*100:.1f}% | {b3_ret*100:.1f}% | {spx_ret*100:.1f}% |")

        # 模块 C: 调仓记录
        print(f"\n**【3. 最近5次调仓】**")
        print("| 日期 | 操作方向 | 之前持有 |")
        print("|---|---|---|")
        for item in switch_history:
            print(f"| {item['date']} | {item['action']} | {item['days']}天 |")

        # 模块 D: 分年度详细战报 (新增!)
        print(f"\n**【4. 分年度详细战报 (过去10年)】**")
        print("| 年份 | 策略 | QQQ | QLD | TQQQ | 评价 |")
        print("|---|---|---|---|---|---|")
        
        # 构造年度统计 DataFrame
        df_perf = pd.DataFrame({
            'Strategy': strat_daily_ret,
            'QQQ': ret_1x,
            'QLD': ret_2x,
            'TQQQ': ret_3x
        })
        
        years = df_perf.index.year.unique()
        # 倒序排列，最近的年份在前面 (或者正序，看个人喜好，这里用正序符合阅读习惯)
        for year in sorted(years):
            # 获取当年的数据
            df_year = df_perf[df_perf.index.year == year]
            # 计算当年总收益
            y_strat = (1 + df_year['Strategy']).prod() - 1
            y_qqq = (1 + df_year['QQQ']).prod() - 1
            y_qld = (1 + df_year['QLD']).prod() - 1
            y_tqqq = (1 + df_year['TQQQ']).prod() - 1
            
            # 评价标签
            tag = ""
            if y_strat > y_tqqq: tag = "🔥完胜"
            elif y_tqqq < -0.2 and y_strat > y_tqqq + 0.15: tag = "🛡️避险"
            elif y_strat < y_qld: tag = "⚠️跑输"
            
            print(f"| {year} | {y_strat*100:6.1f}% | {y_qqq*100:6.1f}% | {y_qld*100:6.1f}% | {y_tqqq*100:6.1f}% | {tag} |")

        # 模块 E: 操作指令
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
        print("❌ 错误: 无法获取数据，请检查网络连接。")

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
