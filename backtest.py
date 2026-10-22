"""Close signal -> following open; old holding earns the overnight return."""
import numpy as np
import pandas as pd

from strategy import ETF_MAP, PARAMETERS


def backtest_returns(market, signals, leg_bps=5):
    close, opened = market.close, market.open
    # No performance credit for the historical forced-QLD warm-up values.
    start = PARAMETERS.ma_window
    dates = close.index[start:]
    target = signals["signal"].shift(1).loc[dates].map(ETF_MAP)
    if target.isna().any() or len(target) == 0:
        raise ValueError("No warmed-up executable signal")
    columns = list(close.columns)
    pos = np.array([columns.index(s) for s in target])
    old = np.r_[pos[0], pos[:-1]]
    rows = np.arange(start, len(close))
    c, o = close.to_numpy(), opened.to_numpy()
    overnight = o[rows, old] / c[rows - 1, old]
    overnight[0] = 1.0  # Start capital at the first executable open.
    intraday = c[rows, pos] / o[rows, pos]
    legs = np.where(pos != old, 2., 0.)
    legs[0] = 1.
    strategy = overnight * (1 - legs * leg_bps / 10000) * intraday - 1
    returns = pd.DataFrame({"策略": strategy}, index=dates)
    for symbol in ("QQQ", "QLD", "TQQQ", "SPY"):
        r = close[symbol].pct_change(fill_method=None).loc[dates].copy()
        r.iloc[0] = close[symbol].loc[dates[0]] / opened[symbol].loc[dates[0]] * (1 - leg_bps / 10000) - 1
        returns[symbol] = r
    return returns


def period_return(returns, days):
    """Use the first session at or after the requested calendar-day boundary."""
    start = returns.index[-1] - pd.Timedelta(days=days)
    if start < returns.index[0]:
        return None
    sub = returns.loc[returns.index > start]
    return (1 + sub).prod() - 1
