"""Attempt independent routes even if one route fails; retain per-route state."""
import logging
import os
import hashlib

from backup_email import BackupEmail, EmailConfig
from notification_state import StateError, state_store
from notify import PushError, PushPlus, Receipt

LOG = logging.getLogger(__name__)


def has_submission(key, store):
    """Read-only check for an accepted email or confirmed WeChat submission.

    Used only to suppress a false overdue alert, never as a user read receipt.
    Unverified PushPlus acceptance alone is deliberately insufficient.
    """
    email_key = hashlib.sha256(('backup-email-v1|' + key).encode()).hexdigest()
    with store:
        entries = store.load()['entries']
        wechat, email = entries.get(key), entries.get(email_key)
        return (isinstance(wechat, dict) and wechat.get('status') == 'delivered') or (
            isinstance(email, dict) and email.get('status') == 'smtp_accepted')


def publish(key, title, content, expires_at, output, primary=None, backup=None, store=None):
    store = store if store is not None else state_store(output)
    results = {}
    receipts = {}
    errors = []
    # Construct each route independently: a missing PushPlus token must not block email.
    for channel in ('wechat', 'email'):
        try:
            if channel == 'wechat':
                client = primary if primary is not None else PushPlus(os.environ.get('PUSH_TOKEN'), os.environ.get('PUSHPLUS_ACCESS_KEY'))
            else:
                config = EmailConfig.from_environment() if backup is None else None
                client = backup if backup is not None else (BackupEmail(config) if config else None)
                if client is None:
                    continue
            receipt = client.publish(key, title, content, expires_at, store)
            receipts[channel] = receipt
            results[channel] = dict(status=receipt.status, duplicate=receipt.duplicate)
        except (PushError, StateError) as exc:
            errors.append(exc)
            results[channel] = dict(status='error', error_type=type(exc).__name__)
            LOG.error('通知渠道未完成 channel=%s error_type=%s', channel, type(exc).__name__)
    if receipts.get('wechat') and receipts['wechat'].status == 'delivered':
        status = 'delivered'
    elif receipts.get('email') and receipts['email'].status == 'smtp_accepted':
        status = 'smtp_accepted'
    elif receipts.get('wechat'):
        status = receipts['wechat'].status
    elif errors:
        # Existing controlled errors remain nonzero and do not expose credentials.
        raise errors[0]
    else:
        raise PushError('没有可用的通知渠道')
    return Receipt(status, duplicate=bool(receipts) and all(r.duplicate for r in receipts.values()), channels=results)
