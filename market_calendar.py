"""Use actual US exchange sessions, including holidays, DST and early closes."""
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd

NY = ZoneInfo("America/New_York")


class OutsideExecutionWindow(RuntimeError):
    pass


@dataclass(frozen=True)
class SessionPlan:
    execution_date: date
    signal_date: date
    market_open: datetime
    market_close: datetime
    late: bool


def calendar_for(now):
    return xcals.get_calendar("XNYS", start="2010-01-01", end=f"{now.year + 2}-12-31")


def session_plan(now=None, calendar=None, preview=False):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("Use timezone-aware time")
    cal = calendar if calendar is not None else calendar_for(now)
    today = pd.Timestamp(now.astimezone(NY).date())
    if not cal.is_session(today):
        if not preview:
            return None
        today = cal.date_to_session(today, direction="next")
    opened = cal.session_open(today).to_pydatetime()
    closed = cal.session_close(today).to_pydatetime()
    # Do not silently roll a late task to a new day's trading instruction.
    if now >= closed:
        if not preview:
            raise OutsideExecutionWindow("本交易日已收盘，停止生成过期交易指令")
        today = cal.next_session(today)
        opened = cal.session_open(today).to_pydatetime()
        closed = cal.session_close(today).to_pydatetime()
    prior = cal.previous_session(today).date()
    return SessionPlan(today.date(), prior, opened, closed, now >= opened)


def expected_sessions(start, end, calendar=None):
    cal = calendar if calendar is not None else calendar_for(datetime.now(timezone.utc))
    return cal.sessions_in_range(pd.Timestamp(start), pd.Timestamp(end)).tz_localize(None)
