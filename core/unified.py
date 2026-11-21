"""
Unified index strategy — "Expansion → Pause → Go" (NIFTY / SENSEX / BANKNIFTY options). Pure functions shared by
scripts/index_bot.py (IOS_ENGINE=unified, live) and scripts/unified_backtest.py (history).

CE shown — PE is the exact mirror (S1, Supertrend down, lower band, below VWAP).
  1 day       pivots from yesterday (P, R1, R2, R3 / S1, S2, S3); trades 09:45 – 13:30
  2 trend     Supertrend(10, 2) on the last CLOSED 15-min candle is up, and the 5-min close is above VWAP
              (session average price when the candles carry no volume)
  3 location  the last closed 15-min candle closed above R1 (R1 is the floor; P – R1 is the chop zone)
  4 arm       a SUPER CANDLE on 5 min: closes outside the upper Bollinger Band (20, 2), range ≥ SUPER_X × the
              average range of the last 20 candles, body ≥ SUPER_BODY of its range, green — the move started;
              it is NOT bought
  5 trigger   within TRIGGER_BARS candles after it:
                a  inside bar(s) inside the super candle (mother) → buy when a candle CLOSES above the
                   inside-bar high; stop below the inside-bar low
                b  a bullish PIN BAR that pulled back to R1 or the middle band → buy when a candle closes above
                   the pin high; stop below the pin low
              the break must come within BREAK_BARS candles of the pattern
              cancel: a close back below R1, the 15-min Supertrend turns, the window runs out, 13:30
  6 risk      stop = pattern low − buffer; risk within RISK_PTS; target = the nearest of R2 / R3 / PDH (live:
              also the call wall) that pays ≥ MIN_RR × risk, else MIN_RR × risk — no trade when that leaves
              less than MIN_RR
  7 manage    (core/ios_strategy.manage_step) +1R → stop to entry · T1 → half booked (2 lots) and the rest trails
              under 5-min swing lows · 5-min change of character → out · plus here: a close back below R1 or the
              15-min Supertrend turning → out · 15:15 out
"""

from __future__ import annotations

import datetime

import numpy as np
import pandas as pd

from core import ios_strategy as ios
from core import pnc_strategy as ps

ST_PERIOD, ST_MULT = 10, 2.0
BB_N, BB_K = 20, 2.0
SUPER_X, SUPER_BODY = 1.5, 0.6
TRIGGER_BARS, BREAK_BARS = 6, 3
ENTRY_FROM, ENTRY_UNTIL = datetime.time(9, 45), datetime.time(13, 30)
MIN_RR = 2.0
PULLBACK_PCT = 0.10            # pin bar low within this % of R1 / the middle band counts as a pullback there
BUFFER = {"NIFTY": 5.0, "BANKNIFTY": 15.0, "SENSEX": 15.0}
RISK_PTS = {"NIFTY": (6.0, 45.0), "BANKNIFTY": (20.0, 130.0), "SENSEX": (20.0, 150.0)}


# ── levels & indicators ──────────────────────────────────────────────────────────────────

def pivots(daily: pd.DataFrame, day: datetime.date) -> dict | None:
    """Classic floor pivots from the previous trading day."""
    d = daily[[x < day for x in daily.index]]
    if d.empty:
        return None
    p = d.iloc[-1]
    h, l, c = float(p["high"]), float(p["low"]), float(p["close"])
    pp = (h + l + c) / 3
    return {"P": pp, "R1": 2 * pp - l, "S1": 2 * pp - h, "R2": pp + (h - l), "S2": pp - (h - l),
            "R3": h + 2 * (pp - l), "S3": l - 2 * (h - pp), "PDH": h, "PDL": l}


def bollinger(close: pd.Series, n: int = BB_N, k: float = BB_K) -> tuple[pd.Series, pd.Series, pd.Series]:
    mid = close.rolling(n).mean()
    sd = close.rolling(n).std(ddof=0)
    return mid, mid + k * sd, mid - k * sd


def session_vwap(m5: pd.DataFrame) -> pd.Series:
    """VWAP when the candles carry volume (futures); else the session's average typical price."""
    if "volume" in m5 and float(m5["volume"].sum()) > 0:
        return ps.vwap(m5)
    tp = (m5["high"] + m5["low"] + m5["close"]) / 3
    return tp.groupby(m5.index.date).transform(lambda s: s.expanding().mean())


def pin_bar(o: float, h: float, l: float, c: float, prev_low: float | None, prev_high: float | None) -> str | None:
    """'BULL' / 'BEAR' / None — body ≤ 33 % of the range, tail ≥ 60 %, nose ≤ 25 %, tail beyond the previous bar."""
    rng = h - l
    if rng <= 0 or abs(c - o) > 0.33 * rng:
        return None
    lower, upper = min(o, c) - l, h - max(o, c)
    if lower >= 0.6 * rng and upper <= 0.25 * rng and (prev_low is None or l < prev_low):
        return "BULL"
    if upper >= 0.6 * rng and lower <= 0.25 * rng and (prev_high is None or h > prev_high):
        return "BEAR"
    return None


