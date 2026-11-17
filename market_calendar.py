"""Use actual US exchange sessions, including holidays, DST and early closes."""
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd

NY = ZoneInfo("America/New_York")
POST_CLOSE_GRACE = timedelta(minutes=20)


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


def session_plan(now=None, calendar=None, preview=False, after_close=False):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("Use timezone-aware time")
    cal = calendar if calendar is not None else calendar_for(now)
    today = pd.Timestamp(now.astimezone(NY).date())
    if after_close:
        # Use the latest ACTUAL closed exchange session, even if a delayed run
        # crosses NY midnight or a weekend. Never use today's unfinished bar.
        signal = cal.date_to_session(today, direction="previous")
        if now < cal.session_close(signal).to_pydatetime():
            signal = cal.previous_session(signal)
        signal_close = cal.session_close(signal).to_pydatetime()
        execution = cal.next_session(signal)
        opened = cal.session_open(execution).to_pydatetime()
        if now < signal_close + POST_CLOSE_GRACE or now >= opened:
            return None
        return SessionPlan(execution.date(), signal.date(), opened,
                           cal.session_close(execution).to_pydatetime(), False)
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


def expired_execution_plan(now, calendar=None):
    """Identify the expired report key for a read-only submission lookup."""
    if now.tzinfo is None:
        raise ValueError("Use timezone-aware time")
    cal = calendar if calendar is not None else calendar_for(now)
    day = pd.Timestamp(now.astimezone(NY).date())
    if not cal.is_session(day) or now < cal.session_close(day).to_pydatetime():
        return None
    return SessionPlan(day.date(), cal.previous_session(day).date(),
                       cal.session_open(day).to_pydatetime(),
                       cal.session_close(day).to_pydatetime(), True)


def recovery_session_plan(now=None, calendar=None, **_):
    """Latest completed close -> same next execution session until its close.

    A recovery can arrive after the open; label it late without rolling an old
    instruction into an unrelated date. The post-close grace still applies.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError('Use timezone-aware time')
    cal = calendar if calendar is not None else calendar_for(now)
    day = pd.Timestamp(now.astimezone(NY).date())
    signal = cal.date_to_session(day, direction='previous')
    if now < cal.session_close(signal).to_pydatetime():
        signal = cal.previous_session(signal)
    closed = cal.session_close(signal).to_pydatetime()
    execution = cal.next_session(signal)
    opened = cal.session_open(execution).to_pydatetime()
    deadline = cal.session_close(execution).to_pydatetime()
    if now < closed + POST_CLOSE_GRACE or now >= deadline:
        return None
    return SessionPlan(execution.date(), signal.date(), opened, deadline, now >= opened)


def expected_sessions(start, end, calendar=None):
    cal = calendar if calendar is not None else calendar_for(datetime.now(timezone.utc))
    return cal.sessions_in_range(pd.Timestamp(start), pd.Timestamp(end)).tz_localize(None)
