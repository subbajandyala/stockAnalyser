"""
Momentum v3 — the context around the candle trigger (branch momentum-v3).

The candle engine (screener/candles.py) still finds the momentum candle. This module
decides whether that candle may be traded:

  day        no new entries on the index's own expiry day after EXPIRY_CUTOFF, none on
             EVENT_DAYS (RBI, Budget …)
  direction  three independent votes, refreshed every REFRESH_MIN minutes; a side is
             allowed only when at least MIN_VOTES agree:
               price  future above / below the session VWAP AND 15-min Supertrend up / down
               OI     ATM ± OI_STRIKES strikes over the last OI_LOOKBACK_MIN minutes:
                      (put OI change − call OI change) ÷ their OI ≥ +OI_MIN_PCT → bull,
                      ≤ −OI_MIN_PCT → bear  (puts written / calls unwound = bullish)
               range  above / below the 9:15–OR_END opening range
  room       no CE when the biggest call-OI wall above is closer than ROOM_PCT of the price,
             no PE when the biggest put-OI wall below is (walls from ATM ± WALL_STRIKES)
  vix        India VIX down more than VIX_CRUSH_PCT since the open → candles must be
             VIX_STRICT_MULT× normal instead of 1.2× (0 = off; untested, off by default)

Data: Kite only — futures 3m / 15m candles, option quotes (OI) snapshotted into
/tmp/v3_oi_<date>_<symbol>.json so a restart keeps the 30-minute history, India VIX quote.
"""

import datetime
import json
import pathlib

import pandas as pd
import requests

from screener.chart_analysis import _IST, _KITE_BASE, _hdrs, fetch_candles, front_future

MIN_VOTES = 2
REFRESH_MIN = 5
ST_PERIOD, ST_MULT = 10, 3.0
OR_END = datetime.time(9, 45)
OI_STRIKES = 2
OI_LOOKBACK_MIN = 30
OI_MIN_PCT = 0.02
WALL_STRIKES = 10
ROOM_PCT = 0.0025
EXPIRY_CUTOFF = datetime.time(13, 0)
EVENT_DAYS: set = set()
VIX_CRUSH_PCT = 0.0
VIX_STRICT_MULT = 1.5

_STEP = {"NIFTY": 50, "SENSEX": 100}
_EXCH = {"NIFTY": "NFO", "SENSEX": "BFO"}
_CACHE: dict = {}          # symbol → (bucket, result)


# ── helpers ─────────────────────────────────────────────────────────────────

def _quote(keys: list, api_key: str, access_token: str) -> dict:
    """Full Kite quote for up to ~500 instruments; {} on error."""
    out = {}
    try:
        for i in range(0, len(keys), 250):
            r = requests.get(f"{_KITE_BASE}/quote", headers=_hdrs(api_key, access_token),
                             params=[("i", k) for k in keys[i:i + 250]], timeout=10)
            r.raise_for_status()
            out.update(r.json().get("data", {}))
    except Exception:
        pass
    return out


def supertrend_up(df: pd.DataFrame, period: int = 10, mult: float = 3.0) -> bool | None:
    """Direction of the usual (TradingView / Kite) Supertrend after the last candle of df."""
    if len(df) < period + 2:
        return None
    h, l, c = df["High"].values, df["Low"].values, df["Close"].values
    tr = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(df))]
    atr = sum(tr[:period]) / period
    upper = lower = None
    up = True
    for i in range(period - 1, len(df)):
        if i >= period:
            atr = (atr * (period - 1) + tr[i]) / period
        mid = (h[i] + l[i]) / 2
        bu, bl = mid + mult * atr, mid - mult * atr
        if upper is None:
            upper, lower = bu, bl
            up = bool(c[i] > upper)
            continue
        upper = bu if (bu < upper or c[i - 1] > upper) else upper
        lower = bl if (bl > lower or c[i - 1] < lower) else lower
        if up and c[i] < lower:
            up = False
        elif not up and c[i] > upper:
            up = True
    return bool(up)


def _closed(df: pd.DataFrame, minutes: int, now: datetime.datetime) -> pd.DataFrame:
    if len(df) and df.index[-1] + pd.Timedelta(minutes=minutes) > now.replace(tzinfo=None):
        return df.iloc[:-1]
    return df


def trade_expiry(kite, symbol: str) -> datetime.date | None:
    """The weekly expiry the bot buys (kite_order's choice: next weekly on expiry day)."""
    from screener import kite_order as _ko
    df = _ko._option_instruments(kite, _EXCH[symbol])
    if df is None:
        return None
    today = datetime.date.today()
    exp = sorted({e for e in df[df["name"] == symbol]["expiry"] if e >= today})
    if not exp:
        return None
    return exp[1] if exp[0] == today and len(exp) > 1 else exp[0]


