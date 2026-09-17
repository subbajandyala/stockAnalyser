"""
Expiry Drama — identifies options with 10x-30x potential on expiry day.

Logic
-----
On expiry day, option writers defend key strikes until the last hour.
When the market breaches a heavily-written strike in the final stretch,
cheap OTM options near that strike can explode 10x-30x in minutes.

This screen:
1. Fetches live Kite option chain for the nearest expiry.
2. Calculates Max Pain, OI Magnet, PCR, and market bias.
3. Identifies "drama candidates" — cheap OTM options positioned between
   the current spot and max pain, backed by OI + volume signals.
4. Scores each candidate 0-100 and classifies them as 🚀/💣/⚡.
"""

from __future__ import annotations

import datetime
from io import StringIO

import numpy as np
import pandas as pd
import requests

_KITE_BASE = "https://api.kite.trade"
_IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

# ── Instrument config ─────────────────────────────────────────────────────────
# expiry_weekday: 0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri
_INSTRUMENT_CONFIG: dict[str, dict] = {
    "NIFTY":      {"exchange": "NFO", "lot": 75,  "tick": 50,  "spot_sym": "NSE:NIFTY 50",     "expiry_weekday": 3},
    "BANKNIFTY":  {"exchange": "NFO", "lot": 30,  "tick": 100, "spot_sym": "NSE:NIFTY BANK",    "expiry_weekday": 2},
    "FINNIFTY":   {"exchange": "NFO", "lot": 40,  "tick": 50,  "spot_sym": "NSE:NIFTY FIN SERVICE", "expiry_weekday": 1},
    "MIDCPNIFTY": {"exchange": "NFO", "lot": 75,  "tick": 25,  "spot_sym": "NSE:NIFTY MID SELECT", "expiry_weekday": 0},
    "SENSEX":     {"exchange": "BFO", "lot": 10,  "tick": 100, "spot_sym": "BSE:SENSEX",         "expiry_weekday": 4},
}

ALL_INSTRUMENTS = list(_INSTRUMENT_CONFIG.keys())


def _kite_hdrs(api_key: str, access_token: str) -> dict:
    return {
        "X-Kite-Version": "3",
        "Authorization":  f"token {api_key}:{access_token}",
    }


def get_today_expiry_instruments() -> list[str]:
    """Return instruments that expire today (by weekday)."""
    today_wd = datetime.datetime.now(_IST).weekday()
    return [sym for sym, cfg in _INSTRUMENT_CONFIG.items()
            if cfg["expiry_weekday"] == today_wd]


def _calc_max_pain(chain_df: pd.DataFrame) -> float:
    s_arr = chain_df["Strike"].values
    ce_oi = chain_df["CE OI"].values.astype(float)
    pe_oi = chain_df["PE OI"].values.astype(float)
    pain = [
        float((ce_oi * np.maximum(0, s - s_arr)).sum() +
              (pe_oi * np.maximum(0, s_arr - s)).sum())
        for s in s_arr
    ]
    return float(s_arr[int(np.argmin(pain))])


def _calc_pcr(chain_df: pd.DataFrame) -> float:
    ce_oi = chain_df["CE OI"].sum()
    return round(chain_df["PE OI"].sum() / ce_oi, 2) if ce_oi > 0 else 0.0


def _calc_oi_magnet(chain_df: pd.DataFrame) -> float:
    chain_df = chain_df.copy()
    chain_df["Combined OI"] = chain_df["CE OI"] + chain_df["PE OI"]
    return float(chain_df.loc[chain_df["Combined OI"].idxmax(), "Strike"])


