"""
Expiry Day Gamma Blast Scanner
Monitors nearby strikes after 2:00 PM for explosive gamma moves.

Logic:
  On expiry day, ATM gamma → ∞. When spot approaches a high-OI strike,
  market makers must delta-hedge aggressively — amplifying the move.
  Short covering (OI dropping at the wall) is the earliest warning sign.

Signal hierarchy:
  GAMMA BLAST ↑/↓  — blast zone + short covering confirmed + momentum aligned
  PRE-BLAST ↑/↓    — spot very close to wall, setup forming
  BUILDING ↑/↓     — conditions aligning but not yet confirmed
  WATCH            — monitor, no directional edge yet
  WAIT             — time/setup not ready
"""

import datetime

import pandas as pd
import requests

_KITE_BASE = "https://api.kite.trade"
_IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
_BFO = {"SENSEX", "BANKEX"}

_SPOT_SYM: dict[str, str] = {
    "NIFTY":      "NSE:NIFTY 50",
    "BANKNIFTY":  "NSE:NIFTY BANK",
    "FINNIFTY":   "NSE:NIFTY FIN SERVICE",
    "MIDCPNIFTY": "NSE:NIFTY MID SELECT",
    "SENSEX":     "BSE:SENSEX",
    "BANKEX":     "BSE:BANKEX",
}


def _hdrs(api_key: str, access_token: str) -> dict:
    return {"X-Kite-Version": "3", "Authorization": f"token {api_key}:{access_token}"}


# ── Chain fetch ───────────────────────────────────────────────────────────────

def fetch_chain_snapshot(
    api_key: str,
    access_token: str,
    symbol: str,
    expiry: str,
    instr_df: pd.DataFrame,
    n_strikes: int = 8,
) -> dict | None:
    """
    Fetch option chain for ±n_strikes around ATM.
    Returns {"spot", "atm", "chain": DataFrame, "strike_gap"} or None on error.
    """
    hdrs = _hdrs(api_key, access_token)
    xch      = "BFO" if symbol in _BFO else "NFO"
    spot_sym = _SPOT_SYM.get(symbol, f"NSE:{symbol}")

    # Spot price
    r = requests.get(
        f"{_KITE_BASE}/quote/ltp",
        headers=hdrs,
        params={"i": [spot_sym]},
        timeout=10,
    )
    if not r.ok:
        return None
    spot = float(r.json().get("data", {}).get(spot_sym, {}).get("last_price", 0))
    if spot <= 0:
        return None

    opts = instr_df[
        (instr_df["name"] == symbol) &
        (instr_df["expiry"] == expiry) &
        (instr_df["instrument_type"].isin(["CE", "PE"]))
    ].copy()
    if opts.empty:
        return None

    strikes = sorted(opts["strike"].unique())
    atm     = min(strikes, key=lambda s: abs(s - spot))
    idx     = strikes.index(atm)
    nearby  = strikes[max(0, idx - n_strikes): idx + n_strikes + 1]

    # Compute strike gap (50 for NIFTY, 100 for BANKNIFTY etc.)
    strike_gap = int(min(b - a for a, b in zip(strikes, strikes[1:]))) if len(strikes) > 1 else 50

    nearby_opts = opts[opts["strike"].isin(nearby)]
    syms = [f"{xch}:{ts}" for ts in nearby_opts["tradingsymbol"]]

    r2 = requests.get(f"{_KITE_BASE}/quote", headers=hdrs, params={"i": syms}, timeout=20)
    if not r2.ok:
        return None
    quotes = r2.json().get("data", {})

    rows: dict = {}
    for _, row in nearby_opts.iterrows():
        key = f"{xch}:{row['tradingsymbol']}"
        q   = quotes.get(key, {})
        s   = float(row["strike"])
        t   = row["instrument_type"]
        if s not in rows:
            rows[s] = {"Strike": s, "CE OI": 0, "CE LTP": 0.0, "PE OI": 0, "PE LTP": 0.0}
        rows[s][f"{t} OI"]  = int(q.get("oi", 0))
        rows[s][f"{t} LTP"] = float(q.get("last_price", 0))

    chain_df = (
        pd.DataFrame(list(rows.values()))
        .sort_values("Strike", ascending=False)   # highest strike at top
        .reset_index(drop=True)
    )

    return {"spot": spot, "atm": atm, "chain": chain_df, "strike_gap": strike_gap}


