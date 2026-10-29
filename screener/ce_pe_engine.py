"""
CE/PE Signal Engine — rule-based 5-minute options signal engine.

Branch: chart-screen-logic

Turns the multi-timeframe Chart Analysis (SMC + EMA + VWAP + RSI + ADX + CPR)
into a deterministic verdict every 5 minutes:

    STRONG_CE | STRONG_PE | MODERATE_CE | MODERATE_PE | WAIT
    HOLD | EXIT | NO_TRADE | NO_DATA

Pipeline
--------
  1. analyze_df() on 5m (execution), 15m (confirmation), 1H + 1D (context),
     using COMPLETED candles only (the forming candle is dropped).
  2. build_snapshot()  → plain JSON-serialisable dict of every input value.
  3. evaluate(snapshot, state) → (verdict, new_state). Pure function:
       Step 0  data + session gates
       Step 1  manage the active signal: CHoCH against it, 5m close beyond stop,
               target hit, 15:15 square-off; an unfilled PULLBACK order is
               dropped (EXIT, points = null) after 6 bars or an opposite CHoCH
       Step 2  hard gates G1–G6 for each side (PE is the exact mirror of CE)
       Step 3  score 0–100  (≥75 STRONG, 60–74 MODERATE, else WAIT)
       Step 4  trade plan: entry (PULLBACK into OB/FVG or BREAKOUT), stop,
               target = nearest opposing level, R:R ≥ 1.5
     Optional OI −5 penalty from the spec is not implemented (no OI in the snapshot yet).

Every snapshot + verdict is meant to be appended to a JSONL log so the
thresholds in CFG can be tuned by backtest (see scripts/ce_pe_bot.py).

Educational tooling — not financial advice.
"""

from __future__ import annotations

import datetime
import math
from typing import Any

import numpy as np
import pandas as pd
import requests

from screener.chart_analysis import INTERVAL_CFG, analyze_df

_IST       = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
_KITE_BASE = "https://api.kite.trade"

EXEC_TF    = "5m"
CONFIRM_TF = "15m"
CONTEXT_TFS = ("1H", "1D")
ALL_TFS    = (EXEC_TF, CONFIRM_TF) + CONTEXT_TFS

TF_MINUTES = {"5m": 5, "15m": 15, "1H": 60}

# ── Tunable thresholds (tune these from the backtest log) ─────────────────────
CFG: dict[str, Any] = {
    # session (IST)
    "session_start":      datetime.time(9, 30),   # no new signals before this bar close
    "no_new_after":       datetime.time(15, 0),   # no new signals after this
    "square_off":         datetime.time(15, 15),  # force EXIT of active signals
    "expiry_cutoff":      datetime.time(14, 0),   # no new signals on expiry day after this
    "stale_minutes":      6,                      # last 5m bar close older than this → NO_DATA

    # G1 structure
    "structure_max_bars": 6,
    # G3 strength
    "adx_min":            25.0,
    "adx_lookback":       3,
    "di_spread_min":      5.0,
    # G4 momentum
    "rsi_ce":             (50.0, 72.0),
    "rsi_pe":             (28.0, 50.0),
    # G5 confirmation (15m)
    "htf_rsi_ce_max":     75.0,
    "htf_rsi_pe_min":     25.0,
    # G6 room / plan
    "min_rr":             1.5,
    "near_level_pct":     0.10,   # within 0.10% of an opposing level → G6 fails
    "pullback_near_pct":  0.05,   # score bonus if price is within 0.05% of zone
    "pullback_zone_pct":  0.40,   # OB/FVG must be within 0.40% below price to be the entry
    "stop_buffer_pct":    0.03,   # stop placed this far beyond the zone / swing
    "min_stop_pct":       0.08,   # minimum stop distance (avoid fake huge R:R)
    "fallback_target_r":  2.0,    # target when no opposing level is found
    # scoring
    "strong":             75,
    "moderate":           60,
    "adx_strong":         35.0,
    # signal management
    "pending_max_bars":   6,      # cancel an unfilled PULLBACK order after N bars
    "open_moderate":      True,   # also track MODERATE signals as active (for backtest)
}

BULL_TRENDS = {"Bullish", "Mildly Bullish"}
BEAR_TRENDS = {"Bearish", "Mildly Bearish"}


# ── Small helpers ─────────────────────────────────────────────────────────────

