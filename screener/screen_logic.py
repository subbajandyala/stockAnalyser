"""
Screen logic — rule-based 5-minute index signal engine (chart_bot engine "screen").

After every 5-minute candle closes, a snapshot of indicators is built from the
closed 5m / 15m / 1h / daily candles and the rules below decide one action.
Only the data in the snapshot is used. Chart source (CHART_SCREEN_SOURCE):
  future (default)  the front-month NIFTY FUT / SENSEX FUT candles for 5m / 15m / 1h, with
                    their own volume for VWAP; daily = continuous futures (spot daily if Kite
                    refuses). Levels are in futures prices; the order converts them to spot
                    with the basis (future − spot at the decision candle).
  spot              the index itself; VWAP weighted by the future's volume.

STEP 0  data / session gates
        last closed 5m candle older than 6 min, or a 5m / 15m value missing → NO_DATA
        before 09:30 or after 15:00 IST → no new signal (an open one is still managed)
        expiry day after 14:00 → no new signal
STEP 1  manage the open signal first
        CE: EXIT on a 5m CHoCH DOWN, a close below the stop, or the target hit; else HOLD
        PE: the mirror. Never opens the other side in the same decision as an EXIT.
STEP 2  hard gates for CE (PE is the mirror)
        G1 5m last structure UP (BOS / CHoCH) ≤ 6 bars ago, close still above the broken level
        G2 5m EMA20 > EMA50 and close > VWAP
        G3 5m ADX14 ≥ 25, ADX ≥ ADX 3 bars ago, DI+ − DI− ≥ 5
        G4 5m RSI14 50–72 (PE 28–50)
        G5 15m trend Bullish / Mildly Bullish and 15m RSI < 75 (PE: Bearish / Mildly Bearish, RSI > 25)
        G6 room to the nearest opposing level ≥ 1.5 × risk, and not within 0.1% of a resistance
STEP 3  score 0–100: +20 BOS / +15 CHoCH, +15 EMA stack, +10 ADX ≥ 35 rising, +10 15m
        structure agrees, +10 1h trend agrees, +10 beyond CPR (TC for CE, BC for PE),
        +10 pullback zone, +5 / −10 daily trend agrees / opposes, −10 RSI divergence
        → ≥ 75 STRONG, 60–74 MODERATE, < 60 WAIT
STEP 4  plan: PULLBACK entry at the top of an unmitigated bullish OB / unfilled bullish FVG
        within 0.05% of price, else BREAKOUT at the close; stop just below that zone or the
        last swing low (the closer valid one); target = nearest opposing level; R:R < 1.5 → WAIT
If both sides qualify → WAIT.

Definitions used here (the input spec leaves them open):
  trend_label   EMA20 > EMA50 > EMA200 and close > EMA20 → Bullish; mirror → Bearish;
                else close vs EMA50 → Mildly Bullish / Mildly Bearish; else Neutral
  swings        3-bar fractals, confirmed only once the 3 candles after them have closed
  BOS / CHoCH   a close beyond the latest confirmed swing high (low) that has not been broken
                yet: CHoCH if it is against the current structure, BOS if with it
  OB mitigated  a later candle CLOSED through the far side of the block (it failed)
  FVG filled    a later candle traded through the whole gap
  CPR           previous trading day's H / L / C;  resistance / support = 1h swing highs /
                lows, previous day high / low, pivots R1–R3 / S1–S3 (not the 5m / 15m swings:
                those are the structure itself, and a minor one sits within 0.1% of price
                almost all the time, so G6 never passed); levels within 0.05% count once
  stop buffer   0.02% beyond the zone / swing (the stop sits BELOW the level)
"""

import datetime

import numpy as np
import pandas as pd

from screener.chart_analysis import (_KITE_BASE, _hdrs, _with_futures_volume, compute_indicators, fetch_candles,
                                     front_future_token, resolve_token)

_IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

