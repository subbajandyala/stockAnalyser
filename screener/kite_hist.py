"""
Kite API helpers for equity screeners.

Provides:
  - batch_quote_nse(): live /quote for a batch of NSE symbols (one API call per 400)
  - patch_df_with_kite(): replace the most recent candle in a yfinance DataFrame
    with live Kite price data so screeners see real-time prices.
  - fetch_kite_daily_ohlcv(): full historical daily candles via Kite historical API
    (requires Kite Historical Data add-on; falls back gracefully if unavailable).
"""

import datetime
import requests
import pandas as pd

_KITE_BASE = "https://api.kite.trade"


def kite_hdrs(api_key: str, access_token: str) -> dict:
    return {"X-Kite-Version": "3", "Authorization": f"token {api_key}:{access_token}"}


def batch_quote_nse(api_key: str, access_token: str, symbols: list[str]) -> dict:
    """
    Batch-fetch live /quote for NSE equity symbols.
    symbols: plain NSE tickers, e.g. ['RELIANCE', 'INFY'] (not .NS suffix).
    Returns dict keyed by 'NSE:<SYMBOL>'.
    """
    hdrs   = kite_hdrs(api_key, access_token)
    keys   = [f"NSE:{s.upper()}" for s in symbols]
    out: dict = {}
    for i in range(0, len(keys), 400):
        r = requests.get(
            f"{_KITE_BASE}/quote", headers=hdrs,
            params={"i": keys[i : i + 400]}, timeout=30,
        )
        if r.ok:
            out.update(r.json().get("data", {}))
    return out


def patch_df_with_kite(df: pd.DataFrame, quote: dict) -> pd.DataFrame:
    """
    Replace/append today's candle in a yfinance daily OHLCV DataFrame with
    live Kite data.  Handles both IST (market open) and post-close scenarios.

    df must have columns Open/High/Low/Close/Volume (capitalised, as yfinance returns).
    """
    if df is None or df.empty or not quote:
        return df
    lp = float(quote.get("last_price", 0))
    if lp <= 0:
        return df
    ohlc = quote.get("ohlc") or {}
    vol  = int(quote.get("volume_traded", 0) or quote.get("volume", 0))
    today = pd.Timestamp.today().normalize()

    new_row = pd.DataFrame(
        {
            "Open":   [float(ohlc.get("open",  lp))],
            "High":   [float(ohlc.get("high",  lp))],
            "Low":    [float(ohlc.get("low",   lp))],
            "Close":  [lp],
            "Volume": [float(vol)],
        },
        index=[today],
    )
    df = df.copy()
    if today in df.index:
        df = df.drop(today)
    return pd.concat([df, new_row]).sort_index()


def fetch_kite_daily_ohlcv(
    instrument_token: int,
    api_key: str,
    access_token: str,
    days: int = 400,
) -> pd.DataFrame:
    """
    Fetch daily OHLCV candles via Kite historical API.
    Returns an empty DataFrame if the call fails (e.g. no historical subscription).
    Columns: Open, High, Low, Close, Volume  (capitalised, same as yfinance).
    """
    to_d = datetime.date.today()
    fr_d = to_d - datetime.timedelta(days=days)
    try:
        r = requests.get(
            f"{_KITE_BASE}/instruments/historical/{instrument_token}/day",
            headers=kite_hdrs(api_key, access_token),
            params={"from": fr_d.strftime("%Y-%m-%d"), "to": to_d.strftime("%Y-%m-%d")},
            timeout=30,
        )
        if not r.ok:
            return pd.DataFrame()
        candles = r.json().get("data", {}).get("candles", [])
        if not candles:
            return pd.DataFrame()
        df = pd.DataFrame(
            candles, columns=["date", "Open", "High", "Low", "Close", "Volume"]
        )
        df["date"] = pd.to_datetime(df["date"]).dt.normalize()
        return df.set_index("date").sort_index()
    except Exception:
        return pd.DataFrame()


def load_nse_equity_tokens(api_key: str, access_token: str) -> pd.DataFrame:
    """
    Download NSE instrument master and return a DataFrame with
    columns [tradingsymbol, instrument_token] for EQ segment instruments.
    Cache this result in session_state externally.
    """
    from io import StringIO
    r = requests.get(
        f"{_KITE_BASE}/instruments/NSE",
        headers=kite_hdrs(api_key, access_token),
        timeout=30,
    )
    r.raise_for_status()
    df = pd.read_csv(StringIO(r.text))
    eq = df[df.get("segment", pd.Series(dtype=str)) == "NSE"][
        ["tradingsymbol", "instrument_token"]
    ].copy()
    eq["instrument_token"] = eq["instrument_token"].astype(int)
    return eq.set_index("tradingsymbol")
