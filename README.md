# Nasdaq daily strategy

## Notification reliability update (prepared 2026-11-10)

The notification update adds an independent SMTP HTML email route alongside
WeChat. It preserves the existing report, model, data and WeChat deduplication
key. Each route records its own result; SMTP acceptance is not an inbox/read
receipt. `BACKUP_EMAIL_ENABLED=true` enables email only when all SMTP settings
are present. Use TLS (`SMTP_SECURITY=ssl` or `starttls`); never use a QQ login
password instead of its SMTP authorization code.

Required email environment variables: `SMTP_HOST`, `SMTP_USERNAME`,
`SMTP_PASSWORD`, `SMTP_FROM`, `SMTP_TO`. Optional: `SMTP_PORT` (465 for SSL,
587 for STARTTLS), `SMTP_SECURITY` (SSL by default). The workflow maps account
fields from Actions Secrets and server settings from Actions Variables. No
personal address or credential belongs in source control.

If only PushPlus acceptance is available, with no confirmed WeChat result or
SMTP acceptance, the normal sender exits **2** with `delivery_unverified`.
Failures remain nonzero. A task arriving after the market close emits only
a short-lived operational alert, never a rolled-over trading instruction.

Manual `preview_email=true` generates a fresh dry-run report and explicitly
sends one labeled HTML preview by SMTP only. It has separate stable deduplication
from the daily report. Rerunning the same Actions run does not knowingly resend.
Selecting both preview modes rejects the run without either preview.

For an independently timed host directly running this program, set
`NOTIFICATION_STATE_BACKEND=github` and the repository/token environment so both
hosts share the existing CAS-protected ledger. This setting does not install
a host, cloud service or schedule. GitHub cron alone still cannot guarantee
punctual runs; changing the channel cannot repair an unstarted task.

The older sections below describe the original engineering and strategy
baseline. Their statement that no email is configured is superseded when the
new SMTP route is explicitly enabled and verified.

QQQ / QLD / TQQQ rotation uses MA170, simple rolling RSI14 thresholds 65/80,
1% bear buffer and state hysteresis. With user approval on 2026-10-24,
the VIX40 condition and its data dependency were removed; other rules are unchanged.
This is a model report, not an automated brokerage order.

## Run and verify

Use Python 3.12:

```sh
python -m pip install -r requirements-dev.txt
python -m pip check
python -m unittest discover -s tests -v
python main.py --dry-run
```

Dry run downloads real public market data and writes `output/report.html`,
`run.json`, signals, returns and logs. It never calls PushPlus or reads/writes the
remote notification ledger. Manual Actions runs default to dry run.
After an exchange session closes, dry run may preview the next trading session;
the production sender never uses this preview exemption.

The frozen fixture checks all 4,199 signals against an independently frozen
VIX-disabled version of the previous rules. Exactly 20 signal days differ from
the original VIX-enabled model, all in 2020. Other
tests cover look-ahead, next-open execution, overnight ownership, costs, holidays,
DST, early close, missing/stale data, HTTP/business errors, asynchronous receipts,
unknown results, persistent state, duplicates, and failure exit codes.

## What changed

- Read Yahoo's daily chart API directly, with finite timeouts, bounded retries,
  and query1/query2 recovery. This is the same data provider used by yfinance;
  query2 is not an independent data source. Missing required values are rejected,
  not filled forward. All five assets must contain the expected completed session.
- Initialize the original state machine from TQQQ's first actual trading day.
  Exchange sessions determine the preceding signal date, including holidays and
  early closes. Intraday/current/future bars are excluded. Reports generated
  after the open explicitly say they are late. After that session closes, a late
  job fails instead of rolling into a different trading instruction.
- Backtest close signals at the following open. The old holding earns the
  overnight return; the new holding earns the open-to-close return. Charge 5 bps
  per traded leg. Unwarmed-up bars are not counted as executable performance.
  Daily targets do not depend on the display-only performance module succeeding.