def _score_candidate(
    row: pd.Series,
    spot: float,
    max_pain: float,
    oi_magnet: float,
    pcr: float,
    total_ce_oi: float,
    total_pe_oi: float,
    opt_type: str,
) -> tuple[int, list[str]]:
    """
    Score a candidate option 0-100 for 10x-30x potential.
    Returns (score, tags).
    """
    score = 0
    tags: list[str] = []

    strike  = float(row["Strike"])
    ltp     = float(row[f"{opt_type} LTP"])
    oi      = float(row[f"{opt_type} OI"])
    vol     = float(row[f"{opt_type} Vol"])
    chng_oi = float(row.get(f"{opt_type} Chng OI", 0))
    gap_to_pain = abs(max_pain - spot)

    # 1. Low premium → high multiplier potential (max 30 pts)
    if 0.50 <= ltp <= 5:
        score += 30; tags.append("Dirt Cheap")
    elif 5 < ltp <= 15:
        score += 22; tags.append("Cheap")
    elif 15 < ltp <= 30:
        score += 12; tags.append("Affordable")
    else:
        return 0, []   # too expensive for 10x

    # 2. Between spot and max pain — in the direction the market is being pulled (max 20 pts)
    if opt_type == "CE":
        in_path = spot <= strike <= max_pain  # market needs to rally past this strike
    else:
        in_path = max_pain <= strike <= spot  # market needs to fall past this strike

    if in_path:
        score += 20; tags.append("In Max Pain Path")
    else:
        score -= 10  # wrong direction, penalise

    # 3. OI at this strike — writers defending = explosive if breached (max 15 pts)
    if oi > 100_000:
        score += 15; tags.append("High OI Wall")
    elif oi > 50_000:
        score += 10; tags.append("Medium OI Wall")
    elif oi > 20_000:
        score += 5

    # 4. OI change today — fresh writing = smart money positioning (max 10 pts)
    if chng_oi > 50_000:
        score += 10; tags.append("Fresh Writing")
    elif chng_oi > 20_000:
        score += 6
    elif chng_oi > 0:
        score += 2

    # 5. Volume surge today (max 10 pts)
    vol_oi_ratio = vol / oi if oi > 0 else 0
    if vol_oi_ratio > 0.5:
        score += 10; tags.append("Volume Surge")
    elif vol_oi_ratio > 0.2:
        score += 6
    elif vol_oi_ratio > 0.05:
        score += 2

    # 6. PCR alignment with direction (max 10 pts)
    if opt_type == "CE" and pcr > 1.3:
        score += 10; tags.append("PCR Bullish")
    elif opt_type == "PE" and pcr < 0.7:
        score += 10; tags.append("PCR Bearish")
    elif (opt_type == "CE" and pcr >= 0.9) or (opt_type == "PE" and pcr <= 1.1):
        score += 4

    # 7. Max pain gap — bigger gap = more drama needed = more explosive potential (max 5 pts)
    if gap_to_pain > 300:
        score += 5; tags.append("Big Gap")
    elif gap_to_pain > 150:
        score += 3

    # 8. OI magnet proximity — if strike is near the OI magnet (max 5 pts)
    if abs(strike - oi_magnet) < 200:
        score += 5; tags.append("OI Magnet Zone")

    return max(0, score), tags


def _classify(score: int, ltp: float, opt_type: str) -> str:
    """Return an emoji label for the candidate."""
    if score >= 70:
        return "🚀 Top Pick"
    elif score >= 55:
        return "💣 Strong"
    elif score >= 40:
        return "⚡ Watch"
    else:
        return "👀 Speculative"


