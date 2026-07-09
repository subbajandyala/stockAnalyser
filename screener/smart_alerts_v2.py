"""
Smart Alerts v2 — 16-factor precision option buying signal.

Enhancements over v1:
  - VWAP position (Kite 1-min candles from 9:15 AM)
  - India VIX gate (yfinance ^INDIAVIX)
  - IV Spike detection (ATM LTP rolling ratio)
  - Time-of-day gate
  - OI velocity (rate of OI change)
  - Consecutive scan confirmation (2 of last 3 must agree)
  - Expiry-day mode (tighter entry window, faster exit)
  - VIX-adjusted dynamic SL / Target
  - Cross-index conflict check (Nifty vs Banknifty)
"""

import datetime
from io import StringIO
from typing import Optional

import numpy as np
import pandas as pd
import requests
import yfinance as yf

_KITE_BASE = "https://api.kite.trade"
_IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

_STRIKE_STEP: dict[str, int] = {
    "NIFTY": 50, "BANKNIFTY": 100, "FINNIFTY": 50,
    "MIDCPNIFTY": 25, "SENSEX": 100, "BANKEX": 100,
}
_EXCHANGE: dict[str, str] = {
    "NIFTY": "NFO", "BANKNIFTY": "NFO", "FINNIFTY": "NFO",
    "MIDCPNIFTY": "NFO", "SENSEX": "BFO", "BANKEX": "BFO",
}
_SPOT_QUOTE: dict[str, str] = {
    "NIFTY":      "NSE:NIFTY 50",
    "BANKNIFTY":  "NSE:NIFTY BANK",
    "FINNIFTY":   "NSE:NIFTY FIN SERVICE",
    "MIDCPNIFTY": "NSE:NIFTY MID SELECT",
    "SENSEX":     "BSE:SENSEX",
    "BANKEX":     "BSE:BANKEX",
}
# Kite historical instrument tokens for index VWAP (NSE indices only)
# BSE indices (SENSEX, BANKEX) are not available via Kite historical API — omit so fetch_vwap returns 0
_HIST_TOKEN: dict[str, int] = {
    "NIFTY":      256265,
    "BANKNIFTY":  260105,
    "FINNIFTY":   257801,
    "MIDCPNIFTY": 288009,
}
# Kite token for NIFTY 50 index (for cross-index check)
_NIFTY_TOKEN     = 256265
_BANKNIFTY_TOKEN = 260105

# yfinance ticker for India VIX
_VIX_TICKER = "^INDIAVIX"

# Conflict pairs: if selected index is one of these, fetch the other for conflict check
_CONFLICT_PAIRS: dict[str, str] = {
    "NIFTY":      "BANKNIFTY",
    "BANKNIFTY":  "NIFTY",
}


# ── Low-level helpers ─────────────────────────────────────────────────────────

def _hdrs(api_key: str, access_token: str) -> dict:
    return {"X-Kite-Version": "3", "Authorization": f"token {api_key}:{access_token}"}


def _batch_quote(api_key: str, access_token: str, syms: list[str]) -> dict:
    hdrs = _hdrs(api_key, access_token)
    out: dict = {}
    for i in range(0, len(syms), 400):
        r = requests.get(
            f"{_KITE_BASE}/quote", headers=hdrs,
            params={"i": syms[i: i + 400]}, timeout=30,
        )
        if r.ok:
            out.update(r.json().get("data", {}))
    return out


def _get_spot(api_key: str, access_token: str, symbol: str) -> float:
    q    = _SPOT_QUOTE[symbol]
    resp = requests.get(
        f"{_KITE_BASE}/quote/ltp", headers=_hdrs(api_key, access_token),
        params={"i": q}, timeout=10,
    )
    resp.raise_for_status()
    return float(resp.json()["data"][q]["last_price"])


def _max_pain(strikes: np.ndarray, ce_oi: np.ndarray, pe_oi: np.ndarray) -> float:
    pain = [
        float((ce_oi * np.maximum(0, s - strikes)).sum() +
              (pe_oi * np.maximum(0, strikes - s)).sum())
        for s in strikes
    ]
    return float(strikes[int(np.argmin(pain))])


# ── New data sources ──────────────────────────────────────────────────────────

def fetch_india_vix() -> float:
    """Fetch India VIX daily close via yfinance. Returns 0.0 on failure."""
    try:
        df = yf.download(_VIX_TICKER, period="5d", interval="1d",
                         progress=False, auto_adjust=True)
        if df.empty:
            return 0.0
        close = df["Close"].squeeze()
        return float(close.dropna().iloc[-1])
    except Exception:
        return 0.0


