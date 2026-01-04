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
    # ------------------ 配置参数 ------------------
    symbol_1x = 'QQQ'   
    symbol_2x = 'QLD'   
    symbol_3x = 'TQQQ'
    symbol_spx = 'SPY'
    indicator_asset = '^NDX'

    # 核心参数 (已修改 RSI 卖出阈值为 75)
    ma_window = 200
    rsi_window = 14
    rsi_buy_3x = 50     # RSI < 50 进 3x
    rsi_sell_3x = 75    # RSI > 75 退 2x (原为 80)
    bear_buffer = 0.0   
    bull_buffer = 0.005 
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
        # 修改：下载 10 年数据，以支持“近5年”回测
        raw_data = yf.download(
            [symbol_1x, symbol_2x, symbol_3x, symbol_spx, indicator_asset], 
            period="10y", interval="1d", auto_adjust=False, progress=False
        )
        if isinstance(raw_data.columns, pd.MultiIndex):
            data = raw_data['Adj Close'].ffill().dropna()
        else:
            data = raw_data['Adj Close'].ffill().dropna()
            
    except Exception as e:
        print(f"❌ 数据下载失败: {e}")
        return 

    if not data.empty:
        # ------------------ 指标计算 & 信号重建 ------------------
        sma_200 = data[indicator_asset].rolling(window=ma_window).mean()
        
        delta = data[indicator_asset].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=rsi_window).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=rsi_window).mean().replace(0, 1e-10)
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))

        signals = [] 
        current_state = 2 

        for i in range(len(data)):
            price = data[indicator_asset].iloc[i]
            ma = sma_200.iloc[i]
            r = rsi.iloc[i]
            
            if pd.isna(ma): 
                signals.append(2)
                continue

            if price < ma * (1 - bear_buffer):
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
        
        trades = (pos_series != pos_series.shift(1)).astype(int)
        strat_daily_ret -= (trades * transaction_cost)
        
        strat_cum = (1 + strat_daily_ret).cumprod()
        bench_cum_1x = (1 + ret_1x).cumprod()
        bench_cum_2x = (1 + ret_2x).cumprod()
        bench_cum_3x = (1 + ret_3x).cumprod()
        bench_cum_spx = (1 + ret_spx).cumprod()

        def get_period_return(cum_series, days_lookback):
            if len(cum_series) < days_lookback: return 0.0
            target_date = cum_series.index[-1] - timedelta(days=days_lookback)
            idx = cum_series.index.searchsorted(target_date)
            if idx >= len(cum_series): idx = len(cum_series) - 1
            return (cum_series.iloc[-1] / cum_series.iloc[idx]) - 1

        # ------------------ 历史切换记录 (新增) ------------------
        # 回溯寻找最近 5 次切换
        switch_history = []
        temp_signal = signals[-1]
        temp_end_idx = len(signals) - 1
        
        # 倒序遍历寻找切换点
        for i in range(len(signals) - 2, -1, -1):
            if signals[i] != temp_signal:
                # 发现切换
                prev_sig = signals[i]
                curr_sig = temp_signal
                switch_date = data.index[i+1] # 信号生效日（或产生日次日）
                
                # 计算这一个波段持有了多久
                hold_days = temp_end_idx - i 
                
                switch_history.append({
                    'date': switch_date.strftime('%Y-%m-%d'),
                    'action': f"{etf_map[prev_sig]} -> {etf_map[curr_sig]}",
                    'days': hold_days
                })
                
                # 重置状态往前找
                temp_signal = prev_sig
                temp_end_idx = i
                
            if len(switch_history) >= 5:
                break
        
        # ------------------ 当前持仓统计 ------------------
        last_signal = signals[-1]
        sig_prev = signals[-2]
        
        # 计算当前持仓天数
        current_held_days = 0
        for i in range(len(signals) - 2, -1, -1):
            if signals[i] == last_signal:
                current_held_days += 1
            else:
                break
        current_held_days += 1

        price_now = data[indicator_asset].iloc[-1]
        ma_now = sma_200.iloc[-1]
        rsi_now = rsi.iloc[-1]

        # ------------------ 输出看板 (Markdown格式) ------------------
        print("\n" + "---")
        print(f"### 📊 策略决策看板")
        
        # 模块 A: 市场体检
        print(f"**【1. 市场体检】**")
        print(f"- 纳指价格: `{price_now:.2f}`")
        print(f"- 200日线: `{ma_now:.2f}`")
        if price_now < ma_now: print("- 趋势: ❌ **熊市 (均线下方)**")
        else: print("- 趋势: ✅ **牛市 (均线上方)**")
        
        rsi_desc = "⚪ 震荡区"
        if rsi_now < rsi_buy_3x: rsi_desc = "🔵 机会区 (回调)"
        elif rsi_now > rsi_sell_3x: rsi_desc = "🔴 风险区 (过热)"
        print(f"- RSI(14): `{rsi_now:.2f}` {rsi_desc}")

        # 模块 B: 业绩PK (新增3年和5年)
        print(f"\n**【2. 历史业绩PK】**")
        print("| 区间 | 策略 | QQQ | QLD | TQQQ | SPY |")
        print("|---|---|---|---|---|---|")
        
        periods = {
            '近1周': 7, '近1月': 30, '近3月': 90, 
            '近6月': 180, '近1年': 365, '近3年': 1095, '近5年': 1825
        }
        
        for label, days in periods.items():
            s_ret = get_period_return(strat_cum, days)
            b1_ret = get_period_return(bench_cum_1x, days)
            b2_ret = get_period_return(bench_cum_2x, days)
            b3_ret = get_period_return(bench_cum_3x, days)
            spx_ret = get_period_return(bench_cum_spx, days)
            
            icon = "🔥" if s_ret > b2_ret else " " 
            print(f"| {label} | {icon}{s_ret*100:.1f}% | {b1_ret*100:.1f}% | {b2_ret*100:.1f}% | {b3_ret*100:.1f}% | {spx_ret*100:.1f}% |")

        # 模块 C: 调仓记录 (新增模块)
        print(f"\n**【3. 最近5次调仓】**")
        print("| 日期 | 操作方向 | 之前持有 |")
        print("|---|---|---|")
        for item in switch_history:
            print(f"| {item['date']} | {item['action']} | {item['days']}天 |")

        # 模块 D: 操作指令
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
