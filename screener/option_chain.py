import time
import numpy as np
import pandas as pd

_NSE = "https://www.nseindia.com"


# ── Playwright fetch (primary) ────────────────────────────────────────────────
# Uses a real headless Chromium browser so Akamai JavaScript challenges are
# executed and solved automatically.  We intercept the XHR the page makes
# to the option-chain API rather than making a separate request.

def _fetch_via_playwright(symbol: str) -> dict:
    from playwright.sync_api import sync_playwright

    if symbol in {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"}:
        api_pattern = f"option-chain-indices?symbol={symbol}"
    else:
        api_pattern = f"option-chain-equities?symbol={symbol}"

    captured: dict = {}

    def _on_response(response):
        if api_pattern in response.url and not captured:
            try:
                captured["data"] = response.json()
            except Exception:
                pass

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-setuid-sandbox",
                "--single-process",
            ],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        page = context.new_page()
        page.on("response", _on_response)

        try:
            page.goto(
                f"{_NSE}/option-chain",
                wait_until="networkidle",
                timeout=60_000,
            )
            page.wait_for_timeout(4_000)
        except Exception:
            pass
        finally:
            browser.close()

    data = captured.get("data")
    if not data or not data.get("records"):
        raise RuntimeError("Playwright: no option chain data captured from NSE page")

    return data


# ── curl_cffi fetch (fallback) ────────────────────────────────────────────────
# Impersonates Chrome's TLS fingerprint — works on servers where Playwright
# can't launch a browser but the IP isn't hard-blocked.

def _fetch_via_curl_cffi(symbol: str) -> dict:
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

    resp = s.get(url, timeout=20, headers={
        "Referer": f"{_NSE}/option-chain",
        "Accept": "application/json, text/plain, */*",
    })
    resp.raise_for_status()
    data = resp.json()

    if not data.get("records"):
        raise RuntimeError("curl_cffi: NSE returned empty data")

    return data


# ── Public entry point ────────────────────────────────────────────────────────

def fetch_option_chain(symbol: str) -> dict:
    """Try Playwright first, fall back to curl_cffi, raise with clear message."""
    try:
        return _fetch_via_playwright(symbol)
    except Exception:
        pass

    for attempt in range(3):
        try:
            return _fetch_via_curl_cffi(symbol)
        except Exception as e:
            if attempt == 2:
                raise RuntimeError(
                    "NSE option chain could not be fetched from this server.\n\n"
                    "NSE India actively blocks cloud hosting IPs (Streamlit Cloud, "
                    "AWS, GCP, etc.) even when a real browser is used.\n\n"
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
