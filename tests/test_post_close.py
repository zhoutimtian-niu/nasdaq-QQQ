from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import yaml
import main
import delivery
from backup_email import BackupEmail
from market_calendar import OutsideExecutionWindow, expired_execution_plan, session_plan
from notification_state import FileState, StateError
from notify import PushPlus, Receipt
from test_engineering import fixture, response
from test_delivery_reliability import config

UTC=timezone.utc
NY=ZoneInfo('America/New_York')


class PostCloseCalendarTests(unittest.TestCase):
    def test_summer_close_to_next_open(self):
        p=session_plan(datetime(2026,10,21,20,25,tzinfo=UTC),after_close=True)
        self.assertEqual((p.signal_date,p.execution_date),(date(2026,10,21),date(2026,10,22)))
        self.assertEqual((p.market_open.hour,p.market_open.minute),(13,30))
        self.assertFalse(p.late)

    def test_winter_close_to_next_open(self):
        p=session_plan(datetime(2026,11,12,21,25,tzinfo=UTC),after_close=True)
        self.assertEqual((p.signal_date,p.execution_date),(date(2026,11,12),date(2026,11,13)))
        self.assertEqual((p.market_open.hour,p.market_open.minute),(14,30))

    def test_unclosed_or_not_yet_settled_bar_is_never_used(self):
        for hour,minute in ((19,59),(20,0),(20,19)):
            self.assertIsNone(session_plan(datetime(2026,10,21,hour,minute,tzinfo=UTC),after_close=True))
        self.assertIsNotNone(session_plan(datetime(2026,10,21,20,20,tzinfo=UTC),after_close=True))

    def test_dry_run_does_not_override_close_grace(self):
        self.assertIsNone(session_plan(datetime(2026,10,21,20,10,tzinfo=UTC),preview=True,after_close=True))

    def test_friday_targets_monday(self):
        p=session_plan(datetime(2026,11,13,21,25,tzinfo=UTC),after_close=True)
        self.assertEqual((p.signal_date,p.execution_date),(date(2026,11,13),date(2026,11,16)))

    def test_holiday_weekend_targets_next_actual_session(self):
        p=session_plan(datetime(2026,5,22,20,25,tzinfo=UTC),after_close=True)
        self.assertEqual((p.signal_date,p.execution_date),(date(2026,5,22),date(2026,5,26)))

    def test_delay_across_ny_midnight_preserves_last_closed_session(self):
        p=session_plan(datetime(2026,11,14,6,25,tzinfo=UTC),after_close=True)
        self.assertEqual((p.signal_date,p.execution_date),(date(2026,11,13),date(2026,11,16)))

    def test_holiday_delay_cannot_create_holiday_price_or_instruction(self):
        p=session_plan(datetime(2026,5,25,20,25,tzinfo=UTC),after_close=True)
        self.assertEqual((p.signal_date,p.execution_date),(date(2026,5,22),date(2026,5,26)))

    def test_early_close_uses_exchange_close_not_fixed_16h(self):
        self.assertIsNone(session_plan(datetime(2026,11,27,18,19,tzinfo=UTC),after_close=True))
        p=session_plan(datetime(2026,11,27,18,25,tzinfo=UTC),after_close=True)
        self.assertEqual((p.signal_date,p.execution_date),(date(2026,11,27),date(2026,11,30)))

    def test_after_close_mode_stops_at_next_open(self):
        self.assertIsNotNone(session_plan(datetime(2026,11,16,14,29,tzinfo=UTC),after_close=True))
        self.assertIsNone(session_plan(datetime(2026,11,16,14,30,tzinfo=UTC),after_close=True))
        self.assertIsNone(session_plan(datetime(2026,11,16,18,0,tzinfo=UTC),after_close=True))

    def test_old_preopen_mode_still_rejects_expired_task(self):
        with self.assertRaises(OutsideExecutionWindow):
            session_plan(datetime(2026,11,13,21,25,tzinfo=UTC))

    def test_dst_change_between_signal_and_execution(self):
        spring=session_plan(datetime(2026,3,6,21,25,tzinfo=UTC),after_close=True)
        autumn=session_plan(datetime(2026,10,30,20,25,tzinfo=UTC),after_close=True)
        self.assertEqual((spring.execution_date,spring.market_open.hour),(date(2026,3,9),13))
        self.assertEqual((autumn.execution_date,autumn.market_open.hour),(date(2026,11,2),14))

    def test_year_boundary_observes_new_year_holiday(self):
        p=session_plan(datetime(2026,12,31,21,25,tzinfo=UTC),after_close=True)
        self.assertEqual((p.signal_date,p.execution_date),(date(2026,12,31),date(2027,1,4)))

    def test_calendar_advance_to_next_execution_is_explicit_mode_only(self):
        now=datetime(2026,11,13,21,25,tzinfo=UTC)
        expired=expired_execution_plan(now)
        early=session_plan(now,after_close=True)
        self.assertEqual(expired.execution_date,date(2026,11,13))
        self.assertEqual(early.execution_date,date(2026,11,16))
        self.assertNotEqual(main.notification_key(expired),main.notification_key(early))


class PostCloseIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _,cls.market=fixture()
        cls.early=datetime(2026,10,21,20,25,tzinfo=UTC)
        cls.preopen=datetime(2026,10,22,11,3,tzinfo=UTC)

    def test_early_and_preopen_generate_identical_date_and_target(self):
        results=[]
        for now,early in ((self.early,True),(self.preopen,False)):
            with tempfile.TemporaryDirectory() as folder:
                publisher=Mock(side_effect=AssertionError('dry run must not send'))
                code=main.run(True,folder,now,loader=lambda _:self.market,publisher=publisher,after_close=early)
                self.assertEqual(code,0)
                s=json.loads((Path(folder)/'run.json').read_text(encoding='utf-8'))
                html=(Path(folder)/'report.html').read_text(encoding='utf-8')
                self.assertIn('计划执行目标',html)
                self.assertNotIn('今日模型目标',html)
                self.assertIn('2026-10-22执行',html)
                results.append((s['signal_date'],s['execution_date'],s['target']))
                publisher.assert_not_called()
        self.assertEqual(results[0],results[1])

    def test_same_dates_keep_original_wechat_key(self):
        early=session_plan(self.early,after_close=True);pre=session_plan(self.preopen)
        original=hashlib.sha256(f'{main.MODEL_VERSION}|2026-10-21|2026-10-22|wechat|daily'.encode()).hexdigest()
        self.assertEqual(main.notification_key(early),original)
        self.assertEqual(main.notification_key(pre),original)

    def test_unverified_wechat_waits_then_fallback_posts_once(self):
        early=session_plan(self.early,after_close=True);pre=session_plan(self.preopen)
        session=Mock();session.post.return_value=response()
        primary=PushPlus('test-token',session=session)
        smtp=Mock();smtp.send_message.return_value={}
        backup=BackupEmail(config(),connector=lambda:smtp)
        expiry=datetime.now(UTC)+timedelta(days=1)
        with tempfile.TemporaryDirectory() as folder:
            store=FileState(Path(folder)/'state.json')
            now=[datetime.now(UTC)]
            for i,plan in enumerate((early,pre,pre)):
                now[0]+=timedelta(minutes=11 if i else 0)
                delivery.publish(main.notification_key(plan),'title','html',expiry,folder,primary=primary,backup=backup,store=store,clock=lambda:now[0])
        self.assertEqual(session.post.call_count,1)
        self.assertEqual(smtp.send_message.call_count,1)

    def test_before_close_skips_before_loading_or_publishing(self):
        loader=Mock();publisher=Mock()
        with tempfile.TemporaryDirectory() as folder:
            code=main.run(False,folder,datetime(2026,10,21,19,0,tzinfo=UTC),loader=loader,publisher=publisher,after_close=True)
        self.assertEqual(code,0)
        loader.assert_not_called();publisher.assert_not_called()

    def test_early_missing_data_cannot_issue_a_target(self):
        from market_data import DataError
        publisher=Mock(return_value=Receipt('smtp_accepted'))
        with tempfile.TemporaryDirectory() as folder:
            code=main.run(False,folder,self.early,loader=Mock(side_effect=DataError('missing close')),publisher=publisher,after_close=True)
        self.assertEqual(code,1)
        publisher.assert_not_called()

    def test_overdue_fallback_does_not_send_false_failure_after_email_submission(self):
        now=datetime(2026,11,13,21,25,tzinfo=UTC);plan=expired_execution_plan(now)
        key=hashlib.sha256(('backup-email-v1|'+main.notification_key(plan)).encode()).hexdigest()
        with tempfile.TemporaryDirectory() as folder:
            store=FileState(Path(folder)/'state.json')
            state={'version':1,'entries':{key:{'status':'smtp_accepted'}}};store.save(state)
            loader=Mock();publisher=Mock();health=Mock()
            with patch('main.state_store',return_value=store):
                code=main.run(False,folder,now,loader=loader,publisher=publisher,health_publisher=health)
            self.assertEqual(code,0)
            self.assertEqual(store.load(),state)
            summary=json.loads((Path(folder)/'run.json').read_text(encoding='utf-8'))
            self.assertEqual(summary['status'],'skipped_already_submitted')
            self.assertNotIn('error_type',summary)
            self.assertNotIn('error',summary)
        loader.assert_not_called();publisher.assert_not_called();health.assert_not_called()

    def test_unknown_primary_receipt_is_not_falsely_claimed_as_submission(self):
        with tempfile.TemporaryDirectory() as folder:
            store=FileState(Path(folder)/'state.json')
            store.save({'version':1,'entries':{'key':{'status':'accepted_unverified'}}})
            self.assertFalse(delivery.has_submission('key',store))

    def test_failed_state_lookup_keeps_overdue_failure_visible(self):
        health=Mock(return_value=Receipt('smtp_accepted'))
        with tempfile.TemporaryDirectory() as folder,patch('main.state_store',side_effect=StateError('cannot read')):
            code=main.run(False,folder,datetime(2026,11,13,21,25,tzinfo=UTC),publisher=Mock(),health_publisher=health)
        self.assertEqual(code,1)
        health.assert_not_called()

    def test_cli_explicitly_selects_after_close_with_safe_dry_run(self):
        with patch('main.run',return_value=0) as run,patch('main.logging.FileHandler'),patch('main.logging.basicConfig'),tempfile.TemporaryDirectory() as folder:
            with patch('sys.argv',['main.py','--after-close','--dry-run','--output-dir',folder]):
                self.assertEqual(main.main(),0)
        self.assertTrue(run.call_args.kwargs['after_close'])
        self.assertTrue(run.call_args.kwargs['dry_run'])


