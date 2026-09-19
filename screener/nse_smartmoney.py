"""
NSE Smart Money Screener — three screens powered by NSE public data + Kite live option chains.

Screen 1  run_fii_compass()          — FII/DII daily flow trend → NIFTY/BANKNIFTY index options
Screen 2  run_smart_money_options()  — Insider buys (SEBI PIT) + bulk deals → F&O stock options
Screen 3  run_promoter_pulse()       — Promoter-category accumulation → F&O stock options

NSE public data (Akamai-protected; degrades gracefully when blocked on cloud):
  /api/fiidiiTradeReact          — daily FII/DII net buy-sell
  /api/corporates-pit            — SEBI PIT insider trading disclosures
  /api/bulk-deal-data            — today's bulk deals

Kite Connect (live, reliable with credentials):
  /instruments/NFO               — full F&O instruments master
  /quote/ltp                     — spot prices for any exchange:symbol
  /quote                         — full quote (OI, volume, LTP) for options
"""

from __future__ import annotations

import datetime
import time
from io import StringIO

import numpy as np
import pandas as pd
import requests

_KITE_BASE = "https://api.kite.trade"
_NSE_BASE  = "https://www.nseindia.com"
_IST       = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

_NSE_HDR = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection":      "keep-alive",
    "Referer":         _NSE_BASE,
}


# ── Shared helpers ────────────────────────────────────────────────────────────

def _kite_hdrs(api_key: str, access_token: str) -> dict:
    return {
        "X-Kite-Version": "3",
        "Authorization":  f"token {api_key}:{access_token}",
    }


def _nse_get(path: str, params: dict | None = None) -> dict | list:
    """Single NSE API call with session-cookie warm-up."""
    s = requests.Session()
    s.headers.update(_NSE_HDR)
    try:
        s.get(_NSE_BASE, timeout=10)
        time.sleep(0.8)
    except Exception:
        pass
    resp = s.get(f"{_NSE_BASE}{path}", params=params, timeout=20)
    resp.raise_for_status()
    return resp.json()


def _safe_float(val) -> float:
    try:
        return float(str(val).replace(",", "") or 0)
    except (ValueError, TypeError):
        return 0.0


# ── NSE data fetchers ─────────────────────────────────────────────────────────

def fetch_fii_dii_flows(days: int = 15) -> pd.DataFrame:
    """Return last `days` rows of FII/DII daily net activity from NSE."""
    data = _nse_get("/api/fiidiiTradeReact")
    rows = []
    for item in (data if isinstance(data, list) else []):
        try:
            rows.append({
                "Date":     item.get("date", ""),
                "FII Buy":  _safe_float(item.get("buyValue",  item.get("buyVal",  0))),
                "FII Sell": _safe_float(item.get("sellValue", item.get("sellVal", 0))),
                "FII Net":  _safe_float(item.get("netValue",  item.get("netVal",  0))),
                "DII Buy":  _safe_float(item.get("buyValue2",  item.get("dii_buy",  0))),
                "DII Sell": _safe_float(item.get("sellValue2", item.get("dii_sell", 0))),
                "DII Net":  _safe_float(item.get("netValue2",  item.get("dii_net",  0))),
            })
        except Exception:
            continue
    df = pd.DataFrame(rows).head(days)
    return df


def fetch_insider_trades(days: int = 10) -> pd.DataFrame:
    """Fetch SEBI PIT insider-trading disclosures for the last `days` calendar days."""
    ist_now  = datetime.datetime.now(_IST)
    from_dt  = ist_now - datetime.timedelta(days=days)
    from_str = from_dt.strftime("%d-%b-%Y")
    to_str   = ist_now.strftime("%d-%b-%Y")

    data = _nse_get(
        "/api/corporates-pit",
        params={"index": "equities", "from_date": from_str, "to_date": to_str},
    )

    rows = []
    for item in (data.get("data", []) if isinstance(data, dict) else []):
        trans = str(item.get("transactionType", "")).strip().upper()
        if not any(k in trans for k in ("BUY", "ACQUI", "PURCH")):
            continue
        rows.append({
            "Date":       str(item.get("broadcastDateTime", ""))[:11].strip(),
            "Symbol":     str(item.get("symbol", "")).strip().upper(),
            "Company":    item.get("company", ""),
            "Acquirer":   item.get("acquirerName", ""),
            "Category":   item.get("acqCategory", item.get("category", "")),
            "Shares":     int(_safe_float(item.get("noOfShareAcq", 0))),
            "Before %":   _safe_float(item.get("beforeAcqSharesPer", 0)),
            "After %":    _safe_float(item.get("afterAcqSharesPer", 0)),
            "Mode":       item.get("acqMode", ""),
        })

    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["% Change"] = (df["After %"] - df["Before %"]).round(4)
    return df


