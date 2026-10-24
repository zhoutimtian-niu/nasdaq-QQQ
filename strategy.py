"""MA170/RSI14 state machine. VIX filter removed with user approval."""
from dataclasses import dataclass

import pandas as pd

HISTORY_START = "2010-02-11"  # TQQQ's first session; preserve the legacy state initialization.
ETF_MAP = {1: "QQQ", 2: "QLD", 3: "TQQQ"}
MODEL_VERSION = "ma170-rsi65-80-no-vix-v2"


@dataclass(frozen=True)
class Parameters:
    ma_window: int = 170
    rsi_window: int = 14
    rsi_buy_3x: float = 65
    rsi_sell_3x: float = 80
    bear_buffer: float = 0.01
    bull_buffer: float = 0.0


PARAMETERS = Parameters()


def next_state(state, price, ma, rsi, p=PARAMETERS):
    if price < ma * (1 - p.bear_buffer):
        return 1
    if state == 1:
        return 2 if price > ma * (1 + p.bull_buffer) else 1
    if rsi > p.rsi_sell_3x:
        return 2
    if rsi < p.rsi_buy_3x:
        return 3
    return state


def calculate_signals(close, p=PARAMETERS):
    """Warm-up values match old code for historical parity; never trade warm-up bars."""
    if "^NDX" not in close.columns:
        raise ValueError("NDX is required")
    if not close.index.is_unique or not close.index.is_monotonic_increasing:
        raise ValueError("Signal dates must be unique and increasing")
    if close[["^NDX"]].isna().any().any():
        raise ValueError("Missing signal inputs cannot be forward-filled")
    price = close["^NDX"]
    ma = price.rolling(p.ma_window).mean()
    delta = price.diff()
    gain = delta.where(delta > 0, 0).rolling(p.rsi_window).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(p.rsi_window).mean().replace(0, 1e-10)
    rsi = 100 - 100 / (1 + gain / loss)
    states, state = [], 2
    for x, m, r in zip(price, ma, rsi):
        if pd.isna(m):
            states.append(2)
            continue
        state = next_state(state, x, m, r, p)
        states.append(state)
    return pd.DataFrame({"signal": states, "ma": ma, "rsi": rsi}, index=close.index)
