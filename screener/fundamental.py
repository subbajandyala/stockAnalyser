import re
import time
import requests
import pandas as pd
from io import StringIO
from urllib.parse import unquote

_BASE = "https://www.screener.in"
_URL  = f"{_BASE}/screen/raw/"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         f"{_BASE}/screen/new/",
}

QUERY = (
    "Market Capitalization > 1000 AND "
    "Return on capital employed > 15 AND "
    "Return on equity > 15 AND "
    "Debt to equity < 0.5 AND "
    "OPM > 12 AND "
    "Sales growth 3Years > 8 AND "
    "Profit growth 3Years > 8 AND "
    "Piotroski score >= 6 AND "
    "Capital work in progress > 0.1 * Current assets AND "
    "OPM last year > 0 AND "
    "Promoter holding >= 0.1 AND "
    "Change in promoter holding > -2 AND "
    "Down from 52w high > 25 AND "
    "Pledged percentage < 10 AND "
    "DII holding + FII holding > 5 AND "
    "Price to Earning < Industry PE"
)


def _extract_symbols(html: str) -> list[str]:
    """Extract ordered, deduplicated NSE symbols from Screener.in /company/ links."""
    raw = re.findall(r'href="/company/([A-Z0-9%&.\-]+)/', html)
    seen: set[str] = set()
    result: list[str] = []
    for s in raw:
        decoded = unquote(s)
        if decoded not in seen:
            seen.add(decoded)
            result.append(decoded)
    return result


def fetch_fundamental_stocks() -> pd.DataFrame:
    """
    Fetches stocks from Screener.in matching the fundamental quality filter.
    Returns a DataFrame with Screener.in columns plus NSE_Symbol for yfinance charting.
    Raises RuntimeError with a user-friendly message on failure.
    """
    session = requests.Session()
    session.headers.update(_HEADERS)

    # Warm-up to pick up session cookies
    try:
        session.get(_BASE, timeout=10)
        time.sleep(1)
    except Exception:
        pass

    resp = session.get(
        _URL,
        params={"sort": "market capitalization desc", "source": "", "query": QUERY},
        timeout=30,
    )
    resp.raise_for_status()
    html = resp.text

    # Detect login wall
    if "/login/" in resp.url or ("id_username" in html and "id_password" in html):
        raise RuntimeError(
            "Screener.in requires a **free account** to run complex queries.\n\n"
            "**Fix:** Sign up at screener.in (free), log in via your browser on this machine, "
            "then click **Run Fundamental Screener** again."
        )

    symbols = _extract_symbols(html)

    # html5lib is pure-Python — works on all platforms including Streamlit Cloud
    try:
        tables = pd.read_html(StringIO(html), flavor="html5lib")
    except Exception as e:
        raise RuntimeError(f"Could not parse Screener.in response: {e}") from e

    if not tables:
        raise RuntimeError(
            "Screener.in returned no results for the current criteria. "
            "The market may have no stocks satisfying all conditions today."
        )

    # Pick the largest table (main results), skip tiny navigation tables
    df = max(tables, key=len).copy()

    if len(df) == 0:
        raise RuntimeError(
            "No stocks passed all the fundamental filters today. "
            "Try relaxing one or more criteria."
        )

    # Drop the serial-number column Screener.in prepends
    first_col = str(df.columns[0]).strip()
    if first_col in {"S.No.", "S. No.", "#", "No."}:
        df = df.iloc[:, 1:].copy()

    # Attach NSE_Symbol for chart lookups
    df["NSE_Symbol"] = [
        (symbols[i] + ".NS") if i < len(symbols) else ""
        for i in range(len(df))
    ]

    return df.reset_index(drop=True)
