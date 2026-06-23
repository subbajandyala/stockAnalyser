import time
from io import StringIO

import numpy as np
import pandas as pd
import requests as _req

_NSE = "https://www.nseindia.com"

# ── Zerodha Kite Connect ──────────────────────────────────────────────────────
_KITE_BASE  = "https://api.kite.trade"
_KITE_INDEX = {
    "NIFTY":      "NSE:NIFTY 50",
    "BANKNIFTY":  "NSE:NIFTY BANK",
    "FINNIFTY":   "NSE:NIFTY FIN SERVICE",
    "MIDCPNIFTY": "NSE:NIFTY MID SELECT",
    "SENSEX":     "BSE:SENSEX",
    "BANKEX":     "BSE:BANKEX",
}
_KITE_EXCHANGE = {
    "SENSEX": "BFO",
    "BANKEX": "BFO",
}  # defaults to NFO for all others


def _fetch_via_kite(symbol: str, api_key: str, access_token: str) -> dict:
    """Real-time option chain via Zerodha Kite Connect REST API — no browser required."""
    hdrs = {
        "X-Kite-Version": "3",
        "Authorization":  f"token {api_key}:{access_token}",
    }

    exchange = _KITE_EXCHANGE.get(symbol, "NFO")

    # 1. Instrument master (NFO for NSE indices, BFO for BSE indices)
    resp = _req.get(f"{_KITE_BASE}/instruments/{exchange}", headers=hdrs, timeout=30)
    resp.raise_for_status()
    instr = pd.read_csv(StringIO(resp.text))

    # 2. Filter CE + PE for the requested index
    opts = instr[
        (instr["name"] == symbol) &
        (instr["instrument_type"].isin(["CE", "PE"]))
    ].copy()
    if opts.empty:
        raise RuntimeError(f"No {symbol} options found in NFO instruments master")

    # 3. Expiry dates (Kite YYYY-MM-DD → DD-Mon-YYYY for NSE-compat format)
    opts["expiry_dt"] = pd.to_datetime(opts["expiry"])
    expiry_strs = sorted({d.strftime("%d-%b-%Y").upper() for d in opts["expiry_dt"]})

    # 4. Spot price
    idx_sym  = _KITE_INDEX.get(symbol, f"NSE:{symbol}")
    ltp_resp = _req.get(f"{_KITE_BASE}/quote/ltp", headers=hdrs,
                        params={"i": idx_sym}, timeout=10)
    ltp_resp.raise_for_status()
    spot = float(ltp_resp.json()["data"][idx_sym]["last_price"])

    # 5. Fetch OI + volume + LTP in batches of 400 instruments
    nfo_syms = (exchange + ":" + opts["tradingsymbol"]).tolist()
    quotes: dict = {}
    for i in range(0, len(nfo_syms), 400):
        q_resp = _req.get(f"{_KITE_BASE}/quote", headers=hdrs,
                          params={"i": nfo_syms[i : i + 400]}, timeout=30)
        q_resp.raise_for_status()
        quotes.update(q_resp.json().get("data", {}))

    # 6. Build NSE-compatible dict so existing parse_chain() works unchanged.
    #    Note: Kite quote API does not expose daily OI change → set to 0.
    rows: dict = {}
    for _, row in opts.iterrows():
        ts_key  = f"{exchange}:{row['tradingsymbol']}"
        q       = quotes.get(ts_key, {})
        exp_str = row["expiry_dt"].strftime("%d-%b-%Y").upper()
        key     = (float(row["strike"]), exp_str)

        if key not in rows:
            rows[key] = {
                "strikePrice": float(row["strike"]),
                "expiryDate":  exp_str,
                "CE": {},
                "PE": {},
            }
        rows[key][row["instrument_type"]] = {
            "openInterest":         int(q.get("oi", 0)),
            "changeinOpenInterest": 0,
            "totalTradedVolume":    int(q.get("volume", 0)),
            "impliedVolatility":    0.0,
            "lastPrice":            float(q.get("last_price", 0)),
        }

    return {
        "records": {
            "expiryDates":     expiry_strs,
            "underlyingValue": spot,
            "data":            list(rows.values()),
        }
    }
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


