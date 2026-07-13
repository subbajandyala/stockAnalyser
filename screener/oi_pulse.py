"""
Intraday OI Pulse Scanner
Reads live option-chain OI build-up / unwinding to detect intraday directional bias.
Works on any trading day (not just expiry) across all market hours.

Signal hierarchy:
  STRONG BUY CE  — strong bullish OI flow (CE score ≥ 7, CE > PE + 2)
  BUY CE         — moderate bullish bias  (CE score ≥ 5, CE > PE)
  STRONG BUY PE  — strong bearish OI flow
  BUY PE         — moderate bearish bias
  WATCH          — setup aligning, no conviction yet
  WAIT           — time gate blocked or no clear bias
"""

import datetime
import pandas as pd

from .gamma_blast import fetch_chain_snapshot   # reuse: fetch spot + ±n strikes from Kite

_IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
_MCX_SYMBOLS = {"CRUDEOIL", "GOLD", "SILVER"}


# ── Session time phases ───────────────────────────────────────────────────────

def _time_phase(ist_now: datetime.datetime, symbol: str = "") -> tuple[str, str, int]:
    """Return (phase_key, phase_label, bonus_pts). bonus = -99 → block signal."""
    m = ist_now.hour * 60 + ist_now.minute

    # MCX commodities trade 9:00 AM – 11:30 PM IST
    if symbol in _MCX_SYMBOLS:
        if m < 9 * 60:
            return "PRE",    "MCX market not open yet (opens 9:00 AM IST)",  -99
        if m >= 23 * 60:
            return "CLOSED", "MCX market closed (after 11:00 PM IST)",       -99
        if m < 9 * 60 + 15:
            return "MCX_OPEN", "MCX opening 9:00–9:15 AM — OI settling",     0
        if 9 * 60 + 15 <= m < 15 * 60 + 30:
            return "MCX_EQUITY", "MCX + equity session overlap — high activity", 1
        return "MCX_EVE",  f"MCX evening session ({ist_now.strftime('%H:%M')} IST)", 1

    # Equity / index options (NFO / BFO)
    if m < 9 * 60 + 15:
        return "PRE",        "Market not open yet",                         -99
    if m < 9 * 60 + 30:
        return "OPEN",       "9:15–9:30 opening noise — OI not settled",    -99
    if m < 10 * 60 + 30:
        return "MORNING",    "Morning session 9:30–10:30",                    0
    if m < 12 * 60:
        return "MID_MORN",   "Late morning 10:30–12:00",                      1
    if m < 13 * 60 + 30:
        return "MIDDAY",     "Midday lull 12:00–1:30 PM — lower conviction",  0
    if m < 14 * 60 + 30:
        return "AFTERNOON",  "Afternoon session 1:30–2:30 PM",                1
    if m < 15 * 60:
        return "POWER",      "Power hour 2:30–3:00 PM — high conviction",     2
    if m < 15 * 60 + 15:
        return "CLOSING",    "Final 15 min 3:00–3:15 PM",                     1
    return "CLOSED",         "Market closed after 3:15 PM",                  -99


# ── Scoring ───────────────────────────────────────────────────────────────────