# ── Time phase ────────────────────────────────────────────────────────────────

def _time_phase(ist_now: datetime.datetime) -> tuple[str, str, int]:
    """Return (phase_key, phase_label, bonus_pts)."""
    mins = ist_now.hour * 60 + ist_now.minute
    if mins < 14 * 60:
        return "PRE",    "Before 2:00 PM",                      -99
    if mins < 14 * 60 + 30:
        return "EARLY",  "2:00–2:30 PM — gamma building",         1
    if mins < 15 * 60:
        return "PRIME",  "2:30–3:00 PM — high gamma zone",        2
    if mins < 15 * 60 + 15:
        return "PEAK",   "3:00–3:15 PM — PEAK gamma window ⚡",   3
    return "CLOSED", "After 3:15 PM — exit all positions", -99


# ── Scoring ───────────────────────────────────────────────────────────────────

def score_gamma_blast(
    chain_df:     pd.DataFrame,
    spot:         float,
    atm:          float,
    spot_history: list | None = None,
    prev_chain:   dict | None = None,
    ist_now:      datetime.datetime | None = None,
    strike_gap:   int = 50,
) -> dict:
    """
    Score gamma blast potential for CE (upside) and PE (downside).
    prev_chain: {strike: {"CE OI": x, "PE OI": y}} from last scan.
    Returns full result dict with factors list.
    """
    if ist_now is None:
        ist_now = datetime.datetime.now(_IST)

    res = {
        "score_ce": 0, "score_pe": 0,
        "blast_zone": False,
        "blast_strike_ce": None, "blast_strike_pe": None,
        "pressure_oi_ce": 0, "pressure_oi_pe": 0,
        "covering_ce": False, "covering_pe": False,
        "spot_direction": "FLAT",
        "signal": "WAIT", "signal_detail": "",
        "recommended_strike": None, "recommended_type": None,
        "factors": [],
    }

    # ── Time gate ────────────────────────────────────────────────────────────
    phase_key, phase_label, time_pts = _time_phase(ist_now)
    if phase_key in ("PRE", "CLOSED"):
        res["signal"] = "WAIT"
        res["signal_detail"] = phase_label
        return res
    # Time bonus applies equally to both directions
    res["score_ce"] += time_pts
    res["score_pe"] += time_pts
    res["factors"].append(("Time Phase", phase_label, "NEUTRAL", time_pts))

    # ── Spot momentum ────────────────────────────────────────────────────────
    if spot_history and len(spot_history) >= 2:
        recent_deltas = [spot_history[i] - spot_history[i - 1]
                         for i in range(max(1, len(spot_history) - 4), len(spot_history))]
        up = sum(1 for d in recent_deltas if d > 0)
        dn = sum(1 for d in recent_deltas if d < 0)
        avg = sum(recent_deltas) / len(recent_deltas)
        if avg > 10 or up >= 3:
            res["spot_direction"] = "UP"
            res["score_ce"] += 3
            res["factors"].append(("Spot Momentum", f"Strong UP move avg {avg:+.1f} pts", "BULL", 3))
        elif avg > 3 or up >= 2:
            res["spot_direction"] = "UP"
            res["score_ce"] += 1
            res["factors"].append(("Spot Momentum", f"Leaning UP avg {avg:+.1f} pts", "BULL", 1))
        elif avg < -10 or dn >= 3:
            res["spot_direction"] = "DOWN"
            res["score_pe"] += 3
            res["factors"].append(("Spot Momentum", f"Strong DOWN move avg {avg:+.1f} pts", "BEAR", 3))
        elif avg < -3 or dn >= 2:
            res["spot_direction"] = "DOWN"
            res["score_pe"] += 1
            res["factors"].append(("Spot Momentum", f"Leaning DOWN avg {avg:+.1f} pts", "BEAR", 1))
        else:
            res["factors"].append(("Spot Momentum", f"Choppy / sideways avg {avg:+.1f} pts", "NEUTRAL", 0))

    # ── CE pressure (strikes ABOVE spot — writer short exposure) ─────────────
    above = chain_df[chain_df["Strike"] > spot].copy()
    if not above.empty and above["CE OI"].max() > 0:
        top = above.loc[above["CE OI"].idxmax()]
        ce_s, ce_oi = float(top["Strike"]), int(top["CE OI"])
        prox = (ce_s - spot) / spot * 100
        res["blast_strike_ce"] = ce_s
        res["pressure_oi_ce"]  = ce_oi

        # Proximity score
        if prox < 0.15:
            pp, pn = 5, f"⚡ BLAST ZONE — within {prox:.2f}% of {ce_s:.0f}"
            res["blast_zone"] = True
        elif prox < 0.30:
            pp, pn = 3, f"Very close to {ce_s:.0f} CE ({prox:.2f}%)"
        elif prox < 0.60:
            pp, pn = 2, f"Approaching {ce_s:.0f} CE ({prox:.2f}%, ~{prox/100*spot:.0f} pts)"
        elif prox < 1.00:
            pp, pn = 1, f"Tracking {ce_s:.0f} CE ({prox:.2f}%)"
        else:
            pp, pn = 0, f"Far from {ce_s:.0f} CE ({prox:.2f}%)"
        res["score_ce"] += pp
        res["factors"].append(("CE Proximity", pn, "BULL", pp))

        # OI magnitude
        oi_l = ce_oi / 1e5
        if ce_oi > 5_000_000:   op, on = 3, f"Massive CE wall {oi_l:.1f}L — extreme writer exposure"
        elif ce_oi > 2_000_000: op, on = 2, f"Large CE wall {oi_l:.1f}L"
        elif ce_oi > 500_000:   op, on = 1, f"Moderate CE wall {oi_l:.1f}L"
        else:                   op, on = 0, f"Thin CE OI {oi_l:.1f}L — less blast potential"
        res["score_ce"] += op
        res["factors"].append((f"CE OI @ {ce_s:.0f}", on, "BULL", op))

        # Short covering (OI unwinding = blast fuel 🔥)
        if prev_chain and ce_s in prev_chain:
            delta = ce_oi - prev_chain[ce_s].get("CE OI", ce_oi)
            if delta < -100_000:
                res["score_ce"] += 4
                res["covering_ce"] = True
                res["factors"].append((f"CE Covering @ {ce_s:.0f}", f"🔥 Heavy short covering! OI -{ abs(delta)/1e5:.1f}L → blast fuel", "BULL", 4))
            elif delta < -30_000:
                res["score_ce"] += 2
                res["covering_ce"] = True
                res["factors"].append((f"CE Covering @ {ce_s:.0f}", f"Short covering -{ abs(delta)/1e5:.1f}L", "BULL", 2))
            elif delta > 50_000:
                res["score_ce"] -= 1
                res["factors"].append((f"CE Writing @ {ce_s:.0f}", f"New calls written +{delta/1e5:.1f}L — resistance building", "BEAR", -1))
            else:
                res["factors"].append((f"CE OI Change @ {ce_s:.0f}", f"Stable ({delta/1e5:+.1f}L)", "NEUTRAL", 0))

    # ── PE pressure (strikes BELOW spot — writer short exposure) ─────────────
    below = chain_df[chain_df["Strike"] < spot].copy()
    if not below.empty and below["PE OI"].max() > 0:
        top = below.loc[below["PE OI"].idxmax()]
        pe_s, pe_oi = float(top["Strike"]), int(top["PE OI"])
        prox = (spot - pe_s) / spot * 100
        res["blast_strike_pe"] = pe_s
        res["pressure_oi_pe"]  = pe_oi

        if prox < 0.15:
            pp, pn = 5, f"⚡ BLAST ZONE — within {prox:.2f}% of {pe_s:.0f}"
            res["blast_zone"] = True
        elif prox < 0.30:
            pp, pn = 3, f"Very close to {pe_s:.0f} PE ({prox:.2f}%)"
        elif prox < 0.60:
            pp, pn = 2, f"Approaching {pe_s:.0f} PE ({prox:.2f}%, ~{prox/100*spot:.0f} pts)"
        elif prox < 1.00:
            pp, pn = 1, f"Tracking {pe_s:.0f} PE ({prox:.2f}%)"
        else:
            pp, pn = 0, f"Far from {pe_s:.0f} PE ({prox:.2f}%)"
        res["score_pe"] += pp
        res["factors"].append(("PE Proximity", pn, "BEAR", pp))

        oi_l = pe_oi / 1e5
        if pe_oi > 5_000_000:   op, on = 3, f"Massive PE wall {oi_l:.1f}L — extreme writer exposure"
        elif pe_oi > 2_000_000: op, on = 2, f"Large PE wall {oi_l:.1f}L"
        elif pe_oi > 500_000:   op, on = 1, f"Moderate PE wall {oi_l:.1f}L"
        else:                   op, on = 0, f"Thin PE OI {oi_l:.1f}L — less blast potential"
        res["score_pe"] += op
        res["factors"].append((f"PE OI @ {pe_s:.0f}", on, "BEAR", op))

        if prev_chain and pe_s in prev_chain:
            delta = pe_oi - prev_chain[pe_s].get("PE OI", pe_oi)
            if delta < -100_000:
                res["score_pe"] += 4
                res["covering_pe"] = True
                res["factors"].append((f"PE Covering @ {pe_s:.0f}", f"🔥 Heavy short covering! OI -{abs(delta)/1e5:.1f}L → blast fuel", "BEAR", 4))
            elif delta < -30_000:
                res["score_pe"] += 2
                res["covering_pe"] = True
                res["factors"].append((f"PE Covering @ {pe_s:.0f}", f"Short covering -{abs(delta)/1e5:.1f}L", "BEAR", 2))
            elif delta > 50_000:
                res["score_pe"] -= 1
                res["factors"].append((f"PE Writing @ {pe_s:.0f}", f"New puts written +{delta/1e5:.1f}L — support building", "BULL", -1))
            else:
                res["factors"].append((f"PE OI Change @ {pe_s:.0f}", f"Stable ({delta/1e5:+.1f}L)", "NEUTRAL", 0))

    # ── Nearby PCR ───────────────────────────────────────────────────────────
    tot_ce = chain_df["CE OI"].sum()
    tot_pe = chain_df["PE OI"].sum()
    if tot_ce > 0:
        pcr = tot_pe / tot_ce
        if pcr > 1.4:
            res["score_ce"] += 1
            res["factors"].append(("Nearby PCR", f"{pcr:.2f} — put heavy, CE blast favored", "BULL", 1))
        elif pcr < 0.7:
            res["score_pe"] += 1
            res["factors"].append(("Nearby PCR", f"{pcr:.2f} — call heavy, PE blast favored", "BEAR", 1))
        else:
            res["factors"].append(("Nearby PCR", f"{pcr:.2f} — balanced", "NEUTRAL", 0))

    # ── Signal determination ──────────────────────────────────────────────────
    sce, spe = res["score_ce"], res["score_pe"]
    bz  = res["blast_zone"]
    sd  = res["spot_direction"]
    cce = res["covering_ce"]
    cpe = res["covering_pe"]

    if bz and cce and sce >= 9 and (sd == "UP" or sce > spe + 2):
        res["signal"] = "GAMMA BLAST ↑"
        res["recommended_type"] = "CE"
        res["recommended_strike"] = res["blast_strike_ce"]
    elif bz and cpe and spe >= 9 and (sd == "DOWN" or spe > sce + 2):
        res["signal"] = "GAMMA BLAST ↓"
        res["recommended_type"] = "PE"
        res["recommended_strike"] = res["blast_strike_pe"]
    elif bz and sce >= 7 and sce > spe:
        res["signal"] = "PRE-BLAST ↑ CE"
        res["recommended_type"] = "CE"
        res["recommended_strike"] = res["blast_strike_ce"]
    elif bz and spe >= 7 and spe > sce:
        res["signal"] = "PRE-BLAST ↓ PE"
        res["recommended_type"] = "PE"
        res["recommended_strike"] = res["blast_strike_pe"]
    elif sce >= 6 and sce > spe:
        res["signal"] = "BUILDING ↑"
        res["recommended_type"] = "CE"
        res["recommended_strike"] = res["blast_strike_ce"]
    elif spe >= 6 and spe > sce:
        res["signal"] = "BUILDING ↓"
        res["recommended_type"] = "PE"
        res["recommended_strike"] = res["blast_strike_pe"]
    elif max(sce, spe) >= 4:
        res["signal"] = "WATCH"
    else:
        res["signal"] = "WAIT"

    return res