def run_expiry_drama(
    api_key: str,
    access_token: str,
    symbol: str = "NIFTY",
) -> dict:
    """
    Full expiry-drama analysis for the given symbol.

    Returns
    -------
    spot, expiry, max_pain, oi_magnet, pcr, bias, gap_pts, direction,
    chain_df       — full option chain (nearest expiry)
    candidates_df  — scored drama candidates
    is_expiry_day  — bool: is today the actual expiry day?
    """
    cfg  = _INSTRUMENT_CONFIG.get(symbol, _INSTRUMENT_CONFIG["NIFTY"])
    exch = cfg["exchange"]
    hdrs = _kite_hdrs(api_key, access_token)

    # 1. Instruments master
    resp = requests.get(f"{_KITE_BASE}/instruments/{exch}", headers=hdrs, timeout=30)
    resp.raise_for_status()
    instr = pd.read_csv(StringIO(resp.text))

    instr["expiry_dt"] = pd.to_datetime(instr["expiry"], errors="coerce")

    opts = instr[
        (instr["name"] == symbol) &
        (instr["instrument_type"].isin(["CE", "PE"]))
    ].copy()

    if opts.empty:
        raise RuntimeError(f"No {symbol} options found in {exch} instruments master.")

    # 2. Nearest expiry
    nearest_expiry = opts["expiry_dt"].min()
    opts = opts[opts["expiry_dt"] == nearest_expiry].copy()
    expiry_label = nearest_expiry.strftime("%d %b %Y (%A)")

    # Is today the expiry day?
    today = datetime.datetime.now(_IST).date()
    is_expiry_day = (nearest_expiry.date() == today)

    # 3. Spot price
    ltp_resp = requests.get(
        f"{_KITE_BASE}/quote/ltp", headers=hdrs,
        params={"i": cfg["spot_sym"]}, timeout=10,
    )
    ltp_resp.raise_for_status()
    spot = float(ltp_resp.json()["data"][cfg["spot_sym"]]["last_price"])

    # 4. Live quotes in batches of 400
    syms = (exch + ":" + opts["tradingsymbol"]).tolist()
    quotes: dict = {}
    for i in range(0, len(syms), 400):
        q_resp = requests.get(
            f"{_KITE_BASE}/quote", headers=hdrs,
            params={"i": syms[i: i + 400]}, timeout=30,
        )
        if q_resp.ok:
            quotes.update(q_resp.json().get("data", {}))

    # 5. Build chain DataFrame
    rows: dict = {}
    for _, row in opts.iterrows():
        strike  = float(row["strike"])
        itype   = row["instrument_type"]
        key     = f"{exch}:{row['tradingsymbol']}"
        q       = quotes.get(key, {})
        if strike not in rows:
            rows[strike] = {
                "Strike":      strike,
                "CE OI":       0, "CE Chng OI": 0, "CE Vol": 0, "CE LTP": 0.0,
                "PE OI":       0, "PE Chng OI": 0, "PE Vol": 0, "PE LTP": 0.0,
            }
        rows[strike][f"{itype} OI"]      = int(q.get("oi", 0))
        rows[strike][f"{itype} Chng OI"] = int(q.get("oi_day_change", 0))
        rows[strike][f"{itype} Vol"]     = int(q.get("volume", 0))
        rows[strike][f"{itype} LTP"]     = float(q.get("last_price", 0.0))

    chain_df = (pd.DataFrame(list(rows.values()))
                .sort_values("Strike")
                .reset_index(drop=True))

    # 6. Max Pain, PCR, OI magnet, ATM
    max_pain  = _calc_max_pain(chain_df)
    pcr       = _calc_pcr(chain_df)
    oi_magnet = _calc_oi_magnet(chain_df)
    atm       = float(chain_df.loc[(chain_df["Strike"] - spot).abs().idxmin(), "Strike"])
    gap_pts   = max_pain - spot   # positive → market must rally; negative → must fall
    direction = "▲ RALLY" if gap_pts > 0 else "▼ FALL"

    # Market bias
    if pcr > 1.3:
        bias = "Bullish"
    elif pcr < 0.7:
        bias = "Bearish"
    else:
        bias = "Neutral"

    # 7. Candidate scoring — only near-ATM strikes (±10 strikes)
    tick  = cfg["tick"]
    total_ce_oi = float(chain_df["CE OI"].sum())
    total_pe_oi = float(chain_df["PE OI"].sum())

    atm_range = 15 * tick
    near_df = chain_df[abs(chain_df["Strike"] - spot) <= atm_range].copy()

    candidates = []
    for _, row in near_df.iterrows():
        strike = float(row["Strike"])
        for opt_type in ("CE", "PE"):
            ltp = float(row[f"{opt_type} LTP"])
            if ltp < 0.50 or ltp > 30:
                continue

            # Only OTM options
            if opt_type == "CE" and strike <= spot:
                continue
            if opt_type == "PE" and strike >= spot:
                continue

            score, tags = _score_candidate(
                row, spot, max_pain, oi_magnet, pcr,
                total_ce_oi, total_pe_oi, opt_type,
            )
            if score < 35:
                continue

            otm_pts = abs(strike - spot)
            multiplier_low  = max(1.0, 50.0 / ltp)    # if premium goes to ₹50
            multiplier_high = max(1.0, 200.0 / ltp)   # if premium goes to ₹200

            # Expiry value if market closes at max pain
            if opt_type == "CE":
                exit_val = max(0.0, max_pain - strike)
            else:
                exit_val = max(0.0, strike - max_pain)
            pain_mult = round(exit_val / ltp, 1) if ltp > 0 else 0.0

            candidates.append({
                "Type":           opt_type,
                "Strike":         int(strike),
                "LTP":            round(ltp, 2),
                "OTM Dist":       int(otm_pts),
                "OI":             int(row[f"{opt_type} OI"]),
                "Vol":            int(row[f"{opt_type} Vol"]),
                "Chng OI":        int(row.get(f"{opt_type} Chng OI", 0)),
                "Score":          score,
                "Label":          _classify(score, ltp, opt_type),
                "Tags":           ", ".join(tags) if tags else "—",
                "If @ Max Pain":  round(exit_val, 2),
                "Pain Mult":      pain_mult,
                "Mult Low":       round(multiplier_low, 1),
                "Mult High":      round(multiplier_high, 1),
            })

    candidates_df = pd.DataFrame(candidates) if candidates else pd.DataFrame()
    if not candidates_df.empty:
        candidates_df = (candidates_df
                         .sort_values("Score", ascending=False)
                         .reset_index(drop=True))

    # CE and PE walls (for resistance/support display)
    ce_wall = float(chain_df.loc[chain_df["CE OI"].idxmax(), "Strike"]) if not chain_df.empty else 0
    pe_wall = float(chain_df.loc[chain_df["PE OI"].idxmax(), "Strike"]) if not chain_df.empty else 0

    return {
        "spot":          spot,
        "expiry":        expiry_label,
        "expiry_dt":     nearest_expiry,
        "is_expiry_day": is_expiry_day,
        "max_pain":      max_pain,
        "oi_magnet":     oi_magnet,
        "pcr":           pcr,
        "atm":           atm,
        "bias":          bias,
        "gap_pts":       gap_pts,
        "direction":     direction,
        "ce_wall":       ce_wall,
        "pe_wall":       pe_wall,
        "chain_df":      chain_df,
        "candidates_df": candidates_df,
    }
