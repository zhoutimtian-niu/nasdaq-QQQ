import base64
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np
import pandas as pd
import requests

import main
from backtest import backtest_returns
from check_state import check_state
from market_calendar import OutsideExecutionWindow, session_plan
from market_data import DataError, MarketData, SYMBOLS, download_symbol, parse_chart, validate_frames
from notification_state import FileState, GitHubState, StateError
from notify import DeliveryUnknown, PushError, PushPlus, Receipt
from report import build_report
from strategy import ETF_MAP, calculate_signals, next_state

FIXTURE = Path(__file__).parent / "fixtures" / "legacy_market.csv.gz"


def fixture():
    f = pd.read_csv(FIXTURE, index_col=0, parse_dates=True)
    c = pd.DataFrame({s: f[s + "_close"] for s in SYMBOLS})
    o = pd.DataFrame({s: f[s + "_open"] for s in SYMBOLS})
    return f, MarketData(c, o, c.index[-1].date())


def response(status=200, body=None):
    r = Mock(status_code=status)
    r.json.return_value = body if body is not None else {"code": 200, "data": "test-short-code"}
    return r


class StrategyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.f, cls.market = fixture()

    def test_all_4199_signals_equal_frozen_original_source(self):
        actual = calculate_signals(self.market.close)["signal"]
        self.assertEqual(len(actual), 4199)
        np.testing.assert_array_equal(actual, self.f.expected_signal)

    def test_prefix_invariance(self):
        whole = calculate_signals(self.market.close)
        for n in (220, 1000, 3000):
            pd.testing.assert_frame_equal(whole.iloc[:n], calculate_signals(self.market.close.iloc[:n]))

    def test_future_perturbation_cannot_change_past(self):
        changed = self.market.close.copy()
        changed.iloc[-100:] *= 1.5
        pd.testing.assert_frame_equal(calculate_signals(changed).iloc[:-100], calculate_signals(self.market.close).iloc[:-100])

    def test_rsi_hysteresis_and_strict_boundaries(self):
        self.assertEqual(next_state(3, 110, 100, 70, 20), 3)
        self.assertEqual(next_state(2, 110, 100, 65, 20), 2)
        self.assertEqual(next_state(3, 110, 100, 80, 20), 3)
        self.assertEqual(next_state(2, 110, 100, 64.9, 20), 3)
        self.assertEqual(next_state(3, 110, 100, 80.1, 20), 2)

    def test_bear_recovery_is_ql_d_before_three_times(self):
        self.assertEqual(next_state(1, 101, 100, 20, 20), 2)
        self.assertEqual(next_state(1, 100, 100, 20, 20), 1)

    def test_vix_and_bear_override(self):
        self.assertEqual(next_state(3, 110, 100, 20, 40.1), 1)
        self.assertEqual(next_state(3, 98.9, 100, 20, 20), 1)

    def test_missing_vix_fails(self):
        bad = self.market.close.copy()
        bad.loc[bad.index[-1], "^VIX"] = np.nan
        with self.assertRaises(ValueError):
            calculate_signals(bad)


class CalendarTests(unittest.TestCase):
    def test_summer_and_winter_open_utc(self):
        summer = session_plan(datetime(2026, 10, 22, 11, tzinfo=timezone.utc))
        winter = session_plan(datetime(2026, 1, 5, 11, tzinfo=timezone.utc))
        self.assertEqual((summer.market_open.hour, summer.market_open.minute), (13, 30))
        self.assertEqual((winter.market_open.hour, winter.market_open.minute), (14, 30))

    def test_holiday_and_weekend_skip(self):
        self.assertIsNone(session_plan(datetime(2026, 5, 25, 11, tzinfo=timezone.utc)))
        self.assertIsNone(session_plan(datetime(2026, 10, 24, 11, tzinfo=timezone.utc)))

    def test_monday_uses_friday_close(self):
        p = session_plan(datetime(2026, 10, 26, 11, tzinfo=timezone.utc))
        self.assertEqual(p.signal_date, date(2026, 10, 23))

    def test_early_close_stops_late_instruction(self):
        p = session_plan(datetime(2026, 11, 27, 11, tzinfo=timezone.utc))
        self.assertEqual(p.market_close.hour, 18)
        with self.assertRaises(OutsideExecutionWindow):
            session_plan(datetime(2026, 11, 27, 18, 1, tzinfo=timezone.utc))

    def test_during_session_warns_without_using_today_close(self):
        p = session_plan(datetime(2026, 10, 22, 15, tzinfo=timezone.utc))
        self.assertTrue(p.late)
        self.assertEqual(p.signal_date, date(2026, 10, 21))


class DataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _, market = fixture()
        cls.frames = {}
        for s in SYMBOLS:
            c, o = market.close[s], market.open[s]
            cls.frames[s] = pd.DataFrame({"open": o, "close": c, "high": pd.concat([c, o], axis=1).max(axis=1), "low": pd.concat([c, o], axis=1).min(axis=1), "adjclose": c})

    def copied(self):
        return {s: f.copy() for s, f in self.frames.items()}

    def test_complete_fixture_validates_actual_exchange_sessions(self):
        market = validate_frames(self.copied(), date(2026, 10, 21))
        self.assertEqual(len(market.close), 4199)

    def test_missing_vix_column_fails(self):
        frames = self.copied()
        del frames["^VIX"]
        with self.assertRaises(DataError):
            validate_frames(frames, date(2026, 10, 21))

    def test_single_asset_stale_tail_fails(self):
        frames = self.copied()
        frames["TQQQ"] = frames["TQQQ"].iloc[:-1]
        with self.assertRaises(DataError):
            validate_frames(frames, date(2026, 10, 21))

    def test_historical_gap_is_not_forward_filled(self):
        frames = self.copied()
        frames["^VIX"].iloc[500, 0] = np.nan
        with self.assertRaises(DataError):
            validate_frames(frames, date(2026, 10, 21))

    def test_unclosed_and_future_rows_are_excluded(self):
        frames = self.copied()
        for f in frames.values():
            f.loc[pd.Timestamp("2026-10-22")] = np.nan
        market = validate_frames(frames, date(2026, 10, 21))
        self.assertEqual(market.close.index[-1].date(), date(2026, 10, 21))

    def test_negative_and_inconsistent_prices_fail(self):
        for field, value in [("adjclose", -1), ("high", 0.01)]:
            frames = self.copied()
            frames["QQQ"].loc[frames["QQQ"].index[-1], field] = value
            with self.assertRaises(DataError):
                validate_frames(frames, date(2026, 10, 21))

    def test_schema_error_fails(self):
        with self.assertRaises(DataError):
            parse_chart({"chart": {"result": None, "error": {"description": "no"}}}, "^VIX")

    def test_download_timeouts_have_three_bounded_attempts(self):
        get = Mock(side_effect=requests.Timeout("private diagnostic"))
        with self.assertRaises(DataError):
            download_symbol("QQQ", date(2026, 10, 21), get=get, sleep=lambda _: None)
        self.assertEqual(get.call_count, 3)
        self.assertTrue(all(c.kwargs["timeout"] == (5, 25) for c in get.call_args_list))


class BacktestTests(unittest.TestCase):
    def test_old_overnight_and_new_intraday_with_two_sided_cost(self):
        dates = pd.bdate_range("2020-01-01", periods=174)
        c = pd.DataFrame(100., index=dates, columns=SYMBOLS)
        o = c.copy()
        c.loc[dates[170], "QQQ"] = 110
        o.loc[dates[171], "QQQ"] = 115
        c.loc[dates[171], "QLD"] = 144
        o.loc[dates[171], "QLD"] = 130
        s = pd.DataFrame({"signal": 1}, index=dates)
        s.loc[dates[170]:, "signal"] = 2
        r = backtest_returns(MarketData(c, o, dates[-1].date()), s)
        self.assertAlmostEqual(1 + r["策略"].iloc[1], 115 / 110 * (1 - .001) * 144 / 130)

    def test_one_asset_returns_telescope_after_entry(self):
        _, market = fixture()
        s = pd.DataFrame({"signal": 1}, index=market.close.index)
        r = backtest_returns(market, s, leg_bps=0)
        self.assertAlmostEqual((1 + r["策略"]).prod(), market.close["QQQ"].iloc[-1] / market.open["QQQ"].iloc[170], places=9)


class PushTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.json"
        self.session = Mock()
        self.session.post.return_value = response()
        self.session.get.return_value = response(body={"code": 200, "data": {"status": 2}})
        self.expiry = datetime.now(timezone.utc) + timedelta(days=1)

    def send(self, client=None):
        client = client or PushPlus("test-secret-token", session=self.session, sleep=lambda _: None)
        return client.publish("key", "title", "html", self.expiry, FileState(self.path))

    def test_acceptance_without_key_is_never_called_delivered(self):
        r = self.send()
        self.assertEqual(r.status, "accepted_unverified")
        self.assertEqual(self.session.post.call_args.kwargs["timeout"], (5, 20))
        self.assertTrue(self.session.post.call_args.args[0].startswith("https://"))

    def test_duplicate_run_does_not_post_twice(self):
        self.send()
        r = self.send()
        self.assertTrue(r.duplicate)
        self.assertEqual(self.session.post.call_count, 1)

    def test_business_error_under_http_200_fails(self):
        self.session.post.return_value = response(body={"code": 600, "msg": "private account diagnostic"})
        with self.assertRaisesRegex(PushError, "code=600"):
            self.send()
        self.assertEqual(FileState(self.path).load()["entries"]["key"]["status"], "failed")

    def test_receipt_query_confirms_delivery_and_prevents_duplicate(self):
        client = PushPlus("test-secret-token", access_key="test-access-key", session=self.session, sleep=lambda _: None)
        self.assertEqual(self.send(client).status, "delivered")
        self.assertTrue(self.send(client).duplicate)
        self.assertEqual(self.session.post.call_count, 1)

    def test_final_delivery_failure_fails(self):
        self.session.get.return_value = response(body={"code": 200, "data": {"status": 3}})
        with self.assertRaises(PushError):
            self.send(PushPlus("token", "access", session=self.session, sleep=lambda _: None))

    def test_pending_receipt_does_not_cause_second_post(self):
        self.session.get.return_value = response(body={"code": 200, "data": {"status": 1}})
        client = PushPlus("token", "access", session=self.session, sleep=lambda _: None)
        self.assertEqual(self.send(client).status, "accepted_pending")
        self.send(client)
        self.assertEqual(self.session.post.call_count, 1)

    def test_timeout_unknown_is_persisted_and_not_retried(self):
        self.session.post.side_effect = requests.Timeout("test-secret-token")
        for _ in range(2):
            with self.assertRaises(DeliveryUnknown):
                self.send()
        self.assertEqual(self.session.post.call_count, 1)
        self.assertNotIn("test-secret-token", self.path.read_text())

    def test_500_is_unknown_not_blindly_retried(self):
        self.session.post.return_value = response(status=500)
        for _ in range(2):
            with self.assertRaises(DeliveryUnknown):
                self.send()
        self.assertEqual(self.session.post.call_count, 1)

    def test_bad_json_does_not_claim_success(self):
        self.session.post.return_value.json.side_effect = ValueError("not json")
        with self.assertRaises(DeliveryUnknown):
            self.send()

    def test_missing_business_code_is_unknown_and_never_reposted(self):
        self.session.post.return_value = response(body={"data": "test-short-code"})
        for _ in range(2):
            with self.assertRaises(DeliveryUnknown):
                self.send()
        self.assertEqual(self.session.post.call_count, 1)

    def test_unrecognized_ledger_status_blocks_post(self):
        FileState(self.path).save({"version": 1, "entries": {"key": {"status": "unrecognized"}}})
        with self.assertRaises(StateError):
            self.send()
        self.session.post.assert_not_called()

    def test_expired_message_is_not_sent(self):
        self.expiry = datetime.now(timezone.utc) - timedelta(seconds=1)
        with self.assertRaises(PushError):
            self.send()
        self.session.post.assert_not_called()

    def test_persist_failure_prevents_outbound_post(self):
        store = Mock()
        store.__enter__ = Mock(return_value=store)
        store.__exit__ = Mock(return_value=False)
        store.load.return_value = {"version": 1, "entries": {}}
        store.save.side_effect = StateError("write denied")
        with self.assertRaises(StateError):
            PushPlus("token", session=self.session).publish("key", "title", "html", self.expiry, store)
        self.session.post.assert_not_called()

    def test_preparing_crash_blocks_duplicate(self):
        FileState(self.path).save({"version": 1, "entries": {"key": {"status": "preparing", "attempts": 1}}})
        with self.assertRaises(DeliveryUnknown):
            self.send()
        self.session.post.assert_not_called()

    def test_known_rejections_have_daily_attempt_limit(self):
        self.session.post.return_value = response(status=429)
        for _ in range(4):
            with self.assertRaises(PushError):
                self.send()
        self.assertEqual(self.session.post.call_count, 3)


