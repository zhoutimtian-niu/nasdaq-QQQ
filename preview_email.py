"""Explicitly requested, labeled email preview; never posts to WeChat."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os
from pathlib import Path

from backup_email import BackupEmail, EmailConfig
import main
from notification_state import state_store
from notify import PushError
from report import style_preview


def run(output_dir='output', generator=main.run, client=None, store=None):
    result=generator(dry_run=True,output_dir=output_dir)
    if result:
        return result
    output=Path(output_dir)
    status=json.loads((output/'run.json').read_text(encoding='utf-8'))
    if status.get('status')!='dry_run_validated':
        raise ValueError('Email preview requires a freshly validated report')
    run_id=os.environ.get('GITHUB_RUN_ID')
    if not run_id:
        raise ValueError('Email preview requires an explicitly dispatched GitHub run')
    title=f"纳指策略｜{status['execution_date']}执行（{status['signal_date']}收盘）"
    title,content=style_preview(title,(output/'report.html').read_text(encoding='utf-8'))
    if '本条为手动发送的格式测试' not in content:
        raise ValueError('Email preview banner missing; no email sent')
    if client is None:
        config=EmailConfig.from_environment()
        if config is None:
            raise PushError('备用邮箱尚未启用，测试邮件未发送')
        client=BackupEmail(config)
    key=hashlib.sha256(('email-style-preview-v1|'+run_id).encode()).hexdigest()
    receipt=client.publish(key,title,content,datetime.now(timezone.utc)+timedelta(minutes=30),store if store is not None else state_store(output))
    status.update(status='email_preview_smtp_accepted',dry_run=False,preview=True,
                  notification=receipt.status,duplicate_suppressed=receipt.duplicate)
    main.save_summary(output,status)
    print(f'Email preview: {receipt.status}; this is SMTP acceptance, not an inbox/read receipt')
    return 0


if __name__=='__main__':
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(run())
