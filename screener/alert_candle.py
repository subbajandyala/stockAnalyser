"""
5 EMA "alert candle" rules (price action, from the 5 EMA alert-candle videos) for the OI monitor.

Alert candle  a candle that does not touch the 5 EMA: entirely above it (low > EMA) = an UP alert
              (the move up is stretched), entirely below it (high < EMA) = a DOWN alert.

1 exit (opposite alert) — for an open trade, watch the 5-min candles after the entry:
    long (CE) trade: wait for an UP alert. While price keeps going up the alert moves with it —
    a later candle that makes a higher high, or forms inside the alert (inside bar / "mother-baby"),
    becomes the new alert. A candle that trades below the alert's low = the move has turned → exit.
    short (PE) trade: the mirror (DOWN alert, exit when a candle trades above its high).
2 don't chase — skip a new entry when the last closed 5-min candle is already an alert candle in the
    trade's direction (CE: entirely above the 5 EMA) or RSI(14) is stretched (CE > RSI_HI, PE < RSI_LO).
"""

from __future__ import annotations

import pandas as pd

EMA_N = 5
RSI_N = 14
RSI_HI, RSI_LO = 70.0, 30.0


def ema(close: pd.Series, n: int = EMA_N) -> pd.Series:
    return close.ewm(span=n, adjust=False).mean()


def rsi(close: pd.Series, n: int = RSI_N) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, float("nan"))
    return (100 - 100 / (1 + rs)).fillna(100.0)


def exit_breaks(m5: pd.DataFrame, side: str, since: pd.Timestamp) -> list:
    """Every opposite-alert break from `since` on (after a break the hunt for the next alert starts
    again) → list of candle times."""
    out, start = [], since
    while True:
        s = exit_signal(m5, side, start)
        if not s:
            return out
        out.append(s["time"])
        start = s["time"] + pd.Timedelta(minutes=1)


def exit_signal(m5: pd.DataFrame, side: str, since: pd.Timestamp) -> dict | None:
    """Opposite-alert exit on closed 5-min candles (OHLC, time index). Only candles from `since` on
    (the entry candle) are walked; the EMA uses the whole frame. → {"time", "price", "alert"} of the
    candle that broke the alert (price = the alert level it broke), or None."""
    if m5 is None or len(m5) < EMA_N + 1:
        return None
    e = ema(m5["close"])
    h, l = m5["high"].to_numpy(float), m5["low"].to_numpy(float)
    idx = m5.index
    long = side == "CE"
    alert = None                                       # (high, low, time)
    for i in range(len(m5)):
        if idx[i] < since:
            continue
        if alert is None:
            if (l[i] > e.iloc[i]) if long else (h[i] < e.iloc[i]):
                alert = (h[i], l[i], idx[i])
            continue
        ah, al, at = alert
        if long and l[i] < al:
            return {"time": idx[i], "price": al, "alert": at}
        if not long and h[i] > ah:
            return {"time": idx[i], "price": ah, "alert": at}
        if (h[i] > ah) if long else (l[i] < al):        # the move goes on: the alert moves with it
            alert = (h[i], l[i], idx[i])
        elif h[i] <= ah and l[i] >= al:                  # inside candle: the "baby" is the alert
            alert = (h[i], l[i], idx[i])
    return None


def stretched(m5: pd.DataFrame, side: str) -> str | None:
    """Why a new entry on `side` would be chasing (last closed candle), or None."""
    if m5 is None or len(m5) < RSI_N + 2:
        return None
    e = float(ema(m5["close"]).iloc[-1])
    r = float(rsi(m5["close"]).iloc[-1])
    c = m5.iloc[-1]
    if side == "CE":
        if float(c["low"]) > e:
            return f"5-min candle entirely above the 5 EMA ({float(c['low']):g} > {e:.2f}) — already stretched"
        if r > RSI_HI:
            return f"RSI {r:.0f} > {RSI_HI:g}"
    else:
        if float(c["high"]) < e:
            return f"5-min candle entirely below the 5 EMA ({float(c['high']):g} < {e:.2f}) — already stretched"
        if r < RSI_LO:
            return f"RSI {r:.0f} < {RSI_LO:g}"
    return None
