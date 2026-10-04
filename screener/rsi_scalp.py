"""
RSI Scalp Screener
==================
Monitors ATM options (CE + PE) for NIFTY and SENSEX on 5-minute candles.
Fires when RSI(14) crosses UP through 70 for the first time in a cycle.

Returns structured dicts ready for notification and table display.
"""

import datetime
import io
import numpy as np
import pandas as pd
import requests

from screener.kite_data import _KITE_BASE, _hdrs, fetch_ohlcv as _fetch_index

_IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

# Strike rounding per index
_STRIKE_STEP = {"NIFTY": 50, "SENSEX": 100}

# Options exchange per index
_OPT_EXCHANGE = {"NIFTY": "NFO", "SENSEX": "BFO"}

# Expiry weekday: NIFTY = Thursday (3), SENSEX = Friday (4)
_EXPIRY_WEEKDAY = {"NIFTY": 3, "SENSEX": 4}

# In-memory instruments cache: {exchange: {"df": DataFrame, "ts": float}}
_INSTR_CACHE: dict = {}
_INSTR_TTL = 3600  # refresh every hour


# ── Helpers ───────────────────────────────────────────────────────────────────

def _nearest_expiry(index: str) -> datetime.date:
    target = _EXPIRY_WEEKDAY[index]
    today  = datetime.datetime.now(_IST).date()
    days   = (target - today.weekday()) % 7
    return today + datetime.timedelta(days=days)


def _load_option_instruments(exchange: str) -> pd.DataFrame:
    now = datetime.datetime.now(_IST).timestamp()
    cached = _INSTR_CACHE.get(exchange, {})
    if cached.get("ts", 0) + _INSTR_TTL > now:
        return cached["df"]
    try:
        r = requests.get(
            f"{_KITE_BASE}/instruments/{exchange}",
            headers=_hdrs(),
            timeout=30,
        )
        if not r.ok:
            return cached.get("df", pd.DataFrame())
        df = pd.read_csv(io.StringIO(r.text))
        _INSTR_CACHE[exchange] = {"df": df, "ts": now}
        return df
    except Exception:
        return cached.get("df", pd.DataFrame())


def _get_option_tokens(index: str, strike: float, expiry: datetime.date) -> dict:
    """Return {"CE": token, "PE": token} for the given index/strike/expiry."""
    exchange = _OPT_EXCHANGE[index]
    df = _load_option_instruments(exchange)
    if df.empty:
        return {}
    try:
        df["expiry_dt"] = pd.to_datetime(df["expiry"]).dt.date
        mask = (
            (df["name"].str.upper() == index) &
            (df["expiry_dt"] == expiry) &
            (df["strike"].astype(float) == float(strike)) &
            (df["instrument_type"].isin(["CE", "PE"]))
        )
        hits = df[mask]
        return {
            row["instrument_type"]: int(row["instrument_token"])
            for _, row in hits.iterrows()
        }
    except Exception:
        return {}


def _fetch_option_5m(token: int, days: int = 5) -> pd.DataFrame:
    """Fetch 5-minute candles for an option instrument token."""
    to_d = datetime.date.today()
    fr_d = to_d - datetime.timedelta(days=days)
    try:
        r = requests.get(
            f"{_KITE_BASE}/instruments/historical/{token}/5minute",
            headers=_hdrs(),
            params={
                "from": fr_d.strftime("%Y-%m-%d %H:%M:%S"),
                "to":   to_d.strftime("%Y-%m-%d %H:%M:%S"),
            },
            timeout=30,
        )
        if not r.ok:
            return pd.DataFrame()
        candles = r.json().get("data", {}).get("candles", [])
        if not candles:
            return pd.DataFrame()
    except Exception:
        return pd.DataFrame()

    df = pd.DataFrame(
        [c[:6] for c in candles],
        columns=["datetime", "Open", "High", "Low", "Close", "Volume"],
    )
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.set_index("datetime").sort_index()
    for col in ("Open", "High", "Low", "Close", "Volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["Open"])


# ── RSI ───────────────────────────────────────────────────────────────────────

def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta    = series.diff()
    gain     = delta.clip(lower=0)
    loss     = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs       = avg_gain / avg_loss.replace(0, np.nan)
    return (100 - (100 / (1 + rs))).fillna(50)


# ── Main analysis ─────────────────────────────────────────────────────────────

def analyze_rsi_scalp(index: str) -> dict:
    """
    Returns dict with keys:
      index, spot, strike, expiry,
      ce_rsi, ce_prev_rsi, ce_price, ce_token,
      pe_rsi, pe_prev_rsi, pe_price, pe_token,
      ce_error, pe_error, error
    """
    try:
        # Current spot price from most recent 5m candle
        df_spot = _fetch_index(index, interval="5minute", days=3)
        if df_spot is None or df_spot.empty:
            return {"index": index, "error": "No spot data"}

        spot   = float(df_spot["Close"].iloc[-1])
        step   = _STRIKE_STEP[index]
        strike = round(spot / step) * step
        expiry = _nearest_expiry(index)

        tokens = _get_option_tokens(index, strike, expiry)
        if not tokens:
            return {
                "index":  index,
                "spot":   round(spot, 2),
                "strike": strike,
                "expiry": expiry.isoformat(),
                "error":  f"No tokens found for {index} {int(strike)} expiry {expiry}",
            }

        result: dict = {
            "index":  index,
            "spot":   round(spot, 2),
            "strike": int(strike),
            "expiry": expiry.isoformat(),
            "error":  None,
        }

        for opt in ("CE", "PE"):
            key = opt.lower()
            token = tokens.get(opt)
            if not token:
                result[f"{key}_error"] = "No token"
                result[f"{key}_rsi"]   = None
                continue

            df = _fetch_option_5m(token, days=5)
            if df is None or len(df) < 16:
                result[f"{key}_error"] = "Insufficient data"
                result[f"{key}_rsi"]   = None
                continue

            rsi_s  = _rsi(df["Close"])
            cur_rsi  = float(rsi_s.iloc[-1])
            prev_rsi = float(rsi_s.iloc[-2])
            price    = float(df["Close"].iloc[-1])

            result[f"{key}_rsi"]      = round(cur_rsi, 1)
            result[f"{key}_prev_rsi"] = round(prev_rsi, 1)
            result[f"{key}_price"]    = round(price, 2)
            result[f"{key}_token"]    = token
            result[f"{key}_error"]    = None

        return result

    except Exception as e:
        return {"index": index, "error": str(e)}