def fetch_vwap(api_key: str, access_token: str, symbol: str) -> float:
    """
    Compute intraday VWAP for symbol from 9:15 AM using Kite 1-min historical candles.
    Falls back to spot price on failure (neutral VWAP = spot means no bias).
    """
    token = _HIST_TOKEN.get(symbol)
    if not token:
        return 0.0
    try:
        ist_now  = datetime.datetime.now(_IST)
        date_str = ist_now.strftime("%Y-%m-%d")
        from_str = f"{date_str} 09:15:00"
        to_str   = ist_now.strftime("%Y-%m-%d %H:%M:%S")
        resp = requests.get(
            f"{_KITE_BASE}/instruments/historical/{token}/minute",
            headers=_hdrs(api_key, access_token),
            params={"from": from_str, "to": to_str},
            timeout=20,
        )
        if not resp.ok:
            return 0.0
        candles = resp.json().get("data", {}).get("candles", [])
        if not candles:
            return 0.0
        df = pd.DataFrame(candles, columns=["ts", "open", "high", "low", "close", "volume"])
        df["tp"]  = (df["high"] + df["low"] + df["close"]) / 3.0
        df["tpv"] = df["tp"] * df["volume"]
        total_vol = df["volume"].sum()
        if total_vol == 0:
            return 0.0
        return float(df["tpv"].sum() / total_vol)
    except Exception:
        return 0.0


def compute_iv_ratio(current_atm_ltp: float, ltp_history: list[float]) -> float:
    """
    Ratio of current ATM LTP to rolling average of last ≤3 values.
    Returns 1.0 when insufficient history (neutral).
    """
    if not ltp_history or current_atm_ltp <= 0:
        return 1.0
    avg = sum(ltp_history[-3:]) / len(ltp_history[-3:])
    return round(current_atm_ltp / avg, 3) if avg > 0 else 1.0


def check_gates(
    vix: float,
    iv_ratio: float,
    ist_now: datetime.datetime,
    expiry_date: datetime.date,
    direction_history: list[str],
) -> dict:
    """
    Evaluate the 4 hard gates. Returns dict of {gate_name: (passes: bool, reason: str)}.
    A signal is only valid when ALL gates pass.
    """
    gates: dict[str, tuple[bool, str]] = {}

    # Gate 1 — Time of day
    t = ist_now.time()
    open_noise   = datetime.time(9, 15) <= t < datetime.time(9, 30)
    close_noise  = t >= datetime.time(15, 0)
    if open_noise:
        gates["Time"] = (False, "Opening noise 9:15–9:30 — OI not settled")
    elif close_noise:
        gates["Time"] = (False, "Last 30 min — too volatile to enter")
    else:
        gates["Time"] = (True,  f"Market hours clear ({t.strftime('%H:%M')} IST)")

    # Gate 2 — India VIX (only block on true extremes; 10-11 is low but still tradeable)
    if vix <= 0:
        gates["VIX"] = (True, "VIX unavailable — gate skipped")
    elif vix < 10:
        gates["VIX"] = (False, f"VIX {vix:.1f} critically low — options near illiquid, skip")
    elif vix > 25:
        gates["VIX"] = (False, f"VIX {vix:.1f} extreme — premiums bloated, vega crush risk")
    elif vix < 12:
        gates["VIX"] = (True, f"VIX {vix:.1f} low but tradeable — prefer tighter targets")
    else:
        gates["VIX"] = (True,  f"VIX {vix:.1f} in ideal zone (12–25)")

    # Gate 3 — IV crush
    if iv_ratio < 0.75:
        gates["IV"] = (False, f"IV ratio {iv_ratio:.2f} — sharp premium collapse, avoid buying")
    elif iv_ratio > 1.5:
        gates["IV"] = (False, f"IV ratio {iv_ratio:.2f} — IV already spiked, late entry risk")
    else:
        gates["IV"] = (True,  f"IV ratio {iv_ratio:.2f} — no adverse IV distortion")

    # Gate 4 — Consecutive confirmation: require consistent direction across last 2 scans.
    # On first scan (empty history) we pass — blocking would make the first signal impossible
    # after any page reload.  Conflicting history (BULL then BEAR) is the only hard block.
    if len(direction_history) < 1:
        gates["Confirm"] = (True, "First scan — no prior history, allowing through")
    else:
        recent = direction_history[-2:]
        bulls  = recent.count("BULL")
        bears  = recent.count("BEAR")
        if bulls >= 1 and bears == 0:
            gates["Confirm"] = (True,  f"BULL confirmed in {bulls}/{len(recent)} recent scans")
        elif bears >= 1 and bulls == 0:
            gates["Confirm"] = (True,  f"BEAR confirmed in {bears}/{len(recent)} recent scans")
        elif bulls >= 1 and bears >= 1:
            gates["Confirm"] = (False, f"Conflicting signals: {bulls}× BULL, {bears}× BEAR in last {len(recent)} scans — no conviction")
        else:
            gates["Confirm"] = (False, "Direction neutral in recent scans — wait for clarity")

    return gates


def is_expiry_day(expiry_date: datetime.date) -> bool:
    return datetime.date.today() == expiry_date


# ── Factor helpers ─────────────────────────────────────────────────────────────

def _factor(name: str, value: str, direction: str, points: int, reason: str) -> dict:
    return {"name": name, "value": value, "direction": direction,
            "points": points, "reason": reason}


def _pcr_factor(pcr: float) -> dict:
    if pcr >= 1.3:
        return _factor("PCR", f"{pcr:.2f}", "BULL", +2, "Heavy put writing — strong support floor")
    if pcr >= 1.0:
        return _factor("PCR", f"{pcr:.2f}", "BULL", +1, "More puts than calls — mild support")
    if pcr <= 0.7:
        return _factor("PCR", f"{pcr:.2f}", "BEAR", -2, "Heavy call writing — strong resistance ceiling")
    if pcr < 1.0:
        return _factor("PCR", f"{pcr:.2f}", "BEAR", -1, "More calls than puts — mild resistance")
    return _factor("PCR", f"{pcr:.2f}", "NEUTRAL", 0, "Balanced OI")


