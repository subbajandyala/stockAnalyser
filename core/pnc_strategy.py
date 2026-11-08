"""
NIFTY / SENSEX intraday option buying — confluence of market structure, trend, VWAP, option OI,
PCR, volume and volatility (Praveen's strategy). Pure functions; scripts/praveen_bot.py feeds them
Kite data and places / manages the orders.

Levels (previous day): PP = (H+L+C)/3, BC = (H+L)/2, TC = 2·PP − BC (top = max(TC, BC)),
R1 = 2·PP − L, S1 = 2·PP − H, PDH / PDL / PDC. CPR width % = (top − bottom) / PDC · 100, classed
narrow / moderate / wide by its percentile among the last ~120 days (≤ 33rd / ≥ 67th).

Entry — breakout and retest on closed 5-min candles (CE; PE is the mirror):
  1. breakout: a candle closes ≥ BREAK_PCT above a level (CPR top, R1, PDH) after the previous
     close was at / below it
  2. retest within RETEST_BARS candles: low within RETEST_PCT of the level (or just under it),
     close back above it, bullish rejection (green candle, close in the upper half of its range)
  3. on that retest candle the confluence score (0–100) must be ≥ MIN_SCORE and every mandatory
     filter must pass:
       09:25–14:30 · 15-min Supertrend with the trade · CPR not wide · risk (entry → stop) within
       the allowed points · room to the next level ≥ 1.5 R · ATM option spread ≤ MAX_SPREAD_PCT
Score (experimental weights from the strategy):
  structure 20 · Supertrend 15 · VWAP 15 · OI buildup 20 · PCR 10 · volume 10 · volatility 10
Stop: below the retest candle's low (CE) / above its high (PE). T1 = entry + 1.5 R (index).
"""

from __future__ import annotations

import datetime
import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

BREAK_PCT = 0.05          # a breakout close at least this % beyond the level
RETEST_PCT = 0.10         # the retest low reaches within this % of the level
RETEST_BARS = 12          # retest must come within an hour (5-min candles)
MIN_SCORE = 80
ENTRY_FROM, ENTRY_UNTIL = datetime.time(9, 25), datetime.time(14, 30)
RISK_PTS = {"NIFTY": (8.0, 40.0), "SENSEX": (25.0, 130.0)}     # allowed entry → stop distance
MIN_RR = 1.5
MAX_SPREAD_PCT = 1.5
NARROW_PCTL, WIDE_PCTL = 33, 67


# ── indicators ──────────────────────────────────────────────────────────────────────────────

def supertrend(df: pd.DataFrame, period: int = 10, mult: float = 3.0) -> pd.Series:
    """+1 (green) / −1 (red) per candle — the usual TradingView / Kite Supertrend (Wilder ATR)."""
    h, l, c = df["high"].to_numpy(float), df["low"].to_numpy(float), df["close"].to_numpy(float)
    n = len(df)
    out = np.full(n, np.nan)
    if n <= period:
        return pd.Series(out, index=df.index)
    tr = np.r_[h[0] - l[0], np.maximum.reduce([h[1:] - l[1:], abs(h[1:] - c[:-1]), abs(l[1:] - c[:-1])])]
    atr = tr[:period].mean()
    upper = lower = None
    up = True
    for i in range(period - 1, n):
        if i >= period:
            atr = (atr * (period - 1) + tr[i]) / period
        mid = (h[i] + l[i]) / 2
        bu, bl = mid + mult * atr, mid - mult * atr
        if upper is None:
            upper, lower, up = bu, bl, bool(c[i] > bu)
        else:
            upper = bu if (bu < upper or c[i - 1] > upper) else upper
            lower = bl if (bl > lower or c[i - 1] < lower) else lower
            if up and c[i] < lower:
                up = False
            elif not up and c[i] > upper:
                up = True
        out[i] = 1.0 if up else -1.0
    return pd.Series(out, index=df.index)