def _api_path(symbol: str) -> str:
    if symbol in {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"}:
        return f"/api/option-chain-indices?symbol={symbol}"
    return f"/api/option-chain-equities?symbol={symbol}"


# ── Fastest: plain requests session (works on most local IPs) ─────────────────

def _fetch_via_requests(symbol: str) -> dict:
    session = _req.Session()
    session.headers.update(_HEADERS)
    try:
        session.get(_NSE, timeout=12)
        time.sleep(1)
        session.get(f"{_NSE}/option-chain", timeout=12)
        time.sleep(1)
    except Exception:
        pass
    resp = session.get(
        f"{_NSE}{_api_path(symbol)}",
        headers={"Referer": f"{_NSE}/option-chain",
                 "X-Requested-With": "XMLHttpRequest"},
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("records"):
        raise RuntimeError("NSE returned empty data")
    return data
# Uses real Chrome but patches the binary so Akamai's JS fingerprint checks
# cannot detect automation.  Once on the page, fires the API fetch from inside
# the browser context so all Akamai cookies are included automatically.

def _fetch_via_uc(symbol: str) -> dict:
    try:
        import undetected_chromedriver as uc
    except ImportError:
        raise RuntimeError("undetected-chromedriver not installed")

    path = _api_path(symbol)

    opts = uc.ChromeOptions()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1280,800")

    driver = uc.Chrome(options=opts)
    try:
        driver.set_page_load_timeout(30)
        driver.get(f"{_NSE}/option-chain")
        time.sleep(6)  # let Akamai JS run and set cookies

        data = driver.execute_async_script(f"""
            var done = arguments[0];
            fetch("{path}", {{
                credentials: "same-origin",
                headers: {{
                    "Accept": "application/json, text/plain, */*",
                    "Referer": "{_NSE}/option-chain"
                }}
            }})
            .then(r => r.json())
            .then(d => done(d))
            .catch(e => done({{"__err__": e.toString()}}));
        """)

        if isinstance(data, dict) and "__err__" in data:
            raise RuntimeError(f"fetch error: {data['__err__']}")
        if not data or not data.get("records"):
            raise RuntimeError("NSE returned empty data")
        return data
    finally:
        try:
            driver.quit()
        except Exception:
            pass


# ── Fallback: curl_cffi Chrome TLS impersonation ─────────────────────────────

def _fetch_via_curl_cffi(symbol: str) -> dict:
    from curl_cffi import requests as cf
    s = cf.Session(impersonate="chrome120")
    try:
        s.get(_NSE, timeout=12)
        time.sleep(2)
        s.get(f"{_NSE}/option-chain", timeout=12)
        time.sleep(2)
    except Exception:
        pass
    resp = s.get(f"{_NSE}{_api_path(symbol)}", timeout=20, headers={
        "Referer": f"{_NSE}/option-chain",
        "Accept":  "application/json, text/plain, */*",
    })
    resp.raise_for_status()
    data = resp.json()
    if not data.get("records"):
        raise RuntimeError("NSE returned empty data")
    return data


# ── Public entry point ────────────────────────────────────────────────────────

def fetch_option_chain(symbol: str,
                       api_key: str = "",
                       access_token: str = "") -> dict:
    """
    Fetch live NSE option chain.
    Priority: Kite Connect (if creds) → undetected-chromedriver → curl_cffi.
    """
    # ── Zerodha Kite (real-time, cloud-compatible) ────────────────────────────
    if api_key and access_token:
        return _fetch_via_kite(symbol, api_key, access_token)

    last_err = None

    # Try plain requests session first (fastest, works on most local IPs)
    try:
        return _fetch_via_requests(symbol)
    except Exception as e:
        last_err = e

    # Try Chrome with bot-detection patches (works locally)
    try:
        return _fetch_via_uc(symbol)
    except Exception as e:
        last_err = e

    # Try curl_cffi TLS impersonation
    for attempt in range(3):
        try:
            return _fetch_via_curl_cffi(symbol)
        except Exception as e:
            last_err = e
            if attempt < 2:
                time.sleep(2 ** attempt)

    raise RuntimeError(
        f"NSE option chain fetch failed ({last_err}).\n\n"
        "**To fix this:**\n"
        "- Connect **Zerodha Kite** in the sidebar for cloud-compatible live data\n"
        "- Or make sure **Google Chrome** is installed and click Refresh 1–2 more times\n"
        "- NSE blocks cloud hosting IPs — Kite Connect is the recommended solution"
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
