"""Mobile-friendly report. Model targets are never described as actual broker holdings."""
from html import escape

import pandas as pd

from backtest import period_return
from market_calendar import NY
from strategy import ETF_MAP, MODEL_VERSION


def header(text):
    return f"<h3 style='border-left:4px solid #007bff;padding-left:8px'>{escape(text)}</h3>"


def table(frame):
    return frame.to_html(index=False, escape=True, border=0, classes="data")


def build_report(market, signals, plan, now, performance=None):
    latest = signals.iloc[-1]
    current, previous = int(latest["signal"]), int(signals["signal"].iloc[-2])
    changed = current != previous
    target, old = ETF_MAP[current], ETF_MAP[previous]
    title = f"纳指策略｜{plan.execution_date}执行（{plan.signal_date}收盘）"
    parts = ["<html><meta charset='utf-8'><body style='font-family:sans-serif;color:#333;font-size:14px'>", "<h2>纳指策略日报</h2>"]
    parts.append(f"<p>行情日期：<b>{plan.signal_date}</b>（美东已收盘）<br>计划执行：<b>{plan.execution_date}</b> 美股常规交易时段<br>生成时间：{now.astimezone(NY):%Y-%m-%d %H:%M %Z}</p>")
    if plan.late:
        parts.append("<p style='color:#b45309'><b>迟到提醒：</b>本次生成时已经开盘，回测开盘价不代表你现在可成交的价格；请先核对实际仓位和当前市场。</p>")
    parts.append(header("今日模型目标"))
    label = "调仓信号" if changed else "模型目标保持不变"
    parts.append(f"<div style='padding:14px;background:#eef6fc'><b>{label}</b><br>上一模型目标：{old}<br>本次模型目标：<b>100% {target}</b></div>")
    if changed:
        parts.append(f"<p>模型由 {old} 切换至 {target}。请在常规交易时段核对实际仓位后自行执行。</p>")
    else:
        parts.append("<p>若你的实际仓位与模型目标一致，可继续持有；模型没有读取你的证券账户。</p>")
    parts.append(header("市场体检"))
    health = pd.DataFrame({"指标": ["NDX", "MA170", "RSI14（简单滚动均值）", "VIX"], "数值": [f"{market.close['^NDX'].iloc[-1]:.2f}", f"{latest['ma']:.2f}", f"{latest['rsi']:.2f}", f"{market.close['^VIX'].iloc[-1]:.2f}"]})
    parts.append(table(health))
    parts.append(header("原交易规则（保持不变）"))
    parts.append("<p>NDX 跌破 MA170 下方1%，或 VIX&gt;40，目标 QQQ；由QQQ恢复时须重新站上MA170，先回QLD。其余时候 RSI&lt;65 转TQQQ，RSI&gt;80 回QLD；65—80之间保留上一状态，可能继续持有TQQQ。</p>")
    parts.append(header("业绩对比｜次日开盘成交假设"))
    if performance is None:
        parts.append("<p>本次业绩模块不可用；上述模型信号已单独通过行情校验。</p>")
    else:
        rows = []
        for label, days in [("1周", 7), ("1月", 30), ("3月", 90), ("半年", 180), ("1年", 365), ("3年", 1095), ("10年", 3650)]:
            values = period_return(performance, days)
            rows.append({"区间": label, **{c: "历史不足" if values is None else f"{values[c]:.1%}" for c in performance}})
        parts.append(table(pd.DataFrame(rows)))
        equity = (1 + performance).cumprod()
        dd = equity.div(equity.cummax().clip(lower=1)) - 1
        parts.append(f"<p style='font-size:12px;color:#666'>回测起点：{performance.index[0].date()}。信号形成后下一交易日开盘换仓，旧仓承担隔夜收益，每交易单边0.05%成本；计复权分红，未计个人税费。策略该区间最大回撤：{dd['策略'].min():.1%}。实际开盘后手动成交可能不同。</p>")
        annual = performance.groupby(performance.index.year).agg(lambda r: (1 + r).prod() - 1).tail(11).iloc[::-1]
        annual.index.name = "年份"
        annual = annual.map(lambda x: f"{x:.1%}").reset_index()
        parts.append(header("年度收益（本年为截至行情日期）"))
        parts.append(table(annual))
    parts.append(f"<p style='font-size:11px;color:#888'>模型版本：{escape(MODEL_VERSION)}。推送受理和渠道投递状态单独记录，不代表账户已成交。</p></body></html>")
    return title, "\n".join(parts)


def data_failure_report(plan):
    return f"纳指策略数据异常｜{plan.execution_date}", f"<h3>今日行情未通过完整性校验</h3><p>执行日：{plan.execution_date}；预期信号日：{plan.signal_date}。</p><p>本次没有生成新交易指令。请勿把旧日报当作今日更新；任务日志和诊断文件已保留，后续补跑会重新检查。</p>"
