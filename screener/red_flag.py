"""
Red Flag Radar — scans all F&O stocks for unusual / suspicious activity.

Flags (each scores 1 point; table sorted by flag count):
  VOL_SPIKE   — Volume > 3× 20-day average                    (yfinance)
  BIG_MOVE    — Price move > 5% intraday                       (yfinance)
  OI_SPIKE    — ATM CE or PE OI > 5× typical lot size          (Kite)
  PCR_LOW     — PCR < 0.5 — heavy call side (bearish sign)     (Kite)
  PCR_HIGH    — PCR > 2.5 — extreme put writing                (Kite)
  HIGH_IV     — ATM straddle cost > 3% of spot                 (Kite)
  BULK_DEAL   — Appeared in NSE bulk deal data today           (NSE CSV)
"""

import datetime
import requests
import pandas as pd
import yfinance as yf
from io import StringIO
from typing import Callable, Optional

_KITE_BASE = "https://api.kite.trade"
_NSE_BULK  = "https://archives.nseindia.com/content/equities/EQBULKDEAL.csv"

# Known F&O stocks — fallback if Kite download fails
_FO_FALLBACK = [
    "RELIANCE","TCS","INFY","HDFCBANK","ICICIBANK","HINDUNILVR","BHARTIARTL",
    "KOTAKBANK","ITC","SBIN","LT","AXISBANK","WIPRO","TITAN","NESTLEIND",
    "BAJFINANCE","MARUTI","HCLTECH","SUNPHARMA","ONGC","POWERGRID","NTPC",
    "COALINDIA","JSWSTEEL","TATAMOTORS","ADANIENT","ADANIPORTS","ULTRACEMCO",
    "GRASIM","ASIANPAINT","TECHM","HDFCLIFE","SBILIFE","BAJAJFINSV",
    "BAJAJ-AUTO","BPCL","CIPLA","DIVISLAB","DRREDDY","EICHERMOT","GAIL",
    "HEROMOTOCO","INDUSINDBK","M&M","BRITANNIA","HINDALCO","SHREECEM",
    "TATACONSUM","UPL","VEDL","ZOMATO","BANKBARODA","CANBK","PNB","UNIONBANK",
    "FEDERALBNK","BANDHANBNK","IDFCFIRSTB","RBLBANK","AUBANK","INDIGO",
    "TATASTEEL","HINDZINC","SAIL","NMDC","IOC","HINDPETRO","PETRONET",
    "BIOCON","AUROPHARMA","TORNTPHARM","MCDOWELL-N","JUBLFOOD","DLF",
    "GODREJPROP","OBEROIRLTY","SIEMENS","ABB","HAVELLS","VOLTAS","MPHASIS",
    "PERSISTENT","LTTS","COFORGE","KPITTECH","TATAPOWER","TORNTPOWER",
    "ADANIGREEN","CHOLAFIN","MUTHOOTFIN","MANAPPURAM","PFC","RECLTD","IRFC",
    "PIIND","CROMPTON","POLYCAB","KEI","PAGEIND","MARICO","DABUR","COLPAL",
    "LAURUSLABS","CONCOR","GICRE","ICICIGI","METROPOLIS","NYKAA","LTF",
    "SBICARD","GODREJCP","EMAMILTD","MRPL","GUJGASLTD","WHIRLPOOL","BLUESTAR",
    "THYROCARE","AMBUJACEM","ACC","DALBHARAT","AARTIIND","ATUL","DEEPAKNTR",
    "NAUKRI","POLICYBZR","FINOLEXCAB","ORIENTELEC","VGUARD","BLUEDART",
    "CHOLAHLDNG","ABBOTINDIA","ZYDUSWELL","LALPATHLAB","HINDPETRO","NMDC",
    "NMDC","MOTHERSON","BALKRISIND","APOLLOTYRE","MRF","CEAT","EXIDEIND",
    "AMARAJABAT","TVSMOTOR","BAJAJ-AUTO","ESCORTS","ASHOKLEY","BHEL",
    "BEL","HAL","COCHINSHIP","MAZAGON","GRINDWELL","CUMMINSIND","THERMAX",
    "AIAENG","KIRLOSKAR","KEC","KALPATPOWR","RPOWER","NHPC","SJVN",
    "GSPL","IGL","MGL","ATGL","GGAS","GUJGASLTD",
]

# NSE index symbols to exclude from equity scan
_INDEX_NAMES = {
    "NIFTY","BANKNIFTY","FINNIFTY","MIDCPNIFTY","NIFTYIT","SENSEX","BANKEX",
    "NIFTY50","NIFTYNXT50",
}


def _hdrs(api_key: str, access_token: str) -> dict:
    return {"X-Kite-Version": "3", "Authorization": f"token {api_key}:{access_token}"}


# ── Bulk deals ────────────────────────────────────────────────────────────────

