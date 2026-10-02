"""
CPR Crossover Scanner — F&O stocks, signal-only, ntfy notification.

Scans ~200 F&O stocks every 15 minutes for FRESH CPR crossovers:
  CE signal : spot just crossed ABOVE TC  (bullish breakout)
  PE signal : spot just crossed BELOW BC  (bearish breakdown)

Narrow CPR (width < 0.30% of PP) = trending day = higher confidence.

Notifications go to CPR_NTFY_TOPIC env var or 'cpr_ntfy_topic' in
~/.kite_creds.json.  No auto-execution — signals only.

Usage:
    python scripts/cpr_scan_bot.py --once    # single scan (cron / GitHub Actions)
    python scripts/cpr_scan_bot.py --loop    # continuous loop every 15 min

Requires KITE_API_KEY and KITE_ACCESS_TOKEN (or ~/.kite_creds.json).
"""

import argparse
import datetime
import json
import logging
import os
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import requests
from kiteconnect import KiteConnect

from screener.cpr import compute_cpr

_LOG = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)

_IST          = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
MARKET_OPEN   = datetime.time(9, 15)
MARKET_CLOSE  = datetime.time(15, 30)
SCAN_INTERVAL = 15 * 60   # seconds

# F&O stocks to scan (reuse from red_flag fallback list)
_FO_STOCKS = [
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
    "LAURUSLABS","CONCOR","GICRE","ICICIGI","NYKAA","LTF","SBICARD",
    "GODREJCP","EMAMILTD","GUJGASLTD","AMBUJACEM","ACC","DALBHARAT",
    "AARTIIND","DEEPAKNTR","NAUKRI","POLICYBZR","ABBOTINDIA","LALPATHLAB",
    "MOTHERSON","BALKRISIND","APOLLOTYRE","MRF","CEAT","EXIDEIND","TVSMOTOR",
    "ESCORTS","ASHOKLEY","BHEL","BEL","HAL","GRINDWELL","CUMMINSIND",
    "THERMAX","AIAENG","KEC","KALPATPOWR","NHPC","SJVN","IGL","MGL","ATGL",
]


# ── Credentials ───────────────────────────────────────────────────────────────

def _kite_creds() -> tuple[str, str]:
    api_key = os.environ.get("KITE_API_KEY", "")
    token   = os.environ.get("KITE_ACCESS_TOKEN", "")
    if not (api_key and token):
        p = pathlib.Path.home() / ".kite_creds.json"
        if p.exists():
            try:
                c = json.loads(p.read_text())
                api_key = api_key or c.get("api_key", "")
                token   = token   or c.get("access_token", "")
            except Exception:
                pass
    return api_key, token


def _ntfy_topic() -> str:
    t = os.environ.get("CPR_NTFY_TOPIC", "")
    if not t:
        p = pathlib.Path.home() / ".kite_creds.json"
        if p.exists():
            try:
                t = json.loads(p.read_text()).get("cpr_ntfy_topic", "")
            except Exception:
                pass
    return t


# ── State ─────────────────────────────────────────────────────────────────────

def _state_path(date_str: str) -> pathlib.Path:
    return pathlib.Path(f"/tmp/cpr_scan_state_{date_str}.json")


def _load_state(date_str: str) -> dict:
    p = _state_path(date_str)
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except Exception:
        return {}


def _save_state(date_str: str, state: dict) -> None:
    _state_path(date_str).write_text(json.dumps(state, indent=2))


# ── Kite helpers ──────────────────────────────────────────────────────────────

def _build_token_map(kite: KiteConnect) -> dict[str, int]:
    """Build symbol → instrument_token map for NSE EQ instruments."""
    try:
        instruments = kite.instruments("NSE")
    except Exception as e:
        _LOG.error(f"instruments(NSE) failed: {e}")
        return {}
    token_map = {}
    for inst in instruments:
        if inst.get("instrument_type") == "EQ":
            sym = inst.get("tradingsymbol", "")
            tok = inst.get("instrument_token")
            if sym and tok:
                token_map[sym] = int(tok)
    _LOG.info(f"Token map built: {len(token_map)} NSE EQ instruments")
    return token_map


