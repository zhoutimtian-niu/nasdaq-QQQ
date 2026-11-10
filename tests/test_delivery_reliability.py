from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import smtplib
import tempfile
import unittest
from unittest.mock import Mock, patch

from backup_email import BackupEmail, EmailConfig
import delivery
import main
import preview_email
from notification_state import FileState, GitHubState, StateError, state_store
from notify import DeliveryUnknown, PushError, PushPlus, Receipt
from test_engineering import fixture, response


def config():
    return EmailConfig('smtp.example.com', 465, 'ssl', 'sender@example.com',
                       'test-private-password', 'sender@example.com', 'reader@example.com')


class EmailTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = FileState(Path(self.tmp.name)/'state.json')
        self.smtp = Mock()
        self.smtp.send_message.return_value = {}
        self.connector = Mock(return_value=self.smtp)
        self.client = BackupEmail(config(), connector=self.connector)
        self.expiry = datetime.now(timezone.utc)+timedelta(days=1)
        self.key = 'original-wechat-key'
        self.html = '<div style="color:#142840">完整日报，不改格式</div>'

    def send(self):
        return self.client.publish(self.key, '日报标题', self.html, self.expiry, self.store)

    def test_original_html_and_two_mime_parts_preserved(self):
        result = self.send()
        message = self.smtp.send_message.call_args.args[0]
        self.assertEqual(message.get_body(preferencelist=('html',)).get_content().rstrip('\n'), self.html)
        self.assertTrue(message.is_multipart())
        self.assertEqual(message['To'], 'reader@example.com')
        self.assertEqual(result.status, 'smtp_accepted')
        self.assertNotEqual(result.status, 'delivered')
        self.assertNotIn('test-private-password', self.store.path.read_text())

    def test_repeat_suppresses_only_email_preserves_old_wechat_state(self):
        self.store.save({'version':1,'entries':{self.key:{'status':'accepted_unverified','short_code':'existing'}}})
        self.send()
        self.assertTrue(self.send().duplicate)
        self.assertEqual(self.smtp.send_message.call_count,1)
        self.assertEqual(self.store.load()['entries'][self.key]['short_code'],'existing')

    def test_send_timeout_is_unknown_and_never_blindly_retried(self):
        self.smtp.send_message.side_effect = TimeoutError('test-private-password')
        for _ in range(2):
            with self.assertRaises(DeliveryUnknown):self.send()
        self.assertEqual(self.smtp.send_message.call_count,1)
        self.assertNotIn('test-private-password',self.store.path.read_text())

    def test_explicit_rejection_retries_at_most_three_times(self):
        self.smtp.send_message.side_effect = smtplib.SMTPDataError(550,b'private server response')
        for _ in range(4):
            with self.assertRaises(PushError):self.send()
        self.assertEqual(self.smtp.send_message.call_count,3)

    def test_auth_failure_safe_and_no_data_sent(self):
        self.smtp.login.side_effect = smtplib.SMTPAuthenticationError(535,b'test-private-password')
        with self.assertRaises(PushError) as e:self.send()
        self.assertNotIn('test-private-password',str(e.exception))
        self.smtp.send_message.assert_not_called()

    def test_missing_durable_state_prevents_all_smtp_connections(self):
        with patch.object(self.store,'save',side_effect=StateError('write denied')):
            with self.assertRaises(StateError):self.send()
        self.connector.assert_not_called()

    def test_quit_failure_after_acceptance_does_not_repeat(self):
        self.smtp.quit.side_effect = smtplib.SMTPServerDisconnected('quit dropped')
        self.assertEqual(self.send().status,'smtp_accepted')
        self.assertTrue(self.send().duplicate)
        self.assertEqual(self.smtp.send_message.call_count,1)

    def test_expired_trade_email_is_not_sent(self):
        self.expiry = datetime.now(timezone.utc)-timedelta(seconds=1)
        with self.assertRaises(PushError):self.send()
        self.connector.assert_not_called()

    def test_crash_after_preparing_blocks_resend(self):
        key=hashlib.sha256(('backup-email-v1|'+self.key).encode()).hexdigest()
        self.store.save({'version':1,'entries':{key:{'status':'preparing','attempts':1}}})
        with self.assertRaises(DeliveryUnknown):self.send()
        self.connector.assert_not_called()

    def test_disabled_email_reads_no_smtp_credentials(self):
        with patch.dict('os.environ',{'BACKUP_EMAIL_ENABLED':'false','SMTP_PASSWORD':'never-used'},clear=True):
            self.assertIsNone(EmailConfig.from_environment())

    def test_incomplete_enabled_configuration_is_explicit(self):
        with patch.dict('os.environ',{'BACKUP_EMAIL_ENABLED':'true'},clear=True):
            with self.assertRaises(PushError):EmailConfig.from_environment()

    def test_mail_header_injection_and_cleartext_auth_rejected(self):
        values=dict(BACKUP_EMAIL_ENABLED='true',SMTP_HOST='smtp.example.com',SMTP_USERNAME='s',SMTP_PASSWORD='p',SMTP_FROM='sender@example.com',SMTP_TO='reader@example.com\nBcc: x@example.com')
        with patch.dict('os.environ',values,clear=True):
            with self.assertRaises(PushError):EmailConfig.from_environment()
        values.update(SMTP_TO='reader@example.com',SMTP_SECURITY='plain')
        with patch.dict('os.environ',values,clear=True):
            with self.assertRaises(PushError):EmailConfig.from_environment()

    def test_empty_optional_workflow_values_use_tls_defaults(self):
        values=dict(BACKUP_EMAIL_ENABLED='true',SMTP_HOST='smtp.example.com',SMTP_USERNAME='s',SMTP_PASSWORD='p',SMTP_FROM='sender@example.com',SMTP_TO='reader@example.com',SMTP_SECURITY='',SMTP_PORT='')
        with patch.dict('os.environ',values,clear=True):
            c=EmailConfig.from_environment()
        self.assertEqual((c.port,c.mode),(465,'ssl'))