def _f(x, nd: int = 2):
    """float rounded, or None for NaN/None."""
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return round(v, nd)


def _iso(ts) -> str:
    return pd.Timestamp(ts).isoformat()


def _ts(s: str) -> pd.Timestamp:
    t = pd.Timestamp(s)
    if t.tzinfo is None:
        t = t.tz_localize("Asia/Kolkata")
    return t


def _pct(a: float, b: float) -> float:
    return abs(a - b) / b * 100 if b else 0.0


def is_expiry_day(symbol: str, d: datetime.date) -> bool:
    """Weekly/monthly expiry weekday. Exchange holiday shifts are NOT handled."""
    sym = symbol.upper()
    weekly = {"NIFTY": 1, "SENSEX": 3}          # Tue, Thu
    if sym in weekly:
        return d.weekday() == weekly[sym]
    if sym == "BANKNIFTY":                      # monthly: last Tuesday
        nxt = d + datetime.timedelta(days=7)
        return d.weekday() == 1 and nxt.month != d.month
    return False


STRIKE_STEP = {"NIFTY": 50, "BANKNIFTY": 100, "SENSEX": 100, "FINNIFTY": 50, "MIDCPNIFTY": 25}


def atm_strike(symbol: str, spot: float) -> int:
    step = STRIKE_STEP.get(symbol.upper(), 50)
    return int(round(spot / step) * step)


# ── Candle helpers ────────────────────────────────────────────────────────────

def drop_forming_candle(df: pd.DataFrame, interval_key: str,
                        now: pd.Timestamp) -> pd.DataFrame:
    """Keep only candles that have fully closed at `now`."""
    if df is None or df.empty:
        return df
    if interval_key == "1D":
        return df[df.index.date < now.date()]
    mins = TF_MINUTES[interval_key]
    return df[df.index + pd.Timedelta(minutes=mins) <= now]


def slice_like_live(df: pd.DataFrame, interval_key: str,
                    now: pd.Timestamp) -> pd.DataFrame:
    """Reproduce what fetch_candles() would have returned at `now` (replay)."""
    days = INTERVAL_CFG[interval_key]["days"]
    df = df[df.index >= now - pd.Timedelta(days=days)]
    df = drop_forming_candle(df, interval_key, now)
    return df.tail(750)


_KITE_CHUNK_DAYS = {"5minute": 90, "15minute": 180, "60minute": 380, "day": 1900}


def fetch_range(token: int, interval_key: str, start: datetime.date,
                end: datetime.date, api_key: str, access_token: str) -> pd.DataFrame:
    """Fetch historical candles for a date range, chunked to Kite's per-request limits."""
    kite_iv = INTERVAL_CFG[interval_key]["kite"]
    chunk   = _KITE_CHUNK_DAYS.get(kite_iv, 60)
    hdrs    = {"X-Kite-Version": "3", "Authorization": f"token {api_key}:{access_token}"}
    rows: list = []
    cur = start
    while cur <= end:
        stop = min(end, cur + datetime.timedelta(days=chunk))
        r = requests.get(
            f"{_KITE_BASE}/instruments/historical/{token}/{kite_iv}",
            headers=hdrs,
            params={"from": f"{cur} 09:00:00", "to": f"{stop} 15:35:00",
                    "continuous": 0, "oi": 0},
            timeout=30,
        )
        if not r.ok:
            raise RuntimeError(f"Kite {r.status_code}: {r.text[:200]}")
        rows += r.json().get("data", {}).get("candles", [])
        cur = stop + datetime.timedelta(days=1)
    return candles_to_df(rows, interval_key)


def candles_to_df(rows: list, interval_key: str) -> pd.DataFrame:
    """Kite candle rows [[dt, o, h, l, c, v], ...] → IST-indexed OHLCV frame."""
    df = pd.DataFrame([r[:6] for r in rows],
                      columns=["datetime", "Open", "High", "Low", "Close", "Volume"])
    if df.empty:
        return df
    df["datetime"] = pd.to_datetime(df["datetime"])
    if df["datetime"].dt.tz is None:
        df["datetime"] = df["datetime"].dt.tz_localize("Asia/Kolkata")
    else:
        df["datetime"] = df["datetime"].dt.tz_convert("Asia/Kolkata")
    df = df.drop_duplicates("datetime").set_index("datetime").sort_index()
    if interval_key != "1D":
        df = df.between_time("09:15", "15:35")
    return df


