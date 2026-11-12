"""
Market structure on OHLC candles — pure functions, no Kite calls.

Swings      a swing high is a candle whose high is the highest of the N candles on each side
            (N = 2); a swing low the mirror. A swing is only known N candles later, so on data
            "up to now" the last N candles can never be swings (no look-ahead).
Trend       from the last two swing highs and lows: HH + HL = up, LH + LL = down, else range.
            A close below the last swing low in an up-trend (above the last swing high in a
            down-trend) is a change of character (CHoCH): the trend is reported as broken.
Zones       base-candle supply / demand: 1–3 small candles (body ≤ 50 % of their range, range ≤
            1 ATR) between two strong candles (range ≥ 1 ATR, body ≥ 50 % of the range).
              rally-base-rally (RBR) / drop-base-rally (DBR) → demand: base low … highest body top
              rally-base-drop (RBD) / drop-base-drop (DBD)   → supply: lowest body bottom … base high
            A zone is broken once a later candle closes beyond its far edge, and "fresh" until a
            later candle first trades back into it.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

SWING_N = 2
BASE_MAX = 3            # candles in a base
BASE_BODY = 0.5         # base candle: body ≤ this × its range …
BASE_RANGE_ATR = 1.0    # … and range ≤ this × ATR
LEG_RANGE_ATR = 1.0     # leg candle: range ≥ this × ATR …
LEG_BODY = 0.5          # … and body ≥ this × its range


@dataclass
class Swing:
    t: pd.Timestamp
    price: float
    kind: str           # "H" / "L"
    i: int


@dataclass
class Zone:
    lo: float
    hi: float
    kind: str           # "demand" / "supply"
    pattern: str        # RBR / DBR / RBD / DBD
    t: pd.Timestamp     # time of the leg-out candle
    out_i: int          # index of the leg-out candle
    strength: float     # leg-out range ÷ ATR
    touches: int = 0    # later candles that traded back into it (up to the one before the last)
    broken: bool = False

    def text(self) -> str:
        return f"{self.pattern} {self.kind} {self.lo:,.1f}–{self.hi:,.1f} ({self.t:%d %b %H:%M})"


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(n, min_periods=3).mean()


def swings(df: pd.DataFrame, n: int = SWING_N) -> list[Swing]:
    h, l = df["high"].to_numpy(float), df["low"].to_numpy(float)
    out: list[Swing] = []
    for i in range(n, len(df) - n):
        if h[i] >= h[i - n:i + n + 1].max() and h[i] > h[i - n:i].max():
            out.append(Swing(df.index[i], float(h[i]), "H", i))
        if l[i] <= l[i - n:i + n + 1].min() and l[i] < l[i - n:i].min():
            out.append(Swing(df.index[i], float(l[i]), "L", i))
    return out


def state(df: pd.DataFrame, n: int = SWING_N) -> dict:
    """Trend from swings plus the change-of-character check on the last close."""
    sw = swings(df, n)
    hs = [s for s in sw if s.kind == "H"]
    ls = [s for s in sw if s.kind == "L"]
    out = {"trend": "range", "last_high": hs[-1] if hs else None, "last_low": ls[-1] if ls else None,
           "choch": False, "swings": sw}
    if len(hs) < 2 or len(ls) < 2:
        return out
    up = hs[-1].price > hs[-2].price and ls[-1].price > ls[-2].price
    down = hs[-1].price < hs[-2].price and ls[-1].price < ls[-2].price
    c = float(df["close"].iloc[-1])
    if up:
        out["trend"] = "up"
        if c < ls[-1].price:
            out["trend"], out["choch"] = "range", True
    elif down:
        out["trend"] = "down"
        if c > hs[-1].price:
            out["trend"], out["choch"] = "range", True
    return out


def zones(df: pd.DataFrame) -> list[Zone]:
    """Unbroken base-candle zones; `touches` counts candles back into the zone, excluding the last
    candle (so a zone with touches == 0 that the last candle trades into is a first retest)."""
    if len(df) < 6:
        return []
    o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    a = atr(df).to_numpy(float)
    rng = h - l
    body = np.abs(c - o)
    small = (body <= BASE_BODY * np.maximum(rng, 1e-9)) & (rng <= BASE_RANGE_ATR * a)
    leg = (rng >= LEG_RANGE_ATR * a) & (body >= LEG_BODY * np.maximum(rng, 1e-9))
    out: list[Zone] = []
    j, n = 1, len(df)
    while j < n - 1:
        found = None
        for k in range(1, BASE_MAX + 1):
            e = j + k                                     # leg-out candle
            if e >= n or not all(small[j:e]):
                break
            if leg[j - 1] and leg[e] and not np.isnan(a[e]):
                found = e
                break
        if found is None:
            j += 1
            continue
        e = found
        up_in, up_out = c[j - 1] > o[j - 1], c[e] > o[e]
        if up_out:
            kind, pattern = "demand", "RBR" if up_in else "DBR"
            lo, hi = float(l[j:e].min()), float(np.maximum(o[j:e], c[j:e]).max())
        else:
            kind, pattern = "supply", "RBD" if up_in else "DBD"
            lo, hi = float(np.minimum(o[j:e], c[j:e]).min()), float(h[j:e].max())
        z = Zone(lo, hi, kind, pattern, df.index[e], e, round(float(rng[e] / a[e]), 2))
        for x in range(e + 1, n):
            if (c[x] < lo) if kind == "demand" else (c[x] > hi):
                z.broken = True
                break
            if x < n - 1 and ((l[x] <= hi) if kind == "demand" else (h[x] >= lo)):
                z.touches += 1
        if not z.broken:
            out.append(z)
        j = e + 1
    return out