def is_expiry_day(kite, symbol: str) -> bool:
    from screener import kite_order as _ko
    df = _ko._option_instruments(kite, _EXCH[symbol])
    if df is None:
        return False
    today = datetime.date.today()
    return any(e == today for e in df[df["name"] == symbol]["expiry"])


# ── day check ───────────────────────────────────────────────────────────────

def day_block(kite, symbol: str, now: datetime.datetime) -> str | None:
    """Why no new entry may be taken today / now on this index, or None."""
    if now.strftime("%Y-%m-%d") in EVENT_DAYS:
        return "event day"
    if EXPIRY_CUTOFF and now.time() >= EXPIRY_CUTOFF and is_expiry_day(kite, symbol):
        return f"{symbol} expiry day after {EXPIRY_CUTOFF:%H:%M}"
    return None


# ── votes ───────────────────────────────────────────────────────────────────

def _price_and_range_votes(symbol: str, api_key: str, access_token: str, now: datetime.datetime) -> tuple:
    fut, fut_key = front_future(symbol, api_key, access_token)
    if not fut:
        return 0, 0, "no futures contract", None
    d3 = fetch_candles(fut, "3m", api_key, access_token)
    d15 = _closed(fetch_candles(fut, "15m", api_key, access_token), 15, now)
    q = _quote([fut_key], api_key, access_token).get(fut_key, {})
    ltp = float(q.get("last_price") or (d3["Close"].iloc[-1] if len(d3) else 0))
    today = d3[d3.index.date == now.date()]
    if today.empty or not ltp:
        return 0, 0, "no candles today", ltp
    tp = (today["High"] + today["Low"] + today["Close"]) / 3
    vol = today["Volume"].clip(lower=0)
    vwap = float((tp * vol).sum() / vol.sum()) if vol.sum() > 0 else float(tp.mean())
    st = supertrend_up(d15.tail(300), ST_PERIOD, ST_MULT)
    if ltp > vwap and st:
        pv = 1
    elif ltp < vwap and st is False:
        pv = -1
    else:
        pv = 0
    p_txt = f"fut {ltp:,.1f} vs VWAP {vwap:,.1f}, ST15 {'up' if st else 'down' if st is False else '?'}"
    orng = today[today.index.time < OR_END]
    if now.time() < OR_END or orng.empty:
        rv, r_txt = 0, "opening range forming"
    else:
        hi, lo = float(orng["High"].max()), float(orng["Low"].min())
        rv = 1 if ltp > hi else -1 if ltp < lo else 0
        r_txt = f"OR {lo:,.1f}–{hi:,.1f}"
    return pv, rv, f"{p_txt}; {r_txt}", ltp


def _oi_snapshot(kite, symbol: str, spot: float, api_key: str, access_token: str,
                 now: datetime.datetime) -> dict:
    """Quote the trade expiry's options around the money; append to today's OI history file."""
    from screener import kite_order as _ko
    df = _ko._option_instruments(kite, _EXCH[symbol])
    exp = trade_expiry(kite, symbol)
    if df is None or exp is None or not spot:
        return {}
    step = _STEP[symbol]
    atm = round(spot / step) * step
    rows = df[(df["name"] == symbol) & (df["expiry"] == exp)
              & (df["strike"].astype(float).between(atm - WALL_STRIKES * step, atm + WALL_STRIKES * step))]
    keys = [f"{_EXCH[symbol]}:{ts}" for ts in rows["tradingsymbol"]]
    q = _quote(keys, api_key, access_token)
    snap = {"t": now.strftime("%H:%M"), "oi": {}}
    for _, r in rows.iterrows():
        v = q.get(f"{_EXCH[symbol]}:{r['tradingsymbol']}", {})
        if v.get("oi") is not None:
            snap["oi"][f"{int(float(r['strike']))}{r['instrument_type']}"] = int(v["oi"])
    path = pathlib.Path(f"/tmp/v3_oi_{now:%Y-%m-%d}_{symbol}.json")
    try:
        hist = json.loads(path.read_text()) if path.exists() else []
    except Exception:
        hist = []
    if snap["oi"]:
        hist.append(snap)
        path.write_text(json.dumps(hist[-200:]))
    return {"atm": atm, "step": step, "now": snap, "hist": hist}


