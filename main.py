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
    # 为了在手机上显示更美观，针对表格做简单的 Markdown 处理
    # 这一步将换行符转换为 HTML/Markdown 认可的格式
    content = content.replace('\n', '\n\n') 
    
    data = {
        "token": token,
        "title": title,
        "content": content,
        "template": "markdown"  # 使用 markdown 模板以支持表格格式
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
    # ------------------ 原有配置参数 ------------------
    symbol_1x = 'QQQ'   
    symbol_2x = 'QLD'   
    symbol_3x = 'TQQQ'
    symbol_spx = 'SPY'
    indicator_asset = '^NDX'

    # 核心参数 (最优解)
    ma_window = 200
    rsi_window = 14
    rsi_buy_3x = 50     # RSI < 50 进 3x
    rsi_sell_3x = 80    # RSI > 80 退 2x
    bear_buffer = 0.0   # 跌破均线立即跑
    bull_buffer = 0.005 # 站稳均线进场
    transaction_cost = 0.001 

    # ------------------ 市场状态检测 ------------------
    ny_tz = pytz.timezone('America/New_York')
    now_ny = datetime.now(ny_tz)
    is_market_open = False

    # 简单判断盘中 (周一到周五, 9:30-16:00)
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
        # 下载最近 3 年数据
        raw_data = yf.download(
            [symbol_1x, symbol_2x, symbol_3x, symbol_spx, indicator_asset], 
            period="3y", interval="1d", auto_adjust=False, progress=False
        )
        # yfinance 新版返回多级索引，这里做一下处理以防万一
        if isinstance(raw_data.columns, pd.MultiIndex):
            data = raw_data['Adj Close'].ffill().dropna()
        else:
            data = raw_data['Adj Close'].ffill().dropna()
            
    except Exception as e:
        print(f"❌ 数据下载失败: {e}")
        return # 数据失败直接结束

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

        # ------------------ 持仓统计 ------------------
        last_signal = signals[-1]
        days_held = 0
        prev_signal = None
        switch_date = None
        
        for i in range(len(signals) - 2, -1, -1):
            if signals[i] == last_signal:
                days_held += 1
            else:
                prev_signal = signals[i]
                switch_date = data.index[i+1].strftime('%Y-%m-%d')
                break
        days_held += 1 

        etf_map = {1: symbol_1x, 2: symbol_2x, 3: symbol_3x}
        name_map = {1: f'{symbol_1x} (1x 防守)', 2: f'{symbol_2x} (2x 常态)', 3: f'{symbol_3x} (3x 进攻)'}
        
        price_now = data[indicator_asset].iloc[-1]
        ma_now = sma_200.iloc[-1]
        rsi_now = rsi.iloc[-1]
        sig_prev = signals[-2]

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

        # 模块 B: 持仓统计
        print(f"\n**【2. 持仓统计】**")
        print(f"- 当前持有: **{name_map[last_signal]}**")
        print(f"- 持仓时间: `{days_held}` 个交易日")
        if prev_signal:
            print(f"- 上次切换: {switch_date} (从 {etf_map[prev_signal]} 切入)")

        # 模块 C: 业绩回测
        print(f"\n**【3. 近期业绩PK】**")
        print("| 区间 | 策略 | QQQ | QLD | TQQQ | SPY |")
        print("|---|---|---|---|---|---|")
        
        periods = {'近1周': 7, '近1月': 30, '近3月': 90, '近6月': 180, '近1年': 365}
        
        for label, days in periods.items():
            s_ret = get_period_return(strat_cum, days)
            b1_ret = get_period_return(bench_cum_1x, days)
            b2_ret = get_period_return(bench_cum_2x, days)
            b3_ret = get_period_return(bench_cum_3x, days)
            spx_ret = get_period_return(bench_cum_spx, days)
            
            icon = "🔥" if s_ret > b2_ret else " " 
            print(f"| {label} | {icon}{s_ret*100:.1f}% | {b1_ret*100:.1f}% | {b2_ret*100:.1f}% | {b3_ret*100:.1f}% | {spx_ret*100:.1f}% |")

        # 模块 D: 操作指令
        print(f"\n### 📢 【今日行动指南】")
        
        if last_signal == sig_prev:
            print(f"#### 🔒 锁仓不动 (HOLD)")
            print(f"策略建议继续持有 **{etf_map[last_signal]}**。")
        else:
            print(f"#### ⚡⚡⚡ 调仓信号 (ACTION) ⚡⚡⚡")
            print(f"- 昨日持有: {etf_map[sig_prev]}")
            print(f"- 今日目标: **{etf_map[last_signal]}**")
            print(f"\n👉 **请立即卖出 {etf_map[sig_prev]}，全仓买入 {etf_map[last_signal]}**")

    else:
        print("❌ 错误: 无法获取数据，请检查网络连接。")

# ================= 2. 主执行入口 =================
if __name__ == "__main__":
    # 1. 创建一个 StringIO 对象来捕获输出
    output_buffer = io.StringIO()
    
    # 2. 将 stdout 重定向到 buffer
    # 这样 run_strategy_logic() 里的所有 print 都不会直接打印到屏幕，而是进入 buffer
    try:
        with contextlib.redirect_stdout(output_buffer):
            run_strategy_logic()
    except Exception as e:
        # 如果策略运行报错，也要捕获错误信息
        output_buffer.write(f"\n\n❌ 程序运行严重错误: {str(e)}")

    # 3. 获取所有输出内容
    final_output = output_buffer.getvalue()

    # 4. 同时打印到控制台 (方便在 GitHub Action 日志里看)
    print(final_output)

    # 5. 发送推送 (提取第一行作为标题的一部分)
    current_date = datetime.now(pytz.timezone('America/New_York')).strftime('%Y-%m-%d')
    title = f"纳指策略日报 ({current_date})"
    
    send_pushplus(title, final_output)
