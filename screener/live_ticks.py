"""
Redis-backed live tick reader for Chart Analysis.

Provides get_live_candles() which returns a DataFrame of 1m candles
from Redis (written by ticker_daemon.py), or None if Redis is unavailable
or the token has no live data.

Also exposes get_live_ltp() for the current incomplete candle's last price.
"""

import datetime
import json

import pandas as pd

_IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

# Instrument token map (same as chart_analysis.py)
_INDEX_TOKENS = {
    "NIFTY":     256265,
    "BANKNIFTY": 260105,
    "SENSEX":    265,
}


def _redis():
    """Return a Redis client or None if redis-py isn't installed / server down."""
    try:
        import redis
        r = redis.Redis(host="localhost", port=6379, db=0,
                        decode_responses=True, socket_connect_timeout=1)
        r.ping()
        return r
    except Exception:
        return None


def is_ticker_running() -> bool:
    """True if the ticker daemon wrote a health ping in the last 2 minutes."""
    r = _redis()
    if r is None:
        return False
    return bool(r.exists("ticker:status"))


def get_live_candles(token: int, resample_to: str = "1min") -> pd.DataFrame | None:
    """
    Return a DataFrame of live candles from Redis for `token`.
    `resample_to` can be '1min', '5min', '15min', '60min' to group 1m data.
    Returns None if no live data exists.
    """
    r = _redis()
    if r is None:
        return None

    raw = r.lrange(f"candles:1m:{token}", 0, 749)
    if not raw:
        return None

    rows = []
    for item in raw:
        try:
            rows.append(json.loads(item))
        except Exception:
            continue

    if not rows:
        return None

    df = pd.DataFrame(rows)
    df["datetime"] = pd.to_datetime(df["datetime"], utc=False)
    if df["datetime"].dt.tz is None:
        df["datetime"] = df["datetime"].dt.tz_localize("Asia/Kolkata")
    else:
        df["datetime"] = df["datetime"].dt.tz_convert("Asia/Kolkata")
    df = df.set_index("datetime").sort_index()

    for col in ("Open", "High", "Low", "Close", "Volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")

    if resample_to != "1min":
        df = (
            df.resample(resample_to, label="left", closed="left")
            .agg({"Open": "first", "High": "max", "Low": "min",
                  "Close": "last", "Volume": "sum"})
            .dropna(subset=["Open"])
        )

    return df if not df.empty else None


def get_live_ltp(token: int) -> float | None:
    """Return the latest trade price for `token` from Redis, or None."""
    r = _redis()
    if r is None:
        return None
    raw = r.get(f"tick:{token}")
    if not raw:
        return None
    try:
        return float(json.loads(raw)["ltp"])
    except Exception:
        return None


def merge_with_live(hist_df: pd.DataFrame, token: int,
                    interval_key: str) -> pd.DataFrame:
    """
    Append any live Redis candles that are newer than the last candle in
    hist_df. Returns hist_df unchanged if no live data or Redis is down.
    """
    _resample_map = {
        "1m":  "1min",
        "2m":  "2min",
        "5m":  "5min",
        "10m": "10min",
        "15m": "15min",
        "1H":  "60min",
        "1D":  "1D",
    }
    if interval_key not in _resample_map or interval_key == "1D":
        return hist_df

    live = get_live_candles(token, resample_to=_resample_map[interval_key])
    if live is None or live.empty:
        return hist_df

    last_hist_ts = hist_df.index[-1]
    new_candles  = live[live.index > last_hist_ts]
    if new_candles.empty:
        return hist_df

    merged = pd.concat([hist_df, new_candles])
    merged = merged[~merged.index.duplicated(keep="last")].sort_index()
    return merged
