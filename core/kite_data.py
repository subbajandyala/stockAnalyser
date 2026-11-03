"""
screener/kite_data.py — central Kite historical OHLCV fetcher.

Replaces yfinance across all screeners. Reads credentials from .env.
Caches the NSE instrument master for 24 h at /tmp/kite_instr_nse.json.
"""

import datetime
import json
import os
from io import StringIO
from pathlib import Path

import pandas as pd
import requests

_KITE_BASE  = "https://api.kite.trade"
_CACHE_FILE = Path("/tmp/kite_instr_nse.json")
_IST        = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

# Known index tokens — no instruments lookup needed
INDEX_TOKENS: dict[str, int] = {
    "NIFTY":      256265,
    "BANKNIFTY":  260105,
    "SENSEX":     265,
    "FINNIFTY":   257801,
    "MIDCPNIFTY": 288009,
    "INDIAVIX":   264969,
}

# yfinance period strings → calendar days
_PERIOD_DAYS: dict[str, int] = {
    "1d":  1,   "5d":  5,   "1mo": 30,  "3mo": 91,
    "6mo": 183, "1y":  365, "2y":  730, "3y":  1095, "5y": 1825,
}

# yfinance interval strings → Kite interval names
_INTERVAL_MAP: dict[str, str] = {
    "1m":  "minute",    "2m":  "3minute",   "3m":  "3minute",
    "5m":  "5minute",   "10m": "10minute",  "15m": "15minute",
    "30m": "30minute",  "1h":  "60minute",  "60m": "60minute",
    "1d":  "day",       "1wk": "week",      "1mo": "month",
}


# ── credentials ───────────────────────────────────────────────────────────────

def _load_creds() -> tuple[str, str]:
    env: dict[str, str] = {}
    for p in [Path(__file__).parent.parent / ".env", Path(".env")]:
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
            break
    for k in ("KITE_API_KEY", "KITE_ACCESS_TOKEN"):
        if k in os.environ:
            env[k] = os.environ[k]
    return env.get("KITE_API_KEY", ""), env.get("KITE_ACCESS_TOKEN", "")


def _hdrs() -> dict:
    api_key, access_token = _load_creds()
    return {
        "X-Kite-Version": "3",
        "Authorization":  f"token {api_key}:{access_token}",
    }


# ── instruments cache ─────────────────────────────────────────────────────────

def _instruments() -> dict[str, int]:
    """Return {tradingsymbol: instrument_token} for NSE EQ segment, cached 24 h."""
    if _CACHE_FILE.exists():
        age = datetime.datetime.now().timestamp() - _CACHE_FILE.stat().st_mtime
        if age < 86400:
            return json.loads(_CACHE_FILE.read_text())
    r = requests.get(f"{_KITE_BASE}/instruments/NSE", headers=_hdrs(), timeout=30)
    r.raise_for_status()
    df = pd.read_csv(StringIO(r.text))
    df = df[df["segment"] == "NSE"][["tradingsymbol", "instrument_token"]].copy()
    df["instrument_token"] = df["instrument_token"].astype(int)
    mapping = dict(zip(df["tradingsymbol"], df["instrument_token"]))
    _CACHE_FILE.write_text(json.dumps(mapping))
    return mapping


def get_token(symbol: str) -> int | None:
    """Return Kite instrument_token for an NSE equity symbol or known index."""
    sym = symbol.upper().replace(".NS", "").replace("-EQ", "").strip()
    if sym in INDEX_TOKENS:
        return INDEX_TOKENS[sym]
    try:
        return _instruments().get(sym)
    except Exception:
        return None


# ── main fetcher ──────────────────────────────────────────────────────────────

LAST_ERROR = ""   # why the last fetch_ohlcv() came back empty (Kite's reply), for logs


def fetch_ohlcv(
    symbol: str,
    interval: str = "day",
    days: int = 365,
    from_date: datetime.date | None = None,
    to_date:   datetime.date | None = None,
) -> pd.DataFrame:
    """
    Fetch OHLCV candles for an NSE equity symbol or index via Kite API.

    symbol   : NSE ticker (e.g. 'RELIANCE', 'RELIANCE.NS') or index name ('NIFTY')
    interval : Kite interval — 'minute','5minute','15minute','60minute',
               'day','week','month'
    days     : calendar days of history (used when from_date is not given)
    Returns DataFrame[Open,High,Low,Close,Volume] with IST DatetimeIndex.
    Empty DataFrame on any failure.
    """
    global LAST_ERROR
    LAST_ERROR = ""
    token = get_token(symbol)
    if token is None:
        LAST_ERROR = "symbol not found"
        return pd.DataFrame()
    api_key, access_token = _load_creds()
    if not (api_key and access_token):
        LAST_ERROR = "KITE_API_KEY / KITE_ACCESS_TOKEN missing (.env not found next to the code?)"
        return pd.DataFrame()

    to_d  = to_date   or datetime.date.today()
    fr_d  = from_date or (to_d - datetime.timedelta(days=days))

    try:
        r = requests.get(
            f"{_KITE_BASE}/instruments/historical/{token}/{interval}",
            headers=_hdrs(),
            params={
                "from": fr_d.strftime("%Y-%m-%d %H:%M:%S"),
                "to":   to_d.strftime("%Y-%m-%d %H:%M:%S"),
            },
            timeout=30,
        )
        if not r.ok:
            LAST_ERROR = f"Kite {r.status_code}: {r.text[:160]}"
            return pd.DataFrame()
        candles = r.json().get("data", {}).get("candles", [])
        if not candles:
            LAST_ERROR = "Kite returned no candles"
            return pd.DataFrame()
    except Exception as e:
        LAST_ERROR = f"request failed: {e}"
        return pd.DataFrame()

    df = pd.DataFrame(
        [c[:6] for c in candles],
        columns=["datetime", "Open", "High", "Low", "Close", "Volume"],
    )
    df["datetime"] = pd.to_datetime(df["datetime"])
    if df["datetime"].dt.tz is None:
        df["datetime"] = df["datetime"].dt.tz_localize("Asia/Kolkata")
    else:
        df["datetime"] = df["datetime"].dt.tz_convert("Asia/Kolkata")
    df = df.set_index("datetime").sort_index()
    for col in ("Open", "High", "Low", "Close", "Volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["Open"])


def fetch_ohlcv_yf_compat(
    nse_symbol: str,
    period: str = "1y",
    interval: str = "1d",
) -> pd.DataFrame:
    """
    Drop-in replacement for yf.download() calls.

    nse_symbol : 'RELIANCE.NS' or 'RELIANCE'
    period     : yfinance-style period ('1d','5d','1mo','3mo','6mo','1y','2y','3y')
    interval   : yfinance-style interval ('1m','5m','15m','1h','1d','1wk','1mo')
    Returns DataFrame with Open/High/Low/Close/Volume and IST DatetimeIndex.
    """
    kite_interval = _INTERVAL_MAP.get(interval, "day")
    days          = _PERIOD_DAYS.get(period, 365)
    return fetch_ohlcv(nse_symbol, interval=kite_interval, days=days)