def fetch_bulk_deals() -> pd.DataFrame:
    """Fetch today's NSE bulk deals."""
    data = _nse_get("/api/bulk-deal-data", params={"type": "bulk_deals"})
    rows = []
    for item in (data.get("data", []) if isinstance(data, dict) else []):
        rows.append({
            "Date":     item.get("date", ""),
            "Symbol":   str(item.get("symbol", "")).strip().upper(),
            "Client":   item.get("clientName", ""),
            "Buy/Sell": str(item.get("buySell", "")).strip().upper(),
            "Qty":      int(_safe_float(item.get("quantity", 0))),
            "Price":    _safe_float(item.get("pricePerShare", 0)),
        })
    return pd.DataFrame(rows) if rows else pd.DataFrame()


# ── Kite helpers ──────────────────────────────────────────────────────────────

def fetch_nfo_instruments(api_key: str, access_token: str) -> pd.DataFrame:
    """Full NFO instruments master from Kite. Cache externally to avoid re-fetch."""
    hdrs = _kite_hdrs(api_key, access_token)
    resp = requests.get(f"{_KITE_BASE}/instruments/NFO", headers=hdrs, timeout=30)
    resp.raise_for_status()
    df = pd.read_csv(StringIO(resp.text))
    df["expiry_dt"] = pd.to_datetime(df["expiry"], errors="coerce")
    return df


def fo_eligible_set(nfo_df: pd.DataFrame) -> set:
    """Set of underlying symbols that have CE/PE contracts in NFO."""
    return set(
        nfo_df[nfo_df["instrument_type"].isin(["CE", "PE"])]["name"].dropna().unique()
    )


def batch_spot_prices(symbols: list, api_key: str, access_token: str) -> dict:
    """LTP for multiple NSE stocks in one Kite batch call."""
    if not symbols:
        return {}
    hdrs  = _kite_hdrs(api_key, access_token)
    syms  = [f"NSE:{s}" for s in symbols]
    out: dict = {}
    for i in range(0, len(syms), 400):
        resp = requests.get(
            f"{_KITE_BASE}/quote/ltp", headers=hdrs,
            params={"i": syms[i: i + 400]}, timeout=20,
        )
        if resp.ok:
            for key, val in resp.json().get("data", {}).items():
                out[key.replace("NSE:", "")] = float(val.get("last_price", 0))
    return out


