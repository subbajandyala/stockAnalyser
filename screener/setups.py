"""
Intraday setup engine for chart_bot — replaces the additive ±5 score for orders.

The old score summed slow, yesterday-anchored votes (EMA 50/200, prior-day CPR,
gamma wall), so after a gap or a mid-day reversal it sat in WAIT while the move
happened. This engine reads only today's session on closed 3-minute candles:

  regime    trend_up / trend_down — price held on one side of VWAP for 6 candles,
            EMA9 vs EMA21 agrees, ADX ≥ 20 with the matching DI on top;
            everything else is "range"
  setups    (first match wins; a CE and a PE on the same candle cancel out)
    trend_pullback  trend day: dip to EMA9 / VWAP, then a candle closing back
                    through the prior high (low for PE)
    orb             first close beyond the 09:15–09:30 opening range, on the
                    VWAP side of the break
    vwap_reclaim    two closes back above VWAP after being below, higher low,
                    RSI rising (mirror: vwap_reject) — catches gap recoveries
    range_fade      range day: rejection candle at the day's low with RSI turning
                    up, at least 1 ATR below VWAP (mirror at the day's high)

Each setup gives a structure stop on the index (beyond the setup's low/high);
kite_order converts it to the option premium and adds the anti-hunt buffer.
"""

import datetime

import pandas as pd

from screener.chart_analysis import (_adx, _ema, _rsi, _vwap, _with_futures_volume,
                                     drop_open_candle, fetch_candles, resolve_token)

OPEN_RANGE_END = datetime.time(9, 30)
TREND_CANDLES  = 6        # closes on one side of VWAP that make a trend
TREND_ADX      = 20
GAP_PCT        = 0.5      # open this far (%) from yesterday's close = gap day

_PRIORITY = ("trend_pullback", "orb", "vwap_reclaim", "range_fade")
_LABEL = {
    "trend_pullback": "Trend pullback",
    "orb":            "Opening-range breakout",
    "vwap_reclaim":   "VWAP reclaim",
    "vwap_reject":    "VWAP reject",
    "range_fade":     "Range fade",
}


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d["EMA9"]  = _ema(d["Close"], 9)
    d["EMA21"] = _ema(d["Close"], 21)
    d["VWAP"]  = _vwap(d, intraday=True)
    d["RSI"]   = _rsi(d["Close"])
    adx, di_p, di_n = _adx(d)
    d["ADX"], d["DI_pos"], d["DI_neg"] = adx, di_p, di_n
    pc = d["Close"].shift(1)
    tr = pd.concat([d["High"] - d["Low"], (d["High"] - pc).abs(), (d["Low"] - pc).abs()],
                   axis=1).max(axis=1)
    d["ATR"] = tr.ewm(span=14, adjust=False).mean()
    return d


def _regime(t: pd.DataFrame) -> str:
    if len(t) < TREND_CANDLES:
        return "range"
    c, last = t.iloc[-1], t.tail(TREND_CANDLES)
    if ((last["Close"] > last["VWAP"]).all() and c["EMA9"] > c["EMA21"]
            and c["ADX"] >= TREND_ADX and c["DI_pos"] > c["DI_neg"]):
        return "trend_up"
    if ((last["Close"] < last["VWAP"]).all() and c["EMA9"] < c["EMA21"]
            and c["ADX"] >= TREND_ADX and c["DI_neg"] > c["DI_pos"]):
        return "trend_down"
    return "range"


