"""
core/pulse.py — NIFTY Pulse: intraday NIFTY weekly CE / PE buyer (spec: NIFTY_CE_PE_Intraday_Bot_Spec.md).

Pure decision logic, no Kite calls, so scripts/pulse_bot.py (live / paper) and scripts/pulse_backtest.py
use the same code.

Signal  : NIFTY 50 spot 5-min candles — EMA 8 / 21 / 200, ATR(14), CPR, levels, candle patterns
Confirm : NIFTY current-month futures 5-min — VWAP and volume (the index has no volume)
Trade   : 1-ITM weekly CE (up) / PE (down); every stop / target is on the SPOT, executed on the option
Entry   : trend + futures flow + not stretched + pullback location + closed candle signal + room ≥ 2R,
          armed for 2 candles, bought when spot breaks the signal candle (+2 pts), not chased > 0.3R
Exit    : stop beyond the wick (ATR buffer) · 1R → half booked + stop to BE+3 · 2R → out (strong trend:
          runner trailed) · profit lock · chandelier · 6-candle time stop · premium −35 % · 15:10
"""

from __future__ import annotations

import math
import os
from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime, timedelta
from datetime import time as dtime
from typing import Dict, List, Optional


# ====================================================================== config
@dataclass
class Config:
    # mode
    mode: str = "paper"                  # "paper" | "live"
    confirm_live: bool = False           # second key; both needed for real orders
    kill_switch_file: str = "STOP_BOT"

    # schedule
    candle_interval: str = "5minute"
    candle_minutes: int = 5
    scan_delay_sec: int = 8              # scan this many s after each candle close
    watch_every_sec: int = 15

    # capital & risk
    capital: float = 200_000.0
    risk_pct: float = 0.01
    max_premium_pct: float = 0.30
    max_lots: int = 0                    # cap on lots per trade (PULSE_MAX_LOTS=1 for a first live week); 0 = none
    max_trades_per_day: int = 3
    max_losses_per_day: int = 2
    daily_loss_limit_pct: float = 0.025
    daily_profit_lock_r: float = 4.0
    cooldown_after_loss_min: int = 30

    # time windows (IST)
    win1_from: str = "09:30"
    win1_to: str = "11:30"
    win2_from: str = "13:30"
    win2_to: str = "14:45"
    square_off: str = "15:10"

    # India VIX
    vix_min: float = 10.0
    vix_max: float = 24.0
    vix_spike_pct: float = 8.0

    # indicators
    ema_fast: int = 8
    ema_slow: int = 21
    ema_trend: int = 200
    atr_len: int = 14
    vol_avg_len: int = 20
    pivot_type: str = "cpr"              # cpr | fib

    # entry
    pullback_zone_atr: float = 0.30
    level_zone_atr: float = 0.30
    vol_mult: float = 1.2
    entry_buffer_pts: float = 2.0
    trigger_valid_bars: int = 2
    overshoot_r: float = 0.3

    # don't buy the top
    max_ext_ema21_atr: float = 1.5
    max_ext_vwap_atr: float = 3.5
    max_run_bars: int = 5
    climax_bar_atr: float = 2.0
    distribution_vol_mult: float = 2.0
    distribution_block_bars: int = 3

    # strong trend
    strong_trend_spread_atr: float = 0.6
    shallow_pullback_atr: float = 0.25
    retest_max_bars: int = 6

    # initial balance = high / low of the first hour (09:15–10:15)
    ib_mode: int = 0                     # 0 off · 1 IB high / low + 1× / 1.5× IB-range extensions as levels ·
                                         # 2 = 1 + after 10:15 trade only the side the IB broke (5-min close
                                         # outside), nothing while inside, a failed break blocks that side for the day,
                                         # and a pullback that holds the broken edge is an entry ("ib retest")
    ib_end: str = "10:15"
    ib_early: str = "strong"             # ib_mode 2, before the IB is complete: all | strong (strong trend only) | none
    ib_fail_atr: float = 0.3             # failed break: a close back inside by more than this × ATR

    # stops & targets (spot points)
    stop_buffer_atr: float = 0.25
    stop_buffer_min_pts: float = 5.0
    min_stop_atr: float = 0.6
    min_stop_pts: float = 12.0
    max_stop_atr: float = 1.6
    min_rr: float = 2.0
    partial_at_r: float = 1.0
    be_at_r: float = 1.0
    be_plus_pts: float = 3.0
    target_r: float = 2.0
    max_target_r: float = 3.0
    chandelier_atr: float = 2.0
    chandelier_from_r: float = 1.5
    lock_trail_gap_r: float = 1.0
    time_stop_bars: int = 6
    time_stop_min_r: float = 0.5
    premium_hard_stop_pct: float = 0.35
    premium_giveback_pct: float = 0.40
    trail_runner_in_strong_trend: bool = True
    runner_tight_gap_r: float = 0.5

    # options
    strike_step: int = 50
    itm_steps: int = 1
    min_dte: int = 2
    skip_expiry_day: bool = True
    min_premium: float = 60.0
    max_premium: float = 350.0
    max_spread_pct: float = 0.01
    min_oi: int = 100_000
    delta_atm: float = 0.50
    delta_itm: float = 0.60
    limit_slip_pct: float = 0.01
    disaster_sl_x: float = 1.3           # broker SL order at entry − delta × R × 1.3
    cost_per_lot_rt: float = 60.0

    PROFIT_LOCK = ((1.0, 0.0), (1.5, 0.5), (2.0, 1.0), (3.0, 2.0))

    @staticmethod
    def t(hhmm: str) -> dtime:
        h, m = hhmm.split(":")
        return dtime(int(h), int(m))

    @property
    def windows(self):
        return ((self.t(self.win1_from), self.t(self.win1_to)), (self.t(self.win2_from), self.t(self.win2_to)))

    @property
    def last_entry(self) -> dtime:
        return max(b for _, b in self.windows)

    @classmethod
    def from_env(cls, env: dict) -> "Config":
        """PULSE_<FIELD> overrides any field, e.g. PULSE_CAPITAL=300000, PULSE_RISK_PCT=0.02, PULSE_MODE=live."""
        c = cls()
        for f in fields(cls):
            v = env.get("PULSE_" + f.name.upper())
            if v is None or v == "":
                continue
            cur = getattr(c, f.name)
            if isinstance(cur, bool):
                setattr(c, f.name, v.strip().lower() in ("1", "true", "yes", "on"))
            elif isinstance(cur, int):
                setattr(c, f.name, int(float(v)))
            elif isinstance(cur, float):
                setattr(c, f.name, float(v))
            else:
                setattr(c, f.name, v.strip())
        if c.candle_interval == "3minute":
            c.candle_minutes = 3
        return c