# ── Main entry ────────────────────────────────────────────────────────────────

def run_gamma_blast_scan(
    api_key:      str,
    access_token: str,
    symbol:       str,
    expiry:       str,
    instr_df:     pd.DataFrame,
    spot_history: list | None = None,
    prev_chain:   dict | None = None,
) -> dict:
    """
    Full scan. Returns dict with chain, scores, signal, trade setup,
    and new_prev_chain for the next call.
    """
    ist_now = datetime.datetime.now(_IST)

    snap = fetch_chain_snapshot(api_key, access_token, symbol, expiry, instr_df)
    if snap is None:
        return {"error": "Failed to fetch option chain — check API credentials"}

    scores = score_gamma_blast(
        snap["chain"], snap["spot"], snap["atm"],
        spot_history=spot_history,
        prev_chain=prev_chain,
        ist_now=ist_now,
        strike_gap=snap["strike_gap"],
    )

    # Build prev_chain for next scan
    new_prev: dict = {}
    for _, row in snap["chain"].iterrows():
        new_prev[row["Strike"]] = {"CE OI": int(row["CE OI"]), "PE OI": int(row["PE OI"])}

    # Entry LTP + expiry-day SL/Target
    ltp_entry = ltp_sl = ltp_target = None
    rt = scores.get("recommended_type")
    rs = scores.get("recommended_strike")
    if rt and rs is not None:
        row = snap["chain"][snap["chain"]["Strike"] == rs]
        if not row.empty:
            ltp_entry = float(row.iloc[0].get(f"{rt} LTP", 0))
            if ltp_entry >= 1:
                ltp_sl     = round(ltp_entry * 0.50, 1)   # 50% stop (expiry = all-or-nothing)
                ltp_target = round(ltp_entry * 3.00, 1)   # 3x target (gamma blast = explosive)

    # OI delta table for display (current vs prev)
    chain = snap["chain"].copy()
    chain["CE ΔOI"] = 0
    chain["PE ΔOI"] = 0
    if prev_chain:
        for i, row in chain.iterrows():
            s = row["Strike"]
            if s in prev_chain:
                chain.at[i, "CE ΔOI"] = int(row["CE OI"]) - prev_chain[s].get("CE OI", int(row["CE OI"]))
                chain.at[i, "PE ΔOI"] = int(row["PE OI"]) - prev_chain[s].get("PE OI", int(row["PE OI"]))

    return {
        "spot":             snap["spot"],
        "atm":              snap["atm"],
        "strike_gap":       snap["strike_gap"],
        "chain":            chain,
        "score_ce":         scores["score_ce"],
        "score_pe":         scores["score_pe"],
        "signal":           scores["signal"],
        "signal_detail":    scores.get("signal_detail", ""),
        "blast_zone":       scores["blast_zone"],
        "blast_strike_ce":  scores["blast_strike_ce"],
        "blast_strike_pe":  scores["blast_strike_pe"],
        "pressure_oi_ce":   scores["pressure_oi_ce"],
        "pressure_oi_pe":   scores["pressure_oi_pe"],
        "covering_ce":      scores["covering_ce"],
        "covering_pe":      scores["covering_pe"],
        "spot_direction":   scores["spot_direction"],
        "recommended_type": rt,
        "recommended_strike": rs,
        "ltp_entry":        ltp_entry,
        "ltp_sl":           ltp_sl,
        "ltp_target":       ltp_target,
        "factors":          scores["factors"],
        "new_prev_chain":   new_prev,
        "ist_now":          ist_now,
    }