def _oi_vote(o: dict, now: datetime.datetime) -> tuple:
    if not o or not o["now"]["oi"]:
        return 0, "OI n/a"
    target = (now - datetime.timedelta(minutes=OI_LOOKBACK_MIN)).strftime("%H:%M")
    old = [h for h in o["hist"] if h["t"] <= target]
    if not old:
        return 0, f"OI history < {OI_LOOKBACK_MIN} min"
    base, cur = old[-1]["oi"], o["now"]["oi"]
    d_ce = d_pe = tot = 0
    for k in range(-OI_STRIKES, OI_STRIKES + 1):
        s = o["atm"] + k * o["step"]
        for side in ("CE", "PE"):
            key = f"{s}{side}"
            if key in cur and key in base:
                tot += cur[key]
                if side == "CE":
                    d_ce += cur[key] - base[key]
                else:
                    d_pe += cur[key] - base[key]
    if not tot:
        return 0, "OI n/a"
    net = (d_pe - d_ce) / tot
    v = 1 if net >= OI_MIN_PCT else -1 if net <= -OI_MIN_PCT else 0
    return v, f"OI {OI_LOOKBACK_MIN}m: puts {d_pe:+,}, calls {d_ce:+,} ({net:+.1%})"


def _walls(o: dict, spot: float) -> tuple:
    if not o or not o["now"]["oi"]:
        return None, None
    ce = {int(k[:-2]): v for k, v in o["now"]["oi"].items() if k.endswith("CE") and int(k[:-2]) > spot}
    pe = {int(k[:-2]): v for k, v in o["now"]["oi"].items() if k.endswith("PE") and int(k[:-2]) < spot}
    return (max(ce, key=ce.get) if ce else None), (max(pe, key=pe.get) if pe else None)


def direction(kite, symbol: str, api_key: str, access_token: str, now: datetime.datetime) -> dict:
    """{side: "CE" | "PE" | None, bull, bear, votes, call_wall, put_wall, spot, text}; cached REFRESH_MIN."""
    bucket = (now.strftime("%Y-%m-%d %H"), now.minute // REFRESH_MIN)
    hit = _CACHE.get(symbol)
    if hit and hit[0] == bucket:
        return hit[1]
    try:
        pv, rv, txt, fut_ltp = _price_and_range_votes(symbol, api_key, access_token, now)
        idx_key = {"NIFTY": "NSE:NIFTY 50", "SENSEX": "BSE:SENSEX"}[symbol]
        spot = float(_quote([idx_key], api_key, access_token).get(idx_key, {}).get("last_price") or 0)
        o = _oi_snapshot(kite, symbol, spot, api_key, access_token, now)
        ov, otxt = _oi_vote(o, now)
        cw, pw = _walls(o, spot)
    except Exception as e:
        res = {"side": None, "bull": 0, "bear": 0, "votes": {}, "call_wall": None, "put_wall": None,
               "spot": None, "text": f"direction error — {e}"}
        _CACHE[symbol] = (bucket, res)
        return res
    votes = {"price": pv, "oi": ov, "range": rv}
    bull, bear = sum(v > 0 for v in votes.values()), sum(v < 0 for v in votes.values())
    side = "CE" if bull >= MIN_VOTES and bear == 0 else "PE" if bear >= MIN_VOTES and bull == 0 else None
    res = {"side": side, "bull": bull, "bear": bear, "votes": votes, "call_wall": cw, "put_wall": pw,
           "spot": spot, "text": (f"{side or 'no side'} (bull {bull} / bear {bear}) — {txt}; {otxt}"
                                  + (f"; walls CE {cw} / PE {pw}" if cw or pw else ""))}
    _CACHE[symbol] = (bucket, res)
    return res


def room_block(d: dict, side: str) -> str | None:
    """Why a trade into the nearest OI wall is skipped, or None."""
    spot = d.get("spot")
    if not ROOM_PCT or not spot:
        return None
    if side == "CE" and d.get("call_wall") and d["call_wall"] - spot < ROOM_PCT * spot:
        return f"call wall {d['call_wall']} only {d['call_wall'] - spot:.0f} pts above"
    if side == "PE" and d.get("put_wall") and spot - d["put_wall"] < ROOM_PCT * spot:
        return f"put wall {d['put_wall']} only {spot - d['put_wall']:.0f} pts below"
    return None


def vix_mult(api_key: str, access_token: str, base: float) -> tuple:
    """(candle multiple to use, note): stricter while India VIX is falling hard since the open."""
    if not VIX_CRUSH_PCT:
        return base, ""
    q = _quote(["NSE:INDIA VIX"], api_key, access_token).get("NSE:INDIA VIX", {})
    ltp, op = q.get("last_price"), (q.get("ohlc") or {}).get("open")
    if ltp and op and (ltp / op - 1) <= -VIX_CRUSH_PCT:
        return max(base, VIX_STRICT_MULT), f"VIX {ltp:.2f} down {(1 - ltp / op):.0%} since open"
    return base, ""
