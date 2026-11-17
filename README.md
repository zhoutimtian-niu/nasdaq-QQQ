# Nasdaq daily strategy

## Completed-close primary report (2026-11-16)

The primary report is scheduled at 16:25 and 16:55 America/New_York, with
13:25/13:55 slots for actual early-close sessions. New York timezone scheduling
handles DST; exchange sessions determine which close is usable and the next
execution day. A 20-minute post-close grace applies even to dry runs. A normal
13:25 run skips before downloading data. Delays across NY midnight/weekends
still use the most recent completed session, never a nonexistent holiday bar.

`python main.py --after-close --dry-run` validates this path without publishing.
`--after-close` (or `AFTER_CLOSE=true`) is an explicit separate mode: it only
publishes between the completed close plus grace and the next exchange open.
The original pre-open path retains its expired-instruction protection.

Existing Beijing 19:03/19:25/19:47 jobs remain recovery opportunities. Both
paths share the original model/signal-date/execution-date/channel/category key,
so an early report does not cause a second daily email/WeChat POST at recovery.
An overdue pre-open run suppresses its failure notice only if the matching
expired report already has a confirmed WeChat or SMTP-accepted record. Unknown
PushPlus acceptance alone is insufficient. State-read failures remain visible.

Report text states the planned execution date rather than calling an overnight
report today's target. The trading rule, history initialization, source input
checks, report layout, and dual-route transport remain unchanged. GitHub cron
may still run late/drop events; these are target start times, not delivery SLAs.

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

## GitHub Actions and notification recovery

The existing post-close and pre-open schedules remain. A UTC `17,47 * * * *`
recovery schedule adds another opportunity every30minutes, including weekends
for Friday-close/Monday-execution reports. GitHub may delay or omit schedules;
this is not a guarantee of exact delivery time or eventual success during a
permanent provider/configuration outage. `concurrency` serializes senders.

`recover.py` pins one signal/execution pair per job, retries temporary data and
transport failures for up to11minutes, then leaves durable state for the next
scheduled run. Retry pauses are15/30/60seconds within a job; confirmed transport
rejections have persistent exponential backoff from1minute to30minutes. There
is no lifetime three-attempt ceiling. Data must pass the full existing validation
before a normal report is sent; no filling missing prices or stale HTML fallback.
A validated data load is reused within a job while waiting for delivery receipts.
The actual execution-session close is a hard expiry. Early-close calendars and
the20minute post-close data grace remain in force. Late reports retain their
original dates and explicitly label generation after the open.

### WeChat first, normal-report email fallback

- First submit the normal HTML report to PushPlus. If provider status confirms
  delivery, do not send email.
- If PushPlus explicitly rejects/fails, use the configured independent SMTP
  immediately. If receipt status is unknown/pending, poll the existing receipt;
  after10minutes from the first routing attempt use normal-report SMTP fallback.
  A missing PushPlus token cannot prevent SMTP fallback.
- Either confirmed WeChat delivery or SMTP acceptance stops all further sends
  for that logical report. SMTP acceptance is not an inbox/read receipt.
- Never send a data-health/window-health failure notice to either channel.
  Failures remain visible in Actions summaries/artifacts with nonzero exit codes;
  recovery continues within the report's validity window.
- A lost WeChat POST response is not blindly reposted without a receipt. The
  fallback can still deliver the normal report. For SMTP DATA timeouts, the
  production router retries after30minutes using the same Message-ID. A lost
  response can have hidden a real delivery, so this may produce a duplicate;
  SMTP cannot promise exactly-once delivery from Message-ID alone.

`PUSH_TOKEN` remains the send credential. The optional `PUSHPLUS_ACCESS_KEY`
enables the official asynchronous receipt query. This repository currently
cannot confirm delivery when it is absent, so the10minute uncertainty fallback
applies even if the user's WeChat happened to receive the message. Official
AccessKeys expire (currently2hours); a static long-lived GitHub Secret is not
a permanent receipt solution. OpenAPI token refresh requires separate
secretKey/developer/IP configuration; this patch does not claim to configure it.
See the official OpenAPI documentation before enabling a maintained receipt
credential or a callback receiver.

SMTP uses the existing `BACKUP_EMAIL_ENABLED` variable and `SMTP_*` configuration
via GitHub Secrets; no secrets or raw responses are logged. Report styling,
strategy rules, trading thresholds and backtest engine are unchanged.

### Durable state and verification

The existing `notification-state` branch and per-channel keys are preserved.
A separate hashed routing record tracks the first attempt and fallback state.
State writes precede network sends. Missing/unreadable state never resets into
an empty ledger or causes an unsafe send; compare-and-swap and workflow
concurrency protect against racing writers. Already-delivered reports skip
price downloads in scheduled recovery jobs. Neither prior failure notices nor
unknown PushPlus acceptance are considered successful daily report delivery.

Validate with a manually dispatched `dry_run=true` (the safe default). It neither
sends nor reads SMTP credentials nor writes remote delivery records. Optional
`check_state=true` separately writes/reads/removes a nonsecret test marker.
Diagnostics retain `run.json`, `run.log`, HTML and signal/performance artifacts.
A local or CI failure is not a user notification of a trading target.

### Rollback

Preserve delivery state during rollback. Do not delete report keys to force a
resend. Restoring the previous source restores its double-send and failure-notice
behavior; check existing receipts before intentionally triggering a live run.

## Reference

- [GitHub schedule limitations](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
- [PushPlus send API](https://www.pushplus.plus/doc/guide/api.html)
- [PushPlus result query and AccessKey](https://www.pushplus.plus/doc/guide/openApi.html)