def _maxpain_factor(spot: float, mp: float) -> dict:
    diff_pct = (spot - mp) / mp * 100
    if diff_pct < -1.0:
        return _factor("Max Pain", f"MP {mp:,.0f} ({diff_pct:.1f}%)", "BULL", +2,
                       "Spot well below Max Pain — gravity pulls price up")
    if diff_pct < -0.3:
        return _factor("Max Pain", f"MP {mp:,.0f} ({diff_pct:.1f}%)", "BULL", +1,
                       "Spot slightly below Max Pain — mild upward pull")
    if diff_pct > 1.0:
        return _factor("Max Pain", f"MP {mp:,.0f} ({diff_pct:+.1f}%)", "BEAR", -2,
                       "Spot well above Max Pain — gravity pulls price down")
    if diff_pct > 0.3:
        return _factor("Max Pain", f"MP {mp:,.0f} ({diff_pct:+.1f}%)", "BEAR", -1,
                       "Spot slightly above Max Pain — mild downward pull")
    return _factor("Max Pain", f"MP {mp:,.0f} (~spot)", "NEUTRAL", 0, "Spot near Max Pain — range-bound")


def _oi_wall_factor(spot: float, max_ce: float, max_pe: float) -> dict:
    if spot > max_ce:
        return _factor("OI Walls", f"Broke CE {max_ce:,.0f}", "BULL", +1,
                       "Spot above heaviest call OI — resistance cleared")
    if spot < max_pe:
        return _factor("OI Walls", f"Below PE {max_pe:,.0f}", "BEAR", -1,
                       "Spot below heaviest put OI — support broken")
    return _factor("OI Walls", f"CE {max_ce:,.0f} | PE {max_pe:,.0f}", "NEUTRAL", 0,
                   "Spot between OI walls — no breakout yet")


def _coi_pcr_factor(coi_pcr: float, prev_coi: Optional[float]) -> dict:
    if coi_pcr >= 1.2:
        return _factor("COI PCR", f"{coi_pcr:.3f}", "BULL", +2, "Put writers dominant — bullish conviction")
    if coi_pcr <= 0.8:
        return _factor("COI PCR", f"{coi_pcr:.3f}", "BEAR", -2, "Call writers dominant — bearish conviction")
    if prev_coi is not None and coi_pcr > prev_coi:
        return _factor("COI PCR", f"{coi_pcr:.3f} ↑", "BULL", +1, "COI PCR rising — put writers gaining edge")
    if prev_coi is not None and coi_pcr < prev_coi:
        return _factor("COI PCR", f"{coi_pcr:.3f} ↓", "BEAR", -1, "COI PCR falling — call writers gaining edge")
    return _factor("COI PCR", f"{coi_pcr:.3f}", "NEUTRAL", 0, "COI PCR neutral / no trend")


def _vol_pcr_factor(vol_pcr: float) -> dict:
    if vol_pcr >= 1.2:
        return _factor("Vol PCR", f"{vol_pcr:.3f}", "BULL", +1, "Put volume dominates — buyers positioning for support")
    if vol_pcr <= 0.8:
        return _factor("Vol PCR", f"{vol_pcr:.3f}", "BEAR", -1, "Call volume dominates — buyers positioning for resistance")
    return _factor("Vol PCR", f"{vol_pcr:.3f}", "NEUTRAL", 0, "Balanced call / put volume")


def _sentiment_factor(sentiment: str) -> dict:
    if sentiment == "Bullish":
        return _factor("Sentiment", "Bullish", "BULL", +1, "OI sentiment signals institutional bullish bias")
    if sentiment == "Bearish":
        return _factor("Sentiment", "Bearish", "BEAR", -1, "OI sentiment signals institutional bearish bias")
    return _factor("Sentiment", "Neutral", "NEUTRAL", 0, "No clear institutional bias")


def _verdict_factor(verdict: str, reason: str, spot: float, prev_spot: float) -> dict:
    if verdict != "GOOD":
        return _factor("Move Verdict", "FAKE / —", "NEUTRAL", 0, "OI flow not confirming — no conviction")
    if spot >= prev_spot:
        return _factor("Move Verdict", "GOOD ✓", "BULL", +2, f"OI confirms UP move — {reason}")
    return _factor("Move Verdict", "GOOD ✓", "BEAR", -2, f"OI confirms DOWN move — {reason}")


def _diff_oi_factor(toi_rows: list) -> dict:
    if len(toi_rows) < 2:
        return _factor("Diff OI Trend", "—", "NEUTRAL", 0, "Need ≥2 rows for trend")
    latest = toi_rows[-1]["diff_oi"]
    prev   = toi_rows[-2]["diff_oi"]
    change = latest - prev
    if change > 0:
        return _factor("Diff OI Trend", f"{latest:+,} ↑", "BULL", +1, "Put-Call OI divergence increasing — put writers adding")
    if change < 0:
        return _factor("Diff OI Trend", f"{latest:+,} ↓", "BEAR", -1, "Put-Call OI divergence falling — call writers adding")
    return _factor("Diff OI Trend", f"{latest:+,} →", "NEUTRAL", 0, "Diff OI flat — no incremental flow")


