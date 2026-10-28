"""
Candle reader for chart_bot — the default order engine ("candles").

Reads the 3-minute candle that is FORMING right now (last tick from the live
feed when the ticker daemon runs) against the previous candles, and names it:

  strong bull / bear     big body (≥ 60% of the range), closing at the top /
                         bottom quarter, bigger than the recent average, through
                         the previous candle's high / low          → CE / PE
  bullish / bearish      body swallows the previous opposite candle → CE / PE
  engulfing
  hammer / shooting star long lower / upper wick (≥ 2× body) at a recent low /
                         high, close in the top / bottom third      → CE / PE
  small / doji / mixed   nothing to trade — "small" = range under MIN_RANGE_MULT
                         (1.2) × the normal range (median of the last 20 candles,
                         today's 09:15–09:30 opening candles left out)

Stop for the trade = the candle's low (CE) or high (PE); kite_order turns it
into the option stop and subtracts the anti-hunt buffer.

The Supertrend filter (screener/trend_filter.py) can block a side: the candle
is still named, but signal stays None.
"""

import datetime

import pandas as pd

from screener import trend_filter
from screener.chart_analysis import _IST, fetch_candles, resolve_token

MIN_AGE_SEC = 60          # a candle younger than this hasn't shown its shape yet
OPENING_END = datetime.time(9, 30)   # opening candles are outsized — kept out of "normal size"
AVG_CANDLES = 20
# Only trade candles at least this many times the normal range (median of the last 20,
# today's 09:15–09:30 candles left out).
# 1.2 cut whipsaw / chop losses by about two-thirds in the stress test (23 Oct);
# chart_bot sets it from CHART_CANDLE_MIN_MULT.
MIN_RANGE_MULT = 1.2
_LABEL = {
    "strong_bull": "Strong bull candle", "strong_bear": "Strong bear candle",
    "bull_engulf": "Bullish engulfing",  "bear_engulf": "Bearish engulfing",
    "hammer":      "Hammer",             "shooting_star": "Shooting star",
}


def _normal_range(d: pd.DataFrame) -> float:
    """Median range of the last AVG_CANDLES candles before the current one.

    Today's opening candles (09:15–09:30) are left out once enough others exist:
    on 23 Oct they pushed a plain 10-candle mean to 45 pts on NIFTY (normal ≈ 15–25),
    so nothing qualified all morning. A median also ignores the odd spike candle.
    """
    prior = d.iloc[:-1]
    today = d.index[-1].date()
    keep = ~((prior.index.date == today) & (prior.index.time < OPENING_END))
    if keep.sum() >= 10:
        prior = prior[keep]
    return float((prior["High"] - prior["Low"]).tail(AVG_CANDLES).median()) or 1e-9


def read(df: pd.DataFrame) -> dict:
    """Name the LAST candle of df (it may still be forming) → signal CE / PE / None.

    Pass a few sessions of candles for the Supertrend filter (it needs the
    history); the candle itself only looks at the last 40."""
    res = {"signal": None, "pattern": None, "label": "", "stop": None, "spot": None,
           "candle_time": None, "text": "", "range_mult": 0.0, "blocked": None,
           "side": None}
    if df is None or len(df) < 12:
        res["text"] = "not enough candles"
        return res
    d = df.tail(40)
    c, p = d.iloc[-1], d.iloc[-2]
    o, h, l, cl = float(c["Open"]), float(c["High"]), float(c["Low"]), float(c["Close"])
    rng, body = h - l, cl - o
    avg = _normal_range(d)
    res.update(spot=cl, candle_time=d.index[-1], range_mult=round(rng / avg, 2))
    if rng <= 0 or rng < MIN_RANGE_MULT * avg:
        res["text"] = (f"small candle ({rng:.1f} pts, needs {MIN_RANGE_MULT:g}× avg {avg:.1f} "
                       f"= {MIN_RANGE_MULT * avg:.1f}) — no trade")
        return res
    pos = (cl - l) / rng                       # 0 = closed at the low, 1 = at the high
    upper, lower = h - max(o, cl), min(o, cl) - l
    p_bear, p_bull = p["Close"] < p["Open"], p["Close"] > p["Open"]
    recent_lo, recent_hi = float(d["Low"].iloc[-6:-1].min()), float(d["High"].iloc[-6:-1].max())

    pattern = None
    if body > 0 and p_bear and cl >= p["Open"] and o <= p["Close"] and body >= 0.5 * rng:
        pattern = "bull_engulf"
    elif body < 0 and p_bull and cl <= p["Open"] and o >= p["Close"] and -body >= 0.5 * rng:
        pattern = "bear_engulf"
    elif body > 0 and body >= 0.6 * rng and pos >= 0.75 and rng >= avg and cl > p["High"]:
        pattern = "strong_bull"
    elif body < 0 and -body >= 0.6 * rng and pos <= 0.25 and rng >= avg and cl < p["Low"]:
        pattern = "strong_bear"
    elif lower >= 2 * abs(body) and pos >= 0.66 and l <= recent_lo:
        pattern = "hammer"
    elif upper >= 2 * abs(body) and pos <= 0.34 and h >= recent_hi:
        pattern = "shooting_star"

    if not pattern:
        res["text"] = f"mixed candle (body {body:+.1f}, range {rng:.1f}, close at {pos:.0%} of range)"
        return res
    side = "CE" if pattern in ("bull_engulf", "strong_bull", "hammer") else "PE"
    # "side" stays set when the filter blocks the entry: an open trade still exits on an opposite candle
    res.update(signal=side, side=side, pattern=pattern, label=_LABEL[pattern], stop=round(l if side == "CE" else h, 2))
    res["text"] = (f"{_LABEL[pattern]} → BUY {side} (O {o:,.1f} H {h:,.1f} L {l:,.1f} C {cl:,.1f}, "
                   f"range {rng:.1f} = {rng / avg:.1f}× avg {avg:.1f})")
    blocked = trend_filter.check(df, side, cl)
    if blocked:
        res.update(signal=None, blocked=blocked)
        res["text"] += f" — skipped: {blocked}"
    return res


def scan(symbol: str, interval: str, api_key: str, access_token: str,
         now: datetime.datetime | None = None) -> dict:
    """Live: Kite candles + the last tick for the forming candle, then read()."""
    try:
        token, _ = resolve_token(symbol, api_key, access_token)
        df = fetch_candles(token, interval, api_key, access_token)
    except Exception as e:
        return {"error": str(e)}
    now = now or datetime.datetime.now(_IST)
    try:                                   # freshest price from the ticker daemon
        from screener.live_ticks import get_live_ltp
        ltp = get_live_ltp(token)
    except Exception:
        ltp = None
    if ltp and not df.empty:
        i = df.index[-1]
        df = df.copy()
        df.loc[i, "Close"] = ltp
        df.loc[i, "High"] = max(df.loc[i, "High"], ltp)
        df.loc[i, "Low"] = min(df.loc[i, "Low"], ltp)
    res = read(df)
    res["error"] = None
    start = res.get("candle_time")
    age = (now - start).total_seconds() if start is not None else 0
    res["forming"] = age < {"1m": 60, "3m": 180, "5m": 300}.get(interval, 180)
    if res["side"] and res["forming"] and age < MIN_AGE_SEC:
        res["text"] += f" — candle only {age:.0f}s old, waiting for its shape"
        res["signal"] = res["side"] = None
    return res
