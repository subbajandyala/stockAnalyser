"""
Red Flag Radar — scans all F&O stocks for unusual / suspicious activity.

Flags (each scores 1 point; table sorted by flag count):
  VOL_SPIKE   — Intraday volume > 3× time-adjusted expected volume  (Kite)
  BIG_MOVE    — Price move > 3% vs previous close                   (Kite)
  OI_SPIKE    — ATM CE or PE OI > 100× lot size                     (Kite)
  PCR_LOW     — PCR < 0.5 — heavy call side (bearish sign)          (Kite)
  PCR_HIGH    — PCR > 2.5 — extreme put writing                     (Kite)
  HIGH_IV     — ATM straddle cost > 3% of spot                      (Kite)
  BULK_DEAL   — Appeared in NSE bulk deal data today                (NSE CSV)
"""

import datetime
import requests
import pandas as pd
from io import StringIO
from typing import Callable, Optional

_IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
_MARKET_OPEN_MINUTE = 9 * 60 + 15   # 9:15 IST in minutes since midnight
_MARKET_TOTAL_MIN   = 375            # 9:15 → 15:30

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


# ── Kite: intraday price + volume ────────────────────────────────────────────

def _elapsed_frac() -> float:
    """Fraction of the trading day elapsed (0.0–1.0). Used for volume normalisation."""
    now = datetime.datetime.now(_IST)
    now_min = now.hour * 60 + now.minute
    elapsed = max(1, now_min - _MARKET_OPEN_MINUTE)
    return min(elapsed / _MARKET_TOTAL_MIN, 1.0)


def _kite_equity_scan(api_key: str, access_token: str,
                      symbols: list[str], lot_map: dict) -> pd.DataFrame:
    """
    Live intraday price change % and volume spike via Kite /quote.

    VOL_SPIKE: today's volume > 3× the expected volume at this point in the
               trading day, where expected = lot_size × 1000 × elapsed_frac.
    BIG_MOVE:  abs(last_price - prev_close) / prev_close > 3%.
    """
    hdrs   = _hdrs(api_key, access_token)
    e_frac = _elapsed_frac()

    nse_keys = [f"NSE:{s}" for s in symbols]
    quotes: dict = {}
    for i in range(0, len(nse_keys), 400):
        try:
            r = requests.get(
                f"{_KITE_BASE}/quote", headers=hdrs,
                params={"i": nse_keys[i:i + 400]}, timeout=30,
            )
            if r.ok:
                quotes.update(r.json().get("data", {}))
        except Exception:
            continue

    rows = []
    for sym in symbols:
        q = quotes.get(f"NSE:{sym}", {})
        if not q:
            continue
        last       = float(q.get("last_price", 0))
        prev_close = float(q.get("ohlc", {}).get("close", 0))
        volume     = int(q.get("volume", 0))
        if last <= 0 or prev_close <= 0:
            continue

        chg_pct   = round((last - prev_close) / prev_close * 100, 2)
        lot        = lot_map.get(sym, 500)
        exp_vol    = max(lot * 1000 * e_frac, 1)
        vol_ratio  = round(volume / exp_vol, 2)

        rows.append({
            "symbol":     sym,
            "close":      round(last, 2),
            "chg_pct":    chg_pct,
            "vol_ratio":  vol_ratio,
            "_vol_spike": vol_ratio > 3.0,
            "_big_move":  abs(chg_pct) > 3.0,
        })
    return pd.DataFrame(rows)


# ── Kite: OI + IV + PCR ──────────────────────────────────────────────────────

def _kite_scan(api_key: str, access_token: str,
               symbols: list[str]) -> tuple[pd.DataFrame, dict]:
    """
    For each stock: fetch ATM CE+PE at nearest expiry.
    Returns (DataFrame with PCR/straddle/OI flags, lot_map dict).
    """
    hdrs = _hdrs(api_key, access_token)

    # Step 1 — batch spot prices from NSE (reuse full quote for equity scan)
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
            return pd.DataFrame(), {}
        instr = pd.read_csv(StringIO(r.text))
    except Exception:
        return pd.DataFrame(), {}

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
    return pd.DataFrame(rows), lot_map


# ── Master scan ───────────────────────────────────────────────────────────────

FLAG_DEFS = {
    "_vol_spike": ("VOL_SPIKE", "Vol > 3× exp",  "#f85149"),
    "_big_move":  ("BIG_MOVE",  "Move > 3%",     "#e6b800"),
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

    if not (api_key and access_token):
        return pd.DataFrame(columns=["symbol", "close", "chg_pct", "vol_ratio",
                                     "flags", "flag_count", "trade"])

    _p("Fetching Kite ATM OI + IV…", 15)
    kite_df, lot_map = _kite_scan(api_key, access_token, fo_symbols)

    _p(f"Live price & volume — {len(fo_symbols)} stocks via Kite…", 55)
    eq_df = _kite_equity_scan(api_key, access_token, fo_symbols, lot_map)

    _p("Checking NSE bulk deals…", 80)
    bulk = _fetch_bulk_deals()

    _p("Building red flag summary…", 93)
    if eq_df.empty:
        return pd.DataFrame()

    merged = eq_df.copy()
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

    def _trade_signal(row) -> str:
        """Derive CE / PE / WATCH from flag combination + price direction."""
        flags = set(row.get("flags", []))
        chg   = float(row.get("chg_pct", 0) or 0)

        bull = 0
        bear = 0

        # PCR — strongest directional signal
        if "PCR_HIGH" in flags:
            bull += 2   # heavy put writing = smart money bullish
        if "PCR_LOW" in flags:
            bear += 2   # heavy call side   = bearish positioning

        # Big price move — direction = price direction
        if "BIG_MOVE" in flags:
            if chg > 0:
                bull += 2
            else:
                bear += 2

        # Volume spike — weight by price direction
        if "VOL_SPIKE" in flags:
            if chg > 0:
                bull += 1
            else:
                bear += 1

        # Bulk deal → typically institutional accumulation
        if "BULK_DEAL" in flags:
            bull += 1

        # OI spike alone without PCR is ambiguous — neutral

        if bull == 0 and bear == 0:
            return "WATCH"
        diff = bull - bear
        if diff >= 3:
            return "STRONG CE"
        if diff >= 1:
            return "BUY CE"
        if diff <= -3:
            return "STRONG PE"
        if diff <= -1:
            return "BUY PE"
        return "WATCH"

    merged["trade"] = merged.apply(_trade_signal, axis=1)

    merged = merged.drop(
        columns=[c for c in FLAG_DEFS if c in merged.columns],
        errors="ignore",
    )

    return merged.sort_values(
        ["flag_count", "vol_ratio"], ascending=[False, False]
    ).reset_index(drop=True)
