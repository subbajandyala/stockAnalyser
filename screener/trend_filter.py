"""
Optional Supertrend + CPR filters for the candles engine — all OFF by default,
so the bot trades exactly as before until one is switched on in .env.

  CHART_SUPERTREND=1        CE only while Supertrend is up (green), PE only while
                            it is down (red). Supertrend(CHART_ST_PERIOD=10,
                            CHART_ST_MULT=3) on the same 3m candles, completed
                            candles only (the forming one can still flip it).
  CHART_CPR_BIAS=1          no CE below the CPR (price under BC), no PE above it
                            (price over TC). Inside the CPR both sides may trade.
  CHART_CPR_ZONE=1          no CE just under or inside the CPR (from
                            CHART_CPR_ZONE_PCT=0.1 % below BC up to TC) — it is
                            running into resistance; no PE just over or inside it
                            (from BC up to 0.1 % above TC) — it is running into support.

CPR from the previous trading day's high / low / close:
  pivot = (H + L + C) / 3,  BC = (H + L) / 2,  TC = 2 × pivot − BC  (BC ≤ TC after sorting)
"""

import pandas as pd

SUPERTREND = False
ST_PERIOD = 10
ST_MULT = 3.0
CPR_BIAS = False
CPR_ZONE = False
CPR_ZONE_PCT = 0.1          # percent
HISTORY = 300               # candles used (3m: about two and a half sessions)


def enabled() -> bool:
    return SUPERTREND or CPR_BIAS or CPR_ZONE


def describe() -> str:
    parts = []
    if SUPERTREND:
        parts.append(f"Supertrend({ST_PERIOD},{ST_MULT:g}) side")
    if CPR_BIAS:
        parts.append("CPR bias")
    if CPR_ZONE:
        parts.append(f"CPR zone ±{CPR_ZONE_PCT:g}%")
    return ", ".join(parts) or "off"


def supertrend_up(df: pd.DataFrame, period: int = 10, mult: float = 3.0) -> bool | None:
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


def cpr(df: pd.DataFrame) -> tuple[float, float, float] | None:
    """(BC, pivot, TC) for the day of df's last candle, from the previous day in df."""
    day = df.index[-1].date()
    prev = df[df.index.date < day]
    if prev.empty:
        return None
    prev = prev[prev.index.date == prev.index[-1].date()]
    hi, lo, cl = float(prev["High"].max()), float(prev["Low"].min()), float(prev["Close"].iloc[-1])
    pivot = (hi + lo + cl) / 3
    bc, tc = (hi + lo) / 2, 2 * pivot - (hi + lo) / 2
    return min(bc, tc), pivot, max(bc, tc)


def check(df: pd.DataFrame, side: str, price: float) -> str | None:
    """Why a CE / PE on the last candle of df is blocked, or None when it may trade.

    df's last candle is the signal candle (forming live, closed in the replay);
    Supertrend reads the completed candles before it."""
    if not enabled() or df is None or len(df) < 2:
        return None
    d = df.tail(HISTORY)
    if SUPERTREND:
        up = supertrend_up(d.iloc[:-1], ST_PERIOD, ST_MULT)
        if up is not None and (side == "CE") != up:
            return f"Supertrend {'up' if up else 'down'}"
    if CPR_BIAS or CPR_ZONE:
        lv = cpr(d)
        if lv:
            bc, _, tc = lv
            if CPR_BIAS and side == "CE" and price < bc:
                return f"below CPR ({bc:,.0f}–{tc:,.0f})"
            if CPR_BIAS and side == "PE" and price > tc:
                return f"above CPR ({bc:,.0f}–{tc:,.0f})"
            z = CPR_ZONE_PCT / 100
            if CPR_ZONE and side == "CE" and bc * (1 - z) <= price <= tc:
                return f"CE into CPR resistance ({bc:,.0f}–{tc:,.0f})"
            if CPR_ZONE and side == "PE" and bc <= price <= tc * (1 + z):
                return f"PE into CPR support ({bc:,.0f}–{tc:,.0f})"
    return None
