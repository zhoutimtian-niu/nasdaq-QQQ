# ================= 1. 策略逻辑封装 (美化版) =================
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
        tickers = [symbol_1x, symbol_2x, symbol_3x, symbol_spx, indicator_asset, vix_asset]
        
        # 重试机制
        max_retries = 3
        data = pd.DataFrame()
        vix_data = pd.Series()
        
        for attempt in range(max_retries):
            try:
                raw_data = yf.download(tickers, period="max", interval="1d", auto_adjust=False, progress=False)
                adj_close = pd.DataFrame()
                if isinstance(raw_data.columns, pd.MultiIndex):
                    try:
                        adj_close = raw_data['Adj Close']
                    except KeyError:
                        adj_close = raw_data['Close']
                else:
                    adj_close = raw_data['Adj Close'] if 'Adj Close' in raw_data else raw_data['Close']
                
                # 检查核心数据
                if symbol_1x in adj_close.columns and len(adj_close[symbol_1x].dropna()) > 200:
                    core_assets = [symbol_1x, symbol_2x, symbol_3x, symbol_spx, indicator_asset]
                    data = adj_close[core_assets].ffill().dropna()
                    if vix_asset in adj_close.columns:
                        vix_data = adj_close[vix_asset].reindex(data.index).ffill().fillna(0)
                    else:
                        vix_data = pd.Series(0, index=data.index)
                    break
            except:
                pass
        
        if data.empty:
            print("❌ 严重错误: 数据下载失败")
            return

    except Exception as e:
        print(f"❌ 数据处理异常: {e}")
        return 

    # ================= 策略逻辑 =================
    if not data.empty:
        # 指标计算
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

        def get_period_return(cum_series, days_lookback):
            if len(cum_series) == 0: return 0.0
            target_date = cum_series.index[-1] - timedelta(days=days_lookback)
            if target_date < cum_series.index[0]:
                start_val = cum_series.iloc[0]
            else:
                idx = cum_series.index.searchsorted(target_date)
                if idx >= len(cum_series): idx = len(cum_series) - 1
                start_val = cum_series.iloc[idx]
            return (cum_series.iloc[-1] / start_val) - 1

        # ------------------ 输出看板 ------------------
        price_now = data[indicator_asset].iloc[-1]
        ma_now = sma.iloc[-1]
        rsi_now = rsi.iloc[-1]
        vix_now = vix_data.iloc[-1]

        print("\n" + "---")
        print(f"### 📊 策略决策看板")
        
        # 1. 市场体检
        print(f"**【1. 市场体检】**")
        if price_now < ma_now * (1 - bear_buffer): status = "❌ 熊市 (破位)"
        elif price_now < ma_now: status = "⚠️ 震荡 (均下)"
        else: status = "✅ 牛市 (均上)"
        
        rsi_desc = "⚪"
        if rsi_now < rsi_buy_3x: rsi_desc = "🔵 机会"
        elif rsi_now > rsi_sell_3x: rsi_desc = "🔴 过热"
        
        vix_icon = "🟢" if vix_now < 30 else "🔴" if vix_now > vix_threshold else "🟡"
        
        print(f"- 趋势: {status} | RSI: `{rsi_now:.1f}` {rsi_desc}")
        print(f"- 纳指: `{price_now:.1f}` | VIX: `{vix_now:.1f}` {vix_icon}")

        # 🔥🔥🔥 2. 历史业绩 PK (HTML 表格美化版) 🔥🔥🔥
        print(f"\n**【2. 历史业绩PK】**")
        
        periods = {
            '近1周': 7, '近1月': 30, '近3月': 90, 
            '近6月': 180, '近1年': 365, '近3年': 1095, 
            '近10年': 3650
        }
        
        perf_data = []
        for label, days in periods.items():
            s = get_period_return(strat_cum, days)
            b1 = get_period_return(bench_cum_1x, days)
            b3 = get_period_return(bench_cum_3x, days) # 只对比1x和3x，手机屏幕窄
            
            icon = "🔥" if (s is not None and b1 is not None and s > b1) else ""
            
            perf_data.append({
                "区间": label,
                "策略": f"{s*100:.1f}% {icon}",
                "QQQ": f"{b1*100:.1f}%",
                "TQQQ": f"{b3*100:.1f}%"
            })
            
        # 生成 HTML 表格
        df_display = pd.DataFrame(perf_data)
        
        # 定义 CSS 样式：居中、边框、紧凑、字体小一点适应手机
        table_style = [
            {'selector': 'table', 'props': [('width', '100%'), ('border-collapse', 'collapse'), ('font-size', '12px'), ('font-family', 'sans-serif')]},
            {'selector': 'th', 'props': [('background-color', '#f2f2f2'), ('color', '#333'), ('padding', '4px'), ('border', '1px solid #ddd'), ('text-align', 'center')]},
            {'selector': 'td', 'props': [('padding', '4px'), ('border', '1px solid #ddd'), ('text-align', 'center')]},
        ]
        
        # 转换为 HTML (不带索引)
        html_table = df_display.style.set_table_styles(table_style).hide(axis='index').to_html()
        print(html_table) # 直接打印 HTML，PushPlus markdown 模板支持渲染

        # ------------------ 3. 调仓记录 ------------------
        switch_history = []
        temp_signal = signals[-1]
        temp_end_idx = len(signals) - 1
        for i in range(len(signals) - 2, -1, -1):
            if signals[i] != temp_signal:
                switch_history.append({
                    '日期': data.index[i+1].strftime('%m-%d'), # 日期缩短一点
                    '操作': f"{etf_map[signals[i]]}->{etf_map[temp_signal]}",
                    '天数': f"{temp_end_idx - i}天"
                })
                temp_signal = signals[i]
                temp_end_idx = i
            if len(switch_history) >= 5: break
            
        print(f"\n**【3. 最近调仓】**")
        if switch_history:
            df_switch = pd.DataFrame(switch_history)
            print(df_switch.style.set_table_styles(table_style).hide(axis='index').to_html())
        else:
            print("无近期调仓记录")

        # ------------------ 4. 年度战报 ------------------
        print(f"\n**【4. 年度战报 (近10年)】**")
        df_perf = pd.DataFrame({'S':strat_daily_ret, 'Q':ret_1x, '3x':ret_3x})
        current_year = datetime.now().year
        start_year = current_year - 10
        df_perf = df_perf[df_perf.index.year >= start_year]

        year_data = []
        for year in sorted(df_perf.index.year.unique(), reverse=True): # 倒序，最近的在上面
            sub = df_perf[df_perf.index.year == year]
            y_s = (1 + sub['S']).prod() - 1
            y_q = (1 + sub['Q']).prod() - 1
            y_3x = (1 + sub['3x']).prod() - 1
            
            tag = ""
            if y_s > y_3x: tag = "🔥" # 完胜
            elif y_3x < -0.2 and y_s > y_3x + 0.1: tag = "🛡️" # 避险
            
            year_data.append({
                "年份": year,
                "策略": f"{y_s*100:.1f}% {tag}",
                "QQQ": f"{y_q*100:.1f}%",
                "TQQQ": f"{y_3x*100:.1f}%"
            })
            
        df_year = pd.DataFrame(year_data)
        print(df_year.style.set_table_styles(table_style).hide(axis='index').to_html())

        # ------------------ 今日指南 ------------------
        last_signal = signals[-1]
        sig_prev = signals[-2]
        
        # 计算持仓天数
        current_held_days = 0
        for i in range(len(signals) - 2, -1, -1):
            if signals[i] == last_signal: current_held_days += 1
            else: break
        current_held_days += 1
        
        print(f"\n### 📢 【今日行动】")
        
        # 使用 HTML 强调样式
        color = "red" if last_signal != sig_prev else "green"
        action_text = "调仓交易 (ACTION)" if last_signal != sig_prev else "锁仓持有 (HOLD)"
        
        print(f"<div style='background-color:#f9f9f9; padding:10px; border-left: 5px solid {color};'>")
        print(f"<b>当前状态:</b> {action_text}<br>")
        print(f"<b>目标持仓:</b> <span style='font-size:16px; color:{color}; font-weight:bold;'>{etf_map[last_signal]}</span><br>")
        print(f"<b>持仓时间:</b> {current_held_days} 天")
        print("</div>")

        if last_signal != sig_prev:
            print(f"\n👉 **请卖出 {etf_map[sig_prev]}，买入 {etf_map[last_signal]}**")

    else:
        print("❌ 错误: 有效数据为空")