def _fetch_cpr_for_symbol(kite: KiteConnect, token: int, symbol: str) -> dict | None:
    """Fetch yesterday's OHLC from Kite and compute CPR."""
    today     = datetime.date.today()
    from_date = today - datetime.timedelta(days=7)
    to_date   = today - datetime.timedelta(days=1)
    try:
        candles = kite.historical_data(token, from_date, to_date, "day")
        if not candles:
            return None
        last = candles[-1]
        cpr  = compute_cpr(float(last["high"]), float(last["low"]), float(last["close"]))
        cpr["ohlc_date"] = str(last["date"])[:10]
        return cpr
    except Exception as e:
        _LOG.debug(f"historical_data failed for {symbol} (token {token}): {e}")
        return None


def _batch_ltp(kite: KiteConnect, symbols: list[str]) -> dict[str, float]:
    """Fetch LTP for all symbols in batches of 500. Returns {symbol: ltp}."""
    result = {}
    batch_size = 500
    keys = [f"NSE:{s}" for s in symbols]
    for i in range(0, len(keys), batch_size):
        batch = keys[i : i + batch_size]
        try:
            data = kite.ltp(batch)
            for key, val in data.items():
                sym = key.split(":")[1]
                result[sym] = float(val.get("last_price", 0))
        except Exception as e:
            _LOG.warning(f"LTP batch {i//batch_size + 1} failed: {e}")
        time.sleep(0.3)
    return result


# ── Notification ──────────────────────────────────────────────────────────────

def _send_ntfy(title: str, body: str, topic: str) -> bool:
    if not topic:
        _LOG.warning("CPR ntfy topic not set — skipping notification")
        return False
    try:
        r = requests.post(
            f"https://ntfy.sh/{topic}",
            data=body.encode("utf-8"),
            headers={
                "Title":        title.encode("utf-8"),
                "Priority":     "high",
                "Tags":         "chart_with_upwards_trend",
                "Content-Type": "text/plain; charset=utf-8",
            },
            timeout=10,
        )
        return r.ok
    except Exception as e:
        _LOG.warning(f"ntfy send failed: {e}")
        return False


# ── Single scan ───────────────────────────────────────────────────────────────

