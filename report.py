"""Inline-styled mobile briefing; targets never imply actual broker holdings."""
from html import escape

import pandas as pd

from backtest import period_return
from market_calendar import NY, calendar_for
from strategy import ETF_MAP, MODEL_VERSION

BLUE, INK, MUTED = "#2563eb", "#172554", "#64748b"


def header(text, subtitle=""):
    return (f"<h3 style='margin:26px 0 10px;font-size:16px;color:{INK};border-left:3px solid {BLUE};padding-left:9px'>{escape(text)}</h3>"
            + (f"<p style='margin:0 0 10px;font-size:11px;color:{MUTED}'>{escape(subtitle)}</p>" if subtitle else ""))


def table(frame, returns=False):
    """Fixed-width, inline styles survive WeChat's stripped stylesheets."""
    parts = ["<table cellpadding='0' cellspacing='0' style='width:100%;table-layout:fixed;border-collapse:collapse;font-size:11px;text-align:right;line-height:1.5;white-space:nowrap'><thead><tr>"]
    for i, column in enumerate(frame.columns):
        align = "left" if i == 0 else "right"
        bg = BLUE if column == "策略" else INK
        parts.append(f"<th style='padding:9px 3px;background:{bg};color:#fff;text-align:{align};font-weight:600'>{escape(str(column))}</th>")
    parts.append("</tr></thead><tbody>")
    for n, row in enumerate(frame.itertuples(index=False, name=None)):
        parts.append(f"<tr style='background:{'#f8fafc' if n % 2 == 0 else '#fff'};border-bottom:1px solid #e8edf5'>")
        for i, value in enumerate(row):
            extra = "background:#eff6ff;font-weight:600;" if frame.columns[i] == "策略" else ""
            if i == 0:
                extra += "text-align:left;font-weight:600;"
            color = INK
            if returns and i > 0:
                if value is None or pd.isna(value):
                    value, color = "—", MUTED
                else:
                    color = "#047857" if value > 0 else "#be123c" if value < 0 else MUTED
                    value = f"{value:+.1%}"
            parts.append(f"<td style='padding:9px 3px;color:{color};{extra}'>{escape(str(value))}</td>")
        parts.append("</tr>")
    return "".join(parts) + "</tbody></table>"


def shell(content):
    return ("<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'></head>"
            f"<body style='margin:0;background:#f1f5f9;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Arial,sans-serif;color:{INK};font-size:13px;line-height:1.7'>"
            "<div style='max-width:560px;margin:0 auto;background:#fff;padding:22px 14px 26px'>" + content + "</div></body></html>")