# ── Snapshot ──────────────────────────────────────────────────────────────────

def _event_dict(df: pd.DataFrame, ev: dict) -> dict:
    kind, direction = ev["type"].split("_")
    pos = df.index.get_loc(ev["index"])
    return {
        "type":      "CHoCH" if kind == "CHOCH" else "BOS",
        "direction": direction,
        "level":     _f(ev["level"]),
        "bars_ago":  int(len(df) - 1 - pos),
        "time":      _iso(ev["index"]),
    }


def _ob_broken(df: pd.DataFrame, ob: dict) -> bool:
    """An OB is invalidated once a later candle CLOSES beyond its far side."""
    after = df[df.index > ob["index"]]
    if after.empty:
        return False
    if ob["type"] == "bullish":
        return bool((after["Close"] < ob["bottom"]).any())
    return bool((after["Close"] > ob["top"]).any())


def tf_snapshot(res: dict) -> dict | None:
    """Compact, JSON-serialisable view of one timeframe's analysis."""
    if not res or res.get("error"):
        return None
    df   = res["df"]
    last = df.iloc[-1]
    lb   = CFG["adx_lookback"]
    evs  = res.get("events") or []

    def swings(col: str, price_col: str) -> list:
        s = df[df[col]].tail(2)
        return [{"price": _f(r[price_col]), "rsi": _f(r["RSI"], 1), "time": _iso(i)}
                for i, r in s.iterrows()]

    obs = [{
        "side":   o["type"],
        "top":    _f(o["top"]),
        "bottom": _f(o["bottom"]),
        "time":   _iso(o["index"]),
        "broken": _ob_broken(df, o),
    } for o in (res.get("obs") or [])]

    fvgs = [{
        "side":   g["type"],
        "top":    _f(g["top"]),
        "bottom": _f(g["bottom"]),
        "time":   _iso(g["index"]),
    } for g in (res.get("fvgs") or [])]

    return {
        "bar_time":       _iso(df.index[-1]),
        "open":           _f(last["Open"]),
        "high":           _f(last["High"]),
        "low":            _f(last["Low"]),
        "close":          _f(last["Close"]),
        "ema20":          _f(last.get("EMA20")),
        "ema50":          _f(last.get("EMA50")),
        "ema200":         _f(last.get("EMA200")),
        "vwap":           _f(last.get("VWAP")),
        "rsi14":          _f(last.get("RSI"), 1),
        "adx14":          _f(last.get("ADX"), 1),
        "adx14_prev":     _f(df["ADX"].iloc[-1 - lb], 1) if len(df) > lb else None,
        "di_plus":        _f(last.get("DI_pos"), 1),
        "di_minus":       _f(last.get("DI_neg"), 1),
        "trend_label":    res["summary"]["trend"],
        "last_structure": _event_dict(df, evs[-1]) if evs else None,
        "recent_events":  [_event_dict(df, e) for e in evs[-6:]],
        "swing_highs":    swings("swing_high", "High"),
        "swing_lows":     swings("swing_low", "Low"),
        "order_blocks":   obs,
        "fvgs":           fvgs,
    }


def build_snapshot(symbol: str, results: dict, now: pd.Timestamp) -> dict:
    """results: {tf_key: analyze_df() result}. now: tz-aware IST timestamp."""
    snap: dict[str, Any] = {
        "symbol":        symbol.upper(),
        "timestamp_ist": _iso(now),
    }
    for tf in ALL_TFS:
        snap[f"tf_{tf}"] = tf_snapshot(results.get(tf))

    cpr = (results.get(EXEC_TF) or {}).get("cpr") or {}
    snap["levels"] = {k: cpr.get(k) for k in
                      ("tc", "pivot", "bc", "r1", "r2", "r3", "s1", "s2", "s3")} if cpr else {}
    snap["levels"]["cpr_width_pct"] = cpr.get("width_pct") if cpr else None

    df5 = (results.get(EXEC_TF) or {}).get("df")
    bars_today = int((df5.index.date == now.date()).sum()) if df5 is not None else 0
    snap["session"] = {
        "time_ist":         now.strftime("%H:%M"),
        "bars_since_open":  bars_today,
        "expiry_day":       is_expiry_day(symbol, now.date()),
    }
    return snap


