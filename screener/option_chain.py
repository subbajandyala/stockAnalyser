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


def _api_path(symbol: str) -> str:
    if symbol in {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"}:
        return f"/api/option-chain-indices?symbol={symbol}"
    return f"/api/option-chain-equities?symbol={symbol}"


# ── Primary: Selenium (uses real Chrome on local machine) ─────────────────────
# Navigates to NSE in headless Chrome so Akamai JS executes and sets cookies,
# then fires the API fetch from within the browser context (same-origin).

def _fetch_via_selenium(symbol: str) -> dict:
    try:
        import logging
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
        from webdriver_manager.chrome import ChromeDriverManager
        logging.getLogger("WDM").setLevel(logging.ERROR)
    except ImportError:
        raise RuntimeError("selenium/webdriver-manager not installed")

    path = _api_path(symbol)

    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--log-level=3")
    opts.add_experimental_option("excludeSwitches", ["enable-logging"])

    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=opts,
    )
    try:
        driver.set_page_load_timeout(30)
        driver.get(f"{_NSE}/option-chain")
        time.sleep(5)  # wait for Akamai JS to run and set cookies

        # Execute the fetch from inside the browser (same-origin — cookies auto-sent)
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
            raise RuntimeError(f"Browser fetch: {data['__err__']}")
        if not data or not data.get("records"):
            raise RuntimeError("Selenium: NSE returned empty data")
        return data
    finally:
        driver.quit()


# ── Fallback 1: requests with session warming ─────────────────────────────────

def _fetch_via_requests(symbol: str) -> dict:
    s = _req.Session()
    s.headers.update(_HEADERS)
    s.get(_NSE, timeout=12)
    time.sleep(1.5)
    s.get(f"{_NSE}/option-chain", timeout=12)
    time.sleep(1.5)
    resp = s.get(f"{_NSE}{_api_path(symbol)}", timeout=20)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("records"):
        raise RuntimeError("NSE returned empty data")
    return data


# ── Fallback 2: curl_cffi Chrome TLS impersonation ───────────────────────────

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

def fetch_option_chain(symbol: str) -> dict:
    """Selenium (local Chrome) → requests → curl_cffi, with retries."""
    last_err = None

    try:
        return _fetch_via_selenium(symbol)
    except Exception as e:
        last_err = e

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
        f"NSE option chain fetch failed ({last_err}).\n\n"
        "**To fix this:**\n"
        "- Run the app locally on your laptop\n"
        "- Make sure **Google Chrome** is installed\n"
        "- Click **Refresh** once or twice — NSE sometimes needs 2 attempts\n"
        "- NSE blocks all cloud hosting IPs (Streamlit Cloud, AWS, GCP)"
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