# ====================================================================== data model
@dataclass
class Bar:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @property
    def range(self) -> float: return self.high - self.low
    @property
    def body(self) -> float: return abs(self.close - self.open)
    @property
    def green(self) -> bool: return self.close > self.open
    @property
    def red(self) -> bool: return self.close < self.open
    @property
    def upper(self) -> float: return self.high - max(self.open, self.close)
    @property
    def lower(self) -> float: return min(self.open, self.close) - self.low


@dataclass
class Signal:
    side: str              # CE | PE
    kind: str              # pullback | continuation | retest
    pattern: str
    bar_ts: str            # signal candle start (ISO)
    trigger: float
    stop: float
    risk: float
    t1: float
    t2: float
    atr: float
    reason: str = ""
    expires_at: str = ""   # ISO; untriggered after this → cancelled
    strong: bool = False

    def text(self) -> str:
        return (f"{self.side} {self.kind} · {self.pattern} · {self.reason} — trigger {self.trigger:.1f} · "
                f"stop {self.stop:.1f} (R {self.risk:.1f}) · T1 {self.t1:.1f} · T2 {self.t2:.1f}"
                f"{' · strong trend' if self.strong else ''}")


@dataclass
class Position:
    sig: Signal
    symbol: str
    lots: int
    lot_size: int
    entry_prem: float
    entry_spot: float
    stop_spot: float
    delta: float
    opened_at: str
    bars_held: int = 0
    partial_done: bool = False
    peak_r: float = 0.0
    peak_prem: float = 0.0
    best_spot: float = 0.0
    qty_open: int = 0
    atr_now: float = 0.0
    realised: float = 0.0
    sl_order_id: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Position":
        d = dict(d)
        d["sig"] = Signal(**d["sig"])
        return cls(**d)


@dataclass
class DayState:
    day: str
    trades: int = 0
    losses: int = 0
    pnl: float = 0.0
    pnl_r: float = 0.0
    last_loss_at: str = ""
    halted: bool = False
    dist_block_until: str = ""   # ISO bar ts: CE blocked while bar ts <= this
    acc_block_until: str = ""    # PE blocked
    used_bars: List[str] = field(default_factory=list)   # signal candles already traded / cancelled


