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

Chart source (chart_bot sets SOURCE from CHART_CANDLE_SOURCE): "spot" (default) reads
the index; "future" reads the current-month NIFTY FUT / SENSEX FUT candles, with the
future's last price from Kite for the forming candle. A futures reading also carries
"basis" (future − index now) so the order can work in spot prices.
"""

import datetime

import pandas as pd

import requests

from screener.chart_analysis import _IST, _KITE_BASE, _hdrs, fetch_candles, front_future, resolve_token

MIN_AGE_SEC = 60          # a candle younger than this hasn't shown its shape yet
OPENING_END = datetime.time(9, 30)   # opening candles are outsized — kept out of "normal size"
AVG_CANDLES = 20
# Only trade candles at least this many times the normal range (median of the last 20,
# today's 09:15–09:30 candles left out).
# 1.2 cut whipsaw / chop losses by about two-thirds in the stress test (23 Oct);
# chart_bot sets it from CHART_CANDLE_MIN_MULT.
MIN_RANGE_MULT = 1.2
SOURCE = "spot"           # "spot" (the index) or "future" (current-month futures)
# Momentum v3 (chart_bot sets these from .env):
PATTERNS = None           # set of patterns that may trade (None = all); v3: strong + engulfing only
ON_CLOSE = False          # True: trade the candle that just CLOSED (within CLOSE_WINDOW_SEC), not the forming one
CLOSE_WINDOW_SEC = 90     # a closed candle older than this is stale — no entry on it any more
CHASE_RANGE = 0.25        # skip when price already ran this share of the candle's range past its close
VOL_MULT = 0.0            # closed candle volume ≥ this × normal (median of 20, opening left out); 0 = off
_INDEX_QUOTE = {"NIFTY": "NSE:NIFTY 50", "BANKNIFTY": "NSE:NIFTY BANK", "SENSEX": "BSE:SENSEX"}
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
    """Name the LAST candle of df (it may still be forming) → signal CE / PE / None."""
    res = {"signal": None, "pattern": None, "label": "", "stop": None, "spot": None,
           "candle_time": None, "text": "", "range_mult": 0.0}
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
    if PATTERNS is not None and pattern not in PATTERNS:
        res["text"] = f"{_LABEL[pattern]} — not a traded pattern"
        res["pattern"] = pattern
        return res
    side = "CE" if pattern in ("bull_engulf", "strong_bull", "hammer") else "PE"
    res.update(signal=side, pattern=pattern, label=_LABEL[pattern], stop=round(l if side == "CE" else h, 2),
               mid=round((h + l) / 2, 2), close=cl, high=h, low=l)
    res["text"] = (f"{_LABEL[pattern]} → BUY {side} (O {o:,.1f} H {h:,.1f} L {l:,.1f} C {cl:,.1f}, "
                   f"range {rng:.1f} = {rng / avg:.1f}× avg {avg:.1f})")
    return res


def _quote_ltp(keys: list, api_key: str, access_token: str) -> dict:
    """{"EXCH:SYMBOL": last price} from Kite's LTP quote; {} on any error."""
    try:
        r = requests.get(f"{_KITE_BASE}/quote/ltp", headers=_hdrs(api_key, access_token),
                         params=[("i", k) for k in keys], timeout=10)
        r.raise_for_status()
        return {k: float(v["last_price"]) for k, v in r.json().get("data", {}).items()}
    except Exception:
        return {}


def _live_ltp(token: int) -> float | None:
    try:                                   # freshest price from the ticker daemon
        from screener.live_ticks import get_live_ltp
        return get_live_ltp(token)
    except Exception:
        return None


def _volume_ratio(d: pd.DataFrame) -> float:
    """Last candle's volume ÷ the normal volume (median of the 20 before it, opening left out)."""
    if "Volume" not in d or len(d) < 12 or float(d["Volume"].iloc[-1]) <= 0:
        return 0.0
    prior = d.iloc[:-1]
    today = d.index[-1].date()
    keep = ~((prior.index.date == today) & (prior.index.time < OPENING_END))
    if keep.sum() >= 10:
        prior = prior[keep]
    normal = float(prior["Volume"].tail(AVG_CANDLES).median() or 0)
    return float(d["Volume"].iloc[-1]) / normal if normal > 0 else 0.0


