from datetime import datetime, timedelta, timezone, date
import json
from pathlib import Path
import smtplib
import tempfile
import unittest
from unittest.mock import Mock, patch

import delivery
import main
import recover
from backup_email import BackupEmail
from market_calendar import recovery_session_plan
from market_data import DataError
from notification_state import FileState, StateError
from notify import PushPlus, PushError, Receipt
from test_delivery_reliability import config
from test_engineering import fixture, response

UTC = timezone.utc


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.output=Path(self.tmp.name)
        self.store=FileState(self.output/'state.json')
        self.now=datetime(2026,10,22,11,tzinfo=UTC)
        self.tick=0.

    def sleep(self,seconds):
        self.tick+=seconds;self.now+=timedelta(seconds=seconds)

    def run_recovery(self,**kwargs):
        return recover.run(self.output,clock=lambda:self.now,monotonic=lambda:self.tick,
                           sleep=self.sleep,store_factory=lambda _:self.store,**kwargs)

    def test_bad_data_then_good_only_sends_one_normal_report(self):
        _,market=fixture();loader=Mock(side_effect=[DataError('bad tail'),market])
        publisher=Mock(return_value=Receipt('delivered'))
        def runner(*a,**k):return main.run(*a,**k,publisher=publisher)
        self.assertEqual(self.run_recovery(runner=runner,loader=loader),0)
        self.assertEqual(loader.call_count,2);publisher.assert_called_once()
        self.assertEqual(len(publisher.call_args.args),4)
        self.assertNotIn('数据异常',publisher.call_args.args[1])
        self.assertEqual(json.loads((self.output/'run.json').read_text(encoding='utf-8'))['recovery_attempts'],2)

    def test_receipt_wait_reuses_valid_data_then_normal_email_once(self):
        _,market=fixture();loader=Mock(return_value=market)
        session=Mock();session.post.return_value=response()
        primary=PushPlus('test-token',session=session,clock=lambda:self.now)
        smtp=Mock();smtp.send_message.return_value={}
        backup=BackupEmail(config(),connector=lambda:smtp,clock=lambda:self.now)
        def publisher(plan,title,content,output):
            return delivery.publish(main.notification_key(plan),title,content,plan.market_close,output,
                                    primary=primary,backup=backup,store=self.store,clock=lambda:self.now)
        def runner(*a,**k):return main.run(*a,**k,publisher=publisher)
        self.assertEqual(self.run_recovery(runner=runner,loader=loader),0)
        self.assertGreaterEqual(self.tick,600)
        self.assertEqual(loader.call_count,1)
        self.assertEqual(session.post.call_count,1);self.assertEqual(smtp.send_message.call_count,1)
        self.assertNotIn('失败',str(smtp.send_message.call_args.args[0]['Subject']))
        # New scheduled process reads durable acceptance before even downloading.
        bad=Mock(side_effect=AssertionError('no regenerated report after completion'))
        self.assertEqual(self.run_recovery(runner=bad),0);bad.assert_not_called()

    def test_permanent_bad_data_exits_budget_without_external_notice(self):
        loader=Mock(side_effect=DataError('bad quote'));publisher=Mock()
        def runner(*a,**k):return main.run(*a,**k,publisher=publisher)
        self.assertEqual(self.run_recovery(runner=runner,loader=loader,budget_seconds=80),1)
        self.assertGreater(loader.call_count,1);publisher.assert_not_called()
        summary=json.loads((self.output/'run.json').read_text(encoding='utf-8'))
        self.assertTrue(summary['retryable']);self.assertEqual(summary['failure_notice'],'suppressed')

    def test_expiry_stops_pinned_report_without_roll_or_send(self):
        self.now=datetime(2026,10,22,19,59,50,tzinfo=UTC)
        publisher=Mock()
        def runner(*a,**k):
            self.sleep(15)
            k['loader']=Mock(side_effect=DataError('late data'))
            return main.run(*a,**k,publisher=publisher)
        self.assertEqual(self.run_recovery(runner=runner),1)
        publisher.assert_not_called()
        summary=json.loads((self.output/'run.json').read_text(encoding='utf-8'))
        self.assertEqual(summary['execution_date'],'2026-10-22')

    def test_dry_run_never_touches_remote_state_or_credentials(self):
        _,market=fixture();bad=Mock(side_effect=AssertionError('remote state forbidden'))
        self.assertEqual(recover.run(self.output,dry_run=True,clock=lambda:self.now,
                                    loader=lambda _:market,store_factory=bad),0)
        bad.assert_not_called()

    def test_recovery_dates_weekend_holiday_earlyclose_and_grace(self):
        friday=recovery_session_plan(datetime(2026,11,13,21,25,tzinfo=UTC))
        self.assertEqual((friday.signal_date,friday.execution_date),(date(2026,11,13),date(2026,11,16)))
        weekend=recovery_session_plan(datetime(2026,11,15,12,tzinfo=UTC))
        self.assertEqual(weekend.execution_date,date(2026,11,16))
        self.assertIsNone(recovery_session_plan(datetime(2026,11,27,18,10,tzinfo=UTC)))
        early=recovery_session_plan(datetime(2026,11,27,18,25,tzinfo=UTC))
        self.assertEqual(early.execution_date,date(2026,11,30))
        live=recovery_session_plan(datetime(2026,11,16,16,tzinfo=UTC))
        self.assertEqual(live.signal_date,date(2026,11,13));self.assertTrue(live.late)