# ====================================================================== indicators
def ema(vals: List[float], n: int) -> List[float]:
    k, out = 2 / (n + 1), []
    for i, v in enumerate(vals):
        out.append(v if i == 0 else out[-1] + k * (v - out[-1]))
    return out


def atr(bars: List[Bar], n: int) -> List[float]:
    out: List[float] = []
    for i, b in enumerate(bars):
        tr = b.range if i == 0 else max(b.range, abs(b.high - bars[i - 1].close), abs(b.low - bars[i - 1].close))
        out.append(tr if i == 0 else (out[-1] * (n - 1) + tr) / n)
    return out


def sma(vals: List[float], n: int) -> List[float]:
    out, s = [], 0.0
    for i, v in enumerate(vals):
        s += v
        if i >= n:
            s -= vals[i - n]
        out.append(s / min(i + 1, n))
    return out


def session_vwap(bars: List[Bar]) -> List[float]:
    out, pv, vv, cur = [], 0.0, 0.0, None
    for b in bars:
        if b.ts.date() != cur:
            cur, pv, vv = b.ts.date(), 0.0, 0.0
        tp = (b.high + b.low + b.close) / 3
        pv += tp * b.volume
        vv += b.volume
        out.append(pv / vv if vv else tp)
    return out


def pivots(h: float, l: float, c: float, kind: str = "cpr") -> Dict[str, float]:
    p, rng = (h + l + c) / 3, h - l
    if kind == "fib":
        return {"P": p, "R1": p + 0.382 * rng, "R2": p + 0.618 * rng, "R3": p + rng,
                "S1": p - 0.382 * rng, "S2": p - 0.618 * rng, "S3": p - rng}
    bc = (h + l) / 2
    tc = 2 * p - bc
    return {"P": p, "TC": max(tc, bc), "BC": min(tc, bc), "R1": 2 * p - l, "S1": 2 * p - h,
            "R2": p + rng, "S2": p - rng}


def swing_points(bars: List[Bar], left: int = 3, right: int = 3):
    """Fractal swings, confirmed only `right` bars later (no look-ahead)."""
    highs, lows = [], []
    for i in range(left, len(bars) - right):
        h, l = bars[i].high, bars[i].low
        if all(h > bars[j].high for j in range(i - left, i + right + 1) if j != i):
            highs.append(h)
        if all(l < bars[j].low for j in range(i - left, i + right + 1) if j != i):
            lows.append(l)
    return highs, lows


# ====================================================================== candle patterns
def is_bull_pin(b: Bar, prev: Bar) -> bool:
    return b.range > 0 and b.body <= 0.33 * b.range and b.lower >= 0.60 * b.range \
        and b.upper <= 0.25 * b.range and b.low < prev.low


def is_bear_pin(b: Bar, prev: Bar) -> bool:
    return b.range > 0 and b.body <= 0.33 * b.range and b.upper >= 0.60 * b.range \
        and b.lower <= 0.25 * b.range and b.high > prev.high


def is_bull_engulf(b: Bar, prev: Bar) -> bool:
    return b.range > 0 and prev.red and b.green and b.close > prev.open and b.open <= prev.close \
        and b.body >= 0.6 * b.range


def is_bear_engulf(b: Bar, prev: Bar) -> bool:
    return b.range > 0 and prev.green and b.red and b.close < prev.open and b.open >= prev.close \
        and b.body >= 0.6 * b.range


def is_inside(b: Bar, mother: Bar) -> bool:
    return b.high < mother.high and b.low > mother.low


def bull_fakey(bars: List[Bar], i: int) -> bool:
    if i < 2:
        return False
    m, ib, b = bars[i - 2], bars[i - 1], bars[i]
    return is_inside(ib, m) and b.low < ib.low and m.low <= b.close <= m.high and b.close > ib.low


def bear_fakey(bars: List[Bar], i: int) -> bool:
    if i < 2:
        return False
    m, ib, b = bars[i - 2], bars[i - 1], bars[i]
    return is_inside(ib, m) and b.high > ib.high and m.low <= b.close <= m.high and b.close < ib.high


