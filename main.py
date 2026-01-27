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
import time

# ================= 0. 推送配置函数 =================
def send_pushplus(title, content):
    token = os.environ.get('PUSH_TOKEN')
    if not token:
        print("⚠️ 未检测到 PUSH_TOKEN，跳过推送。")
        return

    url = 'http://www.pushplus.plus/send'
    content = content.replace('\n', '\n\n') 
    
    data = {
        "token": token,
        "title": title,
        "content": content,
        "template": "html"  # 🔥 改为 html 模板，支持表格渲染
    }
    
    try:
        response = requests.post(url, json=data)
        if response.status_code == 200:
            print("✅ 推送发送成功！")
        else:
            print(f"❌ 推送发送失败: {response.text}")
    except Exception as e:
        print(f"❌ 推送请求出错: {e}")

# ================= 1. 简易 HTML 表格生成器 (轻量版) =================
def df_to_simple_html(df):
    # 手动构建 HTML，避免依赖 jinja2，且代码极简
    html = '<table style="width:100%; border-collapse:collapse; font-size:13px; font-family:sans-serif;">'
    
    # 表头
    html += '<tr style="background-color:#f2f2f2;">'
    for col in df.columns:
        html += f'<th style="border:1px solid #ddd; padding:6px; text-align:center;">{col}</th>'
    html += '</tr>'
    
    # 内容
    for _, row in df.iterrows():
        html += '<tr>'
        for val in row:
            html += f'<td style="border:1px solid #ddd; padding:6px; text-align:center;">{val}</td>'
        html += '</tr>'
    html += '</table>'
    return html

