"""
Entry refinements for the move-START trigger (screener/oi_start.py). All off unless switched on in .env:

OI_ENTRY=retest   a START no longer buys at once: it is ARMED. On the following scans it is bought only when
                  the stock has pulled back to within RETEST_PCT of the level it broke (still on the right side)
                  AND the option writers are building a new floor there — CE: put OI at the strike at / below the
                  level up ≥ FLOOR_PCT over ~10 min (PE: call OI at the strike at / above it). Cancelled when the
                  stock is back past the level (the breakout failed — no money lost), after RETEST_MIN minutes
                  without a pullback, or from RETEST_UNTIL.
OI_EXHAUST=1      no buy when the squeeze is used up: a call wall the price just crossed (CE; put wall for PE)
                  has already lost ≥ EXHAUST_DROP % of today's peak OI and the last ~10 min are flat (covering
                  done), or the next wall is within NEXT_WALL_PCT of the price (no room).
OI_PREMIUM_CHECK=1 no buy when the ATM option's premium rose ≥ PREM_MIN % in ~10 min and more than PREM_X × what
                  the stock's move explains (delta ~0.5): the premium is inflated and will deflate.
"""

from __future__ import annotations

import datetime

import pandas as pd

RETEST_PCT = 0.15
RETEST_MIN = 45
RETEST_UNTIL = "1300"
FLOOR_PCT = 1.0
EXHAUST_DROP = 40.0
EXHAUST_FLAT = 1.0
CROSSED_PCT = 1.0          # walls the price crossed: strikes within this % behind the price
NEXT_WALL_PCT = 0.5
PREM_MIN = 10.0
PREM_X = 2.0