def scan_once(state: dict, token_map: dict, cpr_cache: dict,
              kite: KiteConnect, ntfy_topic: str) -> None:
    now_ist   = datetime.datetime.now(_IST)
    date_str  = now_ist.strftime("%Y-%m-%d")
    time_str  = now_ist.strftime("%H:%M")

    if not (MARKET_OPEN <= now_ist.time() <= MARKET_CLOSE):
        _LOG.info("Market closed — skipping scan")
        return

    # Build CPR cache once per day for symbols that don't have it yet
    missing = [s for s in _FO_STOCKS if s not in cpr_cache]
    if missing:
        _LOG.info(f"Fetching CPR for {len(missing)} symbols...")
        fetched = 0
        for sym in missing:
            tok = token_map.get(sym)
            if not tok:
                continue
            cpr = _fetch_cpr_for_symbol(kite, tok, sym)
            if cpr:
                cpr_cache[sym] = cpr
                fetched += 1
            time.sleep(0.1)   # avoid rate limit
        _LOG.info(f"CPR cache ready: {fetched}/{len(missing)} fetched")

    # Fetch current LTP for all stocks
    scannable = [s for s in _FO_STOCKS if s in cpr_cache]
    if not scannable:
        _LOG.warning("No CPR data available — cannot scan")
        return

    _LOG.info(f"Fetching LTP for {len(scannable)} symbols...")
    ltp_map = _batch_ltp(kite, scannable)

    ce_signals = []
    pe_signals = []

    for sym in scannable:
        ltp = ltp_map.get(sym, 0)
        if ltp <= 0:
            continue
        cpr = cpr_cache[sym]
        tc  = cpr["tc"]
        bc  = cpr["bc"]
        pp  = cpr["pp"]

        # Determine current position relative to CPR
        if ltp > tc:
            position = "above_tc"
        elif ltp < bc:
            position = "below_bc"
        else:
            position = "inside"

        prev_position = state.get(sym, {}).get("position")

        # Detect FRESH crossover only
        is_ce = (prev_position in ("inside", "below_bc")) and position == "above_tc"
        is_pe = (prev_position in ("inside", "above_tc")) and position == "below_bc"

        # Update state
        state[sym] = {"position": position, "ltp": ltp}

        if is_ce:
            ce_signals.append({
                "symbol":   sym,
                "ltp":      ltp,
                "tc":       tc,
                "pp":       pp,
                "narrow":   cpr.get("is_narrow", False),
                "width":    cpr.get("width_pct", 0),
            })
        elif is_pe:
            pe_signals.append({
                "symbol":   sym,
                "ltp":      ltp,
                "bc":       bc,
                "pp":       pp,
                "narrow":   cpr.get("is_narrow", False),
                "width":    cpr.get("width_pct", 0),
            })

    _LOG.info(
        f"Scan done — CE crossovers: {len(ce_signals)}  PE crossovers: {len(pe_signals)}"
    )

    if not ce_signals and not pe_signals:
        return

    # Build notification
    lines = [f"CPR Crossover Scan  {time_str} IST"]
    lines.append(f"Fresh signals: {len(ce_signals) + len(pe_signals)}")
    lines.append("")

    if ce_signals:
        lines.append(f"BUY CE ({len(ce_signals)} stocks):")
        for s in sorted(ce_signals, key=lambda x: x["narrow"], reverse=True):
            tag = " [NARROW]" if s["narrow"] else ""
            lines.append(
                f"  {s['symbol']:15s}  spot {s['ltp']:.1f}  TC {s['tc']:.1f}{tag}"
            )

    if pe_signals:
        if ce_signals:
            lines.append("")
        lines.append(f"BUY PE ({len(pe_signals)} stocks):")
        for s in sorted(pe_signals, key=lambda x: x["narrow"], reverse=True):
            tag = " [NARROW]" if s["narrow"] else ""
            lines.append(
                f"  {s['symbol']:15s}  spot {s['ltp']:.1f}  BC {s['bc']:.1f}{tag}"
            )

    lines.append("")
    lines.append("NARROW = trending day, higher confidence.")
    lines.append("Verify before executing. Not financial advice.")

    message = "\n".join(lines)
    title   = f"CPR: {len(ce_signals)} CE | {len(pe_signals)} PE crossovers"

    print(f"\n{'='*55}\n{message}\n{'='*55}\n")

    ok = _send_ntfy(title, message, ntfy_topic)
    _LOG.info(f"ntfy: {'OK' if ok else 'FAILED'}")

    _save_state(date_str, state)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="CPR crossover scanner for F&O stocks")
    ap.add_argument("--once", action="store_true", help="Single scan then exit")
    ap.add_argument("--loop", action="store_true", help="Continuous 15-min loop")
    ap.add_argument("--test-notify", action="store_true", help="Send test notification and exit")
    args = ap.parse_args()

    api_key, access_token = _kite_creds()
    ntfy_topic = _ntfy_topic()

    if not (api_key and access_token):
        print("ERROR: KITE_API_KEY and KITE_ACCESS_TOKEN are required.")
        sys.exit(1)

    if args.test_notify:
        now  = datetime.datetime.now(_IST).strftime("%H:%M IST")
        title = "CPR Scanner - Test Notification"
        body  = (
            f"CPR Scanner is working!\n"
            f"Time: {now}\n\n"
            f"You will receive alerts for fresh CPR crossovers in F&O stocks "
            f"every 15 minutes during market hours (09:15–15:30 IST).\n"
            f"NARROW CPR = trending day = higher confidence signals."
        )
        print(body)
        ok = _send_ntfy(title, body, ntfy_topic)
        print(f"ntfy: {'OK' if ok else 'FAILED (check CPR_NTFY_TOPIC or cpr_ntfy_topic in creds)'}")
        return

    if not (args.once or args.loop):
        print("Use --once for a single scan or --loop for continuous mode.")
        sys.exit(1)

    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)

    # Build token map once at startup
    _LOG.info("Building NSE instrument token map...")
    token_map = _build_token_map(kite)
    if not token_map:
        print("ERROR: Could not fetch NSE instruments from Kite.")
        sys.exit(1)

    today_str = datetime.datetime.now(_IST).strftime("%Y-%m-%d")
    state     = _load_state(today_str)
    cpr_cache = {}   # {symbol: cpr_dict} — rebuilt fresh each session / day

    if args.once:
        scan_once(state, token_map, cpr_cache, kite, ntfy_topic)
        return

    # Loop mode
    _LOG.info("CPR scanner started — loop every 15 min during market hours")
    while True:
        now_ist = datetime.datetime.now(_IST)
        today   = now_ist.strftime("%Y-%m-%d")

        # Reset state at day boundary
        if today != today_str:
            today_str = today
            state     = {}
            cpr_cache = {}
            _LOG.info("New trading day — state and CPR cache reset")

        scan_once(state, token_map, cpr_cache, kite, ntfy_topic)
        _LOG.info(f"Next scan in {SCAN_INTERVAL // 60} minutes...")
        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
