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

"option" reads the ATM CE and ATM PE 3m charts of the contract the bot would buy (same
expiry choice as kite_order): a bullish candle (strong bull / engulfing / hammer) on the
CE chart → BUY CE, on the PE chart → BUY PE; both at once (premiums jumping together,
a volatility spike) → no trade. A bearish candle on a chart is the exit signal for that
side ("against"). The option candle's low is the stop: it is handed to the order as the
index level an ATM option (delta 0.5) needs to reach it, so the option stop, its 5–15%
clamp and the anti-hunt buffer work as before.
Option charts have real traded volume, so a bullish premium candle also needs volume
≥ VOL_MULT × the normal volume (median of the last 20 candles, opening candles left out).
The forming candle is judged on its pace: volume so far ÷ the share of the candle gone
(a 3m candle 60 s old with 30k traded counts as 90k). Exits don't need volume.
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
SOURCE = "spot"           # "spot" (the index), "future" (current-month futures) or "option" (ATM CE / PE)
VOL_MULT = 1.5            # option charts: candle volume ≥ this × normal (0 = off; CHART_OPTION_VOL_MULT)
_TF_SEC = {"1m": 60, "3m": 180, "5m": 300}
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
    side = "CE" if pattern in ("bull_engulf", "strong_bull", "hammer") else "PE"
    res.update(signal=side, pattern=pattern, label=_LABEL[pattern], stop=round(l if side == "CE" else h, 2))
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


def _with_ltp(df: pd.DataFrame, ltp: float | None) -> pd.DataFrame:
    """The forming candle closes at the last price."""
    if ltp and not df.empty:
        i = df.index[-1]
        df = df.copy()
        df.loc[i, "Close"] = ltp
        df.loc[i, "High"] = max(df.loc[i, "High"], ltp)
        df.loc[i, "Low"] = min(df.loc[i, "Low"], ltp)
    return df


def _volume_pace(df: pd.DataFrame, interval: str, now: datetime.datetime) -> tuple[float, float, float]:
    """(last candle's volume at its current pace, normal volume, ratio)."""
    if "Volume" not in df or len(df) < 12:
        return 0.0, 0.0, 0.0
    d = df.tail(40)
    prior = d.iloc[:-1]
    today = d.index[-1].date()
    keep = ~((prior.index.date == today) & (prior.index.time < OPENING_END))
    if keep.sum() >= 10:
        prior = prior[keep]
    normal = float(prior["Volume"].tail(AVG_CANDLES).median() or 0)
    tf = _TF_SEC.get(interval, 180)
    gone = (now - d.index[-1]).total_seconds() / tf
    vol = float(d["Volume"].iloc[-1]) / min(max(gone, 1 / 6), 1.0)
    return vol, normal, (vol / normal if normal > 0 else 0.0)


def _finish(res: dict, interval: str, now: datetime.datetime) -> dict:
    """Forming flag; no signal from a candle younger than MIN_AGE_SEC."""
    start = res.get("candle_time")
    age = (now - start).total_seconds() if start is not None else 0
    res["forming"] = age < _TF_SEC.get(interval, 180)
    if res["signal"] and res["forming"] and age < MIN_AGE_SEC:
        res["text"] += f" — candle only {age:.0f}s old, waiting for its shape"
        res["signal"] = None
    return res


