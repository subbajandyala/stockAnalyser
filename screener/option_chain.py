import time
import numpy as np
import pandas as pd

_NSE = "https://www.nseindia.com"


def fetch_option_chain(symbol: str) -> dict:
    """Fetch NSE option chain via curl_cffi Chrome TLS impersonation."""
    from curl_cffi import requests as cf

    s = cf.Session(impersonate="chrome120")

    try:
        s.get(_NSE, timeout=15)
        time.sleep(2)
        s.get(f"{_NSE}/option-chain", timeout=15)
        time.sleep(2)
    except Exception:
        pass

    if symbol in {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"}:
        url = f"{_NSE}/api/option-chain-indices?symbol={symbol}"
    else:
        url = f"{_NSE}/api/option-chain-equities?symbol={symbol}"

    for attempt in range(3):
        try:
            resp = s.get(url, timeout=20, headers={
                "Referer": f"{_NSE}/option-chain",
                "Accept": "application/json, text/plain, */*",
            })
            resp.raise_for_status()
            data = resp.json()
            if not data.get("records"):
                raise RuntimeError("NSE returned empty data")
            return data
        except Exception as e:
            if attempt == 2:
                raise RuntimeError(
                    "NSE option chain could not be fetched from this server.\n\n"
                    "NSE India actively blocks cloud hosting IPs (Streamlit Cloud, "
                    "AWS, GCP, etc.).\n\n"
                    "✅ **This feature works perfectly when you run the app locally** "
                    "(`streamlit run app.py` on your laptop)."
                ) from e
            time.sleep(2 ** attempt)


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
