"""
Trending OI tracker — replicates NiftyTrader's Trending OI Data table.

Polls Kite Connect REST API for OI + Volume on selected strikes at a
configurable interval (1 / 3 / 5 / 15 min).  OI delta is computed manually
by diffing against a day-start baseline snapshot taken on initialisation.

Column definitions
------------------
CALLS CHNG OI : cumulative CE OI change from day-start across selected strikes
PUTS  CHNG OI : cumulative PE OI change from day-start across selected strikes
DIFF. IN OI   : PUTS CHNG OI - CALLS CHNG OI
DIFF %        : DIFF IN OI / (CALLS CHNG OI + PUTS CHNG OI) * 100
DIR OF CHNG   : ▲ if DIFF IN OI ≥ prev row, ▼ otherwise
CHNG IN DIR   : DIFF IN OI(current) - DIFF IN OI(previous)
PCR           : total PE OI / total CE OI  (absolute, not delta)
COI PCR       : PUTS CHNG OI / CALLS CHNG OI
VOL PCR       : total PE Volume / total CE Volume
SENTIMENT     : Bullish if COI PCR > 1 or rising, Bearish if < 1 or falling
"""

import datetime
import os
from io import StringIO
from typing import Optional

import pandas as pd
import requests

_KITE_BASE = "https://api.kite.trade"
_IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

SYMBOLS = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX",
           "CRUDEOIL", "GOLD", "SILVER"]

_STRIKE_STEP: dict[str, int] = {
    "NIFTY": 50, "BANKNIFTY": 100, "FINNIFTY": 50,
    "MIDCPNIFTY": 25, "SENSEX": 100, "BANKEX": 100,
    "CRUDEOIL": 50, "GOLD": 100, "SILVER": 500,
}
_EXCHANGE: dict[str, str] = {
    "NIFTY": "NFO", "BANKNIFTY": "NFO", "FINNIFTY": "NFO",
    "MIDCPNIFTY": "NFO", "SENSEX": "BFO", "BANKEX": "BFO",
    "CRUDEOIL": "MCX", "GOLD": "MCX", "SILVER": "MCX",
}
_SPOT_QUOTE: dict[str, str] = {
    "NIFTY":      "NSE:NIFTY 50",
    "BANKNIFTY":  "NSE:NIFTY BANK",
    "FINNIFTY":   "NSE:NIFTY FIN SERVICE",
    "MIDCPNIFTY": "NSE:NIFTY MID SELECT",
    "SENSEX":     "BSE:SENSEX",
    "BANKEX":     "BSE:BANKEX",
    # CRUDEOIL spot is fetched dynamically via near-month futures in gamma_blast
}

INTERVALS = {"1 Min": 60, "3 Min": 180, "5 Min": 300, "15 Min": 900}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _hdrs(api_key: str, access_token: str) -> dict:
    return {"X-Kite-Version": "3", "Authorization": f"token {api_key}:{access_token}"}


def ind_fmt(n: int) -> str:
    """Indian-style number format: 1,57,59,965"""
    neg = n < 0
    s = str(abs(int(n)))
    if len(s) <= 3:
        return ("-" if neg else "") + s
    result, s = s[-3:], s[:-3]
    while len(s) > 2:
        result, s = s[-2:] + "," + result, s[:-2]
    return ("-" if neg else "") + s + "," + result


# ── Instrument helpers ────────────────────────────────────────────────────────

