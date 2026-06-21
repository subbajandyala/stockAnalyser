import yfinance as yf
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable


def _fetch_info(row: dict) -> dict | None:
    """Fetch yfinance fundamentals for one stock and apply filters. Returns dict or None."""
    try:
        info = yf.Ticker(row["NSE_Symbol"]).info

        price      = float(info.get("currentPrice") or info.get("regularMarketPrice") or 0)
        mkt_cap    = float(info.get("marketCap") or 0)
        roe        = info.get("returnOnEquity")
        de         = info.get("debtToEquity")
        opm        = info.get("operatingMargins")
        pe         = info.get("trailingPE")
        rev_gr     = info.get("revenueGrowth")
        earn_gr    = info.get("earningsGrowth")
        high_52w   = float(info.get("fiftyTwoWeekHigh") or price or 1)

        # ── Hard filters ──────────────────────────────────────────────────────
        if price <= 0:
            return None
        if mkt_cap < 10_000_000_000:           # < 1000 Cr (10B INR)
            return None
        if roe is None or float(roe) < 0.15:   # ROE < 15%
            return None
        if opm is None or float(opm) < 0.12:   # OPM < 12%
            return None
        if rev_gr is None or float(rev_gr) < 0.08:    # Revenue growth < 8%
            return None
        if earn_gr is None or float(earn_gr) < 0.08:  # Earnings growth < 8%
            return None

        # D/E: yfinance returns as percentage (50 = ratio 0.50), filter D/E < 0.5
        if de is not None:
            de_f = float(de)
            if de_f > 50:
                return None
        else:
            de_f = None

        # Down from 52W high > 25% (pulled back, buying opportunity)
        down_pct = (high_52w - price) / high_52w * 100 if high_52w > 0 else 0
        if down_pct < 25:
            return None

        return {
            "Symbol":        row["Symbol"],
            "Company":       row["Company"],
            "NSE_Symbol":    row["NSE_Symbol"],
            "Price":         round(price, 2),
            "Mkt Cap (Cr)":  round(mkt_cap / 1e7, 0),
            "ROE %":         round(float(roe) * 100, 1),
            "D/E":           round(de_f / 100, 2) if de_f is not None else None,
            "OPM %":         round(float(opm) * 100, 1),
            "Rev Growth %":  round(float(rev_gr) * 100, 1),
            "Earn Growth %": round(float(earn_gr) * 100, 1),
            "P/E":           round(float(pe), 1) if pe else None,
            "↓ 52W High %":  round(down_pct, 1),
        }
    except Exception:
        return None


def fetch_fundamental_stocks(
    symbols_df: pd.DataFrame,
    max_workers: int = 20,
    progress_cb: Callable[[float], None] | None = None,
) -> pd.DataFrame:
    """
    Screen NIFTY 500 stocks using yfinance fundamental data (parallel fetch).
    Criteria applied: Market Cap >1000Cr, ROE >15%, D/E <0.5, OPM >12%,
    Revenue & Earnings growth >8%, Down from 52W High >25%.
    """
    rows  = symbols_df.to_dict("records")
    total = len(rows)
    done  = 0
    results: list[dict] = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_fetch_info, r): r for r in rows}
        for future in as_completed(futures):
            done += 1
            if progress_cb:
                progress_cb(done / total)
            result = future.result()
            if result:
                results.append(result)

    if not results:
        return pd.DataFrame()

    df = pd.DataFrame(results)
    df = df.sort_values("Mkt Cap (Cr)", ascending=False).reset_index(drop=True)
    return df