def stock_option_metrics(
    symbol: str,
    spot: float,
    nfo_df: pd.DataFrame,
    api_key: str,
    access_token: str,
) -> dict | None:
    """
    Fetch near-ATM option chain for a single F&O stock via Kite.
    Returns dict with spot/expiry/atm/pcr/bias/max_pain/ce_wall/pe_wall/atm_ce/atm_pe,
    or None if the stock has no active options.
    """
    hdrs = _kite_hdrs(api_key, access_token)

    opts = nfo_df[
        (nfo_df["name"] == symbol) &
        (nfo_df["instrument_type"].isin(["CE", "PE"]))
    ].copy()
    if opts.empty:
        return None

    nearest = opts["expiry_dt"].min()
    opts     = opts[opts["expiry_dt"] == nearest].copy()
    expiry   = nearest.strftime("%d %b %Y (%A)")

    # Determine strike interval from available strikes
    strikes_sorted = sorted(opts["strike"].astype(float).unique())
    tick = int(strikes_sorted[1] - strikes_sorted[0]) if len(strikes_sorted) >= 2 else 50

    # Near-ATM only (±12 strikes)
    near = opts[abs(opts["strike"].astype(float) - spot) <= tick * 12].copy()
    if near.empty:
        near = opts

    syms = ("NFO:" + near["tradingsymbol"]).tolist()
    quotes: dict = {}
    for i in range(0, len(syms), 400):
        qr = requests.get(
            f"{_KITE_BASE}/quote", headers=hdrs,
            params={"i": syms[i: i + 400]}, timeout=30,
        )
        if qr.ok:
            quotes.update(qr.json().get("data", {}))

    if not quotes:
        return None

    rows: dict = {}
    for _, row in near.iterrows():
        k  = float(row["strike"])
        it = row["instrument_type"]
        q  = quotes.get(f"NFO:{row['tradingsymbol']}", {})
        if k not in rows:
            rows[k] = {"Strike": k,
                       "CE OI": 0, "CE LTP": 0.0, "CE Vol": 0,
                       "PE OI": 0, "PE LTP": 0.0, "PE Vol": 0}
        rows[k][f"{it} OI"]  = int(q.get("oi", 0))
        rows[k][f"{it} LTP"] = float(q.get("last_price", 0.0))
        rows[k][f"{it} Vol"] = int(q.get("volume", 0))

    cdf = (pd.DataFrame(list(rows.values()))
           .sort_values("Strike")
           .reset_index(drop=True))
    if cdf.empty:
        return None

    total_ce = float(cdf["CE OI"].sum())
    total_pe = float(cdf["PE OI"].sum())
    pcr      = round(total_pe / total_ce, 2) if total_ce > 0 else 0.0

    atm_idx  = (cdf["Strike"] - spot).abs().idxmin()
    atm      = float(cdf.loc[atm_idx, "Strike"])
    atm_ce   = float(cdf.loc[atm_idx, "CE LTP"])
    atm_pe   = float(cdf.loc[atm_idx, "PE LTP"])
    ce_wall  = float(cdf.loc[cdf["CE OI"].idxmax(), "Strike"]) if total_ce > 0 else 0
    pe_wall  = float(cdf.loc[cdf["PE OI"].idxmax(), "Strike"]) if total_pe > 0 else 0

    # Max pain
    try:
        s_arr   = cdf["Strike"].values
        ce_oi   = cdf["CE OI"].values.astype(float)
        pe_oi   = cdf["PE OI"].values.astype(float)
        pain    = [
            float((ce_oi * np.maximum(0, s - s_arr)).sum() +
                  (pe_oi * np.maximum(0, s_arr - s)).sum())
            for s in s_arr
        ]
        max_pain = float(s_arr[int(np.argmin(pain))])
    except Exception:
        max_pain = atm

    bias = "Bullish" if pcr > 1.2 else ("Bearish" if pcr < 0.8 else "Neutral")

    return {
        "expiry":    expiry,
        "atm":       atm,
        "pcr":       pcr,
        "bias":      bias,
        "max_pain":  max_pain,
        "ce_wall":   ce_wall,
        "pe_wall":   pe_wall,
        "atm_ce":    atm_ce,
        "atm_pe":    atm_pe,
        "total_ce_oi": int(total_ce),
        "total_pe_oi": int(total_pe),
    }