def minutes(a_hm: str, b_hm: str) -> int:
    a = datetime.datetime.strptime(a_hm, "%H%M")
    b = datetime.datetime.strptime(b_hm, "%H%M")
    return int((b - a).total_seconds() // 60)


def _oi(g: pd.DataFrame | None, typ: str, k: float) -> float | None:
    if g is None or g.empty:
        return None
    r = g[(g["type"] == typ) & (g["strike"] == k)]
    return float(r["oi"].sum()) if len(r) else None


def floor_strike(g: pd.DataFrame, side: str, level: float) -> float | None:
    strikes = sorted(g["strike"].unique())
    if not strikes:
        return None
    if side == "CE":
        below = [k for k in strikes if k <= level]
        return below[-1] if below else strikes[0]
    above = [k for k in strikes if k >= level]
    return above[0] if above else strikes[-1]


def floor_building(side: str, level: float, g: pd.DataFrame, gp: pd.DataFrame | None,
                   pct: float | None = None) -> tuple[bool, str]:
    """CE: put writers adding at the strike at / below the broken level (a new floor); PE: call writers above."""
    pct = FLOOR_PCT if pct is None else pct
    k = floor_strike(g, side, level)
    typ = "PE" if side == "CE" else "CE"
    a, b = _oi(g, typ, k), _oi(gp, typ, k)
    if k is None or not a or not b:
        return False, "no OI to compare"
    chg = (a / b - 1) * 100
    what = "puts" if side == "CE" else "calls"
    return chg >= pct, f"{what} at {k:g} {chg:+.1f}% in 10 min"


def retest_step(arm: dict, hm: str, g: pd.DataFrame, gp: pd.DataFrame | None) -> tuple[str | None, str]:
    """One scan for an ARMED start → ("buy" | "cancel" | None, reason). arm: side, level, hm (when armed)."""
    if g is None or g.empty:
        return None, "stock not in this snapshot"
    ce = arm["side"] == "CE"
    level = arm["level"]
    spot = float(g["spot"].iloc[0])
    if (spot < level) if ce else (spot > level):
        return "cancel", f"breakout failed — {spot:g} back {'below' if ce else 'above'} {level:g}, no trade"
    if hm >= RETEST_UNTIL:
        return "cancel", f"{RETEST_UNTIL[:2]}:{RETEST_UNTIL[2:]} — no pullback buy this late"
    if minutes(arm["hm"], hm) > RETEST_MIN:
        return "cancel", f"no pullback to {level:g} within {RETEST_MIN} min"
    off = (spot / level - 1) * 100 if ce else (1 - spot / level) * 100
    if off > RETEST_PCT:
        return None, f"waiting — {off:.2f}% above the level" if ce else f"waiting — {off:.2f}% below the level"
    ok, txt = floor_building(arm["side"], level, g, gp)
    if not ok:
        return None, f"at the level but no new floor ({txt})"
    return "buy", f"pulled back to {level:g} (now {spot:g}, {off:.2f}% away) · {txt} — new floor"


def day_peaks(frames: list[pd.DataFrame]) -> dict:
    """(strike, type) → highest OI today, from that stock's rows in today's snapshots so far."""
    if not frames:
        return {}
    allr = pd.concat(frames)
    return allr.groupby(["strike", "type"])["oi"].max().to_dict()


def exhausted(side: str, g: pd.DataFrame, gp: pd.DataFrame | None, peaks: dict) -> str | None:
    """Why the squeeze looks used up (or None)."""
    if g is None or g.empty:
        return None
    ce = side == "CE"
    spot = float(g["spot"].iloc[0])
    typ = "CE" if ce else "PE"
    rows = g[g["type"] == typ]
    ahead = rows[rows["strike"] > spot] if ce else rows[rows["strike"] < spot]
    if len(ahead) and ahead["oi"].max() > 0:
        wall = float(ahead.loc[ahead["oi"].idxmax(), "strike"])
        dist = abs(wall / spot - 1) * 100
        if dist < NEXT_WALL_PCT:
            return f"next {'call' if ce else 'put'} wall {wall:g} only {dist:.2f}% away — no room"
    crossed = rows[(rows["strike"] <= spot) & (rows["strike"] >= spot * (1 - CROSSED_PCT / 100))] if ce else \
        rows[(rows["strike"] >= spot) & (rows["strike"] <= spot * (1 + CROSSED_PCT / 100))]
    for _, r in crossed.iterrows():
        k, now = float(r["strike"]), float(r["oi"])
        peak = peaks.get((k, typ)) or 0
        if peak <= 0:
            continue
        drop = (1 - now / peak) * 100
        prev = _oi(gp, typ, k)
        last = (now / prev - 1) * 100 if prev else 0.0
        if drop >= EXHAUST_DROP and last > -EXHAUST_FLAT:
            return (f"{'call' if ce else 'put'} wall {k:g} already covered {drop:.0f}% from today's peak and the "
                    f"covering stopped ({last:+.1f}% in 10 min) — squeeze used up")
    return None


def premium_inflated(side: str, strike: float, g: pd.DataFrame, gp: pd.DataFrame | None) -> str | None:
    """Why the option's premium looks inflated (or None)."""
    if g is None or gp is None or g.empty or gp.empty:
        return None
    a = g[(g["type"] == side) & (g["strike"] == strike)]
    b = gp[(gp["type"] == side) & (gp["strike"] == strike)]
    if a.empty or b.empty:
        return None
    pa, pb = float(a.iloc[0]["ltp"] or 0), float(b.iloc[0]["ltp"] or 0)
    if pb <= 0 or pa <= 0:
        return None
    ds = (float(g["spot"].iloc[0]) - float(gp["spot"].iloc[0])) * (1 if side == "CE" else -1)
    dp = pa - pb
    rise = dp / pb * 100
    expected = 0.5 * max(ds, 0.0)
    if rise >= PREM_MIN and dp > PREM_X * expected:
        return (f"premium {pb:g} → {pa:g} ({rise:+.0f}%) in 10 min while the stock moved {ds:+.2f} "
                f"(≈ {expected:+.2f} expected) — inflated")
    return None