# ================= 2. 策略逻辑封装 =================
def run_strategy_logic():
    # ------------------ 配置参数 ------------------
    symbol_1x = 'QQQ'   
    symbol_2x = 'QLD'   
    symbol_3x = 'TQQQ'
    symbol_spx = 'SPY'
    indicator_asset = '^NDX' 
    vix_asset = '^VIX'       

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
    print(f"## 📅 日期: {now_ny.strftime('%Y-%m-%d')}") # 标题简洁点

    # ------------------ 数据获取 (带重试) ------------------
    tickers = [symbol_1x, symbol_2x, symbol_3x, symbol_spx, indicator_asset, vix_asset]
    core_assets = [symbol_1x, symbol_2x, symbol_3x, symbol_spx, indicator_asset]
    
    data = pd.DataFrame()
    vix_data = pd.Series()
    
    max_retries = 3
    for attempt in range(max_retries):
        try:
            print(f"⏳ 下载数据 ({attempt+1}/{max_retries})...")
            raw_data = yf.download(tickers, period="max", interval="1d", auto_adjust=False, progress=False)
            
            # 兼容性处理
            adj_close = pd.DataFrame()
            if isinstance(raw_data.columns, pd.MultiIndex):
                try: adj_close = raw_data['Adj Close']
                except KeyError: adj_close = raw_data['Close']
            else:
                adj_close = raw_data['Adj Close'] if 'Adj Close' in raw_data else raw_data['Close']

            # 检查完整性
            if set(core_assets).issubset(adj_close.columns) and len(adj_close[symbol_1x].dropna()) > 200:
                data = adj_close[core_assets].ffill().dropna()
                if vix_asset in adj_close.columns:
                    vix_data = adj_close[vix_asset].reindex(data.index).ffill().fillna(0)
                else:
                    vix_data = pd.Series(0, index=data.index)
                break
            else:
                print("❌ 数据缺失，重试中...")
                time.sleep(5)
        except Exception as e:
            print(f"❌ 下载异常: {e}")
            time.sleep(5)

    if data.empty:
        print("❌ 严重错误: 无法获取数据")
        return

    # ================= 策略计算 =================
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

    # 收益计算
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
    bench_cum_3x = (1 + ret_3x).cumprod()

    def get_ret(cum, days):
        if len(cum) == 0: return 0.0
        target = cum.index[-1] - timedelta(days=days)
        idx = cum.index.searchsorted(target)
        if idx >= len(cum): idx = len(cum) - 1
        return (cum.iloc[-1] / cum.iloc[idx]) - 1

    # ================= 输出看板 (HTML版) =================
    price_now = data[indicator_asset].iloc[-1]
    ma_now = sma.iloc[-1]
    rsi_now = rsi.iloc[-1]
    vix_now = vix_data.iloc[-1]

    print("\n" + "---")
    
    # 1. 市场体检
    print(f"### 📊 市场体检")
    if price_now < ma_now: status = "⚠️ 震荡/熊"
    else: status = "✅ 牛市"
    
    rsi_icon = "🔵" if rsi_now < rsi_buy_3x else "🔴" if rsi_now > rsi_sell_3x else "⚪"
    vix_icon = "🟢" if vix_now < 30 else "🔴"
    
    print(f"- 趋势: **{status}**")
    print(f"- RSI: `{rsi_now:.1f}` {rsi_icon}")
    print(f"- VIX: `{vix_now:.1f}` {vix_icon}")

    # 2. 历史业绩 (HTML表格)
    print(f"\n### 🏆 业绩对比")
    periods = {'1周':7, '1月':30, '3月':90, '半年':180, '1年':365, '3年':1095}
    perf_data = []
    for label, days in periods.items():
        s = get_ret(strat_cum, days)
        b1 = get_ret(bench_cum_1x, days)
        b3 = get_ret(bench_cum_3x, days)
        icon = "🔥" if s > b1 else ""
        perf_data.append({"区间":label, "策略":f"{s*100:.1f}%{icon}", "QQQ":f"{b1*100:.1f}%", "TQQQ":f"{b3*100:.1f}%"})
    
    print(df_to_simple_html(pd.DataFrame(perf_data)))

    # 3. 年度战报 (HTML表格)
    print(f"\n### 📅 年度数据")
    df_perf = pd.DataFrame({'S':strat_daily_ret, 'Q':ret_1x, '3x':ret_3x})
    years = sorted(df_perf.index.year.unique(), reverse=True)[:10] # 只取最近10年
    year_data = []
    for y in years:
        sub = df_perf[df_perf.index.year == y]
        ys = (1+sub['S']).prod()-1
        y3 = (1+sub['3x']).prod()-1
        tag = "👑" if ys > y3 else ""
        year_data.append({"年份":y, "策略":f"{ys*100:.1f}%{tag}", "TQQQ":f"{y3*100:.1f}%"})
    
    print(df_to_simple_html(pd.DataFrame(year_data)))

    # 4. 调仓记录
    switch_history = []
    temp_signal = signals[-1]
    end_idx = len(signals) - 1
    for i in range(len(signals)-2, -1, -1):
        if signals[i] != temp_signal:
            switch_history.append({
                "日期": data.index[i+1].strftime('%m-%d'),
                "操作": f"{etf_map[signals[i]]} -> {etf_map[temp_signal]}",
                "持仓": f"{end_idx - i}天"
            })
            temp_signal = signals[i]
            end_idx = i
        if len(switch_history) >= 5: break
    
    if switch_history:
        print(f"\n### 📝 最近调仓")
        print(df_to_simple_html(pd.DataFrame(switch_history)))

    # 5. 今日行动
    last = signals[-1]
    prev = signals[-2]
    
    # 颜色条
    color = "#e74c3c" if last != prev else "#2ecc71" # 红变动，绿锁仓
    action = "调仓交易" if last != prev else "锁仓持有"
    
    print(f"\n<div style='background-color:#f9f9f9; padding:12px; border-left: 6px solid {color}; margin-top:10px;'>")
    print(f"<b>当前状态:</b> {action}<br>")
    print(f"<b>目标持仓:</b> <b style='color:{color}; font-size:16px;'>{etf_map[last]}</b>")
    print("</div>")
    
    if last != prev:
        print(f"\n❗ **请卖出 {etf_map[prev]}，买入 {etf_map[last]}**")

# ================= 3. 主程序 =================
if __name__ == "__main__":
    output_buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(output_buffer):
            run_strategy_logic()
    except Exception as e:
        output_buffer.write(f"\n❌ 错误: {str(e)}")
        traceback.print_exc(file=output_buffer)

    final_output = output_buffer.getvalue()
    print(final_output) # 打印到日志
    
    # 发送推送
    current_date = datetime.now(pytz.timezone('America/New_York')).strftime('%Y-%m-%d')
    send_pushplus(f"纳指策略 ({current_date})", final_output)