def analyze_frames(symbol: str, frames: dict, now: pd.Timestamp,
                   cache: dict | None = None) -> dict:
    """Slice raw history frames to `now` and run analyze_df on each timeframe.

    `cache` (optional) re-uses higher-timeframe results while their last
    completed candle is unchanged — keeps replay fast.
    """
    results = {}
    for tf in ALL_TFS:
        raw = frames.get(tf)
        if raw is None or raw.empty:
            results[tf] = {"error": "no data"}
            continue
        sl = slice_like_live(raw, tf, now)
        key = (tf, sl.index[-1] if len(sl) else None, len(sl))
        if cache is not None and tf != EXEC_TF and key in cache:
            results[tf] = cache[key]
            continue
        res = analyze_df(sl, tf, symbol=symbol)
        if cache is not None and tf != EXEC_TF:
            cache[key] = res
        results[tf] = res
    return results


# ── Evaluation ────────────────────────────────────────────────────────────────

def _sign(side: str) -> int:
    return 1 if side == "CE" else -1


def _required_ok(tf: dict | None) -> bool:
    need = ("close", "ema20", "ema50", "rsi14", "adx14", "di_plus", "di_minus")
    return bool(tf) and all(tf.get(k) is not None for k in need)


def _pullback_zone(s5: dict, side: str) -> dict | None:
    """Nearest unbroken OB / unfilled FVG on the side of the trade, just behind price."""
    close = s5["close"]
    want  = "bullish" if side == "CE" else "bearish"
    zones = [z for z in s5["order_blocks"] if z["side"] == want and not z["broken"]]
    zones += [z for z in s5["fvgs"] if z["side"] == want]
    max_d = CFG["pullback_zone_pct"] / 100 * close
    near  = CFG["pullback_near_pct"] / 100 * close
    best = None
    for z in zones:
        if side == "CE":
            # zone at/below price (or price slightly below top), within max distance
            if z["bottom"] < close and z["top"] <= close + near and close - z["top"] <= max_d:
                if best is None or z["top"] > best["top"]:
                    best = z
        else:
            if z["top"] > close and z["bottom"] >= close - near and z["bottom"] - close <= max_d:
                if best is None or z["bottom"] < best["bottom"]:
                    best = z
    return best


def _opposing_levels(snap: dict, side: str) -> list[tuple[float, str]]:
    """Levels that stand in the way of the trade, beyond current price."""
    s5, s15 = snap["tf_5m"], snap.get("tf_15m") or {}
    close = s5["close"]
    out: list[tuple[float, str]] = []
    for k, v in (snap.get("levels") or {}).items():
        if k == "cpr_width_pct" or v is None:
            continue
        out.append((float(v), f"CPR {k.upper()}"))
    opp = "bearish" if side == "CE" else "bullish"
    for tf_name, tf in (("5m", s5), ("15m", s15)):
        for z in tf.get("order_blocks", []):
            if z["side"] == opp and not z["broken"]:
                lvl = z["bottom"] if side == "CE" else z["top"]
                out.append((lvl, f"{tf_name} {opp} OB"))
        swings = tf.get("swing_highs" if side == "CE" else "swing_lows", [])
        for s in swings:
            if s["price"] is not None:
                out.append((s["price"], f"{tf_name} swing {'high' if side == 'CE' else 'low'}"))
    if side == "CE":
        return sorted([(l, n) for l, n in out if l > close], key=lambda x: x[0])
    return sorted([(l, n) for l, n in out if l < close], key=lambda x: -x[0])