def _scan_options(symbol: str, interval: str, api_key: str, access_token: str,
                  now: datetime.datetime, token: int) -> dict:
    """ATM CE and ATM PE charts → one reading (see the module docstring)."""
    from screener import kite_order as _ko
    idx_key = _INDEX_QUOTE.get(symbol.upper())
    spot = _live_ltp(token) or _quote_ltp([idx_key], api_key, access_token).get(idx_key)
    if not spot:
        return {"error": "no live index price for the ATM strike"}
    kite = _ko._kite_client(api_key, access_token)
    strike = _ko._atm_strike(spot, symbol)
    ex = _ko._EXCHANGE[symbol]
    legs = {}
    for side in ("CE", "PE"):
        c = _ko.option_contract(kite, symbol, strike, side)
        if not c:
            return {"error": f"no {symbol} {strike} {side} contract"}
        legs[side] = c
    keys = {side: f"{ex}:{c['tradingsymbol']}" for side, c in legs.items()}
    q = _quote_ltp(list(keys.values()), api_key, access_token)
    reads = {}
    vols = {}
    for side, c in legs.items():
        df = fetch_candles(c["instrument_token"], interval, api_key, access_token)
        reads[side] = _finish(read(_with_ltp(df, q.get(keys[side]))), interval, now)
        vols[side] = _volume_pace(df, interval, now)
    for r in reads.values():                           # on a premium chart "BUY CE" means "premium up"
        r["text"] = r["text"].replace("→ BUY CE", "→ premium up").replace("→ BUY PE", "→ premium down")
    ce, pe = reads["CE"], reads["PE"]
    for side, r in reads.items():
        if r["signal"] == "CE" and VOL_MULT:            # bullish premium candle: is the volume behind it?
            v, normal, ratio = vols[side]
            r["vol_ratio"] = round(ratio, 2)
            r["text"] += (f" vol {ratio:.1f}× normal {normal:,.0f}"
                          + (" ✓" if ratio >= VOL_MULT else f" ✗ needs {VOL_MULT:g}×"))
    up = [s for s in ("CE", "PE") if reads[s]["signal"] == "CE"]        # bullish candle on that premium
    against = [s for s in ("CE", "PE") if reads[s]["signal"] == "PE"]   # bearish candle on that premium
    res = {**(reads[up[0]] if len(up) == 1 else ce), "signal": None, "stop": None}
    res["text"] = (f"{strike} CE {legs['CE']['tradingsymbol']}: {ce['text']} | "
                   f"PE {legs['PE']['tradingsymbol']}: {pe['text']}")
    if len(up) == 1 and VOL_MULT and reads[up[0]].get("vol_ratio", 0) < VOL_MULT:
        res["text"] = f"{up[0]} premium up on low volume — no trade [{res['text']}]"
    elif len(up) == 1:
        side, r = up[0], reads[up[0]]
        prem_dist = r["spot"] - r["stop"]                  # premium from the close down to the candle low
        res.update(signal=side, label=f"{side} premium: {r['label']}",
                   stop=round(spot - 2 * prem_dist if side == "CE" else spot + 2 * prem_dist, 2),
                   premium=r["spot"], premium_stop=r["stop"], contract=legs[side]["tradingsymbol"])
        res["text"] = f"BUY {side} — {res['text']}"
    elif len(up) == 2:
        res["text"] = f"CE and PE premiums both jumping (volatility spike) — no trade [{res['text']}]"
    res.update(spot=spot, against=against, error=None, source="option", basis=0.0,
               forming=ce.get("forming", False), candle_time=ce.get("candle_time"))
    return res


def scan(symbol: str, interval: str, api_key: str, access_token: str,
         now: datetime.datetime | None = None) -> dict:
    """Live: Kite candles + the last price for the forming candle, then read()."""
    basis, source = 0.0, "spot"
    if SOURCE == "option" and symbol.upper() in ("NIFTY", "SENSEX"):
        now = now or datetime.datetime.now(_IST)
        try:
            token, _ = resolve_token(symbol, api_key, access_token)
            return _scan_options(symbol.upper(), interval, api_key, access_token, now, token)
        except Exception as e:
            return {"error": str(e)}
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
        source = ("spot" if SOURCE == "spot" else "spot (no option chart for this index)"
                  if SOURCE == "option" else "spot (no futures contract found)")
        ltp = _live_ltp(token)
    res = read(_with_ltp(df, ltp))
    res["error"] = None
    res["source"], res["basis"] = source, round(basis, 2)
    if fut and not basis and res["signal"]:
        # without both live prices the strike would be picked off the futures price
        res["text"] += " — no live futures / index price for the basis, no trade"
        res["signal"] = None
    return _finish(res, interval, now)