def _read_closed(df: pd.DataFrame, ltp: float | None, interval: str, now: datetime.datetime) -> dict:
    """v3 entry: read the last CLOSED candle; trade it only right after it closed, without chasing."""
    tf = {"1m": 60, "3m": 180, "5m": 300}.get(interval, 180)
    closed = df
    if len(df) and (now - df.index[-1]).total_seconds() < tf:
        closed = df.iloc[:-1]                       # drop the forming candle
    res = read(closed)
    res["forming"] = False
    if res.get("candle_time") is None:
        return res
    res["lc_time"], res["lc_close"] = res["candle_time"], res.get("close", res.get("spot"))
    if not res["signal"]:
        return res
    age = (now - res["candle_time"]).total_seconds() - tf     # seconds since it closed
    if age > CLOSE_WINDOW_SEC:
        res["text"] += f" — closed {age:.0f}s ago, too late to enter"
        res["signal"] = None
        return res
    if VOL_MULT:
        vr = _volume_ratio(closed.tail(40))
        res["vol_ratio"] = round(vr, 2)
        if vr < VOL_MULT:
            res["text"] += f" — volume {vr:.1f}× normal, needs {VOL_MULT:g}×"
            res["signal"] = None
            return res
        res["text"] += f" vol {vr:.1f}×"
    rng = res["high"] - res["low"]
    if ltp and rng > 0:
        ran = (ltp - res["close"]) if res["signal"] == "CE" else (res["close"] - ltp)
        if ran > CHASE_RANGE * rng:
            res["text"] += f" — price already ran {ran:.1f} past the close, not chasing"
            res["signal"] = None
            return res
        res["spot"] = ltp                           # the order works from the price now
    return res


def scan(symbol: str, interval: str, api_key: str, access_token: str,
         now: datetime.datetime | None = None) -> dict:
    """Live: Kite candles + the last price for the forming candle, then read()."""
    basis, source = 0.0, "spot"
    try:
        token, _ = resolve_token(symbol, api_key, access_token)
        fut, fut_key = front_future(symbol, api_key, access_token) if SOURCE == "future" else (None, None)
        df = fetch_candles(fut or token, interval, api_key, access_token)
    except Exception as e:
        return {"error": str(e)}
    now = now or datetime.datetime.now(_IST)
    if fut:
        source = "future"
        idx_key = _INDEX_QUOTE.get(symbol.upper())
        q = _quote_ltp([fut_key] + ([idx_key] if idx_key else []), api_key, access_token)
        ltp = q.get(fut_key)
        spot_now = _live_ltp(token) or q.get(idx_key)
        if ltp and spot_now:
            basis = ltp - spot_now
    else:
        source = "spot" if SOURCE == "spot" else "spot (no futures contract found)"
        ltp = _live_ltp(token)
    if ON_CLOSE:
        res = _read_closed(df, ltp, interval, now)
        res["error"] = None
        res["source"], res["basis"] = source, round(basis, 2)
        if fut and not basis and res["signal"]:
            res["text"] += " — no live futures / index price for the basis, no trade"
            res["signal"] = None
        return res
    if ltp and not df.empty:
        i = df.index[-1]
        df = df.copy()
        df.loc[i, "Close"] = ltp
        df.loc[i, "High"] = max(df.loc[i, "High"], ltp)
        df.loc[i, "Low"] = min(df.loc[i, "Low"], ltp)
    res = read(df)
    if len(df) >= 2:                               # the last closed candle (fade exit reads it)
        res["lc_time"], res["lc_close"] = df.index[-2], float(df["Close"].iloc[-2])
    res["error"] = None
    res["source"], res["basis"] = source, round(basis, 2)
    if fut and not basis and res["signal"]:
        # without both live prices the strike would be picked off the futures price
        res["text"] += " — no live futures / index price for the basis, no trade"
        res["signal"] = None
    start = res.get("candle_time")
    age = (now - start).total_seconds() if start is not None else 0
    res["forming"] = age < {"1m": 60, "3m": 180, "5m": 300}.get(interval, 180)
    if res["signal"] and res["forming"] and age < MIN_AGE_SEC:
        res["text"] += f" — candle only {age:.0f}s old, waiting for its shape"
        res["signal"] = None
    return res
