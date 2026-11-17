"""Retry validated normal reports quietly, with a finite per-job time budget."""
import argparse
from dataclasses import replace
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import time

import delivery
import main
from market_calendar import recovery_session_plan, session_plan
from notification_state import StateError, state_store

LOG = logging.getLogger(__name__)


def run(output_dir='output', dry_run=False, after_close=False, recovery=False,
        budget_seconds=660, clock=lambda: datetime.now(timezone.utc),
        monotonic=time.monotonic, sleep=time.sleep, runner=main.run,
        store_factory=state_store, loader=None):
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    start = monotonic()
    now = clock()
    planner = recovery_session_plan if recovery else session_plan
    try:
        plan = planner(now, preview=dry_run, after_close=after_close)
    except main.OutsideExecutionWindow:
        # Main records terminal expiry quietly; never roll it in this mode.
        return runner(dry_run, output, now, after_close=after_close)
    if plan is None:
        main.save_summary(output, dict(status='skipped_recovery_window', notification='not_sent', dry_run=dry_run))
        return 0
    attempts = 0
    market_cache = {}

    def cached_loader(signal_date):
        # Only cache a fully validated loader result; failed downloads retry.
        if signal_date not in market_cache:
            market_cache[signal_date] = (loader or main.load_market_data)(signal_date)
        return market_cache[signal_date]

    while True:
        now = clock()
        if now >= plan.market_close:
            main.save_summary(output, dict(status='expired_without_delivery', notification='not_sent',
                                          retryable=False, signal_date=str(plan.signal_date),
                                          execution_date=str(plan.execution_date), recovery_attempts=attempts))
            return 1
        if not dry_run:
            try:
                if delivery.has_submission(main.notification_key(plan), store_factory(output)):
                    main.save_summary(output, dict(status='skipped_already_submitted', notification='not_resent',
                                                  signal_date=str(plan.signal_date), execution_date=str(plan.execution_date)))
                    return 0
            except StateError:
                LOG.warning('发送状态暂不可读；由正式发送路径继续保护持久化约束')
        attempts += 1
        # Pin dates to the first plan even if an in-job retry crosses a boundary.
        fixed = replace(plan, late=now >= plan.market_open)
        code = runner(dry_run, output, now, planner=lambda *a, **k: fixed,
                      after_close=after_close, loader=cached_loader)
        summary_path = output/'run.json'
        summary = json.loads(summary_path.read_text(encoding='utf-8'))
        summary.update(recovery_attempts=attempts)
        if clock() >= plan.market_close and code != 0:
            summary.update(status='expired_without_delivery', notification='not_sent', retryable=False)
            main.save_summary(output, summary)
            return 1
        main.save_summary(output, summary)
        if code == 0 or dry_run or not summary.get('retryable', False):
            return code
        delay = min(60, 15 * 2 ** min(attempts-1, 2))
        remaining = min(budget_seconds-(monotonic()-start), (plan.market_close-clock()).total_seconds())
        if remaining <= delay:
            LOG.warning('本次运行预算结束；下一定时补跑继续同日报，未发送故障内容')
            return code
        LOG.warning('后台等待重试 attempt=%d delay_seconds=%d', attempts, delay)
        sleep(delay)


def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--recovery', action='store_true', default=main.truthy(os.environ.get('RECOVERY_ONLY', 'false')))
    parser.add_argument('--dry-run', action='store_true', default=main.truthy(os.environ.get('DRY_RUN', 'false')))
    parser.add_argument('--after-close', action='store_true', default=main.truthy(os.environ.get('AFTER_CLOSE', 'false')))
    parser.add_argument('--output-dir', default='output')
    args = parser.parse_args()
    output = Path(args.output_dir); output.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s',
                        handlers=[logging.StreamHandler(), logging.FileHandler(output/'run.log', encoding='utf-8')])
    return run(args.output_dir, args.dry_run, args.after_close, args.recovery)


if __name__ == '__main__':
    raise SystemExit(cli())
