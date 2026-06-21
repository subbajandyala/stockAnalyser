import time
import numpy as np
import requests
import pandas as pd

_NSE = "https://www.nseindia.com"

# Full modern Chrome 126 headers — NSE rejects incomplete sets
_BASE_HDRS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Connection": "keep-alive",
    "sec-ch-ua": '"Not/A)Brand";v="8", "Chromium";v="126", "Google Chrome";v="126"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(_BASE_HDRS)

    try:
        # Step 1 — main page (sets nseappid, ak_bmsc, bm_sz cookies)
        s.headers.update({
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
            "Cache-Control": "max-age=0",
        })
        s.get(_NSE, timeout=15)
        time.sleep(2)

        # Step 2 — equity market page (deepens session cookies)
        s.headers.update({"Referer": _NSE + "/"})
        s.get(f"{_NSE}/market-data/live-equity-market", timeout=15)
        time.sleep(1.5)

        # Step 3 — option chain page (critical: sets oc-specific cookies)
        s.get(f"{_NSE}/option-chain", timeout=15)
        time.sleep(2)

    except Exception:
        pass

    return s


def fetch_option_chain(symbol: str) -> dict:
    s = _session()

    # Switch to XHR headers for the API call
    s.headers.update({
        "Accept": "application/json, text/plain, */*",
        "Referer": f"{_NSE}/option-chain",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "X-Requested-With": "XMLHttpRequest",
    })

    if symbol in {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"}:
        url = f"{_NSE}/api/option-chain-indices?symbol={symbol}"
    else:
        url = f"{_NSE}/api/option-chain-equities?symbol={symbol}"

    last_exc = None
    for attempt in range(3):
        try:
            resp = s.get(url, timeout=20)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("records"):
                    return data
            # 404/401 often means NSE cookie session expired — re-warm and retry
            s = _session()
            s.headers.update({
                "Accept": "application/json, text/plain, */*",
                "Referer": f"{_NSE}/option-chain",
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
                "X-Requested-With": "XMLHttpRequest",
            })
        except Exception as e:
            last_exc = e

        time.sleep(2 ** attempt)

    if last_exc:
        raise last_exc
    resp.raise_for_status()
    return {}


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
