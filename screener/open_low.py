"""
Open = Low Scanner — NIFTY 100 intraday momentum setup.

When today's Low = today's Open, strong buyers absorbed all selling from the
very first tick. The setup signals upside momentum for the rest of the day.
Stop loss: today's Low (which equals the Open).
Tolerance: Low may be up to 0.05 % below Open to account for tick rounding.
"""

import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import pandas as pd
import yfinance as yf

_IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
TOLERANCE_PCT = 0.05   # Low must be within 0.05 % of Open
MAX_WORKERS   = 25

# NIFTY 100 = NIFTY 50 + NIFTY Next 50
NIFTY100: list[tuple[str, str]] = [
    # ── NIFTY 50 ────────────────────────────────────────────────────────────
    ("RELIANCE",   "Reliance Industries"),
    ("TCS",        "Tata Consultancy Services"),
    ("HDFCBANK",   "HDFC Bank"),
    ("INFY",       "Infosys"),
    ("ICICIBANK",  "ICICI Bank"),
    ("HINDUNILVR", "Hindustan Unilever"),
    ("ITC",        "ITC"),
    ("SBIN",       "State Bank of India"),
    ("BHARTIARTL", "Bharti Airtel"),
    ("KOTAKBANK",  "Kotak Mahindra Bank"),
    ("LT",         "Larsen & Toubro"),
    ("AXISBANK",   "Axis Bank"),
    ("ASIANPAINT", "Asian Paints"),
    ("MARUTI",     "Maruti Suzuki"),
    ("SUNPHARMA",  "Sun Pharmaceutical"),
    ("WIPRO",      "Wipro"),
    ("ULTRACEMCO", "UltraTech Cement"),
    ("TITAN",      "Titan Company"),
    ("BAJFINANCE", "Bajaj Finance"),
    ("NESTLEIND",  "Nestle India"),
    ("TECHM",      "Tech Mahindra"),
    ("NTPC",       "NTPC"),
    ("POWERGRID",  "Power Grid Corporation"),
    ("ONGC",       "ONGC"),
    ("HCLTECH",    "HCL Technologies"),
    ("BAJAJFINSV", "Bajaj Finserv"),
    ("M&M",        "Mahindra & Mahindra"),
    ("ADANIENT",   "Adani Enterprises"),
    ("ADANIPORTS", "Adani Ports"),
    ("COALINDIA",  "Coal India"),
    ("JSWSTEEL",   "JSW Steel"),
    ("TATAMOTORS", "Tata Motors"),
    ("TATASTEEL",  "Tata Steel"),
    ("INDUSINDBK", "IndusInd Bank"),
    ("GRASIM",     "Grasim Industries"),
    ("CIPLA",      "Cipla"),
    ("DRREDDY",    "Dr. Reddy's Laboratories"),
    ("EICHERMOT",  "Eicher Motors"),
    ("BRITANNIA",  "Britannia Industries"),
    ("DIVISLAB",   "Divi's Laboratories"),
    ("HEROMOTOCO", "Hero MotoCorp"),
    ("TATACONSUM", "Tata Consumer Products"),
    ("APOLLOHOSP", "Apollo Hospitals"),
    ("BPCL",       "Bharat Petroleum"),
    ("IOC",        "Indian Oil"),
    ("HINDALCO",   "Hindalco Industries"),
    ("UPL",        "UPL"),
    ("BAJAJ-AUTO", "Bajaj Auto"),
    ("SHREECEM",   "Shree Cement"),
    ("SBILIFE",    "SBI Life Insurance"),
    ("HDFCLIFE",   "HDFC Life Insurance"),
    # ── NIFTY Next 50 ───────────────────────────────────────────────────────
    ("ADANIGREEN", "Adani Green Energy"),
    ("AMBUJACEM",  "Ambuja Cements"),
    ("AUROPHARMA", "Aurobindo Pharma"),
    ("BANKBARODA", "Bank of Baroda"),
    ("BEL",        "Bharat Electronics"),
    ("BERGEPAINT", "Berger Paints"),
    ("BOSCHLTD",   "Bosch"),
    ("CANBK",      "Canara Bank"),
    ("CHOLAFIN",   "Cholamandalam Finance"),
    ("COLPAL",     "Colgate-Palmolive"),
    ("DABUR",      "Dabur India"),
    ("DMART",      "Avenue Supermarts"),
    ("DLF",        "DLF"),
    ("GAIL",       "GAIL India"),
    ("GODREJCP",   "Godrej Consumer Products"),
    ("HAL",        "Hindustan Aeronautics"),
    ("HAVELLS",    "Havells India"),
    ("INDIGO",     "IndiGo"),
    ("IRCTC",      "IRCTC"),
    ("LICHSGFIN",  "LIC Housing Finance"),
    ("LUPIN",      "Lupin"),
    ("MARICO",     "Marico"),
    ("MUTHOOTFIN", "Muthoot Finance"),
    ("NYKAA",      "Nykaa"),
    ("PFC",        "Power Finance Corporation"),
    ("PIDILITIND", "Pidilite Industries"),
    ("PNB",        "Punjab National Bank"),
    ("RECLTD",     "REC"),
    ("SAIL",       "Steel Authority of India"),
    ("SBICARD",    "SBI Cards"),
    ("SIEMENS",    "Siemens India"),
    ("SRF",        "SRF"),
    ("TORNTPHARM", "Torrent Pharmaceuticals"),
    ("TRENT",      "Trent"),
    ("TVSMOTOR",   "TVS Motor"),
    ("VBL",        "Varun Beverages"),
    ("VEDL",       "Vedanta"),
    ("VOLTAS",     "Voltas"),
    ("ZOMATO",     "Zomato"),
    ("INDHOTEL",   "Indian Hotels"),
    ("ALKEM",      "Alkem Laboratories"),
    ("BALKRISIND", "Balkrishna Industries"),
    ("BIOCON",     "Biocon"),
    ("ASTRAL",     "Astral"),
    ("PAYTM",      "Paytm"),
    ("ICICIPRULI", "ICICI Prudential Life"),
    ("ICICIGI",    "ICICI Lombard General Insurance"),
    ("MFSL",       "Max Financial Services"),
]