SIGNAL_OPEN = datetime.time(9, 30)
SIGNAL_CLOSE = datetime.time(15, 0)
EXPIRY_CUTOFF = datetime.time(14, 0)
MAX_AGE_MIN = 6
STRUCT_MAX_BARS = 6
# Tunable thresholds (chart_bot reads CHART_SCREEN_* from .env; defaults are the spec)
ROOM_RR = 1.5              # G6 / plan: room to the nearest opposing level, in R   (CHART_SCREEN_ROOM_RR)
NEAR_RES_PCT = 0.001       # G6: no trade within 0.1% of a level (0 = off)        (CHART_SCREEN_NEAR_PCT, in %)
OPEN_TARGET_R = 0.0        # no level beyond the entry: target = entry ± this × R  (CHART_SCREEN_OPEN_TARGET_R, 0 = WAIT)
G5_NEUTRAL = False         # G5: a Neutral 15m trend also confirms               (CHART_SCREEN_15M_NEUTRAL=1)
ZONE_NEAR_PCT = 0.0005     # 0.05%
STOP_BUFFER_PCT = 0.0002   # 0.02%
LEVEL_MERGE_PCT = 0.0005   # 0.05%: levels this close together are one level
LEVEL_TFS = ("tf_1h",)
STRONG, MODERATE = 75, 60  # score grades                                         (CHART_SCREEN_STRONG / _MODERATE)
HISTORY = {"5m": 300, "15m": 300, "1H": 300, "1D": 300}
SOURCE = "future"          # chart: "future" (front-month FUT) or "spot" (the index)  (CHART_SCREEN_SOURCE)
_BULL = ("Bullish", "Mildly Bullish")
_BEAR = ("Bearish", "Mildly Bearish")


# ── Snapshot ─────────────────────────────────────────────────────────────────

def _f(x) -> float | None:
    try:
        v = float(x)
        return None if np.isnan(v) else v
    except (TypeError, ValueError):
        return None


def _trend_label(close, e20, e50, e200) -> str:
    if None in (close, e20, e50, e200):
        return "Neutral"
    if e20 > e50 > e200 and close > e20:
        return "Bullish"
    if e20 < e50 < e200 and close < e20:
        return "Bearish"
    if close > e50:
        return "Mildly Bullish"
    if close < e50:
        return "Mildly Bearish"
    return "Neutral"


def structure(df: pd.DataFrame, n: int = 3) -> tuple[list, list, list]:
    """Causal swings and BOS / CHoCH: at each candle only swings confirmed by then are used.

    Returns (events, swing_highs, swing_lows); events: {type, pos, time, level}, swings:
    (pos, time, price) in the order they were confirmed."""
    hi, lo, cl = df["High"].to_numpy(float), df["Low"].to_numpy(float), df["Close"].to_numpy(float)
    events, sh, slw = [], [], []
    trend, act_h, act_l = None, None, None
    for i in range(len(df)):
        j = i - n                                   # the candle whose swing is confirmed now
        if j >= n:
            if hi[j] == hi[j - n: i + 1].max():
                sh.append((j, df.index[j], float(hi[j])))
                act_h = float(hi[j])
            if lo[j] == lo[j - n: i + 1].min():
                slw.append((j, df.index[j], float(lo[j])))
                act_l = float(lo[j])
        if act_h is not None and cl[i] > act_h:
            events.append({"type": "CHOCH_UP" if trend == "down" else "BOS_UP", "pos": i,
                           "time": df.index[i], "level": act_h})
            trend, act_h = "up", None
        elif act_l is not None and cl[i] < act_l:
            events.append({"type": "CHOCH_DOWN" if trend == "up" else "BOS_DOWN", "pos": i,
                           "time": df.index[i], "level": act_l})
            trend, act_l = "down", None
    return events, sh, slw


def _order_blocks(df: pd.DataFrame, events: list) -> list:
    """Last opposite candle before each structure break; mitigated = later close through it."""
    close = df["Close"].to_numpy(float)
    out = []
    for ev in events:
        pos = ev["pos"]
        up = ev["type"] in ("BOS_UP", "CHOCH_UP")
        for j in range(pos - 1, max(-1, pos - 20), -1):
            o, c = float(df["Open"].iloc[j]), float(df["Close"].iloc[j])
            if (up and c < o) or (not up and c > o):
                top, bottom = (o, c) if up else (c, o)
                later = close[j + 1:]
                mitigated = bool((later < bottom).any()) if up else bool((later > top).any())
                out.append({"side": "bullish" if up else "bearish", "top": top, "bottom": bottom,
                            "mitigated": mitigated, "time": df.index[j]})
                break
    return out[-10:]


