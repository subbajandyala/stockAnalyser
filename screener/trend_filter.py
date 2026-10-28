"""
Supertrend entry filter for the candles engine — ON by default on this branch.

  CE only while Supertrend is up (green), PE only while it is down (red).
  Supertrend(CHART_ST_PERIOD=10, CHART_ST_MULT=2) on the same 3m candles the bot
  reads, completed candles only (the forming one can still flip it).
  CHART_SUPERTREND=0 in .env switches it off (plain chart-success entries).

(10, 2) made the most in the 40-day replay of 28 Oct (+163.6k vs +95.9k with no
filter, 2 lots); (10, 3) made less than no filter — check the neighbours
(CHART_ST_MULT=1.5 / 2.5, CHART_ST_PERIOD=7 / 14) before trusting the setting.
"""

import pandas as pd

SUPERTREND = True
ST_PERIOD = 10
ST_MULT = 2.0
HISTORY = 300               # candles used (3m: about two and a half sessions)


def enabled() -> bool:
    return SUPERTREND


def describe() -> str:
    return f"Supertrend({ST_PERIOD},{ST_MULT:g}) side" if SUPERTREND else "off"


def supertrend_up(df: pd.DataFrame, period: int = 10, mult: float = 2.0) -> bool | None:
    """Direction of the usual (TradingView / Kite) Supertrend after the last candle of df."""
    if len(df) < period + 2:
        return None
    h, l, c = df["High"].values, df["Low"].values, df["Close"].values
    tr = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(df))]
    atr = sum(tr[:period]) / period
    upper = lower = None
    up = True
    for i in range(period - 1, len(df)):
        if i >= period:
            atr = (atr * (period - 1) + tr[i]) / period             # Wilder's smoothing
        mid = (h[i] + l[i]) / 2
        bu, bl = mid + mult * atr, mid - mult * atr
        if upper is None:
            upper, lower = bu, bl
            up = bool(c[i] > upper)
            continue
        upper = bu if (bu < upper or c[i - 1] > upper) else upper
        lower = bl if (bl > lower or c[i - 1] < lower) else lower
        if up and c[i] < lower:
            up = False
        elif not up and c[i] > upper:
            up = True
    return bool(up)


def check(df: pd.DataFrame, side: str, price: float) -> str | None:
    """Why a CE / PE on the last candle of df is blocked, or None when it may trade.

    df's last candle is the signal candle (forming live, closed in the replay);
    Supertrend reads the completed candles before it."""
    if not SUPERTREND or df is None or len(df) < 2:
        return None
    up = supertrend_up(df.tail(HISTORY).iloc[:-1], ST_PERIOD, ST_MULT)
    if up is not None and (side == "CE") != up:
        return f"Supertrend {'up' if up else 'down'}"
    return None