def _fetch_one(symbol: str, company: str) -> Optional[dict]:
    try:
        df = yf.download(
            f"{symbol}.NS", period="1d", interval="1m",
            auto_adjust=True, progress=False,
        )
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        if df.empty or len(df) < 3:
            return None

        idx = df.index
        if idx.tz is None:
            idx = idx.tz_localize("UTC")
        today = datetime.date.today()
        mask = pd.Series(idx.tz_convert(_IST).date, index=df.index) == today
        df_t = df[mask]
        if len(df_t) < 3:
            return None

        day_open = float(df_t["Open"].iloc[0])
        day_low  = float(df_t["Low"].min())
        day_high = float(df_t["High"].max())
        ltp      = float(df_t["Close"].iloc[-1])

        if day_open <= 0:
            return None

        # How far below open is the low? (positive = low < open)
        diff_pct = (day_open - day_low) / day_open * 100
        if diff_pct > TOLERANCE_PCT:
            return None

        return {
            "Symbol":      symbol,
            "Company":     company,
            "NSE_Symbol":  f"{symbol}.NS",
            "Open / SL":   round(day_open, 2),
            "LTP":         round(ltp, 2),
            "Day High":    round(day_high, 2),
            "Chg %":       round((ltp - day_open) / day_open * 100, 2),
            "High Gain %": round((day_high - day_open) / day_open * 100, 2),
            "O=L Diff %":  round(diff_pct, 3),
        }
    except Exception:
        return None


def run_open_low_scan() -> tuple[pd.DataFrame, str]:
    """
    Scan NIFTY 100 for Open=Low setups.
    Returns (df, error_msg). df is empty and error_msg is set on failure.
    """
    results: list[dict] = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futs = {pool.submit(_fetch_one, sym, co): sym for sym, co in NIFTY100}
        for fut in as_completed(futs):
            try:
                r = fut.result()
                if r:
                    results.append(r)
            except Exception:
                pass

    if not results:
        return pd.DataFrame(), "No Open=Low stocks found in NIFTY 100 right now."

    df = (
        pd.DataFrame(results)
        .sort_values("Chg %", ascending=False)
        .reset_index(drop=True)
    )
    return df, ""