# ====================================================================== initial balance
def initial_balance(spot: List[Bar], i: int, cfg: Config, a: float) -> Optional[dict]:
    """First-hour range of bar i's day, from closed candles up to i, and what happened to it since:
    side = "CE" (a 5-min close above the IB high, not failed since) / "PE" (below the low) / None (inside),
    failed = sides whose break closed back inside by > ib_fail_atr × ATR (blocked for the rest of the day)."""
    d, end = spot[i].ts.date(), cfg.t(cfg.ib_end)
    k0 = i
    while k0 > 0 and spot[k0 - 1].ts.date() == d:
        k0 -= 1
    ib = [spot[k] for k in range(k0, i + 1) if spot[k].ts.time() < end]
    if not ib:
        return None
    hi, lo = max(b.high for b in ib), min(b.low for b in ib)
    out = {"hi": hi, "lo": lo, "rng": hi - lo, "side": None, "failed": set(),
           "done": (spot[i].ts + timedelta(minutes=cfg.candle_minutes)).time() >= end}
    if not out["done"]:
        return out
    side, failed = None, out["failed"]
    for k in range(k0, i + 1):
        b = spot[k]
        if b.ts.time() < end:
            continue
        if side is None:
            if b.close > hi and "CE" not in failed:
                side = "CE"
            elif b.close < lo and "PE" not in failed:
                side = "PE"
        elif side == "CE" and b.close < hi - cfg.ib_fail_atr * a:
            failed.add("CE")
            side = None
        elif side == "PE" and b.close > lo + cfg.ib_fail_atr * a:
            failed.add("PE")
            side = None
    out["side"] = side
    return out


def ib_levels(spot: List[Bar], cfg: Config) -> List[float]:
    """IB high / low and the 1× / 1.5× IB-range extensions beyond each edge, once the first hour is complete."""
    ib = initial_balance(spot, len(spot) - 1, cfg, 0.0)
    if not ib or not ib["done"] or ib["rng"] <= 0:
        return []
    hi, lo, r = ib["hi"], ib["lo"], ib["rng"]
    return [hi, lo, hi + r, hi + 1.5 * r, lo - r, lo - 1.5 * r]


# ====================================================================== strategy
def align(spot: List[Bar], fut: List[Bar]):
    """Only candles present in both series, so index i is the same candle in both."""
    fts = {b.ts: b for b in fut}
    s2 = [b for b in spot if b.ts in fts]
    return s2, [fts[b.ts] for b in s2]