def _plan(snap: dict, side: str) -> dict:
    s5    = snap["tf_5m"]
    close = s5["close"]
    sgn   = _sign(side)
    buf   = CFG["stop_buffer_pct"] / 100

    zone  = _pullback_zone(s5, side)
    if zone:
        entry = min(close, zone["top"]) if side == "CE" else max(close, zone["bottom"])
        entry_type = "PULLBACK"
    else:
        entry, entry_type = close, "BREAKOUT"

    # Stop: closest valid candidate beyond entry
    cands = []
    if zone:
        cands.append(zone["bottom"] * (1 - buf) if side == "CE" else zone["top"] * (1 + buf))
    swings = s5["swing_lows" if side == "CE" else "swing_highs"]
    if swings:
        sw = swings[-1]["price"]
        cands.append(sw * (1 - buf) if side == "CE" else sw * (1 + buf))
    cands = [c for c in cands if (c < entry if side == "CE" else c > entry)]
    min_risk = CFG["min_stop_pct"] / 100 * entry
    if cands:
        stop = max(cands) if side == "CE" else min(cands)
    else:
        stop = entry - sgn * 2 * min_risk
    if abs(entry - stop) < min_risk:
        stop = entry - sgn * min_risk
    risk = abs(entry - stop)

    levels = _opposing_levels(snap, side)
    nearest = levels[0] if levels else None
    # target must also be beyond entry
    tgt_lv = next(((l, n) for l, n in levels if (l > entry if side == "CE" else l < entry)), None)
    if tgt_lv:
        target, target_src = tgt_lv
    else:
        target, target_src = entry + sgn * CFG["fallback_target_r"] * risk, \
            f"no opposing level — {CFG['fallback_target_r']}R"
    rr = abs(target - entry) / risk if risk else 0.0

    near_level = bool(nearest and _pct(nearest[0], close) < CFG["near_level_pct"])
    sw_inv = s5["swing_lows" if side == "CE" else "swing_highs"]

    return {
        "entry_type":   entry_type,
        "entry":        round(entry, 1),
        "stop":         round(stop, 1),
        "target":       round(target, 1),
        "target_src":   target_src,
        "rr":           round(rr, 2),
        "invalidation": sw_inv[-1]["price"] if sw_inv else round(stop, 1),
        "zone":         zone,
        "nearest_opposing": {"level": round(nearest[0], 1), "name": nearest[1]} if nearest else None,
        "near_level":   near_level,
    }


