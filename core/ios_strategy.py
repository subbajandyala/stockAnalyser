"""
Index options — option OI + market structure (NIFTY, BANKNIFTY, SENSEX). Pure functions shared by
scripts/index_bot.py (live) and scripts/ios_backtest.py (structure-only history test).

Bias      15-min structure (core/structure.py): CE only when it is not a down-trend, PE only when
          not an up-trend; setup A needs the trend itself.
Setups    on the last CLOSED 5-min candle (CE shown; PE is the mirror):
  A pullback   15-min up-trend · the candle trades into a FRESH demand zone (5-min or 15-min base
               candles), closes back above it as a bullish rejection (green, close in the upper
               half) · stop = below the zone / candle low − buffer
  B breakout   a level (last confirmed 5-min swing high, PDH, opening-range high) broken by a close
               ≥ BREAK_PCT beyond it within the last RETEST_BARS candles, every close since on the
               right side, and the candle retests it (low within RETEST_PCT) and closes back above
               as a bullish rejection · stop = candle low − buffer
Target    the nearest obstacle ahead: fresh supply zone, PDH / R1, 5 / 15-min swing high, the OI
          call wall (PE: demand, PDL / S1, swing lows, put wall). It must be ≥ MIN_RR × risk away,
          else no trade. Nothing ahead → 2R.
Risk      entry → stop in index points within RISK_PTS, else no trade.
OI        (live only) near-ATM option OI over the last ~15 min + futures buildup:
            CE: calls covering · puts added · futures long buildup / short covering · PCR rising
            needs 2 of 4, and no "OI against" (calls added ≥ AGAINST_PCT while puts don't add)
Manage    each closed 5-min candle: 15:15 out · close beyond the stop → out · +1R → stop to entry
          · T1 → whole lot out (1 lot) or half booked and the rest trails under each new 5-min
          swing low − buffer (2 lots) · a close below the last 5-min swing low formed after entry
          (change of character) → out · OI against → out.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field

import pandas as pd

from core import pnc_strategy as ps
from core import structure as stc

BUFFER = {"NIFTY": 10.0, "BANKNIFTY": 30.0, "SENSEX": 40.0}
RISK_PTS = {"NIFTY": (10.0, 45.0), "BANKNIFTY": (30.0, 130.0), "SENSEX": (35.0, 150.0)}
MIN_RR = 1.5
NO_TARGET_RR = 2.0
BREAK_PCT, RETEST_PCT, RETEST_BARS = 0.03, 0.08, 12
ENTRY_FROM, ENTRY_UNTIL, SQUARE_OFF = datetime.time(9, 30), datetime.time(14, 30), datetime.time(15, 15)
AGAINST_PCT = 3.0
OI_NEED = 2


@dataclass
class Setup:
    sym: str
    side: str            # CE / PE
    kind: str            # "A pullback" / "B breakout"
    level: str           # what was tested
    entry: float
    stop: float
    target: float
    target_label: str
    t: pd.Timestamp
    notes: list = field(default_factory=list)

    @property
    def risk(self) -> float:
        return abs(self.entry - self.stop)

    @property
    def rr(self) -> float:
        return abs(self.target - self.entry) / self.risk if self.risk else 0.0


def _reject(side: str, o: float, h: float, l: float, c: float) -> bool:
    if side == "CE":
        return c > o and c >= l + 0.5 * (h - l)
    return c < o and c <= h - 0.5 * (h - l)


def opening_range(today5: pd.DataFrame) -> tuple[float, float] | None:
    first = today5[today5.index.time < datetime.time(9, 30)]
    if len(first) < 3:
        return None
    return float(first["high"].max()), float(first["low"].min())


def obstacles(side: str, price: float, lv: ps.Levels | None, sw: list, zs: list, wall: float | None) -> list:
    """Levels ahead of the trade: (price, label)."""
    out = []
    ce = side == "CE"
    if lv:
        out += [(lv.pdh, "PDH"), (lv.r1, "R1")] if ce else [(lv.pdl, "PDL"), (lv.s1, "S1")]
    out += [(s.price, f"swing {'high' if ce else 'low'} {s.t:%d %b %H:%M}") for s in sw
            if s.kind == ("H" if ce else "L")]
    out += [((z.lo if ce else z.hi), z.text()) for z in zs if z.kind == ("supply" if ce else "demand")]
    if wall:
        out.append((wall, "call wall" if ce else "put wall"))
    gap = price * 0.0003
    return sorted([x for x in out if (x[0] > price + gap if ce else x[0] < price - gap)],
                  key=lambda x: abs(x[0] - price))


def find_setups(sym: str, m5: pd.DataFrame, m15: pd.DataFrame, daily: pd.DataFrame, day: datetime.date,
                walls: tuple = (None, None)) -> tuple[list[Setup], list[str]]:
    """Setups on the last closed 5-min candle → (valid setups, reasons others were dropped)."""
    today5 = m5[m5.index.date == day]
    if len(today5) < 4:
        return [], []
    o, h, l, c = (float(today5[k].iloc[-1]) for k in ("open", "high", "low", "close"))
    t = today5.index[-1]
    if not (ENTRY_FROM <= (t + pd.Timedelta(minutes=5)).time() <= ENTRY_UNTIL):
        return [], []
    buf = BUFFER.get(sym, 10.0)
    lo_r, hi_r = RISK_PTS.get(sym, (10.0, 45.0))
    d15 = m15[m15.index.date >= day - datetime.timedelta(days=6)]
    st15 = stc.state(d15)
    recent5 = m5[m5.index.date >= day - datetime.timedelta(days=3)]
    sw5 = stc.swings(recent5)
    zs = [z for z in stc.zones(recent5) + stc.zones(d15)]
    lv = ps.levels(daily, day)
    orr = opening_range(today5)
    out, why = [], []

    def add(side: str, kind: str, label: str, stop: float, notes: list) -> None:
        obs = obstacles(side, c, lv, sw5 + st15["swings"], zs, walls[0] if side == "CE" else walls[1])
        if obs:
            tgt, tl = obs[0]
        else:
            tgt, tl = (c + NO_TARGET_RR * abs(c - stop) if side == "CE" else c - NO_TARGET_RR * abs(c - stop),
                       f"no level ahead → {NO_TARGET_RR:g}R")
        s = Setup(sym, side, kind, label, c, stop, tgt, tl, t, notes + [f"15m {st15['trend']}"])
        if not lo_r <= s.risk <= hi_r:
            why.append(f"{side} {kind} at {label}: risk {s.risk:.0f} pts outside {lo_r:g}–{hi_r:g}")
        elif s.rr < MIN_RR:
            why.append(f"{side} {kind} at {label}: target {tl} only {s.rr:.1f}R away")
        else:
            out.append(s)

    for side in ("CE", "PE"):
        ce = side == "CE"
        if st15["trend"] == ("down" if ce else "up"):
            continue
        if not _reject(side, o, h, l, c):
            continue
        # A — pullback into a fresh zone in the 15-min trend
        if st15["trend"] == ("up" if ce else "down"):
            for z in zs:
                if z.kind != ("demand" if ce else "supply") or z.touches:
                    continue
                if ce and l <= z.hi and c > z.hi and l >= z.lo - buf:
                    add(side, "A pullback", z.text(), min(z.lo, l) - buf, [f"zone strength {z.strength:g}"])
                    break
                if not ce and h >= z.lo and c < z.lo and h <= z.hi + buf:
                    add(side, "A pullback", z.text(), max(z.hi, h) + buf, [f"zone strength {z.strength:g}"])
                    break
        # B — breakout of a level, retest on this candle
        levels = {}
        prior = [s for s in sw5 if s.kind == ("H" if ce else "L") and s.t < t - pd.Timedelta(minutes=10)]
        if prior:
            levels[f"5m swing {'high' if ce else 'low'} {prior[-1].t:%H:%M}"] = prior[-1].price
        if lv:
            levels["PDH" if ce else "PDL"] = lv.pdh if ce else lv.pdl
        if orr:
            levels["opening-range " + ("high" if ce else "low")] = orr[0] if ce else orr[1]
        closes = today5["close"].to_numpy(float)
        last = len(closes) - 1
        for label, lvl in levels.items():
            hit = None
            for i in range(max(1, last - RETEST_BARS), last):
                if ce and closes[i] >= lvl * (1 + BREAK_PCT / 100) and closes[i - 1] <= lvl:
                    hit = i
                if not ce and closes[i] <= lvl * (1 - BREAK_PCT / 100) and closes[i - 1] >= lvl:
                    hit = i
            if hit is None:
                continue
            held = all((x >= lvl) if ce else (x <= lvl) for x in closes[hit + 1:last])
            touch = (l <= lvl * (1 + RETEST_PCT / 100) and l >= lvl - buf and c > lvl) if ce else \
                    (h >= lvl * (1 - RETEST_PCT / 100) and h <= lvl + buf and c < lvl)
            if held and touch:
                add(side, "B breakout", f"{label} {lvl:,.1f} (broken {today5.index[hit]:%H:%M})",
                    (l - buf) if ce else (h + buf), [])
                break
    return out, why


# ── OI confirmation (live) ─────────────────────────────────────────────────────────────────

def chain_view(now: pd.DataFrame, base: pd.DataFrame | None, recent: pd.DataFrame | None, spot: float,
               recent_spot: float | None) -> dict:
    """ps.chain_read (walls, PCR, change vs yesterday) + near-ATM change over the last ~15 min."""
    out = ps.chain_read(now, base, recent, spot) if not now.empty else {}
    if now.empty or recent is None or recent.empty:
        return out
    strikes = sorted(now["strike"].unique())
    i = min(range(len(strikes)), key=lambda j: abs(strikes[j] - spot))
    near = set(strikes[max(0, i - 3): i + 4])

    def chg(typ: str) -> tuple[float, float]:
        a = now[(now["type"] == typ) & now["strike"].isin(near)].set_index("strike")["oi"]
        b = recent[(recent["type"] == typ) & recent["strike"].isin(near)].set_index("strike")["oi"]
        j = pd.concat([a, b], axis=1, keys=["a", "b"]).dropna()
        d = float((j["a"] - j["b"]).sum())
        return d, (d / float(j["b"].sum()) * 100 if j["b"].sum() else 0.0)

    out["c_rec"], out["c_rec_pct"] = chg("CE")
    out["p_rec"], out["p_rec_pct"] = chg("PE")
    return out


def oi_check(side: str, cv: dict, fut: str | None) -> tuple[int, list, bool]:
    """(points out of 4, reasons, OI against the trade)."""
    if "c_rec" not in cv:
        return 0, ["no chain from ~15 min ago yet"], False
    ce = side == "CE"
    pts = []
    if (cv["c_rec"] < 0) if ce else (cv["p_rec"] < 0):
        pts.append(f"{'calls' if ce else 'puts'} covering {cv['c_rec_pct' if ce else 'p_rec_pct']:+.1f}%")
    if (cv["p_rec"] > 0) if ce else (cv["c_rec"] > 0):
        pts.append(f"{'puts' if ce else 'calls'} added {cv['p_rec_pct' if ce else 'c_rec_pct']:+.1f}%")
    if fut in (("long buildup", "short covering") if ce else ("short buildup", "long unwinding")):
        pts.append(f"futures {fut}")
    if cv.get("pcr") is not None and cv.get("pcr_earlier") is not None and \
            ((cv["pcr"] > cv["pcr_earlier"]) if ce else (cv["pcr"] < cv["pcr_earlier"])):
        pts.append(f"PCR {cv['pcr_earlier']} → {cv['pcr']}")
    against = (cv["c_rec_pct"] >= AGAINST_PCT and cv["p_rec"] <= 0) if ce else \
              (cv["p_rec_pct"] >= AGAINST_PCT and cv["c_rec"] <= 0)
    return len(pts), pts, against


def fut_label(price_chg: float, oi_chg: float) -> str | None:
    if oi_chg == 0:
        return None
    if price_chg >= 0:
        return "long buildup" if oi_chg > 0 else "short covering"
    return "short buildup" if oi_chg > 0 else "long unwinding"


# ── trade management ───────────────────────────────────────────────────────────────────────

def new_trade(s: Setup, lots: int) -> dict:
    return {"sym": s.sym, "side": s.side, "kind": s.kind, "level": s.level, "entry": s.entry, "stop": s.stop,
            "stop0": s.stop, "target": s.target, "target_label": s.target_label, "risk": s.risk,
            "t_entry": str(s.t), "lots": lots, "be": False, "booked": False, "best": s.entry}


def manage_step(tr: dict, m5: pd.DataFrame, oi_against: bool = False) -> tuple[str | None, str]:
    """One closed 5-min candle → (None | "book_half" | "exit", reason). Updates stop / best in tr."""
    ce = tr["side"] == "CE"
    bar = m5.iloc[-1]
    t = m5.index[-1]
    h, l, c = float(bar["high"]), float(bar["low"]), float(bar["close"])
    if (t + pd.Timedelta(minutes=5)).time() >= SQUARE_OFF:
        return "exit", "15:15 square-off"
    if (c < tr["stop"]) if ce else (c > tr["stop"]):
        return "exit", ("trail stop" if tr["booked"] else "breakeven stop" if tr["be"] else "structure stop") + \
            f" {tr['stop']:,.1f} (close {c:,.1f})"
    tr["best"] = max(tr["best"], h) if ce else min(tr["best"], l)
    if (tr["best"] >= tr["target"]) if ce else (tr["best"] <= tr["target"]):
        if not tr["booked"]:
            tr["booked"] = True
            if tr["lots"] >= 2:
                tr["stop"] = max(tr["stop"], tr["entry"]) if ce else min(tr["stop"], tr["entry"])
                return "book_half", f"T1 {tr['target']:,.1f} ({tr['target_label']})"
            return "exit", f"T1 {tr['target']:,.1f} ({tr['target_label']})"
    if not tr["be"] and ((tr["best"] >= tr["entry"] + tr["risk"]) if ce else (tr["best"] <= tr["entry"] - tr["risk"])):
        tr["be"] = True
        tr["stop"] = max(tr["stop"], tr["entry"]) if ce else min(tr["stop"], tr["entry"])
    after = [s for s in stc.swings(m5[m5.index.date == t.date()])
             if s.kind == ("L" if ce else "H") and s.t > pd.Timestamp(tr["t_entry"])]
    if after:
        piv = after[-1].price
        if tr["booked"]:
            buf = BUFFER.get(tr["sym"], 10.0)
            tr["stop"] = max(tr["stop"], piv - buf) if ce else min(tr["stop"], piv + buf)
        elif (c < piv) if ce else (c > piv):
            return "exit", f"5m structure broke (close {c:,.1f} past swing {'low' if ce else 'high'} {piv:,.1f})"
    if oi_against:
        return "exit", "OI against the trade"
    return None, ""
