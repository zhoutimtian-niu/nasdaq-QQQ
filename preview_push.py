"""Explicitly send one validated, labeled style preview, including on weekends."""
import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import main
from notification_state import state_store
from notify import PushPlus
from report import style_preview


def run(output_dir="output", generator=main.run, client=None, store=None):
    output = Path(output_dir)
    result = generator(dry_run=True, output_dir=output_dir)
    if result:
        return result
    status = json.loads((output / "run.json").read_text(encoding="utf-8"))
    if status.get("status") != "dry_run_validated":
        raise ValueError("Preview requires a freshly validated report")
    title = f"纳指策略｜{status['execution_date']}执行（{status['signal_date']}收盘）"
    title, content = style_preview(title, (output / "report.html").read_text(encoding="utf-8"))
    (output / "report.html").write_text(content, encoding="utf-8")
    # Reruns of this Actions run share a key; production daily keys are untouched.
    run_id = os.environ.get("GITHUB_RUN_ID")
    if not run_id:
        raise ValueError("Style preview sending requires an explicit GitHub Actions run")
    key = hashlib.sha256(f"style-preview-v1|{run_id}".encode()).hexdigest()
    client = client or PushPlus(os.environ.get("PUSH_TOKEN"), os.environ.get("PUSHPLUS_ACCESS_KEY"))
    receipt = client.publish(key, title, content, datetime.now(timezone.utc) + timedelta(minutes=30), store or state_store(output))
    status.update(status="style_preview_completed", dry_run=False, preview=True,
                  notification=receipt.status, duplicate_suppressed=receipt.duplicate)
    main.save_summary(output, status)
    print(f"Style preview: {receipt.status}; duplicate_suppressed={receipt.duplicate}")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(run())