def build_report(market, signals, plan, now, performance=None):
    latest = signals.iloc[-1]
    current, previous = int(latest["signal"]), int(signals["signal"].iloc[-2])
    changed = current != previous
    target, old = ETF_MAP[current], ETF_MAP[previous]
    title = f"纳指策略｜{plan.execution_date}执行（{plan.signal_date}收盘）"
    badge = "需要调仓" if changed else "保持目标"
    parts = [f"<div style='font-size:10px;letter-spacing:2px;color:{BLUE};font-weight:700'>NASDAQ · DAILY BRIEF</div>",
             f"<h1 style='margin:3px 0 2px;font-size:25px;color:{INK}'>纳指策略日报</h1>",
             f"<p style='margin:0 0 16px;color:{MUTED};font-size:12px'>{plan.signal_date} 美东收盘 · {plan.execution_date} 执行</p>"]
    parts.append(f"<div style='background:{INK};border-radius:14px;padding:20px;color:#fff'>"
                 f"<span style='display:inline-block;background:{'#b45309' if changed else '#047857'};border-radius:20px;padding:2px 10px;font-size:11px;color:#fff'>{badge}</span>"
                 "<div style='margin-top:12px;font-size:12px;color:#cbd5e1'>今日模型目标</div>"
                 f"<div style='font-size:38px;font-weight:800;line-height:1.3;color:#fff'>{target}<span style='font-size:18px;font-weight:400;color:#cbd5e1'>　100%</span></div>"
                 f"<div style='margin-top:10px;font-size:12px;color:#dbeafe'>{f'目标切换：{old} → {target}' if changed else f'模型目标保持不变 · 上一目标 {old}'}</div></div>")
    instruction = (f"模型由 {old} 切换至 {target}。请在美股常规交易时段核对实际仓位后自行执行。" if changed else "若实际仓位与模型目标一致，可继续持有；本次没有新的模型调仓。")
    parts.append(f"<p style='margin:12px 2px;color:{MUTED};font-size:12px'>{instruction}</p>")
    if plan.late:
        parts.append("<div style='padding:12px;background:#fffbeb;border-left:3px solid #d97706;color:#92400e;font-size:12px'>迟到提醒：本次生成时已经开盘。回测开盘价不代表当前可成交价格，请先核对实际仓位和市场。</div>")

    ndx, ma, rsi = float(market.close['^NDX'].iloc[-1]), float(latest['ma']), float(latest['rsi'])
    gap = ndx / ma - 1
    trend = "趋势线上方" if gap > 0 else "趋势线下方"
    parts.append(header("市场体检", "趋势、强弱与波动，一眼看清"))
    metrics = [("纳指 NDX", f"{ndx:,.2f}", f"{trend} {gap:+.1%}", "#047857" if gap > 0 else "#be123c"),
               ("趋势线 MA170", f"{ma:,.2f}", "170日均线", MUTED),
               ("强弱 RSI14", f"{rsi:.1f}", "低于65 / 高于80触发切换", "#b45309" if rsi > 80 else BLUE),
               ("防守线 MA170×99%", f"{ma * .99:,.2f}", "纳指跌破此线转为QQQ", "#be123c" if ndx < ma * .99 else "#047857")]
    parts.append("<table cellpadding='0' cellspacing='0' style='width:100%;table-layout:fixed'>")
    for j in (0, 2):
        parts.append("<tr>")
        for name, value, note, color in metrics[j:j + 2]:
            parts.append(f"<td style='width:50%;padding:3px;vertical-align:top'><div style='background:#f8fafc;border:1px solid #e8edf5;border-radius:10px;padding:11px 9px'><div style='font-size:11px;color:{MUTED}'>{name}</div><div style='font-size:21px;font-weight:700;color:{INK}'>{value}</div><div style='font-size:10px;color:{color}'>{note}</div></div></td>")
        parts.append("</tr>")
    parts.append("</table>")

    parts.append(header("收益成绩单", "次日开盘成交假设 · 区间累计收益，非年化"))
    if performance is None:
        parts.append("<p style='color:#b45309'>本次业绩模块不可用；模型信号已单独通过行情校验。</p>")
    else:
        rows = []
        for label, days in [("1周", 7), ("1月", 30), ("3月", 90), ("半年", 180), ("1年", 365), ("3年", 1095), ("10年", 3650)]:
            values = period_return(performance, days)
            rows.append({"区间": label, **{c: None if values is None else values[c] for c in performance}})
        parts.append(table(pd.DataFrame(rows), returns=True))
        equity = (1 + performance).cumprod()
        dd = equity.div(equity.cummax().clip(lower=1)) - 1
        parts.append(f"<p style='margin:9px 0;font-size:11px;color:{MUTED}'>策略全区间最大回撤 <b style='color:#be123c'>{dd['策略'].min():.1%}</b>　｜　绿色为正收益，红色为负收益；— 表示历史不足。</p>")
        annual = performance.groupby(performance.index.year).agg(lambda r: (1 + r).prod() - 1).tail(11).iloc[::-1]
        annual.index.name = "年份"
        parts.append(header("年度表现", "本年仅统计截至行情日期的收益"))
        parts.append(table(annual.reset_index(), returns=True))

    parts.append(header("最近5次目标切换", "日期为信号收盘日 → 计划执行日；并非账户成交记录"))
    changes = signals.loc[signals['signal'].ne(signals['signal'].shift())].iloc[1:].tail(5).iloc[::-1]
    cal = calendar_for(now)
    for date, row in changes.iterrows():
        pos = signals.index.get_loc(date)
        before = ETF_MAP[int(signals['signal'].iloc[pos - 1])]
        after = ETF_MAP[int(row['signal'])]
        execution = cal.next_session(pd.Timestamp(date).normalize()).date()
        parts.append(f"<div style='padding:10px 2px;border-bottom:1px solid #e8edf5'><b style='font-size:13px;color:{INK}'>{before} <span style='color:{BLUE}'>→</span> {after}</b><br><span style='font-size:11px;color:{MUTED}'>{date.date()} → {execution}</span></div>")
    if changes.empty:
        parts.append(f"<p style='color:{MUTED}'>当前历史中没有目标切换。</p>")
    parts.append(header("规则备忘", "VIX过滤已移除 · 其余交易规则不变"))
    parts.append(f"<div style='padding:12px;background:#f8fafc;border-radius:10px;font-size:11px;color:{MUTED}'>NDX 跌破 MA170 下方1%，目标 QQQ；由QQQ恢复时须重新站上MA170，先回QLD。其余时候 RSI&lt;65 转TQQQ，RSI&gt;80 回QLD；65—80之间保留上一状态，可能继续持有TQQQ。RSI14采用简单滚动均值。</div>")
    if performance is not None:
        parts.append(f"<p style='font-size:10px;color:{MUTED};margin-top:18px'>回测起点 {performance.index[0].date()}。信号形成后下一交易日开盘换仓，旧仓承担隔夜收益；每交易单边0.05%成本，计复权分红，未计个人税费。开盘后手动成交可能不同。历史收益不保证未来表现。</p>")
    parts.append(f"<div style='margin-top:18px;padding-top:12px;border-top:1px solid #e8edf5;font-size:10px;color:{MUTED}'>生成于 {now.astimezone(NY):%Y-%m-%d %H:%M %Z}<br>模型版本 {escape(MODEL_VERSION)}<br>模型未读取证券账户；推送受理不代表渠道投递或账户成交。</div>")
    return title, shell("\n".join(parts))


def style_preview(title, content):
    banner = ("<div style='margin:0 0 16px;padding:12px;background:#fffbeb;border:1px solid #fde68a;border-radius:10px;color:#92400e;font-size:12px'><b>样式预览 · 请看排版效果</b><br>本条为手动发送的格式测试，不作为新增调仓通知。正式日报和交易规则保持原状。</div>")
    marker = "<div style='font-size:10px;letter-spacing:2px;"
    return "【样式预览】" + title, content.replace(marker, banner + marker, 1)


def data_failure_report(plan):
    content = (header("行情校验未通过") + "<div style='padding:16px;background:#fffbeb;border-radius:12px;color:#92400e'><b>本次没有生成新交易指令</b>"
               f"<p>执行日：{plan.execution_date}<br>预期信号日：{plan.signal_date}</p></div>"
               f"<p style='color:{MUTED}'>请勿把旧日报当作今日更新。任务日志和诊断文件已保留，后续补跑会重新检查。</p>")
    return f"纳指策略数据异常｜{plan.execution_date}", shell(content)
