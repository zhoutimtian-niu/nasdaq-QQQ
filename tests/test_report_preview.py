from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import pandas as pd

from backtest import backtest_returns
from market_calendar import session_plan
from notify import Receipt
from report import build_report, style_preview, table
from strategy import calculate_signals
from test_engineering import fixture
import preview_push


class ReportPreviewTests(unittest.TestCase):
    def test_report_renders_all_target_states_with_history_and_full_returns(self):
        _, market = fixture()
        signals = calculate_signals(market.close)
        now = datetime(2026, 10, 22, 11, tzinfo=timezone.utc)
        performance = backtest_returns(market, signals)
        for target in (1, 2, 3):
            for old in (1, 2, 3):
                sample = signals.copy()
                sample.iloc[-2, sample.columns.get_loc('signal')] = old
                sample.iloc[-1, sample.columns.get_loc('signal')] = target
                title, html = build_report(market, sample, session_plan(now), now, performance)
                self.assertIn('需要调仓' if target != old else '保持目标', html)
                self.assertIn('最近5次目标切换', html)
                self.assertIn('次日开盘', html)
                self.assertNotIn('请立即卖出', html)
                self.assertEqual(html.count('<table '), 3)
                self.assertEqual(html.count('<table '), html.count('</table>'))
                self.assertEqual(html.count('<tr>'), html.count('</tr>'))
                self.assertEqual(html.count('<div '), html.count('</div>'))
                HTMLParser().feed(html)
                preview_title, preview = style_preview(title, html)
                self.assertTrue(preview_title.startswith('【样式预览】'))
                self.assertIn('不作为新增调仓通知', preview)
                self.assertLess(len(preview.encode()), 100000)

    def test_table_escapes_untrusted_labels_and_handles_missing_history(self):
        html = table(pd.DataFrame({'<script>': ['<img>'], '策略': [None]}), returns=True)
        self.assertNotIn('<script>', html)
        self.assertNotIn('<img>', html)
        self.assertIn('—', html)

    def test_failed_fresh_generation_cannot_send_a_stale_report(self):
        client = Mock()
        self.assertEqual(preview_push.run(generator=Mock(return_value=1), client=client), 1)
        client.publish.assert_not_called()

    def test_explicit_preview_run_uses_separate_stable_key_and_label(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            def generate(**kwargs):
                self.assertTrue(kwargs['dry_run'])
                (output / 'run.json').write_text(json.dumps({'status': 'dry_run_validated', 'signal_date': '2026-10-23', 'execution_date': '2026-10-26'}))
                (output / 'report.html').write_text("<div style='font-size:10px;letter-spacing:2px;'>Report</div>")
                return 0
            client = Mock()
            client.publish.return_value = Receipt('accepted_unverified')
            with patch.dict(os.environ, {'GITHUB_RUN_ID': '123456'}):
                for _ in range(2):
                    self.assertEqual(preview_push.run(folder, generate, client, Mock()), 0)
            calls = client.publish.call_args_list
            self.assertEqual(calls[0].args[0], calls[1].args[0])
            self.assertEqual(calls[0].args[0], hashlib.sha256(b'style-preview-v1|123456').hexdigest())
            self.assertIn('【样式预览】', calls[0].args[1])
            self.assertIn('不作为新增调仓通知', calls[0].args[2])
            self.assertEqual(json.loads((output / 'run.json').read_text())['notification'], 'accepted_unverified')
