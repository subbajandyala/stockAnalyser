"""
Noise Area — intraday trend filter (Zarattini, Aziz & Barbon, "Beat the Market", 2024).

For every time of day, the band width is the average absolute move from the day's
open at that time over the previous LOOKBACK_DAYS sessions. The band is anchored to
today's open (gap-adjusted with yesterday's close):

    upper = max(open, prev_close) × (1 + σ(t))
    lower = min(open, prev_close) × (1 − σ(t))

Price above the band → unusual demand (trend up); below → trend down;
inside → normal noise, no trade. Exits: price crosses back through VWAP or the band.
"""

import datetime
import time

import numpy as np
import pandas as pd
import requests

from screener.chart_analysis import (
    _INDEX_TOKENS, _KITE_BASE, _hdrs, drop_open_candle, front_future_token,
)

_IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

LOOKBACK_DAYS = 14
_CACHE_TTL    = 120  # seconds — chart_bot's monitor and scan reuse one fetch
_cache: dict[str, tuple[float, dict]] = {}


def _fetch_5m(token: int, days: int, api_key: str, access_token: str) -> pd.DataFrame:
    now = datetime.datetime.now(_IST)
    r = requests.get(
        f"{_KITE_BASE}/instruments/historical/{token}/5minute",
        headers=_hdrs(api_key, access_token),
        params={
            "from": (now - datetime.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S"),
            "to":   now.strftime("%Y-%m-%d %H:%M:%S"),
            "continuous": 0, "oi": 0,
        },
        timeout=30,
    )
    if not r.ok:
        raise RuntimeError(f"Kite {r.status_code}: {r.text[:200]}")
    candles = r.json().get("data", {}).get("candles", [])
    if not candles:
        raise RuntimeError("No 5m candle data")
    df = pd.DataFrame(candles, columns=["datetime", "Open", "High", "Low", "Close", "Volume"])
    df["datetime"] = pd.to_datetime(df["datetime"])
    if df["datetime"].dt.tz is None:
        df["datetime"] = df["datetime"].dt.tz_localize("Asia/Kolkata")
    else:
        df["datetime"] = df["datetime"].dt.tz_convert("Asia/Kolkata")
    return df.set_index("datetime").sort_index().between_time("09:15", "15:25")


def compute_noise_state(df: pd.DataFrame, fut_volume: pd.Series | None = None,
                        lookback: int = LOOKBACK_DAYS) -> dict:
    """Pure calculation on closed 5m candles (last day in df = 'today')."""
    dates = sorted(set(df.index.date))
    if len(dates) < 3:
        return {"state": "unknown", "reason": "not enough history"}
    today, history = dates[-1], dates[:-1][-lookback:]

    rows = []
    for d in history:
        day = df[df.index.date == d]
        if day.empty:
            continue
        o = float(day["Open"].iloc[0])
        moves = (day["Close"] / o - 1).abs()
        moves.index = day.index.strftime("%H:%M")
        rows.append(moves)
    if not rows:
        return {"state": "unknown", "reason": "no history rows"}
    sigma_by_time = pd.concat(rows, axis=1).mean(axis=1)

    td = df[df.index.date == today]
    if td.empty:
        return {"state": "unknown", "reason": "no bars today"}
    prev_close = float(df[df.index.date == history[-1]]["Close"].iloc[-1])
    today_open = float(td["Open"].iloc[0])
    bar_time   = td.index[-1].strftime("%H:%M")
    sigma      = float(sigma_by_time.get(bar_time, np.nan))
    if np.isnan(sigma):
        return {"state": "unknown", "reason": f"no σ for {bar_time}"}

    upper = max(today_open, prev_close) * (1 + sigma)
    lower = min(today_open, prev_close) * (1 - sigma)
    price = float(td["Close"].iloc[-1])

    tp = (td["High"] + td["Low"] + td["Close"]) / 3
    vol = fut_volume.reindex(td.index).fillna(0) if fut_volume is not None else None
    if vol is not None and vol.sum() > 0:
        vwap = float((tp * vol).sum() / vol.sum())
    else:
        vwap = float(tp.mean())

    state = "above" if price > upper else ("below" if price < lower else "inside")
    return {
        "state": state, "price": round(price, 2),
        "upper": round(upper, 2), "lower": round(lower, 2),
        "vwap": round(vwap, 2), "sigma_pct": round(sigma * 100, 3),
        "bar_time": bar_time, "open": today_open, "prev_close": prev_close,
    }


def noise_state(symbol: str, api_key: str, access_token: str) -> dict:
    """Live Noise Area state for an index. Returns {"state": "unknown"} on failure."""
    sym = symbol.upper()
    hit = _cache.get(sym)
    if hit and time.time() - hit[0] < _CACHE_TTL:
        return hit[1]
    token = _INDEX_TOKENS.get(sym)
    if not token:
        return {"state": "unknown", "reason": f"{sym} not an index"}
    try:
        days = int(LOOKBACK_DAYS * 1.6) + 6
        df = drop_open_candle(_fetch_5m(token, days, api_key, access_token), "5m")
        if df.empty or df.index[-1].date() != datetime.datetime.now(_IST).date():
            res = {"state": "unknown", "reason": "no bars today yet"}
        else:
            fut_vol = None
            fut_tok = front_future_token(sym, api_key, access_token)
            if fut_tok:
                try:
                    fut_vol = _fetch_5m(fut_tok, 1, api_key, access_token)["Volume"]
                except Exception:
                    fut_vol = None
            res = compute_noise_state(df, fut_vol)
    except Exception as e:
        res = {"state": "unknown", "reason": str(e)[:120]}
    _cache[sym] = (time.time(), res)
    return res