def vwap(df: pd.DataFrame) -> pd.Series:
    """Session VWAP (resets each day) from typical price × volume."""
    tp = (df["high"] + df["low"] + df["close"]) / 3
    v = df["volume"].astype(float)
    day = df.index.date
    return (tp * v).groupby(day).cumsum() / v.groupby(day).cumsum().replace(0, np.nan)


def adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    up, dn = h.diff(), -l.diff()
    pdm = up.where((up > dn) & (up > 0), 0.0)
    mdm = dn.where((dn > up) & (dn > 0), 0.0)
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / n, adjust=False).mean()
    pdi = 100 * pdm.ewm(alpha=1 / n, adjust=False).mean() / atr.replace(0, np.nan)
    mdi = 100 * mdm.ewm(alpha=1 / n, adjust=False).mean() / atr.replace(0, np.nan)
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean()


def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_price(s: float, k: float, t: float, iv: float, r: float, call: bool) -> float:
    if t <= 0 or iv <= 0:
        return max(0.0, (s - k) if call else (k - s))
    d1 = (math.log(s / k) + (r + iv * iv / 2) * t) / (iv * math.sqrt(t))
    d2 = d1 - iv * math.sqrt(t)
    if call:
        return s * _norm_cdf(d1) - k * math.exp(-r * t) * _norm_cdf(d2)
    return k * math.exp(-r * t) * _norm_cdf(-d2) - s * _norm_cdf(-d1)


def implied_vol(price: float, s: float, k: float, t: float, call: bool, r: float = 0.065) -> float | None:
    """Black-Scholes IV by bisection (Kite gives no Greeks). None when the price is below intrinsic."""
    if price <= 0 or t <= 0:
        return None
    lo, hi = 0.01, 3.0
    if bs_price(s, k, t, hi, r, call) < price or bs_price(s, k, t, lo, r, call) > price:
        return None
    for _ in range(60):
        mid = (lo + hi) / 2
        if bs_price(s, k, t, mid, r, call) > price:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def delta(s: float, k: float, t: float, iv: float, call: bool, r: float = 0.065) -> float | None:
    if not iv or t <= 0:
        return None
    d1 = (math.log(s / k) + (r + iv * iv / 2) * t) / (iv * math.sqrt(t))
    return _norm_cdf(d1) if call else _norm_cdf(d1) - 1


# ── levels ─────────────────────────────────────────────────────────────────────────────────

@dataclass
class Levels:
    pdh: float
    pdl: float
    pdc: float
    pp: float
    top: float
    bottom: float
    r1: float
    s1: float
    width_pct: float
    width_class: str
    atr_ratio: float | None = None


def levels(daily: pd.DataFrame, day: datetime.date) -> Levels | None:
    """Levels for `day` from the previous trading day; CPR width classed against ~120 earlier days."""
    d = daily[[x < day for x in daily.index]]
    if len(d) < 2:
        return None
    p = d.iloc[-1]
    h, l, c = float(p["high"]), float(p["low"]), float(p["close"])
    pp = (h + l + c) / 3
    bc = (h + l) / 2
    tc = 2 * pp - bc
    top, bottom = max(tc, bc), min(tc, bc)
    width = (top - bottom) / c * 100
    hist = []
    rows = d.tail(121)
    for i in range(1, len(rows)):
        q = rows.iloc[i - 1]
        a, b, cc = float(q["high"]), float(q["low"]), float(q["close"])
        x = (a + b + cc) / 3
        y = (a + b) / 2
        hist.append(abs(2 * x - y - y) / cc * 100)
    if len(hist) >= 20:
        pct = (np.array(hist) < width).mean() * 100
        cls = "narrow" if pct <= NARROW_PCTL else "wide" if pct >= WIDE_PCTL else "moderate"
    else:
        cls = "moderate"
    tr = (d["high"] - d["low"]).tail(14).mean()
    return Levels(h, l, c, pp, top, bottom, 2 * pp - l, 2 * pp - h, round(width, 3), cls,
                  round((top - bottom) / tr, 3) if tr else None)