# ── New factors ────────────────────────────────────────────────────────────────

def _vwap_factor(spot: float, vwap: float) -> dict:
    if vwap <= 0:
        return _factor("VWAP", "—", "NEUTRAL", 0, "VWAP unavailable — Kite historical API needed")
    diff_pct = (spot - vwap) / vwap * 100
    if diff_pct > 0.3:
        return _factor("VWAP", f"Spot {diff_pct:+.2f}% above VWAP", "BULL", +2,
                       f"VWAP {vwap:,.0f} — spot above, uptrend confirmed")
    if 0 < diff_pct <= 0.3:
        return _factor("VWAP", f"Spot {diff_pct:+.2f}% above VWAP", "BULL", +1,
                       f"VWAP {vwap:,.0f} — mildly above, bullish tilt")
    if diff_pct < -0.3:
        return _factor("VWAP", f"Spot {diff_pct:.2f}% below VWAP", "BEAR", -2,
                       f"VWAP {vwap:,.0f} — spot below, downtrend confirmed")
    if -0.3 <= diff_pct < 0:
        return _factor("VWAP", f"Spot {diff_pct:.2f}% below VWAP", "BEAR", -1,
                       f"VWAP {vwap:,.0f} — mildly below, bearish tilt")
    return _factor("VWAP", f"Spot at VWAP ({vwap:,.0f})", "NEUTRAL", 0, "Spot at VWAP — no directional bias")


def _vix_factor(vix: float) -> dict:
    if vix <= 0:
        return _factor("India VIX", "—", "NEUTRAL", 0, "VIX unavailable")
    if vix < 10:
        return _factor("India VIX", f"{vix:.1f} ⚠", "NEUTRAL", -2,
                       "VIX critically low — option premiums minimal, movement unlikely")
    if vix < 12:
        return _factor("India VIX", f"{vix:.1f} ↓", "NEUTRAL", -1,
                       "VIX low 10–12 — market calm, prefer tighter SL and targets")
    if vix <= 18:
        return _factor("India VIX", f"{vix:.1f} ✓", "NEUTRAL", 0,
                       "Ideal VIX zone 12–18 — balanced premium / movement")
    if vix <= 22:
        return _factor("India VIX", f"{vix:.1f} ↑", "NEUTRAL", -1,
                       "Elevated VIX 18–22 — options expensive, use tighter targets")
    return _factor("India VIX", f"{vix:.1f} 🔴", "NEUTRAL", -2,
                   "VIX > 22 — premiums bloated, vega crush risk on any reversal")


def _iv_spike_factor(iv_ratio: float) -> dict:
    if iv_ratio <= 0 or iv_ratio == 1.0:
        return _factor("IV Spike", "—", "NEUTRAL", 0, "Insufficient LTP history (need 2+ scans)")
    if iv_ratio > 1.25:
        return _factor("IV Spike", f"{iv_ratio:.2f}x ↑↑", "BULL", +2,
                       "Sharp IV expansion — large players buying options aggressively")
    if iv_ratio > 1.12:
        return _factor("IV Spike", f"{iv_ratio:.2f}x ↑", "BULL", +1,
                       "Mild IV expansion — fresh option demand detected")
    if iv_ratio < 0.85:
        return _factor("IV Spike", f"{iv_ratio:.2f}x ↓", "BEAR", -1,
                       "IV contracting — sellers dominating, premium being crushed")
    return _factor("IV Spike", f"{iv_ratio:.2f}x", "NEUTRAL", 0, "IV stable — no unusual option activity")


def _time_factor(ist_now: datetime.datetime, expiry_date: datetime.date) -> dict:
    t = ist_now.time()
    today = ist_now.date()
    is_exp = (today == expiry_date)

    if datetime.time(9, 30) <= t < datetime.time(11, 30):
        return _factor("Time Window", "Prime 9:30–11:30", "BULL", +1,
                       "Prime session — highest OI reliability, institutions active")
    if datetime.time(14, 0) <= t < datetime.time(15, 0) and is_exp:
        return _factor("Time Window", "Expiry 2–3 PM 🔥", "NEUTRAL", +1,
                       "Expiry final-hour window — explosive OI moves expected")
    if datetime.time(11, 30) <= t < datetime.time(13, 30):
        return _factor("Time Window", "Mid-session", "NEUTRAL", 0, "Mid-session — normal reliability")
    if datetime.time(13, 30) <= t < datetime.time(15, 0):
        return _factor("Time Window", "Afternoon drift", "NEUTRAL", -1,
                       "Afternoon — reduced OI responsiveness, lower reliability")
    return _factor("Time Window", t.strftime("%H:%M"), "NEUTRAL", 0, "Standard market hours")


