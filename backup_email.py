"""Independent SMTP transport. Acceptance is not an inbox/read receipt."""
import hashlib
import os
import re
import smtplib
import ssl
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from email.utils import format_datetime

from notify import DeliveryUnknown, PushError, Receipt, utc_now, retry_due, schedule_retry
from notification_state import StateError


@dataclass(frozen=True)
class EmailConfig:
    host: str
    port: int
    mode: str
    username: str
    password: str = field(repr=False)
    sender: str
    recipient: str

    @classmethod
    def from_environment(cls):
        if os.environ.get('BACKUP_EMAIL_ENABLED', 'false').lower() not in ('true', '1', 'yes'):
            return None
        names = ('SMTP_HOST', 'SMTP_USERNAME', 'SMTP_PASSWORD', 'SMTP_FROM', 'SMTP_TO')
        values = {name: os.environ.get(name, '') for name in names}
        if any(not value or '\r' in value or '\n' in value for value in values.values()):
            raise PushError('备用邮箱已开启但SMTP配置不完整或含非法换行；检查Secrets')
        # Single recipient: a failure can never silently deliver only part of a list.
        address = r'[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+'
        if not all(re.fullmatch(address, values[n]) for n in ('SMTP_FROM', 'SMTP_TO')):
            raise PushError('SMTP_FROM/SMTP_TO必须各为一个有效邮箱地址')
        mode = (os.environ.get('SMTP_SECURITY') or 'ssl').lower()
        if mode not in ('ssl', 'starttls'):
            raise PushError('邮件必须使用SSL或STARTTLS，不允许明文认证')
        try:
            port = int(os.environ.get('SMTP_PORT') or ('465' if mode == 'ssl' else '587'))
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            raise PushError('SMTP_PORT无效') from None
        return cls(values['SMTP_HOST'], port, mode, values['SMTP_USERNAME'], values['SMTP_PASSWORD'], values['SMTP_FROM'], values['SMTP_TO'])


class BackupEmail:
    def __init__(self, config, connector=None, clock=utc_now, retry_unknown_after=None):
        self.config = config
        self.connector = connector
        self.clock = clock
        self.retry_unknown_after = retry_unknown_after

    def connect(self):
        c = self.config
        if self.connector is not None:
            return self.connector()
        context = ssl.create_default_context()
        if c.mode == 'ssl':
            return smtplib.SMTP_SSL(c.host, c.port, timeout=20, context=context)
        client = smtplib.SMTP(c.host, c.port, timeout=20)
        try:
            client.ehlo()
            client.starttls(context=context)
            client.ehlo()
            return client
        except Exception:
            client.close()
            raise

    def publish(self, key, title, content, expires_at, store):
        now = self.clock()
        if expires_at <= now:
            raise PushError('邮件已过有效窗口，未发送')
        email_key = hashlib.sha256(('backup-email-v1|' + key).encode()).hexdigest()
        # Same logical report + same content is stable across retries, but this
        # header alone is not an SMTP exactly-once guarantee.
        message = EmailMessage()
        message['Subject'] = title
        message['From'] = self.config.sender
        message['To'] = self.config.recipient
        message['Date'] = format_datetime(datetime.now(timezone.utc))
        message['Message-ID'] = '<' + email_key + '@' + self.config.sender.split('@')[1] + '>'
        message.set_content('本邮件包含HTML格式的纳指日报，请在支持HTML的邮箱客户端查看。')
        message.add_alternative(content, subtype='html')
        with store:
            state = store.load()
            entry = state['entries'].get(email_key)
            if entry:
                if not isinstance(entry, dict) or entry.get('status') not in ('preparing', 'unknown', 'failed', 'smtp_accepted'):
                    raise StateError('备用邮件状态无效，停止自动发送')
                if entry['status'] == 'smtp_accepted':
                    return Receipt('smtp_accepted', duplicate=True)
                if entry['status'] in ('preparing', 'unknown'):
                    # Production opts into delayed recovery; Message-ID is not an
                    # exactly-once SMTP guarantee when DATA's response was lost.
                    try:
                        age = now - datetime.fromisoformat(entry['created_at'])
                    except (KeyError, TypeError, ValueError):
                        age = None
                    if self.retry_unknown_after is None or age is None or age < self.retry_unknown_after:
                        raise DeliveryUnknown('上次SMTP结果未知，等待核对或延时恢复')
            attempts = entry.get('attempts', 0) if entry else 0
            if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 0:
                raise StateError('备用邮件尝试计数无效')
            if entry and not retry_due(entry, now):
                return Receipt('retry_pending', duplicate=True)
            entry = dict(status='preparing', attempts=attempts+1,
                         created_at=now.isoformat(),
                         content_sha256=hashlib.sha256(content.encode()).hexdigest())
            schedule_retry(entry, now)
            state['entries'][email_key] = entry
            if len(state['entries']) > 120:
                state['entries'] = dict(list(state['entries'].items())[-120:])
            store.save(state)  # Must succeed before authentication or SMTP DATA.
            client = None
            sending = False
            try:
                client = self.connect()
                client.login(self.config.username, self.config.password)
                sending = True
                refused = client.send_message(message)
                if refused:
                    raise smtplib.SMTPRecipientsRefused(refused)
            except (smtplib.SMTPException, OSError) as exc:
                # A server response explicitly rejecting DATA/recipient/auth is
                # retryable; disconnect/timeout during DATA may already be accepted.
                definite = not sending or isinstance(exc, (smtplib.SMTPResponseException, smtplib.SMTPRecipientsRefused))
                entry['status'] = 'failed' if definite else 'unknown'
                store.save(state)
                if not definite:
                    raise DeliveryUnknown('SMTP投递结果未知，未自动重发；请核对服务记录') from None
                raise PushError('SMTP明确拒绝或连接认证失败；请检查配置／邮箱服务，不输出原始响应') from None
            else:
                entry['status'] = 'smtp_accepted'
                store.save(state)
                return Receipt('smtp_accepted')
            finally:
                if client is not None:
                    try:
                        client.quit()
                    except (smtplib.SMTPException, OSError):
                        client.close()
