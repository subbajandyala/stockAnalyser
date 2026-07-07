"""
Smart Options Buying Signal — aggregates live Kite OI, PCR, Max Pain,
COI PCR trend, Move Verdict, Vol PCR and OI walls into a single
BUY CE / BUY PE / WAIT recommendation with strike, entry, SL, and target.
"""

import datetime
from io import StringIO
from typing import Optional

import numpy as np
import pandas as pd
import requests

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


def _hdrs(api_key: str, access_token: str) -> dict:
    return {"X-Kite-Version": "3", "Authorization": f"token {api_key}:{access_token}"}


def _batch_quote(api_key: str, access_token: str, syms: list[str]) -> dict:
    hdrs = _hdrs(api_key, access_token)
    out: dict = {}
    for i in range(0, len(syms), 400):
        r = requests.get(
            f"{_KITE_BASE}/quote",
            headers=hdrs,
            params={"i": syms[i: i + 400]},
            timeout=30,
        )
        if r.ok:
            out.update(r.json().get("data", {}))
    return out


def _get_spot(api_key: str, access_token: str, symbol: str) -> float:
    q    = _SPOT_QUOTE[symbol]
    resp = requests.get(
        f"{_KITE_BASE}/quote/ltp",
        headers=_hdrs(api_key, access_token),
        params={"i": q},
        timeout=10,
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


# ── Factor helpers ─────────────────────────────────────────────────────────────

def _factor(name: str, value: str, direction: str, points: int, reason: str) -> dict:
    return {"name": name, "value": value, "direction": direction,
            "points": points, "reason": reason}


def _pcr_factor(pcr: float) -> dict:
    if pcr >= 1.3:
        return _factor("PCR", f"{pcr:.2f}", "BULL", +2,
                       "Heavy put writing = strong support floor")
    if pcr >= 1.0:
        return _factor("PCR", f"{pcr:.2f}", "BULL", +1,
                       "More puts than calls = mild support")
    if pcr <= 0.7:
        return _factor("PCR", f"{pcr:.2f}", "BEAR", -2,
                       "Heavy call writing = strong resistance ceiling")
    if pcr < 1.0:
        return _factor("PCR", f"{pcr:.2f}", "BEAR", -1,
                       "More calls than puts = mild resistance")
    return _factor("PCR", f"{pcr:.2f}", "NEUTRAL", 0, "Balanced OI")


def _coi_pcr_factor(coi_pcr: float, prev_coi: Optional[float]) -> dict:
    if coi_pcr >= 1.2:
        return _factor("COI PCR", f"{coi_pcr:.3f}", "BULL", +2,
                       "Put writers dominant — market conviction bullish")
    if coi_pcr <= 0.8:
        return _factor("COI PCR", f"{coi_pcr:.3f}", "BEAR", -2,
                       "Call writers dominant — market conviction bearish")
    if prev_coi is not None and coi_pcr > prev_coi:
        return _factor("COI PCR", f"{coi_pcr:.3f} ↑", "BULL", +1,
                       "COI PCR rising — put writers gaining edge")
    if prev_coi is not None and coi_pcr < prev_coi:
        return _factor("COI PCR", f"{coi_pcr:.3f} ↓", "BEAR", -1,
                       "COI PCR falling — call writers gaining edge")
    return _factor("COI PCR", f"{coi_pcr:.3f}", "NEUTRAL", 0,
                   "COI PCR neutral / no trend")


def _vol_pcr_factor(vol_pcr: float) -> dict:
    if vol_pcr >= 1.2:
        return _factor("Vol PCR", f"{vol_pcr:.3f}", "BULL", +1,
                       "Put volume dominates — buyers positioning for support")
    if vol_pcr <= 0.8:
        return _factor("Vol PCR", f"{vol_pcr:.3f}", "BEAR", -1,
                       "Call volume dominates — buyers positioning for resistance")
    return _factor("Vol PCR", f"{vol_pcr:.3f}", "NEUTRAL", 0,
                   "Balanced call / put volume")


def _verdict_factor(verdict: str, reason: str, spot: float, prev_spot: float) -> dict:
    if verdict != "GOOD":
        return _factor("Move Verdict", "FAKE / —", "NEUTRAL", 0,
                       "OI flow not confirming — no conviction signal")
    price_up = spot >= prev_spot
    if price_up:
        return _factor("Move Verdict", "GOOD ✓", "BULL", +2,
                       f"OI confirms UP move — {reason}")
    else:
        return _factor("Move Verdict", "GOOD ✓", "BEAR", -2,
                       f"OI confirms DOWN move — {reason}")


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
    return _factor("Max Pain", f"MP {mp:,.0f} (≈ spot)", "NEUTRAL", 0,
                   "Spot near Max Pain — range-bound")


def _oi_wall_factor(spot: float, max_ce_strike: float, max_pe_strike: float) -> dict:
    if spot > max_ce_strike:
        return _factor("OI Walls", f"Broke CE wall {max_ce_strike:,.0f}", "BULL", +1,
                       "Spot above heaviest call OI — resistance cleared")
    if spot < max_pe_strike:
        return _factor("OI Walls", f"Below PE wall {max_pe_strike:,.0f}", "BEAR", -1,
                       "Spot below heaviest put OI — support broken")
    return _factor("OI Walls",
                   f"CE wall {max_ce_strike:,.0f} | PE wall {max_pe_strike:,.0f}",
                   "NEUTRAL", 0,
                   "Spot between key OI walls — no breakout yet")


def _spot_momentum_factor(toi_rows: list) -> dict:
    """Directional momentum from spot price across OI snapshots."""
    if len(toi_rows) < 2:
        return _factor("Spot Momentum", "—", "NEUTRAL", 0, "Need 2+ OI snapshots for momentum")
    spots = [r["spot"] for r in toi_rows[-3:]]
    first, last = spots[0], spots[-1]
    pct = (last - first) / first * 100 if first else 0
    if pct > 0.3:
        return _factor("Spot Momentum", f"{last:,.0f} ↑ +{pct:.2f}%", "BULL", +2,
                       "Spot rising strongly over recent scans")
    if pct > 0.05:
        return _factor("Spot Momentum", f"{last:,.0f} ↑ +{pct:.2f}%", "BULL", +1,
                       "Spot drifting up — mild bullish momentum")
    if pct < -0.3:
        return _factor("Spot Momentum", f"{last:,.0f} ↓ {pct:.2f}%", "BEAR", -2,
                       "Spot falling strongly over recent scans")
    if pct < -0.05:
        return _factor("Spot Momentum", f"{last:,.0f} ↓ {pct:.2f}%", "BEAR", -1,
                       "Spot drifting down — mild bearish momentum")
    return _factor("Spot Momentum", f"{last:,.0f} →", "NEUTRAL", 0, "Spot flat — no trend")


def _diff_oi_factor(toi_rows: list) -> dict:
    if len(toi_rows) < 2:
        return _factor("Diff OI Trend", "—", "NEUTRAL", 0,
                       "Need ≥2 rows for trend")
    latest = toi_rows[-1]["diff_oi"]
    prev   = toi_rows[-2]["diff_oi"]
    change = latest - prev
    if change > 0:
        return _factor("Diff OI Trend", f"{latest:+,} ↑", "BULL", +1,
                       "Put-Call OI divergence increasing — put writers adding")
    if change < 0:
        return _factor("Diff OI Trend", f"{latest:+,} ↓", "BEAR", -1,
                       "Put-Call OI divergence falling — call writers adding")
    return _factor("Diff OI Trend", f"{latest:+,} →", "NEUTRAL", 0,
                   "Diff OI flat — no incremental flow")


def _sentiment_factor(sentiment: str) -> dict:
    if sentiment == "Bullish":
        return _factor("Sentiment", "Bullish", "BULL", +1,
                       "COI PCR trend signals institutional bullish bias")
    if sentiment == "Bearish":
        return _factor("Sentiment", "Bearish", "BEAR", -1,
                       "COI PCR trend signals institutional bearish bias")
    return _factor("Sentiment", "Neutral", "NEUTRAL", 0,
                   "No clear directional bias from OI sentiment")


# ── Main scanner ───────────────────────────────────────────────────────────────

def run_smart_signal(
    api_key: str,
    access_token: str,
    symbol: str,
    expiry: str,
    instr_df: pd.DataFrame,
    toi_rows: Optional[list] = None,
) -> dict:
    """
    Aggregate all available Kite signals into a single option buying recommendation.

    Returns a dict with:
        ts, spot, atm, expiry, signal, confidence, score, max_pain,
        pcr, strike, option_type, ltp, sl, target, rr, factors, error(opt)
    """
    exch = _EXCHANGE[symbol]
    step = _STRIKE_STEP[symbol]
    hdrs = _hdrs(api_key, access_token)

    # 1. Spot
    spot = _get_spot(api_key, access_token, symbol)
    atm  = round(spot / step) * step

    # 2. Fetch option chain OI + LTP for ±12 strikes
    n_strikes = 12
    strikes   = [atm + i * step for i in range(-n_strikes, n_strikes + 1)]
    target_dt = pd.to_datetime(expiry)
    sub = instr_df[
        (instr_df["expiry_dt"] == target_dt) &
        (instr_df["strike"].isin([float(s) for s in strikes]))
    ]
    if sub.empty:
        return {"error": f"No instruments found for {symbol} expiry {expiry}",
                "ts": datetime.datetime.now(_IST)}

    syms   = (exch + ":" + sub["tradingsymbol"]).tolist()
    quotes = _batch_quote(api_key, access_token, syms)

    # Build chain dataframe
    chain: dict = {}
    for _, row in sub.iterrows():
        strike  = int(float(row["strike"]))
        itype   = row["instrument_type"].lower()
        key     = f"{exch}:{row['tradingsymbol']}"
        q       = quotes.get(key, {})
        if strike not in chain:
            chain[strike] = {"ce_oi": 0, "pe_oi": 0, "ce_ltp": 0.0, "pe_ltp": 0.0}
        chain[strike][f"{itype}_oi"]  = int(q.get("oi", 0))
        chain[strike][f"{itype}_ltp"] = float(q.get("last_price", 0))

    chain_df = pd.DataFrame([{"strike": k, **v} for k, v in sorted(chain.items())])
    total_ce = float(chain_df["ce_oi"].sum())
    total_pe = float(chain_df["pe_oi"].sum())
    pcr      = round(total_pe / total_ce, 3) if total_ce else 0.0

    # Max pain
    s_arr  = chain_df["strike"].values.astype(float)
    ce_arr = chain_df["ce_oi"].values.astype(float)
    pe_arr = chain_df["pe_oi"].values.astype(float)
    mp     = _max_pain(s_arr, ce_arr, pe_arr)

    max_ce_strike = float(chain_df.loc[chain_df["ce_oi"].idxmax(), "strike"])
    max_pe_strike = float(chain_df.loc[chain_df["pe_oi"].idxmax(), "strike"])

    # 3. Build factors
    factors = []
    factors.append(_pcr_factor(pcr))
    factors.append(_maxpain_factor(spot, mp))
    factors.append(_oi_wall_factor(spot, max_ce_strike, max_pe_strike))

    has_toi = bool(toi_rows and len(toi_rows) >= 1)

    if has_toi:
        lat = toi_rows[-1]
        prev = toi_rows[-2] if len(toi_rows) >= 2 else None

        factors.append(_coi_pcr_factor(lat["coi_pcr"], prev["coi_pcr"] if prev else None))
        factors.append(_vol_pcr_factor(lat["vol_pcr"]))
        factors.append(_sentiment_factor(lat["sentiment"]))
        factors.append(_verdict_factor(
            lat.get("verdict", "—"),
            lat.get("verdict_reason", ""),
            lat["spot"],
            prev["spot"] if prev else lat["spot"],
        ))
        factors.append(_diff_oi_factor(toi_rows))
        factors.append(_spot_momentum_factor(toi_rows))
    else:
        factors.append(_factor("COI PCR", "—", "NEUTRAL", 0,
                               "Initialize Trending OI tab to unlock COI PCR signal"))
        factors.append(_factor("Move Verdict", "—", "NEUTRAL", 0,
                               "Initialize Trending OI tab to unlock verdict signal"))
        factors.append(_factor("Sentiment", "—", "NEUTRAL", 0,
                               "Initialize Trending OI tab to unlock sentiment signal"))
        factors.append(_factor("Diff OI Trend", "—", "NEUTRAL", 0,
                               "Initialize Trending OI tab to unlock diff OI trend"))
        factors.append(_factor("Vol PCR", "—", "NEUTRAL", 0,
                               "Initialize Trending OI tab to unlock Vol PCR"))
        factors.append(_factor("Spot Momentum", "—", "NEUTRAL", 0,
                               "Initialize Trending OI tab to unlock spot momentum"))

    # 4. Composite score
    score = sum(f["points"] for f in factors)

    # 5. Signal classification
    # When Trending OI is not running, effective max score is PCR(2)+MaxPain(2)+OIWalls(1)=5
    # so lower thresholds to ±5 / ±3 to allow signals; with toi_rows, keep full thresholds.
    if has_toi:
        strong_thresh, mild_thresh = 6, 4
    else:
        strong_thresh, mild_thresh = 5, 3

    if score >= strong_thresh:
        signal, confidence, opt_type = "STRONG BUY CE", "HIGH", "CE"
    elif score >= mild_thresh:
        signal, confidence, opt_type = "BUY CE", "MEDIUM", "CE"
    elif score <= -strong_thresh:
        signal, confidence, opt_type = "STRONG BUY PE", "HIGH", "PE"
    elif score <= -mild_thresh:
        signal, confidence, opt_type = "BUY PE", "MEDIUM", "PE"
    else:
        signal, confidence, opt_type = "WAIT", "LOW", None

    # 6. Strike selection and LTP
    rec_strike = None
    ltp = sl = target = rr = None

    if opt_type:
        if "STRONG" in signal:
            rec_strike = atm
        else:
            rec_strike = atm + step if opt_type == "CE" else atm - step

        # Verify strike exists in chain, fallback to ATM
        row_match = chain_df[chain_df["strike"] == rec_strike]
        if row_match.empty:
            rec_strike = atm
            row_match  = chain_df[chain_df["strike"] == atm]

        ltp_col = f"{opt_type.lower()}_ltp"
        ltp = float(row_match.iloc[0][ltp_col]) if not row_match.empty else 0.0

        # If OTM ltp is too low, fall back to ATM
        if ltp < 1.0 and rec_strike != atm:
            rec_strike = atm
            row_match  = chain_df[chain_df["strike"] == atm]
            ltp        = float(row_match.iloc[0][ltp_col]) if not row_match.empty else 0.0

        if ltp and ltp > 0.5:
            sl     = round(ltp * 0.65, 1)
            target = round(ltp * 1.65, 1)
            rr     = round((target - ltp) / (ltp - sl), 1) if ltp > sl else None

    return {
        "ts":           datetime.datetime.now(_IST),
        "spot":         round(spot, 2),
        "atm":          atm,
        "expiry":       expiry,
        "signal":       signal,
        "confidence":   confidence,
        "score":        score,
        "pcr":          pcr,
        "max_pain":     int(mp),
        "max_ce_wall":  int(max_ce_strike),
        "max_pe_wall":  int(max_pe_strike),
        "option_type":  opt_type,
        "strike":       rec_strike,
        "ltp":          ltp,
        "sl":           sl,
        "target":       target,
        "rr":           rr,
        "factors":      factors,
    }