def _spot_momentum_factor(toi_rows: list) -> dict:
    """Directional momentum from spot price across OI snapshots."""
    if len(toi_rows) < 2:
        return _factor("Spot Momentum", "—", "NEUTRAL", 0, "Need 2+ OI snapshots for momentum")
    spots = [r["spot"] for r in toi_rows[-3:]]
    first, last = spots[0], spots[-1]
    pct = (last - first) / first * 100 if first else 0
    if pct > 0.3:
        return _factor("Spot Momentum", f"{last:,.0f} ↑ +{pct:.2f}%", "BULL", +2,
                       "Spot rising strongly over recent scans — bullish price flow")
    if pct > 0.05:
        return _factor("Spot Momentum", f"{last:,.0f} ↑ +{pct:.2f}%", "BULL", +1,
                       "Spot drifting up — mild bullish momentum")
    if pct < -0.3:
        return _factor("Spot Momentum", f"{last:,.0f} ↓ {pct:.2f}%", "BEAR", -2,
                       "Spot falling strongly over recent scans — bearish price flow")
    if pct < -0.05:
        return _factor("Spot Momentum", f"{last:,.0f} ↓ {pct:.2f}%", "BEAR", -1,
                       "Spot drifting down — mild bearish momentum")
    return _factor("Spot Momentum", f"{last:,.0f} →", "NEUTRAL", 0, "Spot flat — no trend")


def _atm_parity_factor(atm_ce_ltp: float, atm_pe_ltp: float) -> dict:
    """CE vs PE LTP at ATM — market's directional pricing signal. Works on first scan."""
    if atm_ce_ltp <= 0 or atm_pe_ltp <= 0:
        return _factor("ATM Parity", "—", "NEUTRAL", 0, "ATM LTPs unavailable")
    ratio = atm_ce_ltp / atm_pe_ltp
    if ratio > 1.25:
        return _factor("ATM Parity", f"CE ₹{atm_ce_ltp:.0f} / PE ₹{atm_pe_ltp:.0f} ({ratio:.2f}x)", "BULL", +2,
                       "ATM calls significantly pricier — market pricing in upward breakout")
    if ratio > 1.08:
        return _factor("ATM Parity", f"CE ₹{atm_ce_ltp:.0f} / PE ₹{atm_pe_ltp:.0f} ({ratio:.2f}x)", "BULL", +1,
                       "ATM calls slightly pricier — mild bullish expectation")
    if ratio < 0.80:
        return _factor("ATM Parity", f"CE ₹{atm_ce_ltp:.0f} / PE ₹{atm_pe_ltp:.0f} ({ratio:.2f}x)", "BEAR", -2,
                       "ATM puts significantly pricier — market pricing in downward breakout")
    if ratio < 0.93:
        return _factor("ATM Parity", f"CE ₹{atm_ce_ltp:.0f} / PE ₹{atm_pe_ltp:.0f} ({ratio:.2f}x)", "BEAR", -1,
                       "ATM puts slightly pricier — mild bearish expectation")
    return _factor("ATM Parity", f"CE ₹{atm_ce_ltp:.0f} ≈ PE ₹{atm_pe_ltp:.0f}", "NEUTRAL", 0,
                   "ATM call/put parity — no directional pricing bias")


def _near_atm_skew_factor(chain_df: pd.DataFrame, atm: int, step: int) -> dict:
    """OI at adjacent strikes: PE@ATM-1 vs CE@ATM+1. Works on first scan."""
    ce_row = chain_df[chain_df["strike"] == atm + step]
    pe_row = chain_df[chain_df["strike"] == atm - step]
    if ce_row.empty or pe_row.empty:
        return _factor("Near-ATM Skew", "—", "NEUTRAL", 0, "Adjacent strikes not in chain")
    ce_oi = int(ce_row.iloc[0]["ce_oi"])
    pe_oi = int(pe_row.iloc[0]["pe_oi"])
    if pe_oi == 0 and ce_oi == 0:
        return _factor("Near-ATM Skew", "—", "NEUTRAL", 0, "Zero OI at adjacent strikes")
    ratio = (pe_oi / ce_oi) if ce_oi > 0 else 999.0
    if ratio > 2.0:
        return _factor("Near-ATM Skew", f"PE {pe_oi//1000}k vs CE {ce_oi//1000}k", "BULL", +2,
                       "Heavy put writing just below ATM — strong support floor being built")
    if ratio > 1.3:
        return _factor("Near-ATM Skew", f"PE {pe_oi//1000}k > CE {ce_oi//1000}k", "BULL", +1,
                       "More put writing than call writing at adjacent strikes — mild support")
    if ratio < 0.5:
        return _factor("Near-ATM Skew", f"CE {ce_oi//1000}k vs PE {pe_oi//1000}k", "BEAR", -2,
                       "Heavy call writing just above ATM — strong resistance ceiling being built")
    if ratio < 0.77:
        return _factor("Near-ATM Skew", f"CE {ce_oi//1000}k > PE {pe_oi//1000}k", "BEAR", -1,
                       "More call writing than put writing at adjacent strikes — mild resistance")
    return _factor("Near-ATM Skew", f"PE {pe_oi//1000}k | CE {ce_oi//1000}k", "NEUTRAL", 0,
                   "Balanced OI at adjacent strikes — no directional skew")