def _index_chain(
    idx_name: str,
    spot_sym: str,
    tick: int,
    api_key: str,
    access_token: str,
    nfo_df: pd.DataFrame,
) -> dict:
    """Option chain metrics for a single index (NIFTY/BANKNIFTY) via Kite."""
    hdrs = _kite_hdrs(api_key, access_token)

    ltp_r = requests.get(
        f"{_KITE_BASE}/quote/ltp", headers=hdrs,
        params={"i": spot_sym}, timeout=10,
    )
    ltp_r.raise_for_status()
    spot = float(ltp_r.json()["data"][spot_sym]["last_price"])

    opts = nfo_df[
        (nfo_df["name"] == idx_name) &
        (nfo_df["instrument_type"].isin(["CE", "PE"]))
    ].copy()
    nearest = opts["expiry_dt"].min()
    opts    = opts[opts["expiry_dt"] == nearest].copy()

    near = opts[abs(opts["strike"].astype(float) - spot) <= tick * 15].copy()
    syms = ("NFO:" + near["tradingsymbol"]).tolist()
    quotes: dict = {}
    for i in range(0, len(syms), 400):
        qr = requests.get(
            f"{_KITE_BASE}/quote", headers=hdrs,
            params={"i": syms[i: i + 400]}, timeout=30,
        )
        if qr.ok:
            quotes.update(qr.json().get("data", {}))

    rows: dict = {}
    for _, row in near.iterrows():
        k  = float(row["strike"])
        it = row["instrument_type"]
        q  = quotes.get(f"NFO:{row['tradingsymbol']}", {})
        if k not in rows:
            rows[k] = {"Strike": k, "CE OI": 0, "CE LTP": 0.0, "PE OI": 0, "PE LTP": 0.0}
        rows[k][f"{it} OI"]  = int(q.get("oi", 0))
        rows[k][f"{it} LTP"] = float(q.get("last_price", 0.0))

    cdf = pd.DataFrame(list(rows.values())).sort_values("Strike").reset_index(drop=True)

    total_ce = float(cdf["CE OI"].sum())
    total_pe = float(cdf["PE OI"].sum())
    pcr      = round(total_pe / total_ce, 2) if total_ce > 0 else 0.0

    atm_idx  = (cdf["Strike"] - spot).abs().idxmin()
    atm      = float(cdf.loc[atm_idx, "Strike"])
    atm_ce   = float(cdf.loc[atm_idx, "CE LTP"])
    atm_pe   = float(cdf.loc[atm_idx, "PE LTP"])
    ce_wall  = float(cdf.loc[cdf["CE OI"].idxmax(), "Strike"]) if total_ce > 0 else 0
    pe_wall  = float(cdf.loc[cdf["PE OI"].idxmax(), "Strike"]) if total_pe > 0 else 0

    s_arr  = cdf["Strike"].values
    ce_oi  = cdf["CE OI"].values.astype(float)
    pe_oi  = cdf["PE OI"].values.astype(float)
    pain   = [
        float((ce_oi * np.maximum(0, s - s_arr)).sum() +
              (pe_oi * np.maximum(0, s_arr - s)).sum())
        for s in s_arr
    ]
    max_pain  = float(s_arr[int(np.argmin(pain))])
    gap       = max_pain - spot
    bias      = "Bullish" if pcr > 1.2 else ("Bearish" if pcr < 0.8 else "Neutral")

    return {
        "spot":     spot,
        "expiry":   nearest.strftime("%d %b %Y (%A)"),
        "atm":      atm,
        "pcr":      pcr,
        "bias":     bias,
        "max_pain": max_pain,
        "gap":      gap,
        "ce_wall":  ce_wall,
        "pe_wall":  pe_wall,
        "atm_ce":   atm_ce,
        "atm_pe":   atm_pe,
        "chain_df": cdf,
    }


# ── Screen runners ────────────────────────────────────────────────────────────

def run_fii_compass(api_key: str, access_token: str) -> dict:
    """
    Screen 1 — FII/DII flow trend + live NIFTY & BANKNIFTY option chains from Kite.
    """
    # NFO instruments (shared across both indices)
    nfo_df = fetch_nfo_instruments(api_key, access_token)

    # FII/DII flows
    fii_df    = pd.DataFrame()
    fii_error = None
    try:
        fii_df = fetch_fii_dii_flows(days=15)
    except Exception as e:
        fii_error = str(e)

    # Index option chains via Kite
    index_data: dict = {}
    for idx, sym, tick in [
        ("NIFTY",     "NSE:NIFTY 50",   50),
        ("BANKNIFTY", "NSE:NIFTY BANK", 100),
    ]:
        try:
            index_data[idx] = _index_chain(idx, sym, tick, api_key, access_token, nfo_df)
        except Exception as e:
            index_data[idx] = {"error": str(e)}

    # FII trend signal
    fii_5d  = float(fii_df["FII Net"].head(5).sum())  if not fii_df.empty else 0.0
    fii_10d = float(fii_df["FII Net"].head(10).sum()) if not fii_df.empty else 0.0
    dii_5d  = float(fii_df["DII Net"].head(5).sum())  if not fii_df.empty else 0.0

    if fii_5d > 3000:
        fii_signal = "🐂 Strong FII Buying"
        fii_bias   = "Bullish"
    elif fii_5d > 800:
        fii_signal = "📈 Moderate FII Buying"
        fii_bias   = "Bullish"
    elif fii_5d < -3000:
        fii_signal = "🐻 Strong FII Selling"
        fii_bias   = "Bearish"
    elif fii_5d < -800:
        fii_signal = "📉 Moderate FII Selling"
        fii_bias   = "Bearish"
    else:
        fii_signal = "⚖️ FII Neutral"
        fii_bias   = "Neutral"

    # Combined signal (FII + index PCR)
    nifty_bias = index_data.get("NIFTY", {}).get("bias", "Neutral")
    if fii_bias == "Bullish" and nifty_bias == "Bullish":
        combined = "💥 STRONG BULL — FII buying + PCR confirms"
        action   = "BUY NIFTY CE"
    elif fii_bias == "Bearish" and nifty_bias == "Bearish":
        combined = "💥 STRONG BEAR — FII selling + PCR confirms"
        action   = "BUY NIFTY PE"
    elif fii_bias == "Bullish":
        combined = "📈 BULL LEAN — FII buying, option chain mixed"
        action   = "Watch NIFTY CE"
    elif fii_bias == "Bearish":
        combined = "📉 BEAR LEAN — FII selling, option chain mixed"
        action   = "Watch NIFTY PE"
    else:
        combined = "⚖️ NEUTRAL — No clear directional edge"
        action   = "No Trade"

    return {
        "fii_df":     fii_df,
        "fii_error":  fii_error,
        "fii_5d":     fii_5d,
        "fii_10d":    fii_10d,
        "dii_5d":     dii_5d,
        "fii_signal": fii_signal,
        "fii_bias":   fii_bias,
        "combined":   combined,
        "action":     action,
        "index_data": index_data,
    }