def fetch_instruments(api_key: str, access_token: str, symbol: str) -> pd.DataFrame:
    """
    Download options instruments for symbol from Kite.
    Returns DataFrame filtered to CE/PE for that symbol only.
    For MCX symbols (e.g. CRUDEOIL), also includes FUT rows so callers can
    derive the near-month futures symbol for spot-price lookup.
    Caller should cache this in session_state (large download, daily stable).
    """
    exch = _EXCHANGE[symbol]
    resp = requests.get(
        f"{_KITE_BASE}/instruments/{exch}",
        headers=_hdrs(api_key, access_token),
        timeout=30,
    )
    if resp.status_code in (401, 403):
        raise RuntimeError(
            "Kite Access Token expired or invalid — regenerate and re-enter in sidebar."
        )
    resp.raise_for_status()
    df = pd.read_csv(StringIO(resp.text))
    df["expiry_dt"] = pd.to_datetime(df["expiry"], errors="coerce")
    df["strike"]    = pd.to_numeric(df["strike"], errors="coerce")
    # MCX: include FUT so fetch_chain_snapshot can find near-month spot symbol
    types = ["CE", "PE", "FUT"] if exch == "MCX" else ["CE", "PE"]
    return df[
        (df["name"] == symbol) &
        (df["instrument_type"].isin(types))
    ].copy()


def get_expiries(instr_df: pd.DataFrame) -> list[str]:
    """Return sorted upcoming expiry date strings (YYYY-MM-DD)."""
    today = datetime.date.today()
    dates = (instr_df["expiry_dt"]
             .dropna()
             .dt.date
             .unique())
    return sorted(str(d) for d in dates if d >= today)


def get_nearest_expiry(instr_df: pd.DataFrame) -> str:
    today = datetime.date.today()
    future = instr_df[instr_df["expiry_dt"].dt.date >= today]
    if future.empty:
        raise RuntimeError("No upcoming expiry found in instruments.")
    return future["expiry_dt"].min().strftime("%Y-%m-%d")


def get_spot(api_key: str, access_token: str, symbol: str) -> float:
    """Fetch current spot/index price."""
    q    = _SPOT_QUOTE[symbol]
    resp = requests.get(
        f"{_KITE_BASE}/quote/ltp",
        headers=_hdrs(api_key, access_token),
        params={"i": q},
        timeout=10,
    )
    resp.raise_for_status()
    return float(resp.json()["data"][q]["last_price"])


def get_atm_strikes(spot: float, symbol: str, n: int = 5) -> list[int]:
    """Return ATM ± n strikes list."""
    step = _STRIKE_STEP.get(symbol, 50)
    atm  = round(spot / step) * step
    return [atm + i * step for i in range(-n, n + 1)]


# ── Snapshot fetching ─────────────────────────────────────────────────────────

def fetch_snapshot(
    api_key: str,
    access_token: str,
    symbol: str,
    expiry: str,           # "YYYY-MM-DD"
    strikes: list[int],
    instr_df: pd.DataFrame,
) -> dict:
    """
    Fetch OI + volume for the given strikes and expiry.

    Returns
    -------
    dict with keys:
      ts, spot,
      per_strike  : {strike: {ce_oi, pe_oi, ce_vol, pe_vol}},
      total_ce_oi, total_pe_oi, total_ce_vol, total_pe_vol
    """
    exch = _EXCHANGE[symbol]
    hdrs = _hdrs(api_key, access_token)

    spot = get_spot(api_key, access_token, symbol)

    target = pd.to_datetime(expiry)
    sub = instr_df[
        (instr_df["expiry_dt"] == target) &
        (instr_df["strike"].isin([float(s) for s in strikes]))
    ]
    if sub.empty:
        raise RuntimeError(
            f"No {symbol} options found for expiry {expiry}, "
            f"strikes {strikes[:3]}… — try re-initialising."
        )

    syms = (exch + ":" + sub["tradingsymbol"]).tolist()

    quotes: dict = {}
    for i in range(0, len(syms), 400):
        r = requests.get(
            f"{_KITE_BASE}/quote",
            headers=hdrs,
            params={"i": syms[i: i + 400]},
            timeout=30,
        )
        if r.ok:
            quotes.update(r.json().get("data", {}))

    per_strike: dict = {}
    for _, row in sub.iterrows():
        strike = int(float(row["strike"]))
        itype  = row["instrument_type"].lower()
        key    = f"{exch}:{row['tradingsymbol']}"
        q      = quotes.get(key, {})
        if strike not in per_strike:
            per_strike[strike] = {"ce_oi": 0, "pe_oi": 0, "ce_vol": 0, "pe_vol": 0}
        per_strike[strike][f"{itype}_oi"]  = int(q.get("oi", 0))
        per_strike[strike][f"{itype}_vol"] = int(q.get("volume", 0))

    return {
        "ts":           datetime.datetime.now(_IST),
        "spot":         spot,
        "per_strike":   per_strike,
        "total_ce_oi":  sum(v["ce_oi"]  for v in per_strike.values()),
        "total_pe_oi":  sum(v["pe_oi"]  for v in per_strike.values()),
        "total_ce_vol": sum(v["ce_vol"] for v in per_strike.values()),
        "total_pe_vol": sum(v["pe_vol"] for v in per_strike.values()),
    }