class RouterTests(unittest.TestCase):
    def send(self,primary,backup):
        return delivery.publish('key','title','html',datetime.now(timezone.utc)+timedelta(days=1),'unused',primary=primary,backup=backup,store=Mock())

    def test_primary_unknown_does_not_block_independent_email(self):
        primary=Mock();primary.publish.side_effect=DeliveryUnknown('unknown')
        backup=Mock();backup.publish.return_value=Receipt('smtp_accepted')
        r=self.send(primary,backup)
        self.assertEqual(r.status,'smtp_accepted')
        self.assertEqual(r.channels['wechat']['error_type'],'DeliveryUnknown')
        backup.publish.assert_called_once()

    def test_backup_failure_does_not_hide_confirmed_wechat(self):
        primary=Mock();primary.publish.return_value=Receipt('delivered')
        backup=Mock();backup.publish.side_effect=PushError('mail rejected')
        r=self.send(primary,backup)
        self.assertEqual(r.status,'delivered')
        self.assertEqual(r.channels['email']['status'],'error')

    def test_unverified_wechat_stays_unverified_when_email_disabled(self):
        primary=Mock();primary.publish.return_value=Receipt('accepted_unverified')
        with patch.dict('os.environ',{},clear=True):
            r=delivery.publish('k','t','h',datetime.now(timezone.utc)+timedelta(days=1),'unused',primary=primary,store=Mock())
        self.assertEqual(r.status,'accepted_unverified')

    def test_missing_push_token_cannot_disable_configured_email(self):
        backup=Mock();backup.publish.return_value=Receipt('smtp_accepted')
        with patch.dict('os.environ',{},clear=True):
            r=delivery.publish('k','t','h',datetime.now(timezone.utc)+timedelta(days=1),'unused',backup=backup,store=Mock())
        self.assertEqual(r.status,'smtp_accepted')

    def test_both_failed_is_never_success(self):
        primary=Mock();primary.publish.side_effect=PushError('primary fail')
        backup=Mock();backup.publish.side_effect=PushError('backup fail')
        with self.assertRaises(PushError):self.send(primary,backup)

    def test_old_delivered_wechat_dedup_does_not_suppress_new_email(self):
        with tempfile.TemporaryDirectory() as folder:
            store=FileState(Path(folder)/'state.json')
            store.save({'version':1,'entries':{'key':{'status':'delivered'}}})
            session=Mock()
            primary=PushPlus('test-token',session=session)
            smtp=Mock();smtp.send_message.return_value={}
            backup=BackupEmail(config(),connector=lambda:smtp)
            r=delivery.publish('key','t','h',datetime.now(timezone.utc)+timedelta(days=1),folder,primary=primary,backup=backup,store=store)
            self.assertEqual(r.status,'delivered')
            session.post.assert_not_called()
            smtp.send_message.assert_called_once()


class MainReliabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _,cls.market=fixture()
        cls.now=datetime(2026,10,22,11,tzinfo=timezone.utc)

    def test_unverified_provider_acceptance_is_nonzero(self):
        with tempfile.TemporaryDirectory() as folder:
            code=main.run(False,folder,self.now,loader=lambda _:self.market,publisher=Mock(return_value=Receipt('accepted_unverified')))
            d=json.loads((Path(folder)/'run.json').read_text(encoding='utf-8'))
        self.assertEqual(code,2)
        self.assertEqual(d['status'],'delivery_unverified')

    def test_smtp_accepted_is_explicit_with_per_channel_diagnostics(self):
        receipt=Receipt('smtp_accepted',channels={'wechat':{'status':'accepted_unverified'},'email':{'status':'smtp_accepted'}})
        with tempfile.TemporaryDirectory() as folder:
            code=main.run(False,folder,self.now,loader=lambda _:self.market,publisher=Mock(return_value=receipt))
            d=json.loads((Path(folder)/'run.json').read_text(encoding='utf-8'))
        self.assertEqual(code,0)
        self.assertEqual(d['status'],'completed_backup_smtp_accepted')
        self.assertEqual(d['notification_channels']['wechat']['status'],'accepted_unverified')

    def test_postclose_emits_only_operational_notice_and_stays_failed(self):
        loader=Mock();trade=Mock();health=Mock(return_value=Receipt('smtp_accepted'))
        with tempfile.TemporaryDirectory() as folder:
            code=main.run(False,folder,datetime(2026,11,9,21,24,tzinfo=timezone.utc),loader=loader,publisher=trade,health_publisher=health)
            content=(Path(folder)/'health-notice.html').read_text(encoding='utf-8')
        self.assertEqual(code,1)
        loader.assert_not_called();trade.assert_not_called()
        self.assertIn('没有生成或发送买卖目标',content)
        self.assertNotIn('TQQQ',content)
        self.assertEqual(health.call_args.kwargs['category'],'window-health')

    def test_dry_run_does_not_read_mail_config_or_send_either_route(self):
        with tempfile.TemporaryDirectory() as folder,patch('delivery.EmailConfig.from_environment',side_effect=AssertionError('must not read credentials')),patch('main.publish_report',side_effect=AssertionError('must not send')):
            self.assertEqual(main.run(True,folder,self.now,loader=lambda _:self.market),0)

    def test_external_runner_can_share_github_ledger(self):
        with patch.dict('os.environ',dict(NOTIFICATION_STATE_BACKEND='github',GITHUB_REPOSITORY='owner/repo',GITHUB_TOKEN='fake'),clear=True):
            self.assertIsInstance(state_store('unused'),GitHubState)


class PreviewEmailTests(unittest.TestCase):
    def generator(self,dry_run,output_dir):
        self.assertTrue(dry_run)
        p=Path(output_dir)
        (p/'run.json').write_text(json.dumps(dict(status='dry_run_validated',signal_date='2026-11-09',execution_date='2026-11-10')),encoding='utf-8')
        (p/'report.html').write_text("<html><body><div style='font-size:10px;letter-spacing:2px;color:blue'>日报样式</div></body></html>",encoding='utf-8')
        return 0

    def test_explicit_email_preview_is_labeled_and_never_posts_wechat(self):
        client=Mock();client.publish.return_value=Receipt('smtp_accepted')
        with tempfile.TemporaryDirectory() as folder,patch.dict('os.environ',{'GITHUB_RUN_ID':'test-run'},clear=True),patch('requests.Session.post',side_effect=AssertionError('no WeChat in email preview')):
            self.assertEqual(preview_email.run(folder,self.generator,client,Mock()),0)
        args=client.publish.call_args.args
        self.assertIn('样式预览',args[1])
        self.assertIn('样式预览',args[2])

    def test_failed_report_generation_prevents_email(self):
        client=Mock()
        self.assertEqual(preview_email.run('unused',lambda **_:1,client,Mock()),1)
        client.publish.assert_not_called()

    def test_no_explicit_run_id_prevents_email(self):
        client=Mock()
        with tempfile.TemporaryDirectory() as folder,patch.dict('os.environ',{},clear=True):
            with self.assertRaises(ValueError):preview_email.run(folder,self.generator,client,Mock())
        client.publish.assert_not_called()


if __name__=='__main__':unittest.main()