def _fetch_bulk_deals() -> set[str]:
    """Return set of symbols that had bulk deals today (NSE CSV)."""
    try:
        r = requests.get(
            _NSE_BULK,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        if not r.ok:
            return set()
        df = pd.read_csv(StringIO(r.text))
        sym_col = next((c for c in df.columns if c.strip().upper() == "SYMBOL"), None)
        if not sym_col:
            return set()
        today_str = datetime.date.today().strftime("%d-%b-%Y").upper()
        date_col  = next((c for c in df.columns if "DATE" in c.upper()), None)
        if date_col:
            df = df[df[date_col].str.strip().str.upper() == today_str]
        return {s.strip() for s in df[sym_col].dropna().tolist()}
    except Exception:
        return set()


# ── yfinance: volume + price ──────────────────────────────────────────────────

def _yf_scan(symbols: list[str]) -> pd.DataFrame:
    """Batch yfinance download: volume spike + big price move."""
    tickers = [f"{s}.NS" for s in symbols]
    try:
        raw = yf.download(
            tickers, period="30d", interval="1d",
            auto_adjust=True, progress=False,
        )
    except Exception:
        return pd.DataFrame()

    is_multi = isinstance(raw.columns, pd.MultiIndex)
    close_df  = raw["Close"]  if is_multi else raw[["Close"]]
    volume_df = raw["Volume"] if is_multi else raw[["Volume"]]

    rows = []
    for sym, tick in zip(symbols, tickers):
        try:
            closes  = (close_df[tick]  if is_multi else close_df["Close"]).dropna()
            volumes = (volume_df[tick] if is_multi else volume_df["Volume"]).dropna()
            if len(closes) < 5:
                continue
            vol_t     = float(volumes.iloc[-1])
            vol_avg   = float(volumes.iloc[-21:-1].mean())
            vol_ratio = round(vol_t / vol_avg, 2) if vol_avg > 0 else 0.0
            close_t   = float(closes.iloc[-1])
            close_p   = float(closes.iloc[-2])
            price_chg = round((close_t - close_p) / close_p * 100, 2)
            rows.append({
                "symbol":     sym,
                "close":      round(close_t, 2),
                "chg_pct":    price_chg,
                "vol_ratio":  vol_ratio,
                "_vol_spike": vol_ratio > 3.0,
                "_big_move":  abs(price_chg) > 5.0,
            })
        except Exception:
            continue
    return pd.DataFrame(rows)


# ── Kite: OI + IV + PCR ──────────────────────────────────────────────────────

def _kite_scan(api_key: str, access_token: str, symbols: list[str]) -> pd.DataFrame:
    """
    For each stock: fetch ATM CE+PE at nearest expiry.
    Returns PCR, straddle%, OI spike flags.
    """
    hdrs = _hdrs(api_key, access_token)

    # Step 1 — batch spot prices from NSE
    spot_map: dict[str, float] = {}
    nse_keys = [f"NSE:{s}" for s in symbols]
    for i in range(0, len(nse_keys), 400):
        try:
            r = requests.get(
                f"{_KITE_BASE}/quote/ltp", headers=hdrs,
                params={"i": nse_keys[i:i + 400]}, timeout=30,
            )
            if r.ok:
                for k, v in r.json().get("data", {}).items():
                    spot_map[k.replace("NSE:", "")] = float(v.get("last_price", 0))
        except Exception:
            continue

    # Step 2 — NFO instruments
    try:
        r = requests.get(f"{_KITE_BASE}/instruments/NFO", headers=hdrs, timeout=60)
        if not r.ok:
            return pd.DataFrame()
        instr = pd.read_csv(StringIO(r.text))
    except Exception:
        return pd.DataFrame()

    instr["expiry_dt"] = pd.to_datetime(instr["expiry"], errors="coerce")
    instr["strike"]    = pd.to_numeric(instr["strike"], errors="coerce")
    today = datetime.date.today()
    opts  = instr[
        instr["instrument_type"].isin(["CE", "PE"]) &
        (instr["expiry_dt"].dt.date >= today)
    ].copy()

    # Lot sizes (for OI spike relative check)
    lot_map: dict[str, int] = {}
    if "lot_size" in instr.columns:
        for name, grp in instr.groupby("name"):
            lot_map[str(name)] = int(grp["lot_size"].iloc[0])

    # Step 3 — find ATM CE+PE symbols
    atm_list: list[str] = []
    atm_meta: dict[str, tuple] = {}
    for sym in symbols:
        spot = spot_map.get(sym, 0)
        if spot <= 0:
            continue
        sub = opts[opts["name"] == sym]
        if sub.empty:
            continue
        near_exp = sub["expiry_dt"].min()
        near     = sub[sub["expiry_dt"] == near_exp]
        strikes  = near["strike"].dropna().unique()
        if not len(strikes):
            continue
        atm = min(strikes, key=lambda s: abs(s - spot))
        for _, row in near[near["strike"] == atm].iterrows():
            ts = f"NFO:{row['tradingsymbol']}"
            atm_list.append(ts)
            atm_meta[ts] = (sym, row["instrument_type"])

    # Step 4 — batch quotes
    quotes: dict = {}
    for i in range(0, len(atm_list), 400):
        try:
            r = requests.get(
                f"{_KITE_BASE}/quote", headers=hdrs,
                params={"i": atm_list[i:i + 400]}, timeout=30,
            )
            if r.ok:
                quotes.update(r.json().get("data", {}))
        except Exception:
            continue

    # Step 5 — aggregate per stock
    agg: dict[str, dict] = {}
    for ts, q in quotes.items():
        sym, itype = atm_meta.get(ts, (None, None))
        if not sym:
            continue
        if sym not in agg:
            agg[sym] = {"ce_oi": 0, "pe_oi": 0, "ce_ltp": 0.0, "pe_ltp": 0.0}
        if itype == "CE":
            agg[sym]["ce_oi"]  = int(q.get("oi", 0))
            agg[sym]["ce_ltp"] = float(q.get("last_price", 0))
        else:
            agg[sym]["pe_oi"]  = int(q.get("oi", 0))
            agg[sym]["pe_ltp"] = float(q.get("last_price", 0))

    rows = []
    for sym, d in agg.items():
        spot     = spot_map.get(sym, 0)
        ce_oi    = d["ce_oi"]
        pe_oi    = d["pe_oi"]
        lot      = lot_map.get(sym, 1)
        pcr      = round(pe_oi / ce_oi, 2) if ce_oi > 0 else 0.0
        straddle = round((d["ce_ltp"] + d["pe_ltp"]) / spot * 100, 2) if spot > 0 else 0.0
        # OI spike: either leg > 5× lot size threshold of 100 lots
        oi_spike = (ce_oi > lot * 100) or (pe_oi > lot * 100)
        rows.append({
            "symbol":       sym,
            "ce_oi":        ce_oi,
            "pe_oi":        pe_oi,
            "pcr":          pcr,
            "straddle_pct": straddle,
            "_oi_spike":    oi_spike,
            "_pcr_low":     0 < pcr < 0.5,
            "_pcr_high":    pcr > 2.5,
            "_high_iv":     straddle > 3.0,
        })
    return pd.DataFrame(rows)


# ── Master scan ───────────────────────────────────────────────────────────────

FLAG_DEFS = {
    "_vol_spike": ("VOL_SPIKE", "Vol > 3×",   "#f85149"),
    "_big_move":  ("BIG_MOVE",  "Move > 5%",  "#e6b800"),
    "_oi_spike":  ("OI_SPIKE",  "OI Spike",   "#d2a8ff"),
    "_pcr_low":   ("PCR_LOW",   "PCR < 0.5",  "#ffa657"),
    "_pcr_high":  ("PCR_HIGH",  "PCR > 2.5",  "#79c0ff"),
    "_high_iv":   ("HIGH_IV",   "High IV",    "#ff7b72"),
    "bulk_deal":  ("BULK_DEAL", "Bulk Deal",  "#56d364"),
}


def run_red_flag_scan(
    api_key: str = "",
    access_token: str = "",
    progress_cb: Optional[Callable[[str, int], None]] = None,
) -> pd.DataFrame:
    """
    Full scan across all F&O stocks.
    Returns DataFrame sorted by flag_count desc.
    progress_cb(message, pct_int) for UI feedback.
    """
    def _p(msg: str, pct: int):
        if progress_cb:
            progress_cb(msg, pct)

    _p("Fetching F&O stock list…", 5)

    fo_symbols = list(_FO_FALLBACK)
    if api_key and access_token:
        try:
            hdrs = _hdrs(api_key, access_token)
            r = requests.get(f"{_KITE_BASE}/instruments/NFO", headers=hdrs, timeout=60)
            if r.ok:
                instr = pd.read_csv(StringIO(r.text))
                fo_symbols = [
                    s for s in
                    instr[instr["instrument_type"].isin(["CE", "PE"])]["name"]
                    .dropna().unique().tolist()
                    if s not in _INDEX_NAMES
                ]
        except Exception:
            pass

    _p(f"Price & volume scan — {len(fo_symbols)} stocks…", 10)
    yf_df = _yf_scan(fo_symbols)

    _p("Checking NSE bulk deals…", 58)
    bulk = _fetch_bulk_deals()

    kite_df = pd.DataFrame()
    if api_key and access_token:
        _p("Fetching Kite ATM OI + IV…", 65)
        kite_df = _kite_scan(api_key, access_token, fo_symbols)

    _p("Building red flag summary…", 93)
    if yf_df.empty:
        return pd.DataFrame()

    merged = yf_df.copy()
    if not kite_df.empty:
        merged = merged.merge(kite_df, on="symbol", how="left")

    merged["bulk_deal"] = merged["symbol"].isin(bulk)

    def _build_flags(row) -> list[str]:
        return [
            label
            for col, (label, _, _color) in FLAG_DEFS.items()
            if col in row.index and bool(row.get(col, False))
        ]

    merged["flags"]      = merged.apply(_build_flags, axis=1)
    merged["flag_count"] = merged["flags"].apply(len)

    merged = merged.drop(
        columns=[c for c in FLAG_DEFS if c in merged.columns],
        errors="ignore",
    )

    return merged.sort_values(
        ["flag_count", "vol_ratio"], ascending=[False, False]
    ).reset_index(drop=True)