class Strategy:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def context(self, spot: List[Bar], fut: List[Bar]) -> dict:
        c = self.cfg
        closes = [b.close for b in spot]
        return {"e8": ema(closes, c.ema_fast), "e21": ema(closes, c.ema_slow), "e200": ema(closes, c.ema_trend),
                "atr": atr(spot, c.atr_len), "fatr": atr(fut, c.atr_len), "vwap": session_vwap(fut),
                "vavg": sma([b.volume for b in fut], c.vol_avg_len)}

    def trend(self, i: int, spot: List[Bar], x: dict) -> Optional[str]:
        b, e8, e21, e200 = spot[i], x["e8"][i], x["e21"][i], x["e200"][i]
        up = e8 > e21 and b.close > e8 and b.close > e21 and b.close > e200 and e8 > x["e8"][i - 1]
        dn = e8 < e21 and b.close < e8 and b.close < e21 and b.close < e200 and e8 < x["e8"][i - 1]
        up = up or (e8 > e21 > e200 and b.close > e21 and e21 > x["e21"][i - 3])
        dn = dn or (e8 < e21 < e200 and b.close < e21 and e21 < x["e21"][i - 3])
        return "CE" if up else "PE" if dn else None

    def strong_trend(self, i: int, side: str, x: dict) -> bool:
        spread = (x["e8"][i] - x["e21"][i]) if side == "CE" else (x["e21"][i] - x["e8"][i])
        rising = (x["e21"][i] > x["e21"][i - 3]) if side == "CE" else (x["e21"][i] < x["e21"][i - 3])
        return rising and spread >= self.cfg.strong_trend_spread_atr * x["atr"][i]

    def overextended(self, i: int, side: str, spot: List[Bar], fut: List[Bar], x: dict) -> Optional[str]:
        c, b, a = self.cfg, spot[i], x["atr"][i]
        ext = (b.close - x["e21"][i]) / a
        if (side == "CE" and ext > c.max_ext_ema21_atr) or (side == "PE" and -ext > c.max_ext_ema21_atr):
            return f"stretched {ext:+.1f} ATR from EMA21"
        fext = (fut[i].close - x["vwap"][i]) / x["fatr"][i] if x["fatr"][i] else 0.0
        if (side == "CE" and fext > c.max_ext_vwap_atr) or (side == "PE" and -fext > c.max_ext_vwap_atr):
            return f"futures stretched {fext:+.1f} ATR from VWAP"
        run = 0
        for k in range(i, max(i - 10, 0), -1):
            if (side == "CE" and spot[k].green) or (side == "PE" and spot[k].red):
                run += 1
            else:
                break
        if run >= c.max_run_bars:
            return f"{run} candles in a row without a pullback"
        if max(spot[i].range, spot[i - 1].range) > c.climax_bar_atr * a:
            return f"climax candle (range > {c.climax_bar_atr:g} ATR)"
        return None

    def update_flow_blocks(self, i: int, spot: List[Bar], fut: List[Bar], x: dict, st: DayState) -> Optional[str]:
        """High-volume red futures candle at the day high blocks CE for 3 candles (green at the low blocks PE)."""
        c, f = self.cfg, fut[i]
        day_bars = [b for b in spot[: i + 1] if b.ts.date() == spot[i].ts.date()]
        if not day_bars:
            return None
        day_hi, day_lo = max(b.high for b in day_bars), min(b.low for b in day_bars)
        big = f.volume >= c.distribution_vol_mult * x["vavg"][i]
        until = (spot[i].ts + timedelta(minutes=c.candle_minutes * c.distribution_block_bars)).isoformat()
        if big and f.red and day_hi - spot[i].high <= 0.3 * x["atr"][i]:
            st.dist_block_until = until
            return "distribution at the day high — CE blocked for 3 candles"
        if big and f.green and spot[i].low - day_lo <= 0.3 * x["atr"][i]:
            st.acc_block_until = until
            return "accumulation at the day low — PE blocked for 3 candles"
        return None

    def pattern(self, i: int, side: str, spot: List[Bar], levels: List[float]) -> Optional[str]:
        b, p = spot[i], spot[i - 1]
        if side == "CE":
            if is_bull_pin(b, p): return "bullish pin bar"
            if is_bull_engulf(b, p): return "bullish engulfing"
            if bull_fakey(spot, i): return "bullish fakey"
            if is_inside(p, spot[i - 2]) and b.close > p.high: return "inside-bar breakout"
            if any(b.low < L < b.close for L in levels): return "false breakdown"
        else:
            if is_bear_pin(b, p): return "bearish pin bar"
            if is_bear_engulf(b, p): return "bearish engulfing"
            if bear_fakey(spot, i): return "bearish fakey"
            if is_inside(p, spot[i - 2]) and b.close < p.low: return "inside-bar breakdown"
            if any(b.high > L > b.close for L in levels): return "false breakout"
        return None

    def at_pullback(self, i, side, spot, x, levels) -> Optional[str]:
        c, b, a = self.cfg, spot[i], x["atr"][i]
        lo_band, hi_band = sorted((x["e8"][i], x["e21"][i]))
        lows, highs = min(b.low, spot[i - 1].low), max(b.high, spot[i - 1].high)
        if side == "CE":
            if lows <= hi_band + c.pullback_zone_atr * a and b.close >= lo_band:
                return "pullback to the 8/21 EMA zone"
            for L in levels:
                if L < b.close and abs(lows - L) <= c.level_zone_atr * a:
                    return f"pullback to support {L:.0f}"
        else:
            if highs >= lo_band - c.pullback_zone_atr * a and b.close <= hi_band:
                return "rally to the 8/21 EMA zone"
            for L in levels:
                if L > b.close and abs(highs - L) <= c.level_zone_atr * a:
                    return f"rally to resistance {L:.0f}"
        return None

    def breakout_retest(self, i, side, spot, x, levels) -> Optional[float]:
        c, a, b = self.cfg, x["atr"][i], spot[i]
        for L in levels:
            for k in range(i - c.retest_max_bars, i - 1):
                if k < 1:
                    continue
                if side == "CE" and spot[k - 1].close < L < spot[k].close:
                    if abs(b.low - L) <= c.level_zone_atr * a and b.close > L and b.green:
                        return L
                if side == "PE" and spot[k - 1].close > L > spot[k].close:
                    if abs(b.high - L) <= c.level_zone_atr * a and b.close < L and b.red:
                        return L
        return None

    def evaluate(self, i: int, spot: List[Bar], fut: List[Bar], x: dict, levels: List[float],
                 st: DayState) -> tuple[Optional[Signal], str]:
        """(Signal or None, why not). Every element must be present."""
        c, b, a = self.cfg, spot[i], x["atr"][i]
        if i < 3 or a <= 0:
            return None, "not enough candles"
        side = self.trend(i, spot, x)
        if side is None:
            return None, "no trend (EMA 8 / 21 / 200 not lined up)"
        ts = b.ts.isoformat()
        if side == "CE" and fut[i].close <= x["vwap"][i]:
            return None, f"CE trend but futures {fut[i].close:.0f} below VWAP {x['vwap'][i]:.0f}"
        if side == "PE" and fut[i].close >= x["vwap"][i]:
            return None, f"PE trend but futures {fut[i].close:.0f} above VWAP {x['vwap'][i]:.0f}"
        if fut[i].volume < c.vol_mult * x["vavg"][i]:
            return None, f"{side}: futures volume {fut[i].volume / max(x['vavg'][i], 1):.1f}× avg (< {c.vol_mult:g}×)"
        if side == "CE" and st.dist_block_until and ts <= st.dist_block_until:
            return None, "CE blocked: distribution at the day high"
        if side == "PE" and st.acc_block_until and ts <= st.acc_block_until:
            return None, "PE blocked: accumulation at the day low"
        why = self.overextended(i, side, spot, fut, x)
        if why:
            return None, f"{side}: {why}"
        if ts in st.used_bars:
            return None, "this candle was already used"
        ib = initial_balance(spot, i, c, a) if c.ib_mode >= 2 else None
        if ib and ib["done"]:
            rng_txt = f"IB {ib['lo']:.0f}–{ib['hi']:.0f}"
            if ib["side"] is None:
                f = f", failed break {'/'.join(sorted(ib['failed']))}" if ib["failed"] else ""
                return None, f"{side} trend but price is inside the {rng_txt}{f} — rotation, no trade"
            if ib["side"] != side:
                return None, f"{side} trend but the {rng_txt} broke {'up' if ib['side'] == 'CE' else 'down'}"

        kind, pat = None, self.pattern(i, side, spot, levels)
        where = self.at_pullback(i, side, spot, x, levels)
        strong = self.strong_trend(i, side, x)
        if pat and where:
            kind = "pullback"
        elif strong:
            touch = (b.low <= x["e8"][i] + c.shallow_pullback_atr * a and b.close > x["e8"][i] and b.green) \
                if side == "CE" else \
                (b.high >= x["e8"][i] - c.shallow_pullback_atr * a and b.close < x["e8"][i] and b.red)
            L = self.breakout_retest(i, side, spot, x, levels)
            if L is not None:
                kind, pat, where = "retest", pat or "retest hold", f"retest of {L:.0f}"
            elif touch:
                kind, pat, where = "continuation", pat or "EMA8 touch", "shallow pullback in a strong trend"
        if kind is None and ib and ib["done"]:
            # the first pullback to the broken IB edge that holds it (a close back beyond it, with-trend candle)
            edge = ib["hi"] if side == "CE" else ib["lo"]
            if side == "CE" and b.low <= edge + c.level_zone_atr * a and b.close > edge and b.green:
                kind, pat, where = "ib retest", pat or "IB edge held", f"pullback to the IB high {edge:.0f}"
            elif side == "PE" and b.high >= edge - c.level_zone_atr * a and b.close < edge and b.red:
                kind, pat, where = "ib retest", pat or "IB edge held", f"pullback to the IB low {edge:.0f}"
        if kind is None:
            return None, f"{side} trend: {'no candle signal' if not pat else 'candle not at a pullback / level'}"
        if ib and not ib["done"] and c.ib_early != "all" and (c.ib_early == "none" or not strong):
            return None, (f"{side} {pat}: first hour still forming (IB until {c.ib_end}) — "
                          f"{'no entries' if c.ib_early == 'none' else 'strong-trend setups only'}")

        buf = max(c.stop_buffer_atr * a, c.stop_buffer_min_pts)
        swing = spot[i - 1: i + 1]
        if side == "CE":
            trigger = b.high + c.entry_buffer_pts
            stop = min(bb.low for bb in swing) - buf
            stop = min(stop, trigger - max(c.min_stop_atr * a, c.min_stop_pts))
        else:
            trigger = b.low - c.entry_buffer_pts
            stop = max(bb.high for bb in swing) + buf
            stop = max(stop, trigger + max(c.min_stop_atr * a, c.min_stop_pts))
        risk = abs(trigger - stop)
        if risk > c.max_stop_atr * a:
            return None, f"{side} {pat}: stop {risk:.0f} pts too wide (> {c.max_stop_atr:g} ATR = {c.max_stop_atr * a:.0f})"

        sgn = 1 if side == "CE" else -1
        opposing = sorted((L for L in levels if sgn * (L - trigger) > 0), key=lambda L: sgn * (L - trigger))
        if opposing and sgn * (opposing[0] - trigger) < c.min_rr * risk:
            return None, (f"{side} {pat}: no room — {'resistance' if sgn > 0 else 'support'} {opposing[0]:.0f} "
                          f"only {sgn * (opposing[0] - trigger) / risk:.1f}R away")
        t1 = trigger + sgn * c.partial_at_r * risk
        t2 = trigger + sgn * c.target_r * risk
        for L in opposing:
            d = sgn * (L - trigger)
            if c.target_r * risk <= d <= c.max_target_r * risk:
                t2 = L - sgn * 0.1 * a
                break
        exp = b.ts + timedelta(minutes=c.candle_minutes * (c.trigger_valid_bars + 1))
        return Signal(side, kind, pat, ts, round(trigger, 2), round(stop, 2), round(risk, 2), round(t1, 2),
                      round(t2, 2), round(a, 2), reason=where, expires_at=exp.isoformat(), strong=strong), ""