# ── breakout and retest ────────────────────────────────────────────────────────────────────

def breakout_retest(m5: pd.DataFrame, lv: Levels, side: str, day: datetime.date) -> dict | None:
    """The retest on the LAST closed candle of a breakout earlier today, or None.
    CE: levels CPR top, R1, PDH; PE: CPR bottom, S1, PDL."""
    today = m5[m5.index.date == day]
    if len(today) < 3:
        return None
    o, h, l, c = (today[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    last = len(today) - 1
    names = ({"CPR top": lv.top, "R1": lv.r1, "PDH": lv.pdh} if side == "CE"
             else {"CPR bottom": lv.bottom, "S1": lv.s1, "PDL": lv.pdl})
    for name, lvl in names.items():
        for i in range(max(1, last - RETEST_BARS), last):           # breakout candle before the retest
            if side == "CE":
                broke = c[i] >= lvl * (1 + BREAK_PCT / 100) and c[i - 1] <= lvl
            else:
                broke = c[i] <= lvl * (1 - BREAK_PCT / 100) and c[i - 1] >= lvl
            if not broke:
                continue
            between = range(i + 1, last)
            if side == "CE":
                held = all(c[j] >= lvl for j in between)             # stayed above until the retest
                touch = l[last] <= lvl * (1 + RETEST_PCT / 100) and l[last] >= lvl * (1 - 2 * RETEST_PCT / 100)
                reject = c[last] > lvl and c[last] > o[last] and c[last] >= (h[last] + l[last]) / 2
            else:
                held = all(c[j] <= lvl for j in between)
                touch = h[last] >= lvl * (1 - RETEST_PCT / 100) and h[last] <= lvl * (1 + 2 * RETEST_PCT / 100)
                reject = c[last] < lvl and c[last] < o[last] and c[last] <= (h[last] + l[last]) / 2
            if held and touch and reject:
                return {"level": name, "price": float(lvl), "breakout_time": str(today.index[i]),
                        "close": float(c[last]), "stop": float(l[last] if side == "CE" else h[last])}
    return None


# ── option chain reads ─────────────────────────────────────────────────────────────────────

def chain_read(now: pd.DataFrame, base: pd.DataFrame | None, earlier: pd.DataFrame | None, spot: float) -> dict:
    """OI buildup near ATM, walls, PCR and change-in-OI PCR from chain snapshots.
    Frames: one row per contract (strike, type, oi, ltp). base = yesterday's close (or today's first),
    earlier = ~15–30 min ago (for PCR movement and wall shifts)."""
    strikes = sorted(now["strike"].unique())
    if not strikes:
        return {}
    atm_i = min(range(len(strikes)), key=lambda j: abs(strikes[j] - spot))
    near = set(strikes[max(0, atm_i - 3): atm_i + 4])
    out = {"atm": strikes[atm_i]}
    calls, puts = now[now["type"] == "CE"], now[now["type"] == "PE"]
    c_oi, p_oi = calls["oi"].sum(), puts["oi"].sum()
    out["pcr"] = round(p_oi / c_oi, 3) if c_oi else None
    above, below = calls[calls["strike"] > spot], puts[puts["strike"] < spot]
    out["call_wall"] = float(above.loc[above["oi"].idxmax(), "strike"]) if len(above) else None
    out["put_wall"] = float(below.loc[below["oi"].idxmax(), "strike"]) if len(below) else None

    def diff(ref: pd.DataFrame | None, typ: str) -> tuple[float, float]:
        if ref is None or ref.empty:
            return 0.0, 0.0
        a = now[(now["type"] == typ) & now["strike"].isin(near)].set_index("strike")
        b = ref[(ref["type"] == typ) & ref["strike"].isin(near)].set_index("strike")
        j = a.join(b, rsuffix="_b", how="inner")
        if j.empty:
            return 0.0, 0.0
        return float((j["oi"] - j["oi_b"]).sum()), float((j["ltp"] - j["ltp_b"]).mean())

    c_doi, c_dpx = diff(base, "CE")
    p_doi, p_dpx = diff(base, "PE")
    out.update({"call_doi": c_doi, "call_dprem": round(c_dpx, 2), "put_doi": p_doi, "put_dprem": round(p_dpx, 2)})
    out["coi_pcr"] = round(p_doi / c_doi, 3) if c_doi > 0 and p_doi > 0 else None
    if earlier is not None and not earlier.empty:
        e_c, e_p = earlier[earlier["type"] == "CE"]["oi"].sum(), earlier[earlier["type"] == "PE"]["oi"].sum()
        out["pcr_earlier"] = round(e_p / e_c, 3) if e_c else None
        ea = earlier[(earlier["type"] == "CE") & (earlier["strike"] > spot)]
        eb = earlier[(earlier["type"] == "PE") & (earlier["strike"] < spot)]
        out["call_wall_earlier"] = float(ea.loc[ea["oi"].idxmax(), "strike"]) if len(ea) else None
        out["put_wall_earlier"] = float(eb.loc[eb["oi"].idxmax(), "strike"]) if len(eb) else None
    return out


# ── score and filters ──────────────────────────────────────────────────────────────────────

@dataclass
class Inputs:
    symbol: str
    side: str                       # "CE" / "PE"
    time: datetime.time
    spot: float
    lv: Levels
    trigger: dict | None            # breakout_retest() result
    st15: float | None              # +1 / −1
    st5: float | None
    fut_close: float | None
    fut_vwap: float | None
    vwap_slope: float | None        # VWAP now − VWAP 3 candles ago
    vol: float | None               # futures volume of the trigger candle
    vol_avg: float | None           # average of the 20 before it
    chain: dict = field(default_factory=dict)
    vix: float | None = None
    vix_chg_pct: float | None = None
    atm_iv: float | None = None
    spread_pct: float | None = None
    adx: float | None = None
    next_level: float | None = None  # nearest level ahead of the trade (for room)


def score(x: Inputs) -> tuple[int, dict]:
    """Confluence score 0–100 and the points per part."""
    ce = x.side == "CE"
    s = {}
    lv = x.lv
    st_ = 0
    if (x.spot > lv.top and x.spot > lv.pp) if ce else (x.spot < lv.bottom and x.spot < lv.pp):
        st_ += 10
    if (x.spot > lv.r1 or x.spot > lv.pdh) if ce else (x.spot < lv.s1 or x.spot < lv.pdl):
        st_ += 5
    if x.trigger and x.next_level:
        risk = abs(x.trigger["close"] - x.trigger["stop"])
        room = (x.next_level - x.trigger["close"]) if ce else (x.trigger["close"] - x.next_level)
        if risk and room >= MIN_RR * risk:
            st_ += 5
    elif x.trigger:
        st_ += 5                                                    # no level ahead → open room
    s["structure"] = st_
    want = 1 if ce else -1
    s["supertrend"] = (8 if x.st15 == want else 0) + (7 if x.st5 == want else 0)
    vw = 0
    if x.fut_close and x.fut_vwap:
        vw += 10 if ((x.fut_close > x.fut_vwap) if ce else (x.fut_close < x.fut_vwap)) else 0
        if x.vwap_slope is not None and ((x.vwap_slope > 0) if ce else (x.vwap_slope < 0)):
            vw += 5
    s["vwap"] = vw
    ch = x.chain or {}
    oi = 0
    if ce:
        oi += 7 if ch.get("put_doi", 0) > 0 and ch.get("put_dprem", 0) < 0 else 0    # put writing
        oi += 7 if ch.get("call_doi", 0) < 0 and ch.get("call_dprem", 0) > 0 else 0  # call short covering
        if ch.get("call_wall") and ch.get("call_wall_earlier") and ch["call_wall"] > ch["call_wall_earlier"]:
            oi += 6
        elif ch.get("call_doi", 0) < 0:
            oi += 3
    else:
        oi += 7 if ch.get("call_doi", 0) > 0 and ch.get("call_dprem", 0) < 0 else 0
        oi += 7 if ch.get("put_doi", 0) < 0 and ch.get("put_dprem", 0) > 0 else 0
        if ch.get("put_wall") and ch.get("put_wall_earlier") and ch["put_wall"] < ch["put_wall_earlier"]:
            oi += 6
        elif ch.get("put_doi", 0) < 0:
            oi += 3
    s["oi"] = oi
    pc = 0
    pcr, pe_ = ch.get("pcr"), ch.get("pcr_earlier")
    if pcr is not None:
        if ce and (pcr >= 1.0 or (pe_ is not None and pcr > pe_)):
            pc += 5
        if not ce and (pcr <= 1.0 or (pe_ is not None and pcr < pe_)):
            pc += 5
    coi = ch.get("coi_pcr")
    if coi is not None and ((coi > 1) if ce else (coi < 1)):
        pc += 5
    s["pcr"] = pc
    s["volume"] = 10 if x.vol and x.vol_avg and x.vol > x.vol_avg else 0
    vol_ = 0
    if x.vix is not None and x.vix < 20:
        vol_ += 5
    if x.vix_chg_pct is not None and x.vix_chg_pct < 5:
        vol_ += 5
    s["volatility"] = vol_
    return int(sum(s.values())), s


def mandatory(x: Inputs, trades_today: int, losses_today: int, max_trades: int, max_losses: int,
              event_day: bool) -> list[str]:
    """Reasons the trade is not allowed (empty = allowed)."""
    why = []
    ce = x.side == "CE"
    if not (ENTRY_FROM <= x.time <= ENTRY_UNTIL):
        why.append(f"outside {ENTRY_FROM:%H:%M}–{ENTRY_UNTIL:%H:%M}")
    if x.st15 != (1 if ce else -1):
        why.append("15-min Supertrend against the trade")
    if x.lv.width_class == "wide":
        why.append(f"wide CPR ({x.lv.width_pct:.2f}%)")
    if event_day:
        why.append("event day")
    if trades_today >= max_trades:
        why.append(f"{max_trades} trades already today")
    if losses_today >= max_losses:
        why.append(f"{max_losses} losses today — stopped")
    if x.trigger:
        risk = abs(x.trigger["close"] - x.trigger["stop"])
        lo, hi = RISK_PTS.get(x.symbol, (8.0, 40.0))
        if not lo <= risk <= hi:
            why.append(f"risk {risk:.0f} pts outside {lo:g}–{hi:g}")
        if x.next_level:
            room = (x.next_level - x.trigger["close"]) if ce else (x.trigger["close"] - x.next_level)
            if risk and room < MIN_RR * risk:
                why.append(f"room {room:.0f} pts to {x.next_level:g} < {MIN_RR:g}R")
    if x.spread_pct is not None and x.spread_pct > MAX_SPREAD_PCT:
        why.append(f"option spread {x.spread_pct:.1f}%")
    return why


def next_level(side: str, price: float, lv: Levels, chain: dict) -> float | None:
    """Nearest level ahead of the trade (CE: above, PE: below) — for the room check and T1."""
    pts = [lv.pdh, lv.r1, lv.top, lv.pp, chain.get("call_wall")] if side == "CE" else \
          [lv.pdl, lv.s1, lv.bottom, lv.pp, chain.get("put_wall")]
    pts = [p for p in pts if p]
    ahead = [p for p in pts if (p > price * 1.0005 if side == "CE" else p < price * 0.9995)]
    if not ahead:
        return None
    return min(ahead) if side == "CE" else max(ahead)
