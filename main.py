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
    # HTML 模板不需要处理换行符，但为了保险
    content = content.replace('\n', '\n') 
    
    data = {
        "token": token,
        "title": title,
        "content": content,
        "template": "html"  # 🔥 必须用 html 模板才能渲染表格
    }
    
    try:
        response = requests.post(url, json=data)
        if response.status_code == 200:
            print("✅ 推送发送成功！")
        else:
            print(f"❌ 推送发送失败: {response.text}")
    except Exception as e:
        print(f"❌ 推送请求出错: {e}")

# ================= 1. 紧凑型 HTML 表格生成器 =================
def df_to_compact_html(df):
    # 针对手机屏幕优化的 CSS：字号 12px，内边距 3px
    html = '<table style="width:100%; border-collapse:collapse; font-size:12px; font-family:sans-serif;">'
    
    # 表头
    html += '<tr style="background-color:#f2f2f2;">'
    for col in df.columns:
        html += f'<th style="border:1px solid #ddd; padding:3px; text-align:center; white-space:nowrap;">{col}</th>'
    html += '</tr>'
    
    # 内容
    for _, row in df.iterrows():
        html += '<tr>'
        for val in row:
            # 自动判断是否需要加粗（比如含 🔥 标记的）
            weight = "bold" if "🔥" in str(val) or "✅" in str(val) else "normal"
            html += f'<td style="border:1px solid #ddd; padding:3px; text-align:center; font-weight:{weight};">{val}</td>'
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

    # 核心参数
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
    
    # ------------------ 数据获取 (带重试) ------------------
    tickers = [symbol_1x, symbol_2x, symbol_3x, symbol_spx, indicator_asset, vix_asset]
    core_assets = [symbol_1x, symbol_2x, symbol_3x, symbol_spx, indicator_asset]
    
    data = pd.DataFrame()
    vix_data = pd.Series()
    
    print("⏳ 开始下载数据...")
    max_retries = 3
    for attempt in range(max_retries):
        try:
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
                print(f"⚠️ 尝试 {attempt+1} 失败，重试中...")
                time.sleep(5)
        except Exception as e:
            print(f"⚠️ 下载异常: {e}")
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

    # 收益回测
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

    def get_period_ret(cum, days):
        if len(cum) == 0: return 0.0
        target = cum.index[-1] - timedelta(days=days)
        idx = cum.index.searchsorted(target)
        if idx >= len(cum): idx = len(cum) - 1
        return (cum.iloc[-1] / cum.iloc[idx]) - 1

    # ================= 输出看板 (全量信息版) =================
    price_now = data[indicator_asset].iloc[-1]
    ma_now = sma.iloc[-1]
    rsi_now = rsi.iloc[-1]
    vix_now = vix_data.iloc[-1]

    # 1. 标题与时间
    is_open = "交易中" if (0<=now_ny.weekday()<=4 and 9.5<=now_ny.hour+now_ny.minute/60<=16) else "已收盘"
    print(f"## 📅 纳指策略日报")
    print(f"<div style='font-size:12px; color:#666;'>时间: {now_ny.strftime('%Y-%m-%d %H:%M')} (美东) | 状态: {is_open}</div>")

    # 2. 市场体检 (恢复详细数据)
    print(f"\n### 📊 市场体检")
    
    status_text = "✅ 牛市"
    if price_now < ma_now * (1 - bear_buffer): status_text = "❌ 熊市 (破位)"
    elif price_now < ma_now: status_text = "⚠️ 震荡 (均下)"
    
    rsi_color = "black"
    if rsi_now < rsi_buy_3x: rsi_color = "blue"
    elif rsi_now > rsi_sell_3x: rsi_color = "red"
    
    vix_icon = "🟢" if vix_now < 30 else "🔴" if vix_now > vix_threshold else "🟡"
    
    print(f"**趋势状态**: {status_text}")
    print(f"- **NDX价格**: `{price_now:.2f}` (MA{ma_window}: `{ma_now:.2f}`)")
    print(f"- **RSI指标**: <span style='color:{rsi_color};'>`{rsi_now:.2f}`</span> (机会<{rsi_buy_3x} / 过热>{rsi_sell_3x})")
    print(f"- **VIX恐慌**: `{vix_now:.2f}` {vix_icon} (熔断: {vix_threshold})")

    # 3. 历史业绩 PK (恢复 SPY 和 QLD 列)
    print(f"\n### 🏆 业绩对比")
    periods = {
        '1周':7, '1月':30, '3月':90, 
        '半年':180, '1年':365, '3年':1095, '10年':3650
    }
    
    perf_data = []
    for label, days in periods.items():
        s = get_period_ret(strat_cum, days)
        b1 = get_period_ret(bench_cum_1x, days)
        b2 = get_period_ret(bench_cum_2x, days)
        b3 = get_period_ret(bench_cum_3x, days)
        spy = get_period_ret(bench_cum_spx, days)
        
        icon = "🔥" if s > b2 else "" # 跑赢2倍算厉害
        
        perf_data.append({
            "区间": label,
            "策略": f"{s*100:.1f}%{icon}",
            "QQQ": f"{b1*100:.1f}%",
            "QLD": f"{b2*100:.1f}%", # 恢复
            "TQQQ": f"{b3*100:.1f}%",
            "SPY": f"{spy*100:.1f}%"  # 恢复
        })
    print(df_to_compact_html(pd.DataFrame(perf_data)))

    # 4. 调仓记录
    switch_history = []
    temp_signal = signals[-1]
    end_idx = len(signals) - 1
    for i in range(len(signals)-2, -1, -1):
        if signals[i] != temp_signal:
            switch_history.append({
                "日期": data.index[i+1].strftime('%Y-%m-%d'),
                "操作": f"{etf_map[signals[i]]} -> {etf_map[temp_signal]}",
                "持有": f"{end_idx - i}天"
            })
            temp_signal = signals[i]
            end_idx = i
        if len(switch_history) >= 5: break
    
    print(f"\n### 📝 最近调仓")
    if switch_history:
        print(df_to_compact_html(pd.DataFrame(switch_history)))
    else:
        print("无近期调仓记录")

    # 5. 年度战报 (保留所有列)
    print(f"\n### 📅 年度详细数据")
    df_perf = pd.DataFrame({'策略':strat_daily_ret, 'QQQ':ret_1x, 'QLD':ret_2x, 'TQQQ':ret_3x})
    years = sorted(df_perf.index.year.unique(), reverse=True)
    
    year_data = []
    for y in years:
        if y < datetime.now().year - 10: break # 只展示10年
        sub = df_perf[df_perf.index.year == y]
        ys = (1+sub['策略']).prod()-1
        yq = (1+sub['QQQ']).prod()-1
        y2 = (1+sub['QLD']).prod()-1
        y3 = (1+sub['TQQQ']).prod()-1
        
        tag = ""
        if ys > y3: tag = "🔥"
        elif y3 < -0.1 and ys > y3: tag = "🛡️"
        
        year_data.append({
            "年份": y,
            "策略": f"{ys*100:.1f}%{tag}",
            "QQQ": f"{yq*100:.1f}%",
            "QLD": f"{y2*100:.1f}%",
            "TQQQ": f"{y3*100:.1f}%"
        })
    print(df_to_compact_html(pd.DataFrame(year_data)))

    # 6. 今日行动 (计算持仓天数)
    last = signals[-1]
    prev = signals[-2]
    
    # 计算持仓天数
    held_days = 0
    for i in range(len(signals)-2, -1, -1):
        if signals[i] == last: held_days += 1
        else: break
    held_days += 1
    
    color = "#e74c3c" if last != prev else "#2ecc71"
    action_text = "调仓交易 (ACTION)" if last != prev else "锁仓持有 (HOLD)"
    
    print(f"\n<div style='background-color:#f9f9f9; padding:15px; border-left: 6px solid {color}; margin-top:15px; border-radius:4px; box-shadow: 2px 2px 5px #ddd;'>")
    print(f"<div style='font-size:16px; font-weight:bold; color:{color};'>{action_text}</div>")
    print(f"<div>昨日持有: {etf_map[prev]}</div>")
    print(f"<div>今日目标: <b>{etf_map[last]}</b></div>")
    print(f"<div style='font-size:12px; color:#666; margin-top:5px;'>已连续持仓: {held_days} 个交易日</div>")
    print("</div>")
    
    if last != prev:
        print(f"\n❗ **请卖出 {etf_map[prev]}，全仓买入 {etf_map[last]}**")

# ================= 3. 主程序 =================
if __name__ == "__main__":
    output_buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(output_buffer):
            run_strategy_logic()
    except Exception as e:
        output_buffer.write(f"\n❌ 错误: {str(e)}\n")
        traceback.print_exc(file=output_buffer)

    final_output = output_buffer.getvalue()
    print(final_output) 
    
    current_date = datetime.now(pytz.timezone('America/New_York')).strftime('%Y-%m-%d')
    send_pushplus(f"纳指策略 ({current_date})", final_output)