class RetryStateTests(unittest.TestCase):
    def test_smtp_lost_data_response_waits_30min_then_uses_same_message_id(self):
        with tempfile.TemporaryDirectory() as folder:
            store=FileState(Path(folder)/'state.json');now=[datetime.now(UTC)]
            smtp=Mock();smtp.send_message.side_effect=[TimeoutError('private'),{}]
            client=BackupEmail(config(),connector=lambda:smtp,clock=lambda:now[0],retry_unknown_after=timedelta(minutes=30))
            from notify import DeliveryUnknown
            expiry=now[0]+timedelta(days=1)
            with self.assertRaises(DeliveryUnknown):client.publish('k','t','normal html',expiry,store)
            first=smtp.send_message.call_args.args[0]['Message-ID']
            with self.assertRaises(DeliveryUnknown):client.publish('k','t','normal html',expiry,store)
            self.assertEqual(smtp.send_message.call_count,1)
            now[0]+=timedelta(minutes=30)
            self.assertEqual(client.publish('k','t','normal html',expiry,store).status,'smtp_accepted')
            self.assertEqual(first,smtp.send_message.call_args.args[0]['Message-ID'])
            self.assertTrue(client.publish('k','t','normal html',expiry,store).duplicate)

    def test_github_ledger_failure_prevents_both_routes(self):
        with tempfile.TemporaryDirectory() as folder:
            store=FileState(Path(folder)/'state.json');primary=Mock();backup=Mock()
            with patch.object(store,'save',side_effect=StateError('write denied')):
                with self.assertRaises(StateError):delivery.publish('k','t','h',datetime.now(UTC)+timedelta(days=1),folder,primary=primary,backup=backup,store=store)
            primary.publish.assert_not_called();backup.publish.assert_not_called()

    def test_confirmed_async_primary_failure_falls_back_without_10min_wait(self):
        with tempfile.TemporaryDirectory() as folder:
            store=FileState(Path(folder)/'state.json')
            store.save({'version':1,'entries':{'k':{'status':'accepted_pending','short_code':'old','attempts':1}}})
            session=Mock();session.get.return_value=response(body={'code':200,'data':{'status':3}})
            session.post.return_value=response()
            client=PushPlus('token','access',session=session,sleep=lambda _:None)
            backup=Mock();backup.publish.return_value=Receipt('smtp_accepted')
            result=delivery.publish('k','t','html',datetime.now(UTC)+timedelta(days=1),folder,primary=client,backup=backup,store=store)
            self.assertEqual(result.status,'smtp_accepted')
            session.post.assert_not_called();backup.publish.assert_called_once()


if __name__=='__main__':unittest.main()