def score_oi_pulse(
    chain_df: pd.DataFrame,
    spot: float,
    atm: float,
    spot_history: list | None = None,
    prev_chain: dict | None = None,
    ist_now: datetime.datetime | None = None,
    symbol: str = "",
) -> dict:
    if ist_now is None:
        ist_now = datetime.datetime.now(_IST)

    res: dict = {
        "score_ce": 0, "score_pe": 0,
        "signal": "WAIT", "signal_detail": "",
        "spot_direction": "FLAT",
        "top_ce_strike": None, "top_pe_strike": None,
        "ce_oi_wall": 0, "pe_oi_wall": 0,
        "covering_ce": False, "covering_pe": False,
        "recommended_type": None, "recommended_strike": None,
        "factors": [],
    }

    # ── Time gate ────────────────────────────────────────────────────────────
    phase_key, phase_label, time_pts = _time_phase(ist_now, symbol)
    if time_pts == -99:
        res["signal"] = "WAIT"
        res["signal_detail"] = phase_label
        return res
    if time_pts > 0:
        res["score_ce"] += time_pts
        res["score_pe"] += time_pts
    res["factors"].append(("Session", phase_label, "NEUTRAL", time_pts if time_pts > 0 else 0))

    # ── Spot momentum (pct-based so it works for NIFTY ≈24k and SENSEX ≈80k) ─
    if spot_history and len(spot_history) >= 2:
        recent = [spot_history[i] - spot_history[i - 1]
                  for i in range(max(1, len(spot_history) - 4), len(spot_history))]
        up  = sum(1 for d in recent if d > 0)
        dn  = sum(1 for d in recent if d < 0)
        avg = sum(recent) / len(recent)
        pct = avg / spot * 100

        if pct > 0.10 or up >= 3:
            res["spot_direction"] = "UP"
            res["score_ce"] += 3
            res["factors"].append(("Spot Momentum", f"Strong UP avg {avg:+.1f} pts ({pct:+.3f}%)", "BULL", 3))
        elif pct > 0.03 or up >= 2:
            res["spot_direction"] = "UP"
            res["score_ce"] += 1
            res["factors"].append(("Spot Momentum", f"Leaning UP avg {avg:+.1f} pts", "BULL", 1))
        elif pct < -0.10 or dn >= 3:
            res["spot_direction"] = "DOWN"
            res["score_pe"] += 3
            res["factors"].append(("Spot Momentum", f"Strong DOWN avg {avg:+.1f} pts ({pct:+.3f}%)", "BEAR", 3))
        elif pct < -0.03 or dn >= 2:
            res["spot_direction"] = "DOWN"
            res["score_pe"] += 1
            res["factors"].append(("Spot Momentum", f"Leaning DOWN avg {avg:+.1f} pts", "BEAR", 1))
        else:
            res["factors"].append(("Spot Momentum", f"Choppy avg {avg:+.1f} pts", "NEUTRAL", 0))

    # ── Adaptive OI thresholds (% of strike's own OI so it works for any instrument) ──
    # Heavy unwind: >15% OI drop  |  Moderate: >5%
    # Heavy buildup: >15% increase |  Moderate: >5%
    # Static wall (first scan): strike holds >25% of its side's total chain OI
    tot_ce_chain = max(chain_df["CE OI"].sum(), 1)
    tot_pe_chain = max(chain_df["PE OI"].sum(), 1)

    def _oi_delta_score(oi_now: int, prev_oi: int) -> tuple[int, str]:
        """Returns (score_delta, label). Positive = bullish (CE unwind / PE build)."""
        if prev_oi <= 0:
            return 0, "stable"
        pct = (oi_now - prev_oi) / prev_oi * 100
        abs_d = abs(oi_now - prev_oi)
        min_abs = max(10, int(prev_oi * 0.01))   # at least 1% absolute change to matter
        if pct < -15 and abs_d >= min_abs:
            return 4, f"🔥 -{abs(pct):.0f}% OI drop"
        if pct < -5 and abs_d >= min_abs:
            return 2, f"-{abs(pct):.0f}% OI drop"
        if pct > 15 and abs_d >= min_abs:
            return -2, f"+{pct:.0f}% OI buildup"
        if pct > 5 and abs_d >= min_abs:
            return -1, f"+{pct:.0f}% OI buildup"
        return 0, f"{pct:+.1f}% (stable)"

    def _fmt_oi(oi: int) -> str:
        if oi >= 100_000: return f"{oi/1e5:.1f}L"
        if oi >= 1_000:   return f"{oi/1_000:.1f}K"
        return str(oi)

    # ── CE Wall — highest OI calls ABOVE spot ─────────────────────────────────
    above = chain_df[chain_df["Strike"] > spot].copy()
    if not above.empty and above["CE OI"].max() > 0:
        top   = above.loc[above["CE OI"].idxmax()]
        ce_s  = float(top["Strike"])
        ce_oi = int(top["CE OI"])
        res["top_ce_strike"] = ce_s
        res["ce_oi_wall"]    = ce_oi

        if prev_chain and ce_s in prev_chain:
            prev_ce = prev_chain[ce_s].get("CE OI", ce_oi)
            sc, lbl = _oi_delta_score(ce_oi, prev_ce)
            pct_chg = (ce_oi - prev_ce) / max(prev_ce, 1) * 100
            if sc > 0:       # unwinding → bullish
                res["score_ce"] += sc
                res["covering_ce"] = True
                res["factors"].append((f"CE Unwinding @ {ce_s:.0f}",
                    f"{lbl} → resistance weakening", "BULL", sc))
            elif sc < 0:     # buildup → bearish
                res["score_pe"] += abs(sc)
                res["factors"].append((f"CE Buildup @ {ce_s:.0f}",
                    f"{lbl} → resistance building", "BEAR", abs(sc)))
            else:
                res["factors"].append((f"CE OI @ {ce_s:.0f}",
                    f"{_fmt_oi(ce_oi)} ({pct_chg:+.1f}% change — stable)", "NEUTRAL", 0))
        else:
            wall_pct = ce_oi / tot_ce_chain * 100
            if wall_pct > 25:
                res["score_pe"] += 1
                res["factors"].append((f"CE Wall @ {ce_s:.0f}",
                    f"{_fmt_oi(ce_oi)} ({wall_pct:.0f}% of chain CE OI) — dominant resistance", "BEAR", 1))
            else:
                res["factors"].append((f"CE Wall @ {ce_s:.0f}",
                    f"{_fmt_oi(ce_oi)} ({wall_pct:.0f}% of chain) — scan again to detect changes", "NEUTRAL", 0))

    # ── PE Wall — highest OI puts BELOW spot ──────────────────────────────────
    below = chain_df[chain_df["Strike"] < spot].copy()
    if not below.empty and below["PE OI"].max() > 0:
        top   = below.loc[below["PE OI"].idxmax()]
        pe_s  = float(top["Strike"])
        pe_oi = int(top["PE OI"])
        res["top_pe_strike"] = pe_s
        res["pe_oi_wall"]    = pe_oi

        if prev_chain and pe_s in prev_chain:
            prev_pe = prev_chain[pe_s].get("PE OI", pe_oi)
            sc, lbl = _oi_delta_score(pe_oi, prev_pe)
            pct_chg = (pe_oi - prev_pe) / max(prev_pe, 1) * 100
            if sc > 0:       # unwinding → bearish (support crumbling)
                res["score_pe"] += sc
                res["covering_pe"] = True
                res["factors"].append((f"PE Unwinding @ {pe_s:.0f}",
                    f"{lbl} → support weakening", "BEAR", sc))
            elif sc < 0:     # buildup → bullish (support building)
                res["score_ce"] += abs(sc)
                res["factors"].append((f"PE Buildup @ {pe_s:.0f}",
                    f"{lbl} → support building", "BULL", abs(sc)))
            else:
                res["factors"].append((f"PE OI @ {pe_s:.0f}",
                    f"{_fmt_oi(pe_oi)} ({pct_chg:+.1f}% change — stable)", "NEUTRAL", 0))
        else:
            wall_pct = pe_oi / tot_pe_chain * 100
            if wall_pct > 25:
                res["score_ce"] += 1
                res["factors"].append((f"PE Wall @ {pe_s:.0f}",
                    f"{_fmt_oi(pe_oi)} ({wall_pct:.0f}% of chain PE OI) — dominant support", "BULL", 1))
            else:
                res["factors"].append((f"PE Wall @ {pe_s:.0f}",
                    f"{_fmt_oi(pe_oi)} ({wall_pct:.0f}% of chain) — scan again to detect changes", "NEUTRAL", 0))

    # ── PCR (nearby strikes) ──────────────────────────────────────────────────
    tot_ce = chain_df["CE OI"].sum()
    tot_pe = chain_df["PE OI"].sum()
    if tot_ce > 0:
        pcr = tot_pe / tot_ce
        if pcr > 1.3:
            res["score_ce"] += 1
            res["factors"].append(("PCR", f"{pcr:.2f} — put heavy → bullish bias", "BULL", 1))
        elif pcr < 0.7:
            res["score_pe"] += 1
            res["factors"].append(("PCR", f"{pcr:.2f} — call heavy → bearish bias", "BEAR", 1))
        else:
            res["factors"].append(("PCR", f"{pcr:.2f} — balanced", "NEUTRAL", 0))

    # ── Signal determination ──────────────────────────────────────────────────
    sce, spe = res["score_ce"], res["score_pe"]

    # MCX commodities (CRUDEOIL/GOLD/SILVER) have lower absolute OI than equity
    # indices so use lower thresholds to avoid under-calling real moves
    _is_mcx      = symbol in _MCX_SYMBOLS
    strong_thresh = 6 if _is_mcx else 7
    buy_thresh    = 4 if _is_mcx else 5
    watch_thresh  = 2 if _is_mcx else 3

    if sce >= strong_thresh and sce > spe + 2:
        res["signal"] = "STRONG BUY CE"
        res["recommended_type"]   = "CE"
        res["recommended_strike"] = atm
    elif sce >= buy_thresh and sce > spe:
        res["signal"] = "BUY CE"
        res["recommended_type"]   = "CE"
        res["recommended_strike"] = atm
    elif spe >= strong_thresh and spe > sce + 2:
        res["signal"] = "STRONG BUY PE"
        res["recommended_type"]   = "PE"
        res["recommended_strike"] = atm
    elif spe >= buy_thresh and spe > sce:
        res["signal"] = "BUY PE"
        res["recommended_type"]   = "PE"
        res["recommended_strike"] = atm
    elif max(sce, spe) >= watch_thresh:
        res["signal"] = "WATCH"
    else:
        res["signal"] = "WAIT"

    return res