# ====================================================================== exits
def manage_exit(pos: Position, bar: Bar, prem: float, cfg: Config, closed_bar: bool) -> List[tuple]:
    """Every watch tick (closed_bar=False, bar = LTP) and every candle close (True).
    → [("partial", qty) | ("exit", reason)]. Stops on every tick; trailing moves only on closed candles."""
    c, s = cfg, pos.sig
    sgn = 1 if s.side == "CE" else -1
    acts: List[tuple] = []
    pos.best_spot = max(pos.best_spot, bar.high) if sgn > 0 else min(pos.best_spot, bar.low)
    pos.peak_prem = max(pos.peak_prem, prem)
    move_r = sgn * ((bar.high if sgn > 0 else bar.low) - pos.entry_spot) / s.risk
    pos.peak_r = max(pos.peak_r, move_r)

    if sgn * (bar.close - pos.stop_spot) <= 0:
        return [("exit", f"stop hit at spot {pos.stop_spot:.1f}")]
    if prem > 0 and prem <= pos.entry_prem * (1 - c.premium_hard_stop_pct):
        return [("exit", f"premium hard stop (−{c.premium_hard_stop_pct:.0%})")]
    if sgn * (bar.close - s.t2) >= 0 and not (s.strong and c.trail_runner_in_strong_trend):
        return [("exit", f"target T2 {s.t2:.1f}")]

    if not pos.partial_done and pos.peak_r >= c.partial_at_r:
        q = (pos.qty_open // pos.lot_size // 2) * pos.lot_size if pos.lots > 1 else 0
        if q:
            acts.append(("partial", q))
        pos.partial_done = True
    if pos.peak_r >= c.be_at_r:
        be = pos.entry_spot + sgn * c.be_plus_pts
        if sgn * (be - pos.stop_spot) > 0:
            pos.stop_spot = be

    if closed_bar:
        pos.bars_held += 1
        lock = None
        for peak, locked in c.PROFIT_LOCK:
            if pos.peak_r >= peak:
                lock = locked
        if lock is not None and pos.peak_r > c.PROFIT_LOCK[-1][0]:
            lock = pos.peak_r - c.lock_trail_gap_r
        if s.strong and c.trail_runner_in_strong_trend and pos.peak_r >= c.target_r:
            lock = max(lock or 0.0, pos.peak_r - c.runner_tight_gap_r)
        if lock is not None:
            lvl = pos.entry_spot + sgn * lock * s.risk
            if sgn * (lvl - pos.stop_spot) > 0:
                pos.stop_spot = lvl
        if pos.peak_r >= c.chandelier_from_r and pos.atr_now:
            ch = pos.best_spot - sgn * c.chandelier_atr * pos.atr_now
            if sgn * (ch - pos.stop_spot) > 0:
                pos.stop_spot = ch
        if pos.bars_held >= c.time_stop_bars and pos.peak_r < c.time_stop_min_r:
            acts.append(("exit", f"time stop: < {c.time_stop_min_r:g}R after {pos.bars_held} candles"))
        gain_peak = pos.peak_prem - pos.entry_prem
        if pos.partial_done and gain_peak > 0 and 0 < prem < pos.peak_prem - c.premium_giveback_pct * gain_peak:
            acts.append(("exit", f"premium gave back {c.premium_giveback_pct:.0%} of its peak gain"))
    return acts


# ====================================================================== option selection / sizing
def trading_days_between(a: date, b: date, holidays: set) -> int:
    n, d = 0, a
    while d < b:
        d += timedelta(days=1)
        if d.weekday() < 5 and d not in holidays:
            n += 1
    return n


def choose_expiry(expiries: List[date], today: date, cfg: Config, holidays: set) -> Optional[date]:
    for e in sorted(x for x in expiries if x >= today):
        if cfg.skip_expiry_day and e == today:
            continue
        if trading_days_between(today, e, holidays) >= cfg.min_dte:
            return e
    return None


def choose_strike(spot: float, side: str, cfg: Config) -> int:
    atm = int(round(spot / cfg.strike_step) * cfg.strike_step)
    off = cfg.itm_steps * cfg.strike_step
    return atm - off if side == "CE" else atm + off


def size_lots(risk_pts: float, prem: float, delta: float, lot_size: int, cfg: Config, half: bool = False) -> int:
    opt_risk = min(delta * risk_pts * 1.15, prem * cfg.premium_hard_stop_pct)
    risk_rupees = cfg.capital * cfg.risk_pct * (0.5 if half else 1.0)
    lots = math.floor(risk_rupees / (opt_risk * lot_size + cfg.cost_per_lot_rt))
    max_lots = math.floor(cfg.capital * cfg.max_premium_pct / (prem * lot_size)) if prem > 0 else 0
    if cfg.max_lots > 0:
        lots = min(lots, cfg.max_lots)
    return max(0, min(lots, max_lots))


# ====================================================================== guards
def can_trade(now: datetime, st: DayState, cfg: Config, vix: Optional[float], has_pos: bool,
              root: str = ".") -> Optional[str]:
    t = now.time()
    if os.path.exists(os.path.join(root, cfg.kill_switch_file)): return "kill switch (STOP_BOT file)"
    if st.halted: return "halted for the day"
    if not any(a <= t <= b for a, b in cfg.windows): return "outside the entry windows"
    if has_pos: return "position open"
    if st.trades >= cfg.max_trades_per_day: return f"max {cfg.max_trades_per_day} trades reached"
    if st.losses >= cfg.max_losses_per_day: return f"max {cfg.max_losses_per_day} losses reached"
    if st.pnl <= -cfg.daily_loss_limit_pct * cfg.capital:
        st.halted = True
        return f"daily loss limit (−{cfg.daily_loss_limit_pct:.1%})"
    if st.last_loss_at and now - datetime.fromisoformat(st.last_loss_at) < timedelta(minutes=cfg.cooldown_after_loss_min):
        return f"{cfg.cooldown_after_loss_min}-min cooldown after a loss"
    if vix is not None and not (cfg.vix_min <= vix <= cfg.vix_max):
        return f"India VIX {vix:.1f} outside {cfg.vix_min:g}–{cfg.vix_max:g}"
    return None


# ====================================================================== levels
def build_levels(spot: List[Bar], cfg: Config, atr_now: float, oi_levels: Optional[List[float]] = None) -> List[float]:
    """PDH / PDL / PDC, pivots, day open, swings (yesterday + today), max-OI strikes, round 100s → merged zones."""
    today = spot[-1].ts.date()
    prev_days = sorted({b.ts.date() for b in spot if b.ts.date() < today})
    raw: List[float] = []
    if prev_days:
        pd_bars = [b for b in spot if b.ts.date() == prev_days[-1]]
        ph, pl, pc = max(b.high for b in pd_bars), min(b.low for b in pd_bars), pd_bars[-1].close
        raw += [ph, pl, pc] + list(pivots(ph, pl, pc, cfg.pivot_type).values())
    day = [b for b in spot if b.ts.date() == today]
    if day:
        raw.append(day[0].open)
    keep = set(prev_days[-1:]) | {today}
    hs, ls = swing_points([b for b in spot if b.ts.date() in keep])
    raw += hs + ls + [float(v) for v in (oi_levels or [])]
    if cfg.ib_mode >= 1:
        raw += ib_levels(spot, cfg)
    last = spot[-1].close
    raw += [math.floor(last / 100) * 100.0, math.ceil(last / 100) * 100.0]
    zones: List[List[float]] = []
    for L in sorted(raw):
        if zones and L - zones[-1][-1] <= cfg.level_zone_atr * atr_now:
            zones[-1].append(L)
        else:
            zones.append([L])
    return [sum(z) / len(z) for z in zones]