- Use HTTPS and check both HTTP status and PushPlus JSON business status.
  Save a durable `preparing` record before sending; distinguish accepted,
  pending, confirmed delivered, failed, and unknown.
- Catch controlled failures with a nonzero exit, preserve useful diagnostics,
  and never log raw vendor bodies or credentials.

## GitHub Actions and notification state

The two existing Beijing time slots, 19:03 and 19:25 on weekdays, remain; 19:47
adds a finite recovery opportunity. `concurrency` serializes runs. GitHub's
schedule can still be delayed or dropped: this cannot guarantee punctual delivery.
A host that directly runs the same code with an independent clock is needed for
a stronger scheduling guarantee. Merely dispatching Actions from another clock
still inherits Actions queue delays.

`PUSH_TOKEN` is still the only mandatory PushPlus secret. Keep the existing
secret; no value is stored in code or the delivery ledger.

Optional `PUSHPLUS_ACCESS_KEY` enables the official asynchronous status query.
Without it, a valid send response is recorded as `accepted_unverified`, never
described as confirmed channel delivery. An AccessKey is different from the
PUSH_TOKEN. Even confirmed provider delivery is not a user read receipt.

The workflow `GITHUB_TOKEN` needs `contents: write` to maintain a dedicated
`notification-state` branch with `pushplus-state.json`. This branch is created
only on the first real send or explicit storage self-check. It contains bounded, nonsecret
per-report keys, status, content hashes and provider receipt identifiers.
It does not change the strategy/default-branch source. Source CI ignores pushes
to this state branch. Local runs use an atomic file and lock instead.

For a deployment check, manually run the daily workflow with `dry_run=true` and
`check_state=true`. The separate storage step writes, reads and removes a random
test marker using the job's actual GitHub token; the main dry run still never
sends a message or writes notification state. This verifies real persistence
permissions before enabling production; the marker contains no message/token.

The persistence ordering is intentional: if durable storage fails, do not send.
If a POST times out, returns an ambiguous server error or the process dies after
the preparing record, do not blindly repeat it. This favors avoiding duplicate
trading instructions over claiming impossible exactly-once delivery.

## Troubleshooting / recovery

Inspect the Actions job summary and `strategy-report-*` artifact first:

- `failed` + DataError: no valid daily trading target was issued. A separate
  data-health notice may be sent once; later scheduled recovery rechecks input.
- `accepted_unverified`: provider accepted the request; delivery is not verified.
  Configure the optional AccessKey or inspect PushPlus's latest requests page.
- `accepted_pending`: receipt polling has not yet confirmed a final result.
  Subsequent runs query the existing receipt instead of sending again.
- `failed` business rejection: a later recovery may retry, at most three attempts
  per report key. Account quota or permission problems must be fixed upstream.
- `preparing` / `unknown`: inspect the matching request on PushPlus. If delivered,
  update that ledger entry to `delivered`. Only after confirming it was **not**
  accepted should an operator set the entry to `failed` to allow a bounded retry.
  Do not delete the whole ledger. The optional AccessKey can query a known receipt.
- A local stale `.lock` also needs inspection before manual removal.

The ledger key uses strategy version + signal session + execution session +
channel + category. It does not assert the user completed a brokerage trade.

No independent email/other-channel alert is configured here: Actions failure
status and artifacts are observable, but a platform outage needs a genuinely
independent monitor/channel. Do not rely on PushPlus to report its own outage.

## Rollback

Do not remove the ledger during rollback. Keep the current production job and
its secrets unchanged until replacement checks pass. Code rollback can revert
the engineering commit, but the historical old sender did not honor this ledger
and may send duplicates; pause scheduled sending while intentionally rolling
back and check whether that day's report was already accepted.

## Reference

- [GitHub schedule limitations](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
- [PushPlus send API](https://www.pushplus.plus/doc/guide/api.html)
- [PushPlus result query and AccessKey](https://www.pushplus.plus/doc/guide/openApi.html)