# ── Main entry ────────────────────────────────────────────────────────────────

def run_oi_pulse_scan(
    api_key:      str,
    access_token: str,
    symbol:       str,
    expiry:       str,
    instr_df,
    spot_history: list | None = None,
    prev_chain:   dict | None = None,
) -> dict:
    """Full scan. Returns signal dict + new_prev_chain for next call."""
    ist_now = datetime.datetime.now(_IST)

    snap = fetch_chain_snapshot(api_key, access_token, symbol, expiry, instr_df)
    if snap is None:
        return {"error": "Failed to fetch option chain — check API credentials"}

    scores = score_oi_pulse(
        snap["chain"], snap["spot"], snap["atm"],
        spot_history=spot_history,
        prev_chain=prev_chain,
        ist_now=ist_now,
        symbol=symbol,
    )

    # Build prev_chain for next scan
    new_prev: dict = {}
    for _, row in snap["chain"].iterrows():
        new_prev[row["Strike"]] = {"CE OI": int(row["CE OI"]), "PE OI": int(row["PE OI"])}

    # Entry LTP + intraday SL/Target (30% SL, 2× target)
    ltp_entry = ltp_sl = ltp_target = None
    rt = scores.get("recommended_type")
    rs = scores.get("recommended_strike")
    if rt and rs is not None:
        row = snap["chain"][snap["chain"]["Strike"] == rs]
        if not row.empty:
            ltp_entry = float(row.iloc[0].get(f"{rt} LTP", 0))
            if ltp_entry >= 1:
                ltp_sl     = round(ltp_entry * 0.70, 1)   # 30% SL
                ltp_target = round(ltp_entry * 2.00, 1)   # 2× target

    # OI delta chain for display
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
        "spot":              snap["spot"],
        "atm":               snap["atm"],
        "strike_gap":        snap["strike_gap"],
        "chain":             chain,
        "score_ce":          scores["score_ce"],
        "score_pe":          scores["score_pe"],
        "signal":            scores["signal"],
        "signal_detail":     scores.get("signal_detail", ""),
        "spot_direction":    scores["spot_direction"],
        "top_ce_strike":     scores["top_ce_strike"],
        "top_pe_strike":     scores["top_pe_strike"],
        "ce_oi_wall":        scores["ce_oi_wall"],
        "pe_oi_wall":        scores["pe_oi_wall"],
        "covering_ce":       scores["covering_ce"],
        "covering_pe":       scores["covering_pe"],
        "recommended_type":  rt,
        "recommended_strike": rs,
        "ltp_entry":         ltp_entry,
        "ltp_sl":            ltp_sl,
        "ltp_target":        ltp_target,
        "factors":           scores["factors"],
        "new_prev_chain":    new_prev,
        "ist_now":           ist_now,
    }