def _candidates(t: pd.DataFrame, regime: str, or_hi: float | None, or_lo: float | None) -> dict:
    """{(setup, side): stop} for every setup the last closed candle triggers."""
    out: dict = {}
    c, p = t.iloc[-1], t.iloc[-2]
    atr = float(c["ATR"]) or 1.0

    # Trend pullback
    if regime == "trend_up":
        touched = min(c["Low"], p["Low"]) <= max(c["EMA9"], c["VWAP"]) + 0.1 * atr
        if touched and c["Close"] > c["EMA9"] and c["Close"] > c["Open"] and c["Close"] > p["High"]:
            out[("trend_pullback", "CE")] = float(t["Low"].tail(3).min())
    if regime == "trend_down":
        touched = max(c["High"], p["High"]) >= min(c["EMA9"], c["VWAP"]) - 0.1 * atr
        if touched and c["Close"] < c["EMA9"] and c["Close"] < c["Open"] and c["Close"] < p["Low"]:
            out[("trend_pullback", "PE")] = float(t["High"].tail(3).max())

    # Opening-range breakout — only the first close beyond each side of the range
    if or_hi is not None:
        earlier = t[t.index.time >= OPEN_RANGE_END].iloc[:-1]
        if (c["Close"] > or_hi >= p["Close"] and c["Close"] > c["VWAP"]
                and not (earlier["Close"] > or_hi).any()):
            out[("orb", "CE")] = float(min(c["Low"], p["Low"]))
        if (c["Close"] < or_lo <= p["Close"] and c["Close"] < c["VWAP"]
                and not (earlier["Close"] < or_lo).any()):
            out[("orb", "PE")] = float(max(c["High"], p["High"]))

    # VWAP reclaim / reject: below → two closes above (or the reverse)
    if len(t) >= 6:
        pp = t.iloc[-3]
        before = t.iloc[-9:-2] if len(t) >= 9 else t.iloc[:-2]
        rsi_then = float(t["RSI"].iloc[-4])
        if (pp["Close"] <= pp["VWAP"] and p["Close"] > p["VWAP"] and c["Close"] > c["VWAP"]
                and min(c["Low"], p["Low"]) > before["Low"].min() and c["RSI"] > rsi_then):
            out[("vwap_reclaim", "CE")] = float(t["Low"].tail(8).min())
        if (pp["Close"] >= pp["VWAP"] and p["Close"] < p["VWAP"] and c["Close"] < c["VWAP"]
                and max(c["High"], p["High"]) < before["High"].max() and c["RSI"] < rsi_then):
            out[("vwap_reclaim", "PE")] = float(t["High"].tail(8).max())

    # Range fade at the day's extremes, back toward VWAP
    if regime == "range" and len(t) >= 6:
        day_lo, day_hi = float(t["Low"].min()), float(t["High"].max())
        rng = float(c["High"] - c["Low"]) or 1e-9
        if (c["Low"] <= day_lo + 0.3 * atr and c["Close"] > c["Open"]
                and (c["Close"] - c["Low"]) >= 0.6 * rng and c["RSI"] < 45 and c["RSI"] > p["RSI"]
                and c["VWAP"] - c["Close"] >= atr):
            out[("range_fade", "CE")] = day_lo
        if (c["High"] >= day_hi - 0.3 * atr and c["Close"] < c["Open"]
                and (c["High"] - c["Close"]) >= 0.6 * rng and c["RSI"] > 55 and c["RSI"] < p["RSI"]
                and c["Close"] - c["VWAP"] >= atr):
            out[("range_fade", "PE")] = day_hi
    return out


def detect(df: pd.DataFrame) -> dict:
    """Read the last CLOSED candle of df (multi-day, IST index). Never raises on short data."""
    res = {"signal": None, "setup": None, "label": "", "regime": "range", "stop": None,
           "trigger_time": None, "spot": None, "reasons": [], "gap_pct": 0.0}
    if df is None or len(df) < 30:
        res["reasons"].append("not enough candles")
        return res
    d = add_indicators(df)
    today = d.index[-1].date()
    t = d[d.index.date == today]
    prev = d[d.index.date < today]
    c = d.iloc[-1]
    res["spot"], res["trigger_time"] = float(c["Close"]), d.index[-1]
    if len(t) < 3:
        res["reasons"].append("waiting for the first candles of the day")
        return res

    if not prev.empty:
        prev_close = float(prev["Close"].iloc[-1])
        res["gap_pct"] = round((float(t["Open"].iloc[0]) - prev_close) / prev_close * 100, 2)
    or_bars = t[t.index.time < OPEN_RANGE_END]
    or_ready = bool((t.index.time >= OPEN_RANGE_END).any()) and not or_bars.empty
    or_hi = float(or_bars["High"].max()) if or_ready else None
    or_lo = float(or_bars["Low"].min()) if or_ready else None

    regime = _regime(t)
    res["regime"] = regime
    cands = _candidates(t, regime, or_hi, or_lo)
    sides = {side for (_, side) in cands}
    ctx = (f"{regime.replace('_', ' ')}, close {c['Close']:,.1f} vs VWAP {c['VWAP']:,.1f}, "
           f"RSI {c['RSI']:.0f}, ADX {c['ADX']:.0f}"
           + (f", gap {res['gap_pct']:+.2f}%" if abs(res["gap_pct"]) >= GAP_PCT else ""))
    res["reasons"].append(ctx)
    if not cands:
        return res
    if len(sides) > 1:
        res["reasons"].append("CE and PE setups on the same candle — skipped")
        return res

    side = sides.pop()
    setup = next(s for s in _PRIORITY if (s, side) in cands)
    stop = cands[(setup, side)]
    atr = float(c["ATR"]) or 1.0
    # A stop on the wrong side (or touching price) falls back to 1 ATR
    if side == "CE" and stop >= c["Close"] - 0.2 * atr:
        stop = float(c["Close"] - atr)
    if side == "PE" and stop <= c["Close"] + 0.2 * atr:
        stop = float(c["Close"] + atr)

    name = "vwap_reject" if setup == "vwap_reclaim" and side == "PE" else setup
    res.update({"signal": side, "setup": name, "label": _LABEL[name], "stop": round(stop, 2)})
    res["reasons"].insert(0, f"{_LABEL[name]} → {'BUY CE' if side == 'CE' else 'BUY PE'}, "
                             f"index stop {stop:,.1f}")
    return res


def scan(symbol: str, interval: str, api_key: str, access_token: str) -> dict:
    """Live: fetch closed candles from Kite (futures volume for VWAP) and detect."""
    try:
        token, name = resolve_token(symbol, api_key, access_token)
        df = fetch_candles(token, interval, api_key, access_token)
    except Exception as e:
        return {"error": str(e)}
    df = _with_futures_volume(df, name, interval, api_key, access_token)
    df = drop_open_candle(df, interval)
    res = detect(df)
    res["error"] = None
    return res