class PostCloseWorkflowTests(unittest.TestCase):
    def test_schedules_preserve_fallback_and_use_ny_timezone(self):
        root=Path(__file__).resolve().parents[1]
        w=yaml.load((root/'.github/workflows/daily_run.yml').read_text(encoding='utf-8'),Loader=yaml.BaseLoader)
        schedules=w['on']['schedule']
        self.assertEqual(schedules[0]['cron'],'03,25,47 11 * * 1-5')
        self.assertEqual({x['cron'] for x in schedules[1:3]},{'25,55 16 * * 1-5','25,55 13 * * 1-5'})
        self.assertTrue(all(x['timezone']=='America/New_York' for x in schedules[1:3]))
        self.assertEqual(w['on']['workflow_dispatch']['inputs']['after_close']['default'],'false')
        expression=next(s for s in w['jobs']['build']['steps'] if s['name']=='Run Strategy')['env']['AFTER_CLOSE']
        self.assertIn('inputs.after_close',expression)
        self.assertIn('25,55 16',expression)
        self.assertIn('25,55 13',expression)

    def test_ny_schedule_to_beijing_dst_conversion(self):
        bj=ZoneInfo('Asia/Shanghai')
        summer=datetime(2026,10,21,16,25,tzinfo=NY).astimezone(bj)
        winter=datetime(2026,11,12,16,25,tzinfo=NY).astimezone(bj)
        self.assertEqual((summer.day,summer.hour,summer.minute),(22,4,25))
        self.assertEqual((winter.day,winter.hour,winter.minute),(13,5,25))


if __name__=='__main__':unittest.main()