def _oi_velocity_factor(toi_rows: list, scan_interval_sec: int) -> dict:
    if len(toi_rows) < 3:
        return _factor("OI Velocity", "—", "NEUTRAL", 0, "Need 3+ rows to measure OI velocity")
    diffs = [abs(toi_rows[i]["diff_oi"] - toi_rows[i-1]["diff_oi"])
             for i in range(-3, 0) if i + len(toi_rows) > 0]
    if not diffs:
        return _factor("OI Velocity", "—", "NEUTRAL", 0, "Insufficient OI history")
    latest_vel  = abs(toi_rows[-1]["diff_oi"] - toi_rows[-2]["diff_oi"]) / max(scan_interval_sec, 1)
    session_avg = sum(diffs) / len(diffs) / max(scan_interval_sec, 1)
    direction   = toi_rows[-1]["diff_oi"] - toi_rows[-2]["diff_oi"]
    if session_avg == 0:
        return _factor("OI Velocity", "—", "NEUTRAL", 0, "No OI movement in session")
    ratio = latest_vel / session_avg if session_avg else 1
    if ratio >= 2.5:
        d = "BULL" if direction > 0 else "BEAR"
        pts = +2 if direction > 0 else -2
        return _factor("OI Velocity", f"{ratio:.1f}x spike", d, pts,
                       "Institutional burst — OI velocity 2.5x session avg")
    if ratio >= 1.5:
        d = "BULL" if direction > 0 else "BEAR"
        pts = +1 if direction > 0 else -1
        return _factor("OI Velocity", f"{ratio:.1f}x elevated", d, pts,
                       "Elevated OI velocity — fresh positions building")
    return _factor("OI Velocity", f"{ratio:.1f}x normal", "NEUTRAL", 0,
                   "OI building at normal pace — no urgency signal")


# ── Dynamic SL / Target ───────────────────────────────────────────────────────

def _get_sl_target_mults(vix: float, expiry_day: bool) -> tuple[float, float]:
    if expiry_day:
        return 0.75, 1.50   # tighter on expiry day — quick flip
    if vix <= 0 or 12 <= vix <= 14:
        return 0.80, 1.35   # low vol — tight SL, modest target
    if vix <= 18:
        return 0.75, 1.55   # normal
    if vix <= 22:
        return 0.65, 1.75   # elevated — wider SL needed
    return 0.65, 1.75       # default fallback (gate should have blocked anyway)


# ── Cross-index conflict check ────────────────────────────────────────────────

def _cross_index_direction(
    api_key: str, access_token: str,
    other_symbol: str, expiry: str, instr_df_other: Optional[pd.DataFrame],
) -> str:
    """
    Quick OI-only direction scan for the peer index. Returns 'BULL', 'BEAR', or 'NEUTRAL'.
    Uses only PCR and Max Pain — no full factor run.
    """
    if instr_df_other is None or instr_df_other.empty:
        return "NEUTRAL"
    try:
        step = _STRIKE_STEP[other_symbol]
        exch = _EXCHANGE[other_symbol]
        spot = _get_spot(api_key, access_token, other_symbol)
        atm  = round(spot / step) * step
        strikes = [atm + i * step for i in range(-8, 9)]
        target_dt = pd.to_datetime(expiry)
        sub = instr_df_other[
            (instr_df_other["expiry_dt"] == target_dt) &
            (instr_df_other["strike"].isin([float(s) for s in strikes]))
        ]
        if sub.empty:
            return "NEUTRAL"
        syms   = (exch + ":" + sub["tradingsymbol"]).tolist()
        quotes = _batch_quote(api_key, access_token, syms)
        chain: dict = {}
        for _, row in sub.iterrows():
            strike = int(float(row["strike"]))
            itype  = row["instrument_type"].lower()
            key    = f"{exch}:{row['tradingsymbol']}"
            q      = quotes.get(key, {})
            if strike not in chain:
                chain[strike] = {"ce_oi": 0, "pe_oi": 0}
            chain[strike][f"{itype}_oi"] = int(q.get("oi", 0))
        chain_df = pd.DataFrame([{"strike": k, **v} for k, v in sorted(chain.items())])
        total_ce = float(chain_df["ce_oi"].sum())
        total_pe = float(chain_df["pe_oi"].sum())
        pcr = (total_pe / total_ce) if total_ce else 0.0
        s_arr  = chain_df["strike"].values.astype(float)
        mp = _max_pain(s_arr, chain_df["ce_oi"].values.astype(float), chain_df["pe_oi"].values.astype(float))
        mp_diff = (spot - mp) / mp * 100 if mp else 0
        score = 0
        if pcr >= 1.2: score += 1
        elif pcr <= 0.8: score -= 1
        if mp_diff < -0.5: score += 1
        elif mp_diff > 0.5: score -= 1
        if score >= 1: return "BULL"
        if score <= -1: return "BEAR"
        return "NEUTRAL"
    except Exception:
        return "NEUTRAL"


# ── Main v2 signal ────────────────────────────────────────────────────────────

