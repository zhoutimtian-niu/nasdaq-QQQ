"""WeChat first; normal-report email fallback after failure or receipt timeout."""
import hashlib
import logging
import os
from datetime import datetime, timedelta

from backup_email import BackupEmail, EmailConfig
from notification_state import StateError, state_store
from notify import DeliveryUnknown, PushError, PushPlus, Receipt, utc_now

LOG = logging.getLogger(__name__)
FALLBACK_WAIT = timedelta(minutes=10)


def email_key(key):
    return hashlib.sha256(('backup-email-v1|' + key).encode()).hexdigest()


def has_submission(key, store):
    with store:
        entries = store.load()['entries']
        wechat, email = entries.get(key), entries.get(email_key(key))
        return (isinstance(wechat, dict) and wechat.get('status') == 'delivered') or (
            isinstance(email, dict) and email.get('status') == 'smtp_accepted')


def publish(key, title, content, expires_at, output, primary=None, backup=None, store=None, clock=utc_now):
    now = clock()
    if now >= expires_at:
        raise PushError('日报已超过有效交易窗口，未发送')
    store = store if store is not None else state_store(output)
    route_key = hashlib.sha256(('notification-routing-v1|' + key).encode()).hexdigest()
    with store:
        state = store.load()
        wechat, email = state['entries'].get(key), state['entries'].get(email_key(key))
        if isinstance(wechat, dict) and wechat.get('status') == 'delivered':
            return Receipt('delivered', duplicate=True, channels={'wechat': {'status': 'delivered', 'duplicate': True}})
        if isinstance(email, dict) and email.get('status') == 'smtp_accepted':
            return Receipt('smtp_accepted', duplicate=True, channels={'email': {'status': 'smtp_accepted', 'duplicate': True}})
        route = state['entries'].get(route_key)
        if route is None:
            route = {'status': 'routing', 'created_at': now.isoformat(), 'fallback_started': False}
            state['entries'][route_key] = route
            store.save(state)
        try:
            started = datetime.fromisoformat(route['created_at'])
            if route.get('status') != 'routing' or not isinstance(route.get('fallback_started'), bool):
                raise ValueError
            waiting = now - started < FALLBACK_WAIT
        except (KeyError, TypeError, ValueError):
            raise StateError('通知路由记录无效') from None
        fallback_started = route['fallback_started']
    results = {}
    definite_failure = False
    try:
        client = primary if primary is not None else PushPlus(os.environ.get('PUSH_TOKEN'), os.environ.get('PUSHPLUS_ACCESS_KEY'), clock=clock)
        receipt = client.publish(key, title, content, expires_at, store)
        results['wechat'] = dict(status=receipt.status, duplicate=receipt.duplicate)
        if receipt.status == 'delivered':
            return Receipt('delivered', duplicate=receipt.duplicate, channels=results)
    except DeliveryUnknown:
        results['wechat'] = dict(status='unknown')
    except PushError as exc:
        definite_failure = True
        results['wechat'] = dict(status='failed', error_type=type(exc).__name__)
    # StateError propagates: shared ledger failure must not send.
    if not definite_failure and waiting and not fallback_started:
        return Receipt('retry_pending', channels=results)
    if not fallback_started:
        with store:
            state = store.load()
            state['entries'][route_key]['fallback_started'] = True
            store.save(state)
    try:
        config = EmailConfig.from_environment() if backup is None else None
        client = backup if backup is not None else (
            BackupEmail(config, clock=clock, retry_unknown_after=timedelta(minutes=30)) if config else None)
        if client is None:
            results['email'] = dict(status='disabled')
        else:
            receipt = client.publish(key, title, content, expires_at, store)
            results['email'] = dict(status=receipt.status, duplicate=receipt.duplicate)
            if receipt.status == 'smtp_accepted':
                return Receipt('smtp_accepted', duplicate=receipt.duplicate, channels=results)
    except PushError as exc:
        results['email'] = dict(status='retry_pending', error_type=type(exc).__name__)
        LOG.warning('备用邮件尚未确认受理，保留重试 type=%s', type(exc).__name__)
    return Receipt('retry_pending', channels=results)