def _side_check(snap: dict, side: str) -> dict:
    s5, s15 = snap["tf_5m"], snap["tf_15m"]
    s1h, s1d = snap.get("tf_1H") or {}, snap.get("tf_1D") or {}
    up      = side == "CE"
    close   = s5["close"]
    want    = "UP" if up else "DOWN"
    ls      = s5.get("last_structure")
    reasons: list[str] = []
    gates: dict[str, bool] = {}

    # G1 structure
    g1 = bool(ls and ls["direction"] == want
              and ls["bars_ago"] <= CFG["structure_max_bars"]
              and (close > ls["level"] if up else close < ls["level"]))
    gates["G1"] = g1
    if ls:
        reasons.append(f"5m {ls['type']} {ls['direction']} @ {ls['level']:,.1f}, {ls['bars_ago']} bars ago")

    # G2 trend
    vwap = s5.get("vwap")
    if up:
        g2 = s5["ema20"] > s5["ema50"] and vwap is not None and close > vwap
    else:
        g2 = s5["ema20"] < s5["ema50"] and vwap is not None and close < vwap
    gates["G2"] = bool(g2)

    # G3 strength
    adx, adx_p = s5["adx14"], s5.get("adx14_prev")
    spread = (s5["di_plus"] - s5["di_minus"]) * (1 if up else -1)
    g3 = adx >= CFG["adx_min"] and adx_p is not None and adx >= adx_p and spread >= CFG["di_spread_min"]
    gates["G3"] = bool(g3)

    # G4 momentum
    lo, hi = CFG["rsi_ce"] if up else CFG["rsi_pe"]
    gates["G4"] = bool(lo <= s5["rsi14"] <= hi)

    # G5 15m confirmation
    if up:
        g5 = s15["trend_label"] in BULL_TRENDS and s15["rsi14"] < CFG["htf_rsi_ce_max"]
    else:
        g5 = s15["trend_label"] in BEAR_TRENDS and s15["rsi14"] > CFG["htf_rsi_pe_min"]
    gates["G5"] = bool(g5)

    # G6 room
    plan = _plan(snap, side)
    gates["G6"] = bool(plan["rr"] >= CFG["min_rr"] and not plan["near_level"])

    reasons.append(f"5m EMA20 {s5['ema20']:,.1f} vs EMA50 {s5['ema50']:,.1f}, "
                   f"close {close:,.1f} vs VWAP {vwap if vwap is None else f'{vwap:,.1f}'}")
    reasons.append(f"5m ADX {adx} (3 bars ago {adx_p}), DI+ {s5['di_plus']} / DI− {s5['di_minus']}, RSI {s5['rsi14']}")
    reasons.append(f"15m {s15['trend_label']}, RSI {s15['rsi14']}")
    no = plan["nearest_opposing"]
    reasons.append(f"Plan {plan['entry_type']} {plan['entry']:,.1f} / SL {plan['stop']:,.1f} / "
                   f"T {plan['target']:,.1f} ({plan['target_src']}) R:R {plan['rr']}"
                   + (f"; nearest opposing {no['name']} {no['level']:,.1f}" if no else ""))

    # Score (meaningful only when all gates pass, but always computed for the log)
    score = 0
    parts: list[str] = []
    if g1:
        pts = 20 if ls["type"] == "BOS" else 15
        score += pts; parts.append(f"+{pts} 5m {ls['type']}")
    if (up and s5["ema20"] > s5["ema50"] > (s5["ema200"] or -1e18)) or \
       (not up and s5["ema200"] is not None and s5["ema20"] < s5["ema50"] < s5["ema200"]):
        score += 15; parts.append("+15 EMA stack")
    if adx >= CFG["adx_strong"] and adx_p is not None and adx >= adx_p:
        score += 10; parts.append("+10 ADX≥35 rising")
    ls15 = s15.get("last_structure")
    if ls15 and ls15["direction"] == want:
        score += 10; parts.append("+10 15m structure agrees")
    if s1h.get("trend_label") in (BULL_TRENDS if up else BEAR_TRENDS):
        score += 10; parts.append("+10 1H trend agrees")
    lv = snap.get("levels") or {}
    if up and lv.get("tc") and close > lv["tc"]:
        score += 10; parts.append("+10 above CPR TC")
    if not up and lv.get("bc") and close < lv["bc"]:
        score += 10; parts.append("+10 below CPR BC")
    z = plan["zone"]
    if z:
        near = CFG["pullback_near_pct"] / 100 * close
        inside = (z["bottom"] <= close <= z["top"] + near) if up else (z["bottom"] - near <= close <= z["top"])
        if inside:
            score += 10; parts.append("+10 at OB/FVG pullback")
    d_tr = s1d.get("trend_label")
    if d_tr in (BULL_TRENDS if up else BEAR_TRENDS):
        score += 5; parts.append("+5 daily agrees")
    elif d_tr in (BEAR_TRENDS if up else BULL_TRENDS):
        score -= 10; parts.append("−10 daily opposes")
    if _divergence(s5, side):
        score -= 10; parts.append("−10 RSI divergence")
    score = max(0, min(100, score))

    return {"side": side, "gates": gates, "all_pass": all(gates.values()),
            "score": score, "score_parts": parts, "plan": plan,
            "trigger_time": ls["time"] if ls else None, "reasons": reasons}


def _divergence(s5: dict, side: str) -> bool:
    """CE: higher high in price with lower RSI. PE: mirror on swing lows.
    Compares the last two confirmed swings, and the current bar vs the last swing."""
    key  = "swing_highs" if side == "CE" else "swing_lows"
    sw   = [s for s in s5.get(key, []) if s["price"] is not None and s["rsi"] is not None]
    rsi  = s5["rsi14"]
    if side == "CE":
        if len(sw) == 2 and sw[1]["price"] > sw[0]["price"] and sw[1]["rsi"] < sw[0]["rsi"]:
            return True
        if sw and s5["high"] > sw[-1]["price"] and rsi < sw[-1]["rsi"]:
            return True
    else:
        if len(sw) == 2 and sw[1]["price"] < sw[0]["price"] and sw[1]["rsi"] > sw[0]["rsi"]:
            return True
        if sw and s5["low"] < sw[-1]["price"] and rsi > sw[-1]["rsi"]:
            return True
    return False