def inside(h: float, l: float, mh: float, ml: float) -> bool:
    return h < mh and l > ml


# ── the setup state machine ──────────────────────────────────────────────────────────────

def frame(m5: pd.DataFrame) -> pd.DataFrame:
    """5-min candles with the Bollinger bands, average range and VWAP added (computed once)."""
    f = m5.copy()
    f["bb_mid"], f["bb_up"], f["bb_lo"] = bollinger(f["close"])
    f["avg_rng"] = (f["high"] - f["low"]).rolling(20).mean().shift(1)
    f["vwap"] = session_vwap(f)
    return f


def st15_at(m15: pd.DataFrame, t_end: pd.Timestamp) -> float | None:
    """Supertrend(10, 2) direction of the last 15-min candle CLOSED by t_end (+1 / −1)."""
    done = m15[m15.index + pd.Timedelta(minutes=15) <= t_end]
    if len(done) <= ST_PERIOD + 1:
        return None
    s = ps.supertrend(done, ST_PERIOD, ST_MULT)
    v = s.iloc[-1]
    return None if pd.isna(v) else float(v)


def last15_close(m15: pd.DataFrame, t_end: pd.Timestamp) -> float | None:
    done = m15[m15.index + pd.Timedelta(minutes=15) <= t_end]
    return float(done["close"].iloc[-1]) if len(done) else None


def step(sym: str, f: pd.DataFrame, i: int, m15: pd.DataFrame, pv: dict, state: dict) -> tuple[str | None, dict]:
    """Process the closed 5-min candle f.iloc[i] for one symbol. `state` holds the armed / pending setups (one per
    side). → ("enter", setup dict) when a trade triggers, ("info", text) for a push-worthy event, (None, {})."""
    t = f.index[i]
    t_end = t + pd.Timedelta(minutes=5)
    row = f.iloc[i]
    o, h, l, c = (float(row[x]) for x in ("open", "high", "low", "close"))
    st15 = st15_at(m15, t_end)
    c15 = last15_close(m15, t_end)
    event = None
    for side in ("CE", "PE"):
        ce = side == "CE"
        lvl = pv["R1"] if ce else pv["S1"]
        trend_ok = st15 == (1.0 if ce else -1.0)
        s = state.get(side)
        # ── an armed / pending setup: cancel, find the pattern, or trigger
        if s:
            if (c < lvl) if ce else (c > lvl):
                state.pop(side)
                event = ("info", f"{sym} {side}: cancelled — closed back {'below R1' if ce else 'above S1'} {lvl:,.1f}")
                continue
            if not trend_ok:
                state.pop(side)
                event = ("info", f"{sym} {side}: cancelled — 15-min Supertrend turned")
                continue
            if t_end.time() > ENTRY_UNTIL:
                state.pop(side)
                continue
            if s.get("pattern"):
                p = s["pattern"]
                if (c < p["stop"]) if ce else (c > p["stop"]):
                    s.pop("pattern")                                   # pattern failed; others may still come
                elif ((c > p["trigger"]) if ce else (c < p["trigger"])) and t_end.time() >= ENTRY_FROM:
                    setup = make(sym, side, p, c, t, pv, s)
                    state.pop(side)
                    if setup:
                        return "enter", setup
                    event = ("info", f"{sym} {side}: trigger at {c:,.1f} but {p['kind']} risk / reward did not fit")
                    continue
                elif i - p["i"] > BREAK_BARS:
                    s.pop("pattern")
            if not s.get("pattern"):
                if i - s["i"] > TRIGGER_BARS:
                    state.pop(side)
                    event = ("info", f"{sym} {side}: no pause / pullback pattern within {TRIGGER_BARS} candles")
                    continue
                # a) inside bar(s) in the super candle
                mh, ml = s["sc_high"], s["sc_low"]
                ibs = s.setdefault("ibs", [])
                if inside(h, l, mh, ml) and (not ibs or all(inside(x[0], x[1], mh, ml) for x in ibs)) and \
                        i == s["i"] + len(ibs) + 1:
                    ibs.append((h, l))
                    hi, lo = max(x[0] for x in ibs), min(x[1] for x in ibs)
                    s["pattern"] = {"kind": f"{len(ibs)} inside bar(s)", "i": i, "t": str(t),
                                    "trigger": hi if ce else lo, "stop": (lo if ce else hi)}
                    continue
                # b) pin bar on a pullback to R1 / the middle band
                prev = f.iloc[i - 1]
                pin = pin_bar(o, h, l, c, float(prev["low"]), float(prev["high"]))
                mid = float(row["bb_mid"]) if not pd.isna(row["bb_mid"]) else None
                if pin == ("BULL" if ce else "BEAR"):
                    ref = [lvl] + ([mid] if mid else [])
                    tol = c * PULLBACK_PCT / 100
                    near = any(abs((l if ce else h) - r) <= tol or ((l <= r) if ce else (h >= r)) for r in ref)
                    if near:
                        s["pattern"] = {"kind": "pin bar on the pullback", "i": i, "t": str(t),
                                        "trigger": h if ce else l, "stop": l if ce else h}
            continue
        # ── not armed: is this candle a super candle in a valid context?
        if not trend_ok or c15 is None or pd.isna(row["bb_up"]) or pd.isna(row["avg_rng"]):
            continue
        if not ((c15 > lvl) if ce else (c15 < lvl)):
            continue
        if not ((c > lvl and c > row["vwap"]) if ce else (c < lvl and c < row["vwap"])):
            continue
        rng = h - l
        big = rng >= SUPER_X * float(row["avg_rng"]) and abs(c - o) >= SUPER_BODY * rng
        outside = (c > float(row["bb_up"]) and c > o) if ce else (c < float(row["bb_lo"]) and c < o)
        if big and outside and t_end.time() <= ENTRY_UNTIL:
            state[side] = {"i": i, "t": str(t), "sc_high": h, "sc_low": l, "level": lvl}
            event = ("info", f"{sym} {side}: SUPER CANDLE {t_end:%H:%M} closed {'above the upper' if ce else 'below the lower'} "
                             f"band, {'above R1' if ce else 'below S1'} {lvl:,.1f}, 15-min Supertrend "
                             f"{'up' if ce else 'down'} — armed, waiting for an inside bar / pin bar")
    return (event[0], {"text": event[1]}) if event else (None, {})