def _fvgs(df: pd.DataFrame) -> list:
    """Three-candle gaps; filled = a later candle traded through the whole gap."""
    hi, lo = df["High"].to_numpy(float), df["Low"].to_numpy(float)
    out = []
    for i in range(1, len(df) - 1):
        if lo[i + 1] > hi[i - 1]:
            top, bottom = lo[i + 1], hi[i - 1]
            out.append({"side": "bullish", "top": float(top), "bottom": float(bottom),
                        "filled": bool((lo[i + 2:] <= bottom).any()), "time": df.index[i]})
        elif hi[i + 1] < lo[i - 1]:
            top, bottom = lo[i - 1], hi[i + 1]
            out.append({"side": "bearish", "top": float(top), "bottom": float(bottom),
                        "filled": bool((hi[i + 2:] >= top).any()), "time": df.index[i]})
    return out[-15:]


def tf_view(df: pd.DataFrame, intraday: bool = True, detail: bool = True) -> dict:
    """Indicators, structure, swings (and OBs / FVGs when detail) for one timeframe."""
    if df is None or len(df) < 30:
        return {}
    d = compute_indicators(df, intraday=intraday)
    events, sh, sl = structure(d)
    last = d.iloc[-1]
    n = len(d)
    close = _f(last["Close"])
    v = {
        "time": d.index[-1], "close": close, "high": _f(last["High"]), "low": _f(last["Low"]),
        "ema20": _f(last["EMA20"]), "ema50": _f(last["EMA50"]), "ema200": _f(last["EMA200"]),
        "vwap": _f(last["VWAP"]) if intraday else None,
        "rsi14": _f(last["RSI"]), "adx14": _f(last["ADX"]),
        "adx14_prev": _f(d["ADX"].iloc[-4]) if n >= 4 else None,
        "di_plus": _f(last["DI_pos"]), "di_minus": _f(last["DI_neg"]),
    }
    v["trend_label"] = _trend_label(close, v["ema20"], v["ema50"], v["ema200"])
    if events:
        ev = events[-1]
        v["last_structure"] = {"type": "BOS" if ev["type"].startswith("BOS") else "CHoCH",
                               "direction": "UP" if ev["type"].endswith("UP") else "DOWN",
                               "level": float(ev["level"]), "bars_ago": n - 1 - ev["pos"], "time": ev["time"]}
    else:
        v["last_structure"] = None
    v["events"] = [{"type": e["type"], "time": e["time"], "level": e["level"]} for e in events[-20:]]
    rsi = d["RSI"].to_numpy(float)
    v["swing_highs"] = [(t, x) for _, t, x in sh[-20:]]
    v["swing_lows"] = [(t, x) for _, t, x in sl[-20:]]
    v["swing_high_rsi"] = [_f(rsi[p]) for p, _, _ in sh[-2:]]
    v["swing_low_rsi"] = [_f(rsi[p]) for p, _, _ in sl[-2:]]
    v["last_swing_high"] = v["swing_highs"][-1][1] if v["swing_highs"] else None
    v["last_swing_low"] = v["swing_lows"][-1][1] if v["swing_lows"] else None
    if detail:
        v["order_blocks"] = _order_blocks(d, events)
        v["fvgs"] = _fvgs(d.tail(120))
    return v


def _cached_view(cache: dict | None, key: str, df: pd.DataFrame, **kw) -> dict:
    """tf_view, reused while the timeframe has no new candle (the replay passes a cache)."""
    if df is None or not len(df):
        return {}
    if cache is None:
        return tf_view(df, **kw)
    k = (key, df.index[-1], len(df))
    if k not in cache:
        cache[k] = tf_view(df, **kw)
    return cache[k]


def _merge(levels: list) -> list:
    """Levels sorted outward from price; one within 0.05% of the previous kept one is dropped."""
    out = []
    for x in levels:
        if not out or abs(x - out[-1]) > out[-1] * LEVEL_MERGE_PCT:
            out.append(round(x, 2))
    return out


def settings() -> dict:
    return {"room_rr": ROOM_RR, "near_pct": NEAR_RES_PCT * 100, "open_target_r": OPEN_TARGET_R,
            "g5_neutral": G5_NEUTRAL, "strong": STRONG, "moderate": MODERATE}


