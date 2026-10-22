"""Yahoo daily chart data with explicit session validation; never fill missing risk data."""
import concurrent.futures
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import numpy as np
import pandas as pd
import requests

from market_calendar import expected_sessions
from strategy import HISTORY_START

LOG = logging.getLogger(__name__)
SYMBOLS = ("QQQ", "QLD", "TQQQ", "SPY", "^NDX", "^VIX")


class DataError(RuntimeError):
    pass


@dataclass
class MarketData:
    close: pd.DataFrame
    open: pd.DataFrame
    signal_date: object


def parse_chart(payload, symbol):
    try:
        chart = payload["chart"]
        if chart.get("error") or not chart.get("result"):
            raise DataError(f"{symbol}: provider reported no data")
        result = chart["result"][0]
        raw_dates = pd.to_datetime(result["timestamp"], unit="s", utc=True)
        # These US daily charts use session-calendar dates in UTC, including CBOE VIX.
        dates = raw_dates.tz_localize(None).normalize()
        values = result["indicators"]["quote"][0]
        f = pd.DataFrame({k: values[k] for k in ("open", "high", "low", "close")}, index=dates)
        f["adjclose"] = result["indicators"]["adjclose"][0]["adjclose"]
        if f.empty or not f.index.is_unique or not f.index.is_monotonic_increasing:
            raise DataError(f"{symbol}: empty or duplicate/out-of-order dates")
        return f
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise DataError(f"{symbol}: invalid daily-chart schema") from exc


def download_symbol(symbol, signal_date, get=None, sleep=time.sleep):
    get = get or requests.get
    start = int(pd.Timestamp(HISTORY_START, tz="UTC").timestamp())
    end = int(pd.Timestamp(signal_date + timedelta(days=1), tz="UTC").timestamp())
    last = None
    for attempt in range(3):
        host = "query1.finance.yahoo.com" if attempt != 1 else "query2.finance.yahoo.com"
        url = f"https://{host}/v8/finance/chart/{quote(symbol, safe='')}"
        try:
            response = get(url, params={"period1": start, "period2": end, "interval": "1d", "events": "div,splits"}, headers={"User-Agent": "Mozilla/5.0 NasdaqDailyResearch/1.0"}, timeout=(5, 25))
            response.raise_for_status()
            return parse_chart(response.json(), symbol)
        except (requests.RequestException, ValueError, DataError) as exc:
            # URLs and raw bodies can contain vendor diagnostics; log only bounded type metadata.
            last = exc
            LOG.warning("行情重试 asset=%s attempt=%d reason=%s", symbol, attempt + 1, type(exc).__name__)
            if attempt < 2:
                sleep(2 ** (attempt + 1))
    raise DataError(f"{symbol}: all three download attempts failed ({type(last).__name__})") from None


def validate_frames(frames, signal_date, calendar=None):
    missing = set(SYMBOLS) - frames.keys()
    if missing:
        raise DataError("Missing required assets: " + ",".join(sorted(missing)))
    dates = expected_sessions(HISTORY_START, signal_date, calendar)
    if len(dates) < 201:
        raise DataError("At least 201 complete sessions are required before publishing")
    closes, opens = {}, {}
    for symbol in SYMBOLS:
        f = frames[symbol].loc[HISTORY_START:pd.Timestamp(signal_date)].reindex(dates)
        fields = f[["open", "high", "low", "close", "adjclose"]]
        if fields.isna().any().any() or not np.isfinite(fields.to_numpy()).all():
            raise DataError(f"{symbol}: missing/stale/invalid required session prices")
        if (fields <= 0).any().any():
            raise DataError(f"{symbol}: non-positive prices")
        tolerance = f["close"] * 1e-5
        if ((f["high"] + tolerance < f[["open", "close"]].max(axis=1)) | (f["low"] - tolerance > f[["open", "close"]].min(axis=1))).any():
            raise DataError(f"{symbol}: inconsistent OHLC prices")
        # A very large ETF return needs human review, not a silently corrupted trade.
        limit = 3.0 if symbol == "^VIX" else 0.75
        if (f["adjclose"].pct_change(fill_method=None).abs() > limit).any():
            raise DataError(f"{symbol}: extreme adjusted return requires review")
        closes[symbol] = f["adjclose"]
        opens[symbol] = f["open"] * f["adjclose"] / f["close"]
    return MarketData(pd.DataFrame(closes), pd.DataFrame(opens), signal_date)


def load_market_data(signal_date, downloader=download_symbol, calendar=None):
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        futures = {symbol: executor.submit(downloader, symbol, signal_date) for symbol in SYMBOLS}
        frames = {symbol: future.result() for symbol, future in futures.items()}
    market = validate_frames(frames, signal_date, calendar)
    LOG.info("行情通过校验 signal_date=%s sessions=%d assets=%d", signal_date, len(market.close), len(SYMBOLS))
    return market