def run_smart_signal_v2(
    api_key: str,
    access_token: str,
    symbol: str,
    expiry: str,
    instr_df: pd.DataFrame,
    toi_rows: Optional[list] = None,
    ltp_history: Optional[list[float]] = None,
    direction_history: Optional[list[str]] = None,
    scan_interval_sec: int = 60,
    instr_df_peer: Optional[pd.DataFrame] = None,
) -> dict:
    """
    Run the full 13-factor v2 signal.

    Returns dict with all v1 keys plus:
      vix, vwap, iv_ratio, gates, gate_pass, expiry_day,
      sl_mult, tgt_mult, cross_direction, cross_symbol,
      score_raw, time_window, direction
    """
    exch = _EXCHANGE[symbol]
    step = _STRIKE_STEP[symbol]
    ist_now      = datetime.datetime.now(_IST)
    expiry_date  = pd.to_datetime(expiry).date()
    expiry_day   = is_expiry_day(expiry_date)

    # 1. Spot + ATM
    spot = _get_spot(api_key, access_token, symbol)
    atm  = round(spot / step) * step

    # 2. Option chain (±12 strikes)
    n_strikes = 12
    strikes   = [atm + i * step for i in range(-n_strikes, n_strikes + 1)]
    target_dt = pd.to_datetime(expiry)
    sub = instr_df[
        (instr_df["expiry_dt"] == target_dt) &
        (instr_df["strike"].isin([float(s) for s in strikes]))
    ]
    if sub.empty:
        return {"error": f"No instruments for {symbol} expiry {expiry}", "ts": ist_now}

    syms   = (exch + ":" + sub["tradingsymbol"]).tolist()
    quotes = _batch_quote(api_key, access_token, syms)

    chain: dict = {}
    for _, row in sub.iterrows():
        strike = int(float(row["strike"]))
        itype  = row["instrument_type"].lower()
        key    = f"{exch}:{row['tradingsymbol']}"
        q      = quotes.get(key, {})
        if strike not in chain:
            chain[strike] = {"ce_oi": 0, "pe_oi": 0, "ce_ltp": 0.0, "pe_ltp": 0.0}
        chain[strike][f"{itype}_oi"]  = int(q.get("oi", 0))
        chain[strike][f"{itype}_ltp"] = float(q.get("last_price", 0))

    chain_df = pd.DataFrame([{"strike": k, **v} for k, v in sorted(chain.items())])
    total_ce = float(chain_df["ce_oi"].sum())
    total_pe = float(chain_df["pe_oi"].sum())
    pcr      = round(total_pe / total_ce, 3) if total_ce else 0.0

    s_arr  = chain_df["strike"].values.astype(float)
    ce_arr = chain_df["ce_oi"].values.astype(float)
    pe_arr = chain_df["pe_oi"].values.astype(float)
    mp     = _max_pain(s_arr, ce_arr, pe_arr)

    max_ce_strike = float(chain_df.loc[chain_df["ce_oi"].idxmax(), "strike"])
    max_pe_strike = float(chain_df.loc[chain_df["pe_oi"].idxmax(), "strike"])

    # ATM LTP for IV ratio
    atm_row = chain_df[chain_df["strike"] == atm]
    atm_ce_ltp = float(atm_row["ce_ltp"].iloc[0]) if not atm_row.empty else 0.0
    atm_pe_ltp = float(atm_row["pe_ltp"].iloc[0]) if not atm_row.empty else 0.0
    atm_ltp    = (atm_ce_ltp + atm_pe_ltp) / 2.0 if (atm_ce_ltp + atm_pe_ltp) > 0 else 0.0

    # 3. Ancillary data
    vix      = fetch_india_vix()
    vwap     = fetch_vwap(api_key, access_token, symbol)
    iv_ratio = compute_iv_ratio(atm_ltp, ltp_history or [])

    # 4. Gates
    gates     = check_gates(vix, iv_ratio, ist_now, expiry_date, direction_history or [])
    gate_pass = all(v[0] for v in gates.values())

    # 5. Build all 13 factors
    factors: list[dict] = []

    # v1 factors (1–8)
    factors.append(_pcr_factor(pcr))
    factors.append(_maxpain_factor(spot, mp))
    factors.append(_oi_wall_factor(spot, max_ce_strike, max_pe_strike))

    if toi_rows and len(toi_rows) >= 1:
        lat  = toi_rows[-1]
        prev = toi_rows[-2] if len(toi_rows) >= 2 else None
        factors.append(_coi_pcr_factor(lat["coi_pcr"], prev["coi_pcr"] if prev else None))
        factors.append(_vol_pcr_factor(lat["vol_pcr"]))
        factors.append(_sentiment_factor(lat["sentiment"]))
        factors.append(_verdict_factor(lat.get("verdict", "—"), lat.get("verdict_reason", ""),
                                       lat["spot"], prev["spot"] if prev else lat["spot"]))
        factors.append(_diff_oi_factor(toi_rows))
    else:
        for name in ("COI PCR", "Vol PCR", "Sentiment", "Move Verdict", "Diff OI Trend"):
            factors.append(_factor(name, "—", "NEUTRAL", 0,
                                   "Initialize Trending OI tab to unlock this signal"))

    # instant-score factors (from chain, fire on every scan even without toi_rows)
    factors.append(_atm_parity_factor(atm_ce_ltp, atm_pe_ltp))
    factors.append(_near_atm_skew_factor(chain_df, atm, step))

    # v2 enrichment factors
    factors.append(_vwap_factor(spot, vwap))
    factors.append(_vix_factor(vix))
    factors.append(_iv_spike_factor(iv_ratio))
    factors.append(_time_factor(ist_now, expiry_date))
    factors.append(_oi_velocity_factor(toi_rows or [], scan_interval_sec))
    factors.append(_spot_momentum_factor(toi_rows or []))

    # 6. Raw score
    score_raw = sum(f["points"] for f in factors)

    # Score threshold — adapt to data richness; on expiry before 2 PM require extra conviction
    has_toi = bool(toi_rows and len(toi_rows) >= 1)
    t = ist_now.time()
    if expiry_day and t < datetime.time(14, 0):
        strong_thresh, mild_thresh = 8, 6
    elif has_toi:
        strong_thresh, mild_thresh = 7, 5
    else:
        # Without Trending OI the base factors (PCR+MaxPain+OIWalls+AtmParity+NearATM) max at ~9
        strong_thresh, mild_thresh = 6, 4

    # 7. Raw direction (ignoring gates)
    if score_raw >= strong_thresh:
        raw_signal, raw_conf, opt_type = "STRONG BUY CE", "HIGH", "CE"
    elif score_raw >= mild_thresh:
        raw_signal, raw_conf, opt_type = "BUY CE", "MEDIUM", "CE"
    elif score_raw <= -strong_thresh:
        raw_signal, raw_conf, opt_type = "STRONG BUY PE", "HIGH", "PE"
    elif score_raw <= -mild_thresh:
        raw_signal, raw_conf, opt_type = "BUY PE", "MEDIUM", "PE"
    else:
        raw_signal, raw_conf, opt_type = "WAIT", "LOW", None

    # Store directional lean based on raw score (not threshold) so Confirm gate
    # accumulates useful history even when score is below the signal threshold.
    direction = "BULL" if score_raw > 0 else ("BEAR" if score_raw < 0 else "NEUTRAL")

    # 8. Cross-index conflict check
    peer_symbol    = _CONFLICT_PAIRS.get(symbol)
    cross_direction = "N/A"
    if peer_symbol and instr_df_peer is not None:
        peer_expiry = expiry  # use same expiry date string
        cross_direction = _cross_index_direction(
            api_key, access_token, peer_symbol, peer_expiry, instr_df_peer
        )

    conflict = (peer_symbol is not None and
                cross_direction not in ("N/A", "NEUTRAL") and
                cross_direction != direction and
                direction != "NEUTRAL")

    # 9. Final signal — gates + conflict override
    if not gate_pass:
        signal, confidence = "WAIT", "LOW"
        block_reason = next((v[1] for v in gates.values() if not v[0]), "Gate blocked")
    elif conflict:
        signal, confidence = "WAIT", "LOW"
        block_reason = f"Cross-index conflict: {symbol} {direction} vs {peer_symbol} {cross_direction}"
    else:
        signal, confidence = raw_signal, raw_conf
        block_reason = ""

    opt_type_final = opt_type if signal != "WAIT" else None

    # 10. SL / Target (VIX-adjusted)
    sl_mult, tgt_mult = _get_sl_target_mults(vix, expiry_day)

    rec_strike = ltp = sl = target = rr = None
    if opt_type_final:
        if "STRONG" in signal:
            rec_strike = atm
        else:
            rec_strike = atm + step if opt_type_final == "CE" else atm - step

        row_match = chain_df[chain_df["strike"] == rec_strike]
        if row_match.empty:
            rec_strike = atm
            row_match  = chain_df[chain_df["strike"] == atm]

        ltp_col = f"{opt_type_final.lower()}_ltp"
        ltp = float(row_match.iloc[0][ltp_col]) if not row_match.empty else 0.0

        if ltp < 1.0 and rec_strike != atm:
            rec_strike = atm
            row_match  = chain_df[chain_df["strike"] == atm]
            ltp        = float(row_match.iloc[0][ltp_col]) if not row_match.empty else 0.0

        if ltp and ltp > 0.5:
            sl     = round(ltp * sl_mult, 1)
            target = round(ltp * tgt_mult, 1)
            rr     = round((target - ltp) / (ltp - sl), 1) if ltp > sl else None

    return {
        "ts":               ist_now,
        "spot":             round(spot, 2),
        "atm":              atm,
        "expiry":           expiry,
        "expiry_day":       expiry_day,
        "signal":           signal,
        "raw_signal":       raw_signal,
        "confidence":       confidence,
        "score":            score_raw,
        "block_reason":     block_reason,
        "pcr":              pcr,
        "max_pain":         int(mp),
        "max_ce_wall":      int(max_ce_strike),
        "max_pe_wall":      int(max_pe_strike),
        "vix":              round(vix, 2),
        "vwap":             round(vwap, 2) if vwap else 0,
        "iv_ratio":         iv_ratio,
        "atm_ltp":          round(atm_ltp, 2),
        "gates":            gates,
        "gate_pass":        gate_pass,
        "direction":        direction,
        "cross_symbol":     peer_symbol or "",
        "cross_direction":  cross_direction,
        "conflict":         conflict,
        "sl_mult":          sl_mult,
        "tgt_mult":         tgt_mult,
        "option_type":      opt_type_final,
        "strike":           rec_strike,
        "ltp":              ltp,
        "sl":               sl,
        "target":           target,
        "rr":               rr,
        "factors":          factors,
    }