def run_smart_money_options(
    api_key: str, access_token: str, days: int = 7
) -> dict:
    """
    Screen 2 — Insider buys (SEBI PIT) + bulk deals → F&O stock option chains from Kite.
    """
    # NFO instruments master (used for eligibility + option chains)
    nfo_df = fetch_nfo_instruments(api_key, access_token)
    fo_set = fo_eligible_set(nfo_df)

    # NSE data (best-effort)
    insider_df    = pd.DataFrame()
    insider_error = None
    bulk_df       = pd.DataFrame()

    try:
        insider_df = fetch_insider_trades(days=days)
    except Exception as e:
        insider_error = str(e)

    try:
        bulk_df = fetch_bulk_deals()
    except Exception:
        pass

    # Filter insider BUYS for F&O stocks
    fo_insider = pd.DataFrame()
    if not insider_df.empty:
        fo_insider = insider_df[insider_df["Symbol"].isin(fo_set)].copy()
        if not fo_insider.empty:
            fo_insider = (
                fo_insider.groupby("Symbol", as_index=False)
                .agg(
                    Company     =("Company",   "first"),
                    Transactions=("Symbol",    "count"),
                    Total_Shares=("Shares",    "sum"),
                    Pct_Change  =("% Change",  "sum"),
                    Latest_Date =("Date",      "max"),
                    Category    =("Category",  lambda x: ", ".join(x.unique()[:2])),
                )
                .sort_values("Total_Shares", ascending=False)
                .reset_index(drop=True)
            )

    # Bulk buy symbols in F&O
    bulk_buy_syms: set = set()
    if not bulk_df.empty:
        buys = bulk_df[bulk_df["Buy/Sell"].str.contains("BUY", case=False, na=False)]
        bulk_buy_syms = set(buys["Symbol"].tolist()) & fo_set

    # Union of top insider + bulk symbols
    top_syms = []
    if not fo_insider.empty:
        top_syms += fo_insider["Symbol"].tolist()[:8]
    for s in sorted(bulk_buy_syms):
        if s not in top_syms:
            top_syms.append(s)
    top_syms = top_syms[:10]

    # Spot prices
    spots = batch_spot_prices(top_syms, api_key, access_token) if top_syms else {}

    # Option chain for each
    results = []
    for sym in top_syms:
        spot = spots.get(sym, 0)
        if spot <= 0:
            continue
        m = stock_option_metrics(sym, spot, nfo_df, api_key, access_token)
        if m is None:
            continue

        ins_row  = (fo_insider[fo_insider["Symbol"] == sym].iloc[0]
                    if not fo_insider.empty and sym in fo_insider["Symbol"].values
                    else None)
        in_bulk  = sym in bulk_buy_syms

        signal = ("BUY CE" if m["bias"] == "Bullish" else
                  "BUY PE" if m["bias"] == "Bearish" else "WATCH")

        # Conviction score
        score = 0
        if ins_row is not None:
            score += min(int(ins_row["Transactions"]) * 10, 40)
            score += min(int(ins_row["Total_Shares"]) // 10_000, 20)
        if in_bulk:
            score += 20
        if m["bias"] == "Bullish" and signal == "BUY CE":
            score += 20
        elif m["bias"] == "Bearish" and signal == "BUY PE":
            score += 20

        results.append({
            "symbol":       sym,
            "company":      ins_row["Company"] if ins_row is not None else sym,
            "spot":         spot,
            "transactions": int(ins_row["Transactions"]) if ins_row is not None else 0,
            "total_shares": int(ins_row["Total_Shares"]) if ins_row is not None else 0,
            "category":     ins_row["Category"] if ins_row is not None else "Bulk Only",
            "in_bulk":      in_bulk,
            "signal":       signal,
            "score":        min(score, 100),
            "metrics":      m,
        })
        time.sleep(0.08)

    results.sort(key=lambda x: x["score"], reverse=True)

    return {
        "insider_df":    insider_df,
        "insider_error": insider_error,
        "fo_insider":    fo_insider,
        "bulk_buy_syms": bulk_buy_syms,
        "bulk_df":       bulk_df,
        "results":       results,
    }


def run_promoter_pulse(
    api_key: str, access_token: str, days: int = 30
) -> dict:
    """
    Screen 3 — Promoter-category accumulation (30-day window) + bulk deal cross-reference
    → F&O stock option chains from Kite.
    """
    nfo_df = fetch_nfo_instruments(api_key, access_token)
    fo_set = fo_eligible_set(nfo_df)

    insider_df    = pd.DataFrame()
    insider_error = None
    bulk_df       = pd.DataFrame()

    try:
        insider_df = fetch_insider_trades(days=days)
    except Exception as e:
        insider_error = str(e)

    try:
        bulk_df = fetch_bulk_deals()
    except Exception:
        pass

    # Filter PROMOTER category
    promo_df = pd.DataFrame()
    if not insider_df.empty:
        mask     = insider_df["Category"].str.upper().str.contains("PROMOT", na=False)
        promo_df = insider_df[mask & insider_df["Symbol"].isin(fo_set)].copy()

    bulk_buy_syms: set = set()
    if not bulk_df.empty:
        buys = bulk_df[bulk_df["Buy/Sell"].str.contains("BUY", case=False, na=False)]
        bulk_buy_syms = set(buys["Symbol"].tolist()) & fo_set

    # Aggregate per symbol
    scored = []
    if not promo_df.empty:
        grp = (
            promo_df.groupby("Symbol", as_index=False)
            .agg(
                Company     =("Company",   "first"),
                Txns        =("Symbol",    "count"),
                Total_Shares=("Shares",    "sum"),
                Pct_Gain    =("% Change",  "sum"),
                Latest_Date =("Date",      "max"),
            )
            .sort_values("Total_Shares", ascending=False)
        )
        for _, r in grp.iterrows():
            sym      = r["Symbol"]
            in_bulk  = sym in bulk_buy_syms
            score    = int(r["Txns"]) * 15 + int(r["Total_Shares"]) // 5_000 + (25 if in_bulk else 0)
            scored.append({
                "symbol":       sym,
                "company":      r["Company"],
                "txns":         int(r["Txns"]),
                "total_shares": int(r["Total_Shares"]),
                "pct_gain":     round(float(r["Pct_Gain"]), 4),
                "latest_date":  r["Latest_Date"],
                "in_bulk":      in_bulk,
                "score":        min(score, 100),
            })
        scored.sort(key=lambda x: x["score"], reverse=True)

    # Spot prices + option chains for top 8
    top_syms = [s["symbol"] for s in scored[:8]]
    spots    = batch_spot_prices(top_syms, api_key, access_token) if top_syms else {}

    results = []
    for s in scored[:8]:
        sym  = s["symbol"]
        spot = spots.get(sym, 0)
        if spot <= 0:
            continue
        m = stock_option_metrics(sym, spot, nfo_df, api_key, access_token)
        if m is None:
            continue

        signal     = ("BUY CE" if m["bias"] == "Bullish" else
                      "BUY PE" if m["bias"] == "Bearish" else "WATCH")
        conviction = ("⭐⭐⭐" if s["in_bulk"] and m["bias"] == "Bullish" else
                      "⭐⭐"  if s["in_bulk"] or  m["bias"] == "Bullish" else "⭐")

        results.append({**s, "spot": spot, "metrics": m,
                        "signal": signal, "conviction": conviction})
        time.sleep(0.08)

    return {
        "promo_df":      promo_df,
        "insider_error": insider_error,
        "bulk_buy_syms": bulk_buy_syms,
        "scored":        scored,
        "results":       results,
    }