def _manage_active(snap: dict, active: dict, now: pd.Timestamp) -> tuple[str, dict]:
    """Returns (action, info). action ∈ HOLD | EXIT | FILLED."""
    s5   = snap["tf_5m"]
    side = active["side"]
    up   = side == "CE"
    hi, lo, close = s5["high"], s5["low"], s5["close"]
    opp  = "DOWN" if up else "UP"
    opened = _ts(active["opened_at"])
    opp_choch = next((e for e in s5.get("recent_events", [])
                      if e["type"] == "CHoCH" and e["direction"] == opp
                      # event time is the candle START; it is known once that candle closes
                      and _ts(e["time"]) + pd.Timedelta(minutes=5) > opened), None)
    t = now.time()
    info: dict[str, Any] = {}

    stop_hit = (close < active["stop"]) if up else (close > active["stop"])   # spec: CLOSE beyond stop

    if not active.get("filled"):
        filled_now = (lo <= active["entry"]) if up else (hi >= active["entry"])
        if filled_now:
            info["filled_at"] = _iso(now)
            if stop_hit:                                   # same-bar stop after the fill
                return "EXIT", {**info, "exit_reason": "stop (fill bar)", "exit_price": close}
            return "FILLED", info
        unfilled = None
        if opp_choch:
            unfilled = f"5m CHoCH {opp} before fill"
        elif active.get("bars_waiting", 0) + 1 >= CFG["pending_max_bars"]:
            unfilled = f"not filled in {CFG['pending_max_bars']} bars"
        elif t >= CFG["square_off"]:
            unfilled = "session end"
        if unfilled:
            return "EXIT", {"exit_reason": f"unfilled pullback cancelled — {unfilled}",
                            "exit_price": None, "unfilled": True}
        return "HOLD", {"pending": True}

    if stop_hit:
        return "EXIT", {"exit_reason": "stop (5m close beyond stop)", "exit_price": close}
    if (up and hi >= active["target"]) or (not up and lo <= active["target"]):
        return "EXIT", {"exit_reason": "target", "exit_price": active["target"]}
    if opp_choch:
        return "EXIT", {"exit_reason": f"5m CHoCH {opp}", "exit_price": close}
    if t >= CFG["square_off"]:
        return "EXIT", {"exit_reason": "time (square-off)", "exit_price": close}
    return "HOLD", {}


