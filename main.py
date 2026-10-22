"""Generate a validated daily report; --dry-run never sends or writes remote state."""
import argparse
import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from backtest import backtest_returns
from market_calendar import OutsideExecutionWindow, session_plan
from market_data import DataError, load_market_data
from notification_state import StateError, state_store
from notify import PushError, PushPlus
from report import build_report, data_failure_report
from strategy import MODEL_VERSION, calculate_signals

LOG = logging.getLogger(__name__)


def truthy(value):
    return str(value).lower() in ("true", "1", "yes")


def save_summary(output, values):
    (output / "run.json").write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8")
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write("## 纳指日报执行结果\n\n")
            for key, value in values.items():
                f.write(f"- {key}: {value}\n")


def publish_report(plan, title, content, output, category="daily"):
    client = PushPlus(os.environ.get("PUSH_TOKEN"), os.environ.get("PUSHPLUS_ACCESS_KEY"))
    raw_key = f"{MODEL_VERSION}|{plan.signal_date}|{plan.execution_date}|wechat|{category}"
    key = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    return client.publish(key, title, content, plan.market_close, state_store(output))


def run(dry_run=False, output_dir="output", now=None, loader=load_market_data, planner=session_plan, publisher=publish_report):
    now = now or datetime.now(timezone.utc)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    status = {"model_version": MODEL_VERSION, "dry_run": dry_run, "generated_at_utc": now.isoformat()}
    try:
        plan = planner(now, preview=dry_run)
        if plan is None:
            status.update(status="skipped_non_trading_day", notification="not_sent")
            save_summary(output, status)
            LOG.info("美股非交易日，不生成买卖指令或发送日报")
            return 0
        status.update(signal_date=str(plan.signal_date), execution_date=str(plan.execution_date), generated_after_open=plan.late)
        try:
            market = loader(plan.signal_date)
        except DataError:
            title, content = data_failure_report(plan)
            (output / "report.html").write_text(content, encoding="utf-8")
            if not dry_run:
                try:
                    receipt = publisher(plan, title, content, output, category="data-health")
                    status["failure_notice"] = receipt.status
                except (PushError, StateError) as exc:
                    LOG.error("数据异常通知未完成 type=%s", type(exc).__name__)
            raise
        # Double-check loader contracts before rendering a trading instruction.
        if market.close.empty or market.close.index[-1].date() != plan.signal_date:
            raise DataError("行情日期与预期信号交易日不一致")
        signals = calculate_signals(market.close)
        if signals.iloc[-1][["ma", "rsi"]].isna().any():
            raise DataError("指标尚未完成预热，停止发布交易目标")
        signals.to_csv(output / "signals.csv", encoding="utf-8-sig")
        performance = None
        try:
            performance = backtest_returns(market, signals)
            performance.to_csv(output / "daily_returns.csv", encoding="utf-8-sig")
        except Exception as exc:
            # A display-only backtest failure must not suppress a valid daily target.
            status["performance"] = "unavailable"
            LOG.error("回测模块不可用 type=%s，保留经验证的当日模型信号", type(exc).__name__)
        title, content = build_report(market, signals, plan, now, performance)
        (output / "report.html").write_text(content, encoding="utf-8")
        status.update(target={1: "QQQ", 2: "QLD", 3: "TQQQ"}[int(signals["signal"].iloc[-1])], sessions=len(market.close))
        if dry_run:
            status.update(status="dry_run_validated", notification="not_sent")
            LOG.info("DRY RUN: 报告已生成；未读取推送Token、未发微信、未写远程发送状态")
        else:
            receipt = publisher(plan, title, content, output)
            status.update(status="completed", notification=receipt.status, duplicate_suppressed=receipt.duplicate)
            if receipt.status != "delivered":
                LOG.warning("日报已受理或去重；最终投递尚未确认。查询需要PUSHPLUS_ACCESS_KEY")
        save_summary(output, status)
        return 0
    except (DataError, OutsideExecutionWindow, PushError, StateError, ValueError) as exc:
        # Only controlled errors are included verbatim; never log request bodies or credentials.
        status.update(status="failed", error_type=type(exc).__name__, error=str(exc))
        save_summary(output, status)
        LOG.error("任务失败 type=%s reason=%s", type(exc).__name__, str(exc))
        return 1
    except Exception as exc:
        status.update(status="failed", error_type=type(exc).__name__, error="Unexpected internal error; no success claimed")
        save_summary(output, status)
        LOG.error("未预期异常 type=%s，任务失败；未记录原始响应或凭据", type(exc).__name__)
        return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", default=truthy(os.environ.get("DRY_RUN", "false")))
    parser.add_argument("--output-dir", default="output")
    args = parser.parse_args()
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s", handlers=[logging.StreamHandler(), logging.FileHandler(output / "run.log", encoding="utf-8")])
    return run(dry_run=args.dry_run, output_dir=output)


if __name__ == "__main__":
    raise SystemExit(main())