# ── Move Verdict ─────────────────────────────────────────────────────────────

def classify_move(curr_snapshot: dict, prev_row: dict) -> tuple:
    """
    Compare curr_snapshot OI against the raw OI stored in prev_row to produce
    a GOOD / FAKE verdict.

    GOOD  — OI delta direction confirms price delta direction (institutional conviction)
    FAKE  — OI diverges from price, or both legs are unwinding (short-covering trap)

    Returns (verdict: str, reason: str)
    """
    price_delta = curr_snapshot["spot"] - prev_row.get("_raw_spot", curr_snapshot["spot"])
    call_chg    = curr_snapshot["total_ce_oi"] - prev_row.get("_raw_ce_oi", curr_snapshot["total_ce_oi"])
    put_chg     = curr_snapshot["total_pe_oi"] - prev_row.get("_raw_pe_oi", curr_snapshot["total_pe_oi"])
    # diffChg > 0 → puts increasing relative to calls (put writers supporting rally)
    diff_chg    = put_chg - call_chg

    # Mutual unwind: both OI legs shrinking → short-covering, no new conviction
    if call_chg < 0 and put_chg < 0:
        return "FAKE", "Mutual unwind — both legs unwinding (short-covering, low conviction)"

    both_building = call_chg > 0 and put_chg > 0

    if price_delta == 0:
        if both_building:
            return "GOOD", "Fresh buildup both legs — range-bound accumulation"
        return "FAKE", "No price movement to confirm OI flow"

    price_up    = price_delta > 0
    oi_confirms = (price_up and diff_chg >= 0) or (not price_up and diff_chg <= 0)

    if oi_confirms:
        reason = (
            "Fresh buildup both legs — confirmed direction"
            if both_building
            else "OI flow confirms price direction"
        )
        return "GOOD", reason
    else:
        return "FAKE", "OI flow diverges from price — possible trap / stop-hunt"


# ── Row computation ───────────────────────────────────────────────────────────

