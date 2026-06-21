import time
import numpy as np
import pandas as pd
import requests as _req

_NSE = "https://www.nseindia.com"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection":      "keep-alive",
    "Referer":         f"{_NSE}/option-chain",
}


def _api_url(symbol: str) -> str:
    if symbol in {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"}:
        return f"{_NSE}/api/option-chain-indices?symbol={symbol}"
    return f"{_NSE}/api/option-chain-equities?symbol={symbol}"


def _fetch_via_requests(symbol: str) -> dict:
    s = _req.Session()
    s.headers.update(_HEADERS)
    s.get(_NSE, timeout=12)
    time.sleep(1.5)
    s.get(f"{_NSE}/option-chain", timeout=12)
    time.sleep(1.5)
    resp = s.get(_api_url(symbol), timeout=20)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("records"):
        raise RuntimeError("NSE returned empty data")
    return data


def _fetch_via_curl_cffi(symbol: str) -> dict:
    from curl_cffi import requests as cf
    s = cf.Session(impersonate="chrome120")
    try:
        s.get(_NSE, timeout=12)
        time.sleep(1.5)
        s.get(f"{_NSE}/option-chain", timeout=12)
        time.sleep(1.5)
    except Exception:
        pass
    resp = s.get(_api_url(symbol), timeout=20, headers={
        "Referer": f"{_NSE}/option-chain",
        "Accept":  "application/json, text/plain, */*",
    })
    resp.raise_for_status()
    data = resp.json()
    if not data.get("records"):
        raise RuntimeError("NSE returned empty data")
    return data


def fetch_option_chain(symbol: str) -> dict:
    """Try requests first (works from home IPs), then curl_cffi, then give up."""
    last_err = None

    for attempt in range(2):
        try:
            return _fetch_via_requests(symbol)
        except Exception as e:
            last_err = e
            time.sleep(2 ** attempt)

    for attempt in range(2):
        try:
            return _fetch_via_curl_cffi(symbol)
        except Exception as e:
            last_err = e
            time.sleep(2 ** attempt)

    raise RuntimeError(
        f"NSE option chain fetch failed after retries ({last_err}).\n\n"
        "**Tips:**\n"
        "- Try clicking **Refresh** 1–2 more times\n"
        "- NSE sometimes throttles requests — wait 30 seconds and retry\n"
        "- If on Streamlit Cloud: NSE blocks cloud IPs; run the app locally instead"
    ) from last_err


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_expiries(data: dict) -> list:
    return data.get("records", {}).get("expiryDates", [])


def parse_chain(data: dict, expiry: str) -> tuple:
    records = data.get("records", {})
    spot = float(records.get("underlyingValue", 0))
    rows = []
    for item in records.get("data", []):
        if item.get("expiryDate") != expiry:
            continue
        ce = item.get("CE", {})
        pe = item.get("PE", {})
        rows.append({
            "Strike":     float(item["strikePrice"]),
            "CE OI":      int(ce.get("openInterest", 0)),
            "CE Chng OI": int(ce.get("changeinOpenInterest", 0)),
            "CE Vol":     int(ce.get("totalTradedVolume", 0)),
            "CE IV":      float(ce.get("impliedVolatility", 0)),
            "CE LTP":     float(ce.get("lastPrice", 0)),
            "PE LTP":     float(pe.get("lastPrice", 0)),
            "PE IV":      float(pe.get("impliedVolatility", 0)),
            "PE Vol":     int(pe.get("totalTradedVolume", 0)),
            "PE Chng OI": int(pe.get("changeinOpenInterest", 0)),
            "PE OI":      int(pe.get("openInterest", 0)),
        })
    df = pd.DataFrame(rows).sort_values("Strike", ascending=False).reset_index(drop=True)
    return df, spot


def atm_strike(df: pd.DataFrame, spot: float) -> float:
    if df.empty:
        return spot
    return float(df.loc[(df["Strike"] - spot).abs().idxmin(), "Strike"])


def calc_pcr(df: pd.DataFrame) -> float:
    total_ce = df["CE OI"].sum()
    return round(df["PE OI"].sum() / total_ce, 2) if total_ce else 0.0


def calc_max_pain(df: pd.DataFrame) -> float:
    s_arr = df["Strike"].values
    ce_oi = df["CE OI"].values.astype(float)
    pe_oi = df["PE OI"].values.astype(float)
    pain = [
        float((ce_oi * np.maximum(0, s - s_arr)).sum() + (pe_oi * np.maximum(0, s_arr - s)).sum())
        for s in s_arr
    ]
    return float(s_arr[int(np.argmin(pain))])