def apply(cfg: dict) -> None:
    """Set the thresholds (keys as in settings(); missing keys keep their value)."""
    global ROOM_RR, NEAR_RES_PCT, OPEN_TARGET_R, G5_NEUTRAL, STRONG, MODERATE
    ROOM_RR = float(cfg.get("room_rr", ROOM_RR))
    NEAR_RES_PCT = float(cfg.get("near_pct", NEAR_RES_PCT * 100)) / 100
    OPEN_TARGET_R = float(cfg.get("open_target_r", OPEN_TARGET_R))
    G5_NEUTRAL = bool(cfg.get("g5_neutral", G5_NEUTRAL))
    STRONG = int(cfg.get("strong", STRONG))
    MODERATE = int(cfg.get("moderate", MODERATE))


def build_snapshot(frames: dict, now: datetime.datetime, expiry_day: bool = False,
                   cache: dict | None = None) -> dict:
    """frames: closed candles {"5m", "15m", "1H", "1D"} (IST-naive index). now: IST-naive."""
    f5, f15, f1h, fd = (frames.get(k) for k in ("5m", "15m", "1H", "1D"))
    snap = {"now": now, "expiry_day": expiry_day,
            "tf_5m": tf_view(f5.tail(HISTORY["5m"])) if f5 is not None and len(f5) else {},
            "tf_15m": _cached_view(cache, "15m", f15.tail(HISTORY["15m"]) if f15 is not None else None,
                                   detail=False),
            "tf_1h": _cached_view(cache, "1H", f1h.tail(HISTORY["1H"]) if f1h is not None else None,
                                  detail=False),
            "tf_daily": _cached_view(cache, "1D", fd.tail(HISTORY["1D"]) if fd is not None else None,
                                     intraday=False, detail=False)}
    t5 = snap["tf_5m"]
    snap["data_age_min"] = ((now - (t5["time"] + pd.Timedelta(minutes=5))).total_seconds() / 60
                            if t5 else None)
    levels = {"cpr_tc": None, "cpr_p": None, "cpr_bc": None, "resistance": [], "support": []}
    today = now.date()
    prev = fd[fd.index.date < today] if fd is not None and len(fd) else None
    if prev is not None and len(prev):
        h, l, c = (float(prev[k].iloc[-1]) for k in ("High", "Low", "Close"))
        p = (h + l + c) / 3
        bc, tc = sorted(((h + l) / 2, 2 * p - (h + l) / 2))
        levels.update(cpr_tc=tc, cpr_p=p, cpr_bc=bc)
        levels["resistance"] += [h, 2 * p - l, p + (h - l), h + 2 * (p - l)]   # PDH, R1, R2, R3
        levels["support"] += [l, 2 * p - h, p - (h - l), l - 2 * (h - p)]      # PDL, S1, S2, S3
    for tf in LEVEL_TFS:
        v = snap[tf]
        levels["resistance"] += [x for _, x in v.get("swing_highs", [])]
        levels["support"] += [x for _, x in v.get("swing_lows", [])]
    close = t5.get("close") if t5 else None
    if close:
        levels["resistance"] = _merge(sorted(x for x in levels["resistance"] if x > close))
        levels["support"] = _merge(sorted((x for x in levels["support"] if x < close), reverse=True))
    snap["levels"] = levels
    snap["source"] = frames.get("_source", "spot")
    snap["basis"] = 0.0
    sp = frames.get("_spot5m")
    if t5 and sp is not None and len(sp):
        upto = sp[sp.index <= t5["time"]]
        if len(upto):
            snap["basis"] = round(t5["close"] - float(upto["Close"].iloc[-1]), 2)
    snap["session"] = {"time_ist": now.strftime("%H:%M"),
                       "bars_since_open": int(((t5["time"] - t5["time"].normalize()).total_seconds() / 60
                                               - 555) // 5 + 1) if t5 else None}
    return snap


# ── Decision ─────────────────────────────────────────────────────────────────

_REQUIRED = ("close", "ema20", "ema50", "vwap", "rsi14", "adx14", "adx14_prev", "di_plus", "di_minus")


def _side_gates(snap: dict, side: str) -> tuple[dict, dict, list]:
    """Gates G1–G5 for one side → (gates, context for the plan, notes)."""
    t5, t15 = snap["tf_5m"], snap["tf_15m"]
    ce = side == "CE"
    close = t5["close"]
    st = t5.get("last_structure")
    g, notes = {}, []
    g["G1"] = bool(st and st["direction"] == ("UP" if ce else "DOWN") and st["bars_ago"] <= STRUCT_MAX_BARS
                   and (close >= st["level"] if ce else close <= st["level"]))
    notes.append(f"5m {st['type']} {st['direction']} at {st['level']:.1f}, {st['bars_ago']} bars ago"
                 if st else "5m: no structure break yet")
    g["G2"] = (t5["ema20"] > t5["ema50"] and close > t5["vwap"]) if ce else \
              (t5["ema20"] < t5["ema50"] and close < t5["vwap"])
    di = t5["di_plus"] - t5["di_minus"] if ce else t5["di_minus"] - t5["di_plus"]
    g["G3"] = t5["adx14"] >= 25 and t5["adx14"] >= t5["adx14_prev"] and di >= 5
    g["G4"] = (50 <= t5["rsi14"] <= 72) if ce else (28 <= t5["rsi14"] <= 50)
    r15 = t15.get("rsi14")
    ok15 = (_BULL if ce else _BEAR) + (("Neutral",) if G5_NEUTRAL else ())
    g["G5"] = bool(r15 is not None and t15.get("trend_label") in ok15 and (r15 < 75 if ce else r15 > 25))
    return g, {"di": di}, notes


def _plan(snap: dict, side: str) -> dict:
    """Entry / stop / target / R:R for one side (STEP 4) — used by G6 as well."""
    t5, lv = snap["tf_5m"], snap["levels"]
    ce = side == "CE"
    close = t5["close"]
    buf = close * STOP_BUFFER_PCT
    zones = [z for z in t5.get("order_blocks", []) if z["side"] == ("bullish" if ce else "bearish")
             and not z["mitigated"]]
    zones += [z for z in t5.get("fvgs", []) if z["side"] == ("bullish" if ce else "bearish") and not z["filled"]]
    if ce:
        near = [z for z in zones if z["bottom"] <= close <= z["top"] * (1 + ZONE_NEAR_PCT)]
        zone = max(near, key=lambda z: z["top"]) if near else None
    else:
        near = [z for z in zones if z["bottom"] * (1 - ZONE_NEAR_PCT) <= close <= z["top"]]
        zone = min(near, key=lambda z: z["bottom"]) if near else None
    entry_type = "PULLBACK" if zone else "BREAKOUT"
    entry = (zone["top"] if ce else zone["bottom"]) if zone else close
    swing = t5.get("last_swing_low") if ce else t5.get("last_swing_high")
    cands = []
    if zone:
        cands.append(zone["bottom"] - buf if ce else zone["top"] + buf)
    if swing is not None:
        cands.append(swing - buf if ce else swing + buf)
    valid = [s for s in cands if (s < entry if ce else s > entry)]
    stop = (max(valid) if ce else min(valid)) if valid else None
    # Opposing levels: resistance / bearish OBs / CPR TC from below (PE: support / bullish OBs / CPR BC from above)
    opp = list(lv["resistance"] if ce else lv["support"])
    opp += [z["bottom"] if ce else z["top"] for z in t5.get("order_blocks", [])
            if z["side"] == ("bearish" if ce else "bullish") and not z["mitigated"]]
    if ce and lv.get("cpr_tc") and entry < lv["cpr_tc"]:
        opp.append(lv["cpr_tc"])
    if not ce and lv.get("cpr_bc") and entry > lv["cpr_bc"]:
        opp.append(lv["cpr_bc"])
    opp = [x for x in opp if (x > entry if ce else x < entry)]
    target = (min(opp) if ce else max(opp)) if opp else None
    risk = (entry - stop if ce else stop - entry) if stop is not None else None
    if target is None and OPEN_TARGET_R and risk and risk > 0:      # beyond every level: open room
        target = entry + OPEN_TARGET_R * risk if ce else entry - OPEN_TARGET_R * risk
    rr = (abs(target - entry) / risk) if (target is not None and risk and risk > 0) else None
    near_res = bool(NEAR_RES_PCT) and any(abs(x - close) <= close * NEAR_RES_PCT
                                          for x in (lv["resistance"] if ce else lv["support"]))
    return {"entry_type": entry_type, "entry": round(entry, 2), "stop": round(stop, 2) if stop is not None else None,
            "target": round(target, 2) if target is not None else None, "rr": round(rr, 2) if rr else None,
            "invalidation": round(swing, 2) if swing is not None else None,
            "strike_hint": f"ATM {side}", "_zone": zone, "_near_level": near_res, "_risk": risk}


def _divergence(t5: dict, side: str) -> bool:
    """CE: last two swing highs — price higher high, RSI lower high. PE: the mirror on swing lows."""
    pts, rsis = (t5["swing_highs"], t5["swing_high_rsi"]) if side == "CE" else (t5["swing_lows"], t5["swing_low_rsi"])
    if len(pts) < 2 or len(rsis) < 2 or None in rsis:
        return False
    (_, p1), (_, p2) = pts[-2], pts[-1]
    return (p2 > p1 and rsis[1] < rsis[0]) if side == "CE" else (p2 < p1 and rsis[1] > rsis[0])


def _score(snap: dict, side: str, plan: dict) -> tuple[int, list]:
    t5, t15, t1h, td, lv = (snap[k] for k in ("tf_5m", "tf_15m", "tf_1h", "tf_daily", "levels"))
    ce = side == "CE"
    agree, oppose = (_BULL, _BEAR) if ce else (_BEAR, _BULL)
    s, why = 0, []

    def add(pts, cond, text):
        nonlocal s
        if cond:
            s += pts
            why.append(f"{pts:+d} {text}")

    st = t5.get("last_structure") or {}
    add(20, st.get("type") == "BOS", "5m BOS")
    add(15, st.get("type") == "CHoCH", "5m CHoCH")
    add(15, (t5["ema20"] > t5["ema50"] > (t5["ema200"] or 0)) if ce else
            (t5["ema200"] is not None and t5["ema20"] < t5["ema50"] < t5["ema200"]), "5m EMA stack")
    add(10, t5["adx14"] >= 35 and t5["adx14"] >= t5["adx14_prev"], f"5m ADX {t5['adx14']:.0f} rising")
    st15 = t15.get("last_structure") or {}
    add(10, st15.get("direction") == ("UP" if ce else "DOWN"), "15m structure agrees")
    add(10, t1h.get("trend_label") in agree, f"1h {t1h.get('trend_label')}")
    add(10, (lv.get("cpr_tc") and t5["close"] > lv["cpr_tc"]) if ce else
            (lv.get("cpr_bc") and t5["close"] < lv["cpr_bc"]), "beyond CPR")
    add(10, plan["entry_type"] == "PULLBACK", "pullback zone")
    add(5, td.get("trend_label") in agree, f"daily {td.get('trend_label')}")
    add(-10, td.get("trend_label") in oppose, f"daily {td.get('trend_label')}")
    add(-10, _divergence(t5, side), "5m RSI divergence")
    return max(0, min(100, s)), why


def _manage(snap: dict, active: dict) -> dict:
    """STEP 1: EXIT / HOLD for the open signal."""
    t5 = snap["tf_5m"]
    ce = active["side"] == "CE"
    opened = pd.Timestamp(active["opened_at"])
    choch = [e for e in t5.get("events", []) if e["type"] == ("CHOCH_DOWN" if ce else "CHOCH_UP")
             and pd.Timestamp(e["time"]) >= opened]
    close, stop, target = t5["close"], active.get("stop"), active.get("target")
    hi, lo = t5.get("high"), t5.get("low")
    why = None
    if choch:
        why = f"5m CHoCH {'DOWN' if ce else 'UP'} at {choch[-1]['level']:.1f}"
    elif stop and (close < stop if ce else close > stop):
        why = f"5m close {close:.1f} {'below' if ce else 'above'} the stop {stop:.1f}"
    elif target and ((hi or close) >= target if ce else (lo or close) <= target):
        why = f"target {target:.1f} hit"
    return {"action": "EXIT" if why else "HOLD", "failed_reason": why or "",
            "reasoning": [why] if why else [f"{active['side']} open: close {close:.1f}, stop {stop}, target {target}"]}


def evaluate_sides(snap: dict) -> dict:
    """{side: (gates G1–G6, plan, notes)} for CE and PE (also used by the replay's gate report)."""
    sides = {}
    for side in ("CE", "PE"):
        gates, _, notes = _side_gates(snap, side)
        plan = _plan(snap, side)
        risk = plan["_risk"]
        gates["G6"] = bool(plan["stop"] is not None and plan["target"] is not None and risk and risk > 0
                           and abs(plan["target"] - plan["entry"]) >= ROOM_RR * risk and not plan["_near_level"])
        sides[side] = (gates, plan, notes)
    return sides


def decide(snap: dict, active: dict | None = None) -> dict:
    """The rules → {action, score, gates, failed_reason, plan, reasoning, side}."""
    t5, t15 = snap.get("tf_5m") or {}, snap.get("tf_15m") or {}
    out = {"timestamp_ist": snap["now"].strftime("%Y-%m-%d %H:%M"), "action": "NO_DATA", "score": None,
           "gates": {}, "failed_reason": "", "plan": None, "reasoning": [], "side": None}
    missing = [k for k in _REQUIRED if t5.get(k) is None] + \
              [f"15m {k}" for k in ("rsi14", "trend_label") if t15.get(k) is None]
    if not t5 or missing or snap.get("data_age_min") is None or snap["data_age_min"] > MAX_AGE_MIN:
        out["failed_reason"] = (f"missing {', '.join(missing)}" if missing else
                                f"last 5m candle {snap.get('data_age_min') or 0:.0f} min old")
        return out
    if active:
        out.update(_manage(snap, active))
        return out
    t = snap["now"].time()
    if not (SIGNAL_OPEN <= t <= SIGNAL_CLOSE):
        out.update(action="NO_TRADE", failed_reason=f"outside {SIGNAL_OPEN:%H:%M}–{SIGNAL_CLOSE:%H:%M}")
        return out
    if snap.get("expiry_day") and t > EXPIRY_CUTOFF:
        out.update(action="NO_TRADE", failed_reason="expiry day after 14:00")
        return out

    sides = evaluate_sides(snap)
    passed = [s for s, (g, _, _) in sides.items() if all(g.values())]
    if len(passed) == 2:
        out.update(action="WAIT", failed_reason="both sides qualify", gates=sides["CE"][0])
        return out
    if not passed:
        best = max(sides, key=lambda s: sum(sides[s][0].values()))
        g, plan, notes = sides[best]
        failed = [k for k, ok in g.items() if not ok]
        out.update(action="WAIT", side=best, gates=g,
                   failed_reason=f"{best}: {', '.join(failed)} failed",
                   reasoning=notes + [f"5m close {t5['close']:.1f} EMA20 {t5['ema20']:.1f} EMA50 {t5['ema50']:.1f} "
                                      f"VWAP {t5['vwap']:.1f}",
                                      f"5m RSI {t5['rsi14']:.1f} ADX {t5['adx14']:.1f} (3 bars ago "
                                      f"{t5['adx14_prev']:.1f}) DI+ {t5['di_plus']:.1f} DI− {t5['di_minus']:.1f}",
                                      f"15m {t15.get('trend_label')} RSI {t15.get('rsi14') or 0:.1f}"][:5])
        return out
    side = passed[0]
    g, plan, notes = sides[side]
    score, why = _score(snap, side, plan)
    grade = "STRONG" if score >= STRONG else "MODERATE" if score >= MODERATE else None
    public_plan = {k: v for k, v in plan.items() if not k.startswith("_")}
    out.update(side=side, gates=g, score=score, plan=public_plan,
               reasoning=(notes + why)[:5])
    if not grade:
        out.update(action="WAIT", failed_reason=f"{side} score {score} < {MODERATE}")
    elif not plan["rr"] or plan["rr"] < ROOM_RR:
        out.update(action="WAIT", failed_reason=f"{side} R:R {plan['rr']} < {ROOM_RR}")
    else:
        out["action"] = f"{grade}_{side}"
    return out


# ── Live ─────────────────────────────────────────────────────────────────────

_TF_DROP = {"5m": 5, "15m": 15, "1H": 60}


def _closed(df: pd.DataFrame, minutes: int | None, now: datetime.datetime) -> pd.DataFrame:
    """IST-naive index, forming candle dropped (Kite stamps candles at their start)."""
    if df is None or df.empty:
        return df
    df = df.copy()
    if df.index.tz is not None:
        df.index = df.index.tz_convert(_IST).tz_localize(None)
    if minutes:
        df = df[df.index + pd.Timedelta(minutes=minutes) <= now]
    else:
        df = df[df.index.date < now.date()]          # daily: completed days only
    return df


def daily_continuous(fut_token: int, api_key: str, access_token: str, days: int = 500) -> pd.DataFrame:
    """Daily candles of the futures as one continuous series (Kite stitches the expired months)."""
    to = datetime.date.today()
    r = requests.get(f"{_KITE_BASE}/instruments/historical/{fut_token}/day", headers=_hdrs(api_key, access_token),
                     params={"from": f"{to - datetime.timedelta(days=days)} 00:00:00", "to": f"{to} 23:59:59",
                             "continuous": 1, "oi": 0}, timeout=30)
    r.raise_for_status()
    c = r.json().get("data", {}).get("candles", [])
    if not c:
        raise RuntimeError("no continuous daily candles")
    df = pd.DataFrame(c, columns=["datetime", "Open", "High", "Low", "Close", "Volume"])
    df["datetime"] = pd.to_datetime(df["datetime"])
    df["datetime"] = (df["datetime"].dt.tz_localize("Asia/Kolkata") if df["datetime"].dt.tz is None
                      else df["datetime"].dt.tz_convert("Asia/Kolkata"))
    return df.set_index("datetime").sort_index()


def fetch_frames(symbol: str, api_key: str, access_token: str, now: datetime.datetime,
                 source: str | None = None) -> dict:
    """Closed 5m / 15m / 1h / daily candles of the futures (default) or the index.

    Futures: also the index's 5m candles ("_spot5m") for the basis. Without a futures
    contract the index is used and "_source" says so."""
    source = (source or SOURCE).lower()
    token, _ = resolve_token(symbol, api_key, access_token)
    fut = front_future_token(symbol, api_key, access_token) if source == "future" else None
    frames = {}
    if fut:
        frames["_source"] = "future"
        for key in ("5m", "15m", "1H"):
            frames[key] = _closed(fetch_candles(fut, key, api_key, access_token), _TF_DROP.get(key), now)
        try:
            frames["1D"] = _closed(daily_continuous(fut, api_key, access_token), None, now)
        except Exception:
            frames["1D"] = _closed(fetch_candles(token, "1D", api_key, access_token), None, now)
            frames["_source"] = "future (daily: spot)"
        frames["_spot5m"] = _closed(fetch_candles(token, "5m", api_key, access_token), 5, now)
        return frames
    frames["_source"] = "spot" if source == "spot" else "spot (no futures contract found)"
    for key in ("5m", "15m", "1H", "1D"):
        df = fetch_candles(token, key, api_key, access_token)
        if key in ("5m", "15m"):
            df = _with_futures_volume(df, symbol, key, api_key, access_token)
        frames[key] = _closed(df, _TF_DROP.get(key), now)
    return frames


def scan(symbol: str, api_key: str, access_token: str, now: datetime.datetime | None = None,
         active: dict | None = None, expiry_day: bool = False) -> dict:
    """Live: fetch, build the snapshot at the last closed 5m candle, decide."""
    now = (now or datetime.datetime.now(_IST))
    now = now.astimezone(_IST).replace(tzinfo=None) if now.tzinfo else now
    try:
        frames = fetch_frames(symbol, api_key, access_token, now)
    except Exception as e:
        return {"error": str(e)}
    snap = build_snapshot(frames, now, expiry_day=expiry_day)
    res = decide(snap, active)
    res["bar"] = snap["tf_5m"].get("time") if snap["tf_5m"] else None
    res["close"] = snap["tf_5m"].get("close") if snap["tf_5m"] else None
    res["source"], res["basis"] = snap.get("source"), snap.get("basis", 0.0)
    res["error"] = None
    return res


def summary(res: dict) -> str:
    """One log / push line for a decision."""
    if res.get("error"):
        return f"ERROR — {res['error']}"
    a = res["action"]
    gates = " ".join(f"{k}{'✓' if v else '✗'}" for k, v in (res.get("gates") or {}).items())
    p = res.get("plan") or {}
    plan = (f" | {p['entry_type']} entry {p['entry']:.1f} stop {p['stop']:.1f} target {p['target']:.1f} "
            f"R:R {p['rr']}" if p and p.get("stop") is not None and p.get("target") is not None else "")
    score = f" score {res['score']}" if res.get("score") is not None else ""
    reason = f" — {res['failed_reason']}" if res.get("failed_reason") else ""
    return f"{a}{score} {gates}{plan}{reason}".strip()