class StateTests(unittest.TestCase):
    def test_storage_self_check_preserves_existing_delivery_entries(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / "state.json"
            prior = {"version": 1, "entries": {"existing": {"status": "delivered"}}}
            FileState(p).save(prior)
            check_state(FileState(p))
            self.assertEqual(FileState(p).load(), prior)
    def test_corrupt_file_is_not_treated_as_empty(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / "state.json"
            p.write_text("not json")
            with self.assertRaises(StateError):
                FileState(p).load()

    def test_local_concurrent_sender_is_blocked(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / "state.json"
            with FileState(p):
                with self.assertRaises(StateError):
                    with FileState(p):
                        pass

    def test_github_state_cas_uses_read_sha_and_conflicts_fail(self):
        session = Mock(headers={})
        body = {"version": 1, "entries": {}}
        session.request.return_value = response(body={"sha": "prior-sha", "content": base64.b64encode(json.dumps(body).encode()).decode()})
        state = GitHubState("owner/repo", "token", session)
        state.load()
        session.request.return_value = response(status=409)
        with self.assertRaises(StateError):
            state.save(body)
        self.assertEqual(session.request.call_args.kwargs["json"]["sha"], "prior-sha")


class MainTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _, cls.market = fixture()
        cls.now = datetime(2026, 10, 22, 11, tzinfo=timezone.utc)

    def test_dry_run_has_report_but_never_calls_notifier(self):
        publisher = Mock(side_effect=AssertionError("dry run must not send"))
        with tempfile.TemporaryDirectory() as folder:
            code = main.run(True, folder, self.now, loader=lambda _: self.market, publisher=publisher)
            self.assertEqual(code, 0)
            content = (Path(folder) / "report.html").read_text(encoding="utf-8")
            self.assertIn("2026-10-21", content)
            self.assertIn("2026-10-22", content)
            self.assertIn("次日开盘", content)
            self.assertNotIn("请立即卖出", content)
        publisher.assert_not_called()

    def test_data_failure_nonzero_and_notice_has_no_trade_target(self):
        publisher = Mock(return_value=Receipt("accepted_unverified"))
        with tempfile.TemporaryDirectory() as folder:
            code = main.run(False, folder, self.now, loader=Mock(side_effect=DataError("missing VIX")), publisher=publisher)
            self.assertEqual(code, 1)
            self.assertEqual(publisher.call_args.kwargs["category"], "data-health")
            self.assertNotIn("100%", publisher.call_args.args[2])

    def test_missing_token_is_nonzero_and_never_claims_sent(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict("os.environ", {}, clear=True):
            self.assertEqual(main.run(False, folder, self.now, loader=lambda _: self.market), 1)
            status = json.loads((Path(folder) / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(status["error_type"], "PushError")

    def test_optional_backtest_failure_preserves_valid_signal(self):
        publisher = Mock(return_value=Receipt("accepted_unverified"))
        with tempfile.TemporaryDirectory() as folder, patch("main.backtest_returns", side_effect=ValueError("bad metrics")):
            self.assertEqual(main.run(False, folder, self.now, loader=lambda _: self.market, publisher=publisher), 0)
            self.assertIn("业绩模块不可用", (Path(folder) / "report.html").read_text(encoding="utf-8"))
        publisher.assert_called_once()

    def test_holiday_skips_before_download_and_push(self):
        loader, publisher = Mock(), Mock()
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(main.run(False, folder, datetime(2026, 12, 25, 11, tzinfo=timezone.utc), loader=loader, publisher=publisher), 0)
        loader.assert_not_called()
        publisher.assert_not_called()

    def test_after_close_is_nonzero_and_no_push(self):
        publisher = Mock()
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(main.run(False, folder, datetime(2026, 10, 22, 21, tzinfo=timezone.utc), publisher=publisher), 1)
        publisher.assert_not_called()

    def test_dry_run_after_close_can_preview_next_session_without_sending(self):
        publisher = Mock()
        with tempfile.TemporaryDirectory() as folder:
            code = main.run(True, folder, datetime(2026, 10, 22, 3, tzinfo=timezone.utc), loader=lambda _: self.market, publisher=publisher)
            self.assertEqual(code, 0)
            status = json.loads((Path(folder) / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(status["execution_date"], "2026-10-22")
        publisher.assert_not_called()

    def test_notification_failure_returns_nonzero(self):
        publisher = Mock(side_effect=PushError("business rejected"))
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(main.run(False, folder, self.now, loader=lambda _: self.market, publisher=publisher), 1)


class WorkflowTests(unittest.TestCase):
    def test_manual_defaults_safe_and_storage_has_write_permission(self):
        import yaml
        root = Path(__file__).resolve().parents[1]
        w = yaml.load((root / ".github/workflows/daily_run.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        self.assertEqual(w["on"]["workflow_dispatch"]["inputs"]["dry_run"]["default"], "true")
        self.assertEqual(w["on"]["workflow_dispatch"]["inputs"]["check_state"]["default"], "false")
        self.assertEqual(w["concurrency"]["cancel-in-progress"], "false")
        self.assertEqual(w["jobs"]["build"]["permissions"]["contents"], "write")
        self.assertIn("03,25,47", w["on"]["schedule"][0]["cron"])


if __name__ == "__main__":
    unittest.main()