def evaluate(snap: dict, state: dict | None = None) -> tuple[dict, dict]:
    """Pure decision function.

    state: {"active": {...} | None, "last_trigger": {"CE": iso|None, "PE": iso|None}}
    Returns (verdict, new_state). Verdict is JSON-serialisable.
    """
    state = dict(state or {})
    state.setdefault("active", None)
    state.setdefault("last_trigger", {"CE": None, "PE": None})
    now = _ts(snap["timestamp_ist"])
    t   = now.time()

    out: dict[str, Any] = {
        "timestamp_ist": snap["timestamp_ist"],
        "symbol":        snap.get("symbol"),
        "bar_time":      (snap.get("tf_5m") or {}).get("bar_time"),
        "action":        "WAIT",
        "side":          None,
        "grade":         None,
        "score":         None,
        "gates":         None,
        "failed_reason": None,
        "plan":          None,
        "reasoning":     [],
        "checks":        None,
    }

    # Step 0 — data
    s5, s15 = snap.get("tf_5m"), snap.get("tf_15m")
    if not (_required_ok(s5) and _required_ok(s15)):
        out.update(action="NO_DATA", failed_reason="missing 5m/15m indicator fields")
        return out, state
    bar_close = _ts(s5["bar_time"]) + pd.Timedelta(minutes=5)
    if (now - bar_close) > pd.Timedelta(minutes=CFG["stale_minutes"]):
        out.update(action="NO_DATA", failed_reason=f"stale data: last 5m bar closed {bar_close:%H:%M}")
        return out, state

    # Step 1 — manage active signal
    active = state.get("active")
    if active:
        action, info = _manage_active(snap, active, now)
        out["side"], out["grade"] = active["side"], active.get("grade")
        out["plan"] = {k: active[k] for k in ("entry_type", "entry", "stop", "target", "rr")}
        if action == "EXIT":
            pts = None
            if info.get("exit_price") is not None:
                pts = round((info["exit_price"] - active["entry"]) * _sign(active["side"]), 1)
            out.update(action="EXIT", **info, points=pts,
                       reasoning=[f"EXIT {active['side']}: {info['exit_reason']}"])
            state["active"] = None
            return out, state
        new_active = dict(active)
        if action == "FILLED":
            new_active.update(filled=True, filled_at=info["filled_at"])
            out["reasoning"] = [f"PULLBACK entry {active['entry']:,.1f} filled"]
        elif info.get("pending"):
            new_active["bars_waiting"] = active.get("bars_waiting", 0) + 1
            out["reasoning"] = [f"Waiting for pullback fill at {active['entry']:,.1f}"]
        out["action"] = "HOLD"
        state["active"] = new_active
        return out, state

    # Session gates (no new signals)
    sess = snap.get("session") or {}
    if t < CFG["session_start"]:
        out.update(action="NO_TRADE", failed_reason="before 09:30 IST")
        return out, state
    if t > CFG["no_new_after"]:
        out.update(action="NO_TRADE", failed_reason="after 15:00 IST — no new signals")
        return out, state
    if sess.get("expiry_day") and t >= CFG["expiry_cutoff"]:
        out.update(action="NO_TRADE", failed_reason="expiry day after 14:00 IST")
        return out, state

    # Steps 2–4
    ce, pe = _side_check(snap, "CE"), _side_check(snap, "PE")
    out["checks"] = {
        s["side"]: {"gates": s["gates"], "score": s["score"], "score_parts": s["score_parts"]}
        for s in (ce, pe)
    }
    passing = [s for s in (ce, pe) if s["all_pass"]]
    if len(passing) == 2:
        out.update(failed_reason="both sides qualified — conflicting data")
        return out, state
    if not passing:
        best = max((ce, pe), key=lambda s: (sum(s["gates"].values()), s["score"]))
        failed = [g for g, ok in best["gates"].items() if not ok]
        out.update(side=best["side"], gates=best["gates"], score=None,
                   failed_reason=f"{best['side']} closest; failed {', '.join(failed)}",
                   reasoning=best["reasons"][:5])
        return out, state

    sel = passing[0]
    side, score = sel["side"], sel["score"]
    out.update(side=side, gates=sel["gates"], score=score, reasoning=sel["reasons"][:5])
    if score >= CFG["strong"]:
        grade = "STRONG"
    elif score >= CFG["moderate"]:
        grade = "MODERATE"
    else:
        out.update(failed_reason=f"all gates pass but score {score} < {CFG['moderate']}")
        return out, state

    if state["last_trigger"].get(side) and state["last_trigger"][side] == sel["trigger_time"]:
        out.update(failed_reason=f"{side} already signalled on this structure break")
        return out, state

    plan = sel["plan"]
    out.update(action=f"{grade}_{side}", grade=grade,
               plan={k: plan[k] for k in ("entry_type", "entry", "stop", "target",
                                          "target_src", "rr", "invalidation")})
    out["plan"]["strike_hint"] = f"{atm_strike(snap.get('symbol') or '', s5['close'])} {side} (ATM)"
    out["score_parts"] = sel["score_parts"]

    if grade == "STRONG" or CFG["open_moderate"]:
        filled = plan["entry_type"] == "BREAKOUT" or \
            (plan["entry"] >= s5["close"] if side == "CE" else plan["entry"] <= s5["close"])
        state["active"] = {
            "side": side, "grade": grade, "entry_type": plan["entry_type"],
            "entry": plan["entry"], "stop": plan["stop"], "target": plan["target"],
            "rr": plan["rr"], "opened_at": _iso(now), "filled": filled,
            "filled_at": _iso(now) if filled else None, "bars_waiting": 0,
            "trigger_time": sel["trigger_time"],
        }
        lt = dict(state["last_trigger"]); lt[side] = sel["trigger_time"]
        state["last_trigger"] = lt
    return out, state


# ── Live entry point ──────────────────────────────────────────────────────────

def run_live(symbol: str, api_key: str, access_token: str,
             state: dict | None = None, now: pd.Timestamp | None = None) -> tuple[dict, dict, dict]:
    """Fetch all timeframes now, evaluate once. Returns (snapshot, verdict, new_state)."""
    from screener.chart_analysis import fetch_candles, resolve_token
    now = now or pd.Timestamp.now(tz="Asia/Kolkata")
    token, name = resolve_token(symbol, api_key, access_token)
    results = {}
    for tf in ALL_TFS:
        try:
            df = fetch_candles(token, tf, api_key, access_token)
            df = drop_forming_candle(df, tf, now)
            results[tf] = analyze_df(df, tf, symbol=name)
        except Exception as e:  # keep going — evaluate() reports NO_DATA
            results[tf] = {"error": str(e)}
    snap = build_snapshot(name, results, now)
    verdict, new_state = evaluate(snap, state)
    return snap, verdict, new_state
