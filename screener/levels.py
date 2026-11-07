"""
Support / resistance levels from the last days, for the OI monitor's order decision.

A signal that points straight into a level from the last 1–2 days tends to fail (BHEL PE onto
the 400 CPR of 4 Nov, BAJFINANCE into its 1040–1047 zone). Levels built here:

  CPR           today's (from yesterday's H / L / C), day −1's and day −2's — pivot, TC, BC
  day high/low  day −1 and day −2
  opening range today's first 15-min candle (only after it has closed)
  zones         15-min consolidations of the last 2 days: ≥ ZONE_BARS candles in a row whose
                whole range is ≤ ZONE_PCT % of the price
  OI walls      put wall (support) / call wall (resistance), passed in by the caller
Levels / zones closer than MERGE_PCT % are merged.

check(): a PE is blocked by support at most LEVEL_PCT % below the price, or price inside a zone;
a CE by resistance at most LEVEL_PCT % above. broke() / held(): the wait-for-break decision on
closed 10-min candles.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

import pandas as pd

LEVEL_PCT = 1.0       # a level this close (%) blocks the order
MERGE_PCT = 0.2       # levels closer than this are one zone
ZONE_BARS = 4         # consolidation: at least this many 15-min candles in a row …
ZONE_PCT = 0.6        # … whose whole range is at most this % of the price
BREAK_PCT = 0.1       # a break = a 10-min close this far beyond the level
CANCEL_PCT = 1.0      # the level held = price this far away from it the wrong way


@dataclass
class Level:
    lo: float
    hi: float
    label: str

    @property
    def mid(self) -> float:
        return (self.lo + self.hi) / 2

    def text(self) -> str:
        p = f"{self.lo:g}" if abs(self.hi - self.lo) < 1e-9 else f"{self.lo:g}–{self.hi:g}"
        return f"{p} ({self.label})"


def cpr(h: float, l: float, c: float) -> tuple[float, float, float]:
    piv = (h + l + c) / 3
    bc = (h + l) / 2
    return piv, 2 * piv - bc, bc


def build(daily: pd.DataFrame, m15: pd.DataFrame, day: datetime.date, upto: datetime.datetime | None,
          call_wall: float | None = None, put_wall: float | None = None, days: int = 2) -> list[Level]:
    """Levels known at `upto` on `day`. daily: OHLC by date (index = dates before `day` are used);
    m15: 15-min OHLC with an IST-naive DatetimeIndex."""
    out: list[Level] = []
    daily = daily.copy()
    daily.index = [d.date() if isinstance(d, datetime.datetime) else d for d in daily.index]  # Kite: Timestamps
    prev = daily[[d < day for d in daily.index]].tail(days + 1)      # D-3 … D-1
    rows = list(prev.itertuples())
    # CPR shown on today and the previous `days` days: each from the day before it
    names = ["today"] + [f"{r.Index:%d %b}" for r in reversed(rows[1:])]
    for k, src in enumerate(reversed(rows)):
        if k > days:
            break
        piv, tc, bc = cpr(float(src.high), float(src.low), float(src.close))
        label = "CPR " + (names[k] if k < len(names) else "")
        lo, hi = min(tc, bc), max(tc, bc)
        out.append(Level(round(lo, 2), round(hi, 2), label.strip()))
    for r in rows[-days:]:
        out.append(Level(float(r.high), float(r.high), f"high {r.Index:%d %b}"))
        out.append(Level(float(r.low), float(r.low), f"low {r.Index:%d %b}"))
    if m15 is not None and len(m15):
        today = m15[m15.index.date == day]
        first = today.iloc[:1]
        if len(first) and (upto is None or first.index[0] + datetime.timedelta(minutes=15) <= upto):
            out.append(Level(float(first["high"].iloc[0]), float(first["high"].iloc[0]), "opening high"))
            out.append(Level(float(first["low"].iloc[0]), float(first["low"].iloc[0]), "opening low"))
        past_days = sorted({d for d in m15.index.date if d < day})[-days:]
        for z in zones(m15[[d in past_days for d in m15.index.date]]):
            out.append(z)
    if call_wall:
        out.append(Level(call_wall, call_wall, "call wall"))
    if put_wall:
        out.append(Level(put_wall, put_wall, "put wall"))
    return merge(out)


def zones(m15: pd.DataFrame) -> list[Level]:
    """15-min consolidations: ZONE_BARS+ candles in a row inside a band ≤ ZONE_PCT % wide."""
    out, h, l, idx = [], m15["high"].to_numpy(float), m15["low"].to_numpy(float), m15.index
    i, n = 0, len(m15)
    while i < n:
        hi, lo, j = h[i], l[i], i + 1
        while j < n and idx[j].date() == idx[i].date():
            nh, nl = max(hi, h[j]), min(lo, l[j])
            if (nh - nl) / nl * 100 > ZONE_PCT:
                break
            hi, lo, j = nh, nl, j + 1
        if j - i >= ZONE_BARS:
            out.append(Level(round(lo, 2), round(hi, 2), f"zone {idx[i]:%d %b %H:%M}"))
            i = j
        else:
            i += 1
    return out


def merge(levels: list[Level]) -> list[Level]:
    levels = sorted(levels, key=lambda z: z.lo)
    out: list[Level] = []
    for z in levels:
        if out and (z.lo - out[-1].hi) / max(out[-1].hi, 1e-9) * 100 <= MERGE_PCT:
            last = out[-1]
            out[-1] = Level(min(last.lo, z.lo), max(last.hi, z.hi),
                            last.label if z.label in last.label else f"{last.label} + {z.label}")
        else:
            out.append(z)
    return out


def check(side: str, price: float, levels: list[Level], pct: float = LEVEL_PCT) -> Level | None:
    """The level that blocks a CE / PE at `price`, or None when there is room."""
    near = None
    for z in levels:
        if z.lo <= price <= z.hi:
            return z                                    # inside a level / zone
        if side == "PE" and z.hi < price and (price - z.hi) / price * 100 <= pct:
            near = z if near is None or z.hi > near.hi else near        # nearest support below
        if side == "CE" and z.lo > price and (z.lo - price) / price * 100 <= pct:
            near = z if near is None or z.lo < near.lo else near        # nearest resistance above
    return near


def broke(side: str, z: Level, close: float) -> bool:
    return close < z.lo * (1 - BREAK_PCT / 100) if side == "PE" else close > z.hi * (1 + BREAK_PCT / 100)


def held(side: str, z: Level, close: float) -> bool:
    """The level held: price has moved CANCEL_PCT % away from it the wrong way."""
    return close > z.hi * (1 + CANCEL_PCT / 100) if side == "PE" else close < z.lo * (1 - CANCEL_PCT / 100)


def gap_into(side: str, day_open: float, prev_high: float, prev_low: float, blocked: Level | None) -> bool:
    """Opened beyond yesterday's range straight into a level (PE: gap down onto support)."""
    if blocked is None:
        return False
    return day_open < prev_low if side == "PE" else day_open > prev_high