def compute_row(
    snapshot: dict,
    day_start: dict,
    prev_row: Optional[dict] = None,
) -> dict:
    """Compute one Trending OI table row from current + baseline snapshots."""
    ce_oi  = snapshot["total_ce_oi"]
    pe_oi  = snapshot["total_pe_oi"]
    ce_vol = snapshot["total_ce_vol"]
    pe_vol = snapshot["total_pe_vol"]

    ds_ce = day_start["total_ce_oi"]
    ds_pe = day_start["total_pe_oi"]

    ce_chng = ce_oi - ds_ce
    pe_chng = pe_oi - ds_pe

    diff_oi    = pe_chng - ce_chng
    total_chng = ce_chng + pe_chng
    diff_pct   = round(diff_oi / total_chng * 100, 1) if total_chng else 0.0

    pcr     = round(pe_oi   / ce_oi,   3) if ce_oi   else 0.0
    coi_pcr = round(pe_chng / ce_chng, 3) if ce_chng else 0.0
    vol_pcr = round(pe_vol  / ce_vol,  3) if ce_vol  else 0.0

    if prev_row is not None:
        chng_in_dir = diff_oi - prev_row["diff_oi"]
        dir_chng    = "▲" if chng_in_dir >= 0 else "▼"
    else:
        chng_in_dir = 0
        dir_chng    = "—"

    # Sentiment: COI PCR value + trend
    prev_coi = prev_row["coi_pcr"] if prev_row else None
    if coi_pcr >= 1.2:
        sentiment = "Bullish"
    elif coi_pcr <= 0.8:
        sentiment = "Bearish"
    elif prev_coi is not None and coi_pcr > prev_coi:
        sentiment = "Bullish"
    elif prev_coi is not None and coi_pcr < prev_coi:
        sentiment = "Bearish"
    else:
        sentiment = "Neutral"

    # Per-strike OI delta vs previous snapshot (for alert checking)
    ps      = snapshot.get("per_strike", {})
    prev_ps = prev_row.get("per_strike", {}) if prev_row else {}
    strike_deltas = {
        s: {
            "ce_delta": ps[s]["ce_oi"] - prev_ps.get(s, ps[s])["ce_oi"],
            "pe_delta": ps[s]["pe_oi"] - prev_ps.get(s, ps[s])["pe_oi"],
        }
        for s in ps
    }

    # Move Verdict — only meaningful when we have a prior snapshot to diff against
    if prev_row is not None:
        verdict, verdict_reason = classify_move(snapshot, prev_row)
    else:
        verdict, verdict_reason = "—", "First snapshot — no prior row to compare"

    return {
        "time":           snapshot["ts"].strftime("%H:%M"),
        "spot":           round(snapshot["spot"], 2),
        "ce_chng_oi":     ce_chng,
        "pe_chng_oi":     pe_chng,
        "diff_oi":        diff_oi,
        "diff_pct":       diff_pct,
        "dir_chng":       dir_chng,
        "chng_in_dir":    chng_in_dir,
        "pcr":            pcr,
        "coi_pcr":        coi_pcr,
        "vol_pcr":        vol_pcr,
        "sentiment":      sentiment,
        "verdict":        verdict,
        "verdict_reason": verdict_reason,
        "per_strike":     ps,
        "strike_deltas":  strike_deltas,
        # Raw snapshot OI stored so the next row's classify_move can diff against them
        "_raw_ce_oi":     ce_oi,
        "_raw_pe_oi":     pe_oi,
        "_raw_spot":      round(snapshot["spot"], 2),
    }


# ── Alerting ──────────────────────────────────────────────────────────────────

def check_alerts(row: dict, threshold: int) -> list[str]:
    """Return list of alert strings if any OI delta exceeds threshold."""
    alerts = []
    if abs(row["diff_oi"]) >= threshold:
        sign = "+" if row["diff_oi"] >= 0 else ""
        alerts.append(
            f"🚨 Aggregate Diff OI spike: {sign}{ind_fmt(row['diff_oi'])} "
            f"at {row['time']} (threshold: {ind_fmt(threshold)})"
        )
    for strike, d in row.get("strike_deltas", {}).items():
        if abs(d["ce_delta"]) >= threshold:
            alerts.append(
                f"🚨 CE OI spike at {strike}: {ind_fmt(d['ce_delta'])} at {row['time']}"
            )
        if abs(d["pe_delta"]) >= threshold:
            alerts.append(
                f"🚨 PE OI spike at {strike}: {ind_fmt(d['pe_delta'])} at {row['time']}"
            )
    return alerts


def send_telegram(message: str, bot_token: str = "", chat_id: str = "") -> bool:
    """Send alert to Telegram. Falls back to TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID env vars."""
    bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id   = chat_id   or os.getenv("TELEGRAM_CHAT_ID", "")
    if not (bot_token and chat_id):
        return False
    try:
        requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            data={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
            timeout=10,
        )
        return True
    except Exception:
        return False