def make(sym: str, side: str, p: dict, entry: float, t: pd.Timestamp, pv: dict, s: dict,
         wall: float | None = None) -> dict | None:
    """Entry → stop / target. None when the risk is out of range or no target pays MIN_RR."""
    ce = side == "CE"
    buf = BUFFER.get(sym, 5.0)
    stop = p["stop"] - buf if ce else p["stop"] + buf
    risk = (entry - stop) if ce else (stop - entry)
    lo, hi = RISK_PTS.get(sym, (5.0, 50.0))
    if not lo <= risk <= hi:
        return None
    cands = [("R2", pv["R2"]), ("R3", pv["R3"]), ("PDH", pv["PDH"])] if ce else \
        [("S2", pv["S2"]), ("S3", pv["S3"]), ("PDL", pv["PDL"])]
    if wall:
        cands.append(("OI wall", wall))
    ahead = sorted([(lab, x) for lab, x in cands if (x > entry if ce else x < entry)],
                   key=lambda z: abs(z[1] - entry))
    target, label = None, None
    for lab, x in ahead:
        if abs(x - entry) >= MIN_RR * risk:
            target, label = x, lab
            break
    if target is None:
        target, label = (entry + MIN_RR * risk if ce else entry - MIN_RR * risk), f"{MIN_RR:g}R"
    return {"side": side, "kind": f"U {p['kind']}", "level": f"{'R1' if ce else 'S1'} {s['level']:,.1f}",
            "entry": entry, "stop": stop, "target": target, "target_label": label, "t": t,
            "risk": risk, "rr": abs(target - entry) / risk}


def to_setup(sym: str, d: dict) -> ios.Setup:
    return ios.Setup(sym, d["side"], d["kind"], d["level"], d["entry"], d["stop"], d["target"], d["target_label"],
                     d["t"], [f"super candle → {d['kind']}"])


def extra_exit(tr: dict, f: pd.DataFrame, m15: pd.DataFrame, pv: dict) -> str | None:
    """Unified exits on top of ios.manage_step: a 5-min close back past R1 / S1, or the 15-min Supertrend turning."""
    ce = tr["side"] == "CE"
    c = float(f["close"].iloc[-1])
    lvl = pv["R1"] if ce else pv["S1"]
    if (c < lvl) if ce else (c > lvl):
        return f"closed back {'below R1' if ce else 'above S1'} {lvl:,.1f}"
    st = st15_at(m15, f.index[-1] + pd.Timedelta(minutes=5))
    if st is not None and st != (1.0 if ce else -1.0):
        return "15-min Supertrend turned"
    return None


def live_step(sym: str, m5: pd.DataFrame, m15: pd.DataFrame, daily: pd.DataFrame, day: datetime.date,
              state: dict) -> tuple[str | None, dict]:
    """The live bot's call: process the LAST closed 5-min candle. State positions are stored as candle times
    (JSON-safe) and turned back into row numbers here."""
    pv = pivots(daily, day)
    if pv is None or m5 is None or len(m5) < BB_N + 2:
        return None, {}
    f = frame(m5)
    for s in state.values():
        for d in (s, s.get("pattern") or {}):
            if "t" in d:
                try:
                    d["i"] = int(f.index.get_loc(pd.Timestamp(d["t"])))
                except KeyError:
                    pass
        if "ibs" in s:
            s["ibs"] = [tuple(x) for x in s["ibs"]]
    return step(sym, f, len(f) - 1, m15, pv, state)
