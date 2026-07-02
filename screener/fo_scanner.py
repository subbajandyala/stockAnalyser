from io import StringIO
from typing import Callable

import numpy as np
import pandas as pd
import requests as _req

_KITE_BASE   = "https://api.kite.trade"
_INDEX_NAMES = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50",
                "SENSEX", "BANKEX", "MIDCPNIFTY"}


def _kite_hdrs(api_key: str, access_token: str) -> dict:
    return {"X-Kite-Version": "3", "Authorization": f"token {api_key}:{access_token}"}


def _batch_fetch(url: str, syms: list, hdrs: dict, batch: int = 400) -> dict:
    out: dict = {}
    for i in range(0, len(syms), batch):
        r = _req.get(url, headers=hdrs, params={"i": syms[i : i + batch]}, timeout=30)
        if r.status_code in (401, 403):
            raise RuntimeError(
                "Kite Access Token expired or invalid. "
                "Generate a new token from kite.zerodha.com and re-enter it in the sidebar."
            )
        if r.ok:
            out.update(r.json().get("data", {}))
    return out


def _score_stock(df: pd.DataFrame, spot: float) -> tuple:
    """Return (score, signal, atm, pcr, max_pain)."""
    atm_i    = int((df["Strike"] - spot).abs().values.argmin())
    atm      = float(df.iloc[atm_i]["Strike"])
    total_ce = float(df["CE OI"].sum())
    total_pe = float(df["PE OI"].sum())
    if total_ce == 0:
        return 0, "NEUTRAL ⚖️", atm, 0.0, spot

    pcr = round(total_pe / total_ce, 2)

    # Max Pain
    s_arr = df["Strike"].values
    ce_oi = df["CE OI"].values.astype(float)
    pe_oi = df["PE OI"].values.astype(float)
    pain  = [
        float((ce_oi * np.maximum(0, s - s_arr)).sum() +
              (pe_oi * np.maximum(0, s_arr - s)).sum())
        for s in s_arr
    ]
    max_pain = float(s_arr[int(np.argmin(pain))])
    mp_diff  = (spot - max_pain) / max_pain * 100

    score = 0
    if   pcr >= 1.3: score += 2
    elif pcr >= 1.0: score += 1
    elif pcr <= 0.7: score -= 2
    elif pcr <  1.0: score -= 1

    if   mp_diff < -1.0: score += 2
    elif mp_diff < -0.3: score += 1
    elif mp_diff >  1.0: score -= 2
    elif mp_diff >  0.3: score -= 1

    max_ce_strike = float(df.loc[df["CE OI"].idxmax(), "Strike"])
    max_pe_strike = float(df.loc[df["PE OI"].idxmax(), "Strike"])
    if   spot > max_ce_strike: score += 1
    elif spot < max_pe_strike: score -= 1

    if   score >=  4: signal = "STRONG BUY CE 📈"
    elif score >=  2: signal = "BUY CE 📈"
    elif score <= -4: signal = "STRONG BUY PE 📉"
    elif score <= -2: signal = "BUY PE 📉"
    else:             signal = "NEUTRAL ⚖️"

    return score, signal, atm, pcr, max_pain


def run_fo_scan(
    api_key: str,
    access_token: str,
    progress_cb: Callable[[float, str], None] | None = None,
    max_stocks: int = 200,
) -> pd.DataFrame:
    """
    Scan all NSE F&O stocks for CE/PE option signals via Zerodha Kite Connect.
    Returns DataFrame sorted by signal strength.
    """
    hdrs = _kite_hdrs(api_key, access_token)

    def _cb(pct: float, msg: str) -> None:
        if progress_cb:
            progress_cb(pct, msg)

    # 0. Pre-flight: validate token before doing anything
    _cb(0.01, "Validating Kite token…")
    _ping = _req.get(f"{_KITE_BASE}/user/profile", headers=hdrs, timeout=10)
    if _ping.status_code in (401, 403):
        raise RuntimeError(
            "Kite Access Token is expired or invalid. "
            "Generate a new token from kite.zerodha.com and re-enter it in the sidebar."
        )

    # 1. NFO instrument master
    _cb(0.02, "Downloading F&O instrument list…")
    resp = _req.get(f"{_KITE_BASE}/instruments/NFO", headers=hdrs, timeout=30)
    if resp.status_code in (401, 403):
        raise RuntimeError(
            "Kite Access Token is expired or invalid. "
            "Generate a new token from kite.zerodha.com and re-enter it in the sidebar."
        )
    resp.raise_for_status()
    instr = pd.read_csv(StringIO(resp.text))

    # Validate CSV has expected columns (guards against error JSON being parsed as CSV)
    if "instrument_type" not in instr.columns or len(instr) < 100:
        raise RuntimeError(
            f"NFO instrument list is invalid or empty ({len(instr)} rows). "
            "Kite token may be expired — regenerate it and try again."
        )

    # 2. Options only, nearest expiry that has STOCK (non-index) options
    opts = instr[instr["instrument_type"].isin(["CE", "PE"])].copy()
    opts["expiry_dt"] = pd.to_datetime(opts["expiry"], errors="coerce")
    opts = opts.dropna(subset=["expiry_dt"])      # remove any unparseable dates

    if opts.empty:
        raise RuntimeError(
            f"NFO instruments downloaded ({len(instr)} rows) but no CE/PE options found. "
            "Check that the Kite token is valid and the market is open."
        )

    # Walk expiries from nearest to find one that has actual stock options
    nearest = None
    stock_names = []
    for expiry_dt in sorted(opts["expiry_dt"].unique()):
        _slice = opts[opts["expiry_dt"] == expiry_dt]
        _names = sorted(set(_slice["name"].unique()) - _INDEX_NAMES)
        if _names:
            nearest = expiry_dt
            stock_names = _names
            break

    if nearest is None or not stock_names:
        all_expiries = sorted(opts["expiry_dt"].unique())
        all_names    = sorted(set(opts["name"].unique()) - _INDEX_NAMES)
        raise RuntimeError(
            f"No F&O stock options found across {len(all_expiries)} expiry date(s) in NFO instruments. "
            f"Expiries seen: {[str(e.date()) for e in all_expiries[:5]]}. "
            f"Non-index names found: {len(all_names)}. "
            "Market may be closed, or it is a holiday — stock F&O options are absent from the instrument list."
        )

    expiry_str = nearest.strftime("%d-%b-%Y").upper()
    opts = opts[(opts["expiry_dt"] == nearest) & (opts["name"].isin(stock_names))].copy()

    _cb(0.08, f"Found {len(stock_names)} F&O stocks — fetching spot prices…")

    # 4. Spot prices for all underlyings in one batch
    nse_syms = [f"NSE:{s}" for s in stock_names]
    spots    = _batch_fetch(f"{_KITE_BASE}/quote/ltp", nse_syms, hdrs)
    valid_spots = sum(1 for v in spots.values() if float(v.get("last_price", 0)) > 0)
    if valid_spots == 0:
        raise RuntimeError(
            f"Got 0 valid spot prices for {len(stock_names)} stocks. "
            "Kite token may be expired — generate a new Access Token and re-enter in sidebar."
        )

    # 5. Option quotes — batched
    nfo_syms = ("NFO:" + opts["tradingsymbol"]).tolist()
    total    = len(nfo_syms)
    quotes: dict = {}
    batch_size = 400
    n_batches  = (total + batch_size - 1) // batch_size
    for i, start in enumerate(range(0, total, batch_size)):
        batch = nfo_syms[start : start + batch_size]
        r = _req.get(f"{_KITE_BASE}/quote", headers=hdrs,
                     params={"i": batch}, timeout=30)
        if r.status_code in (401, 403):
            raise RuntimeError(
                "Kite Access Token expired or invalid while fetching option quotes. "
                "Generate a new token from kite.zerodha.com and re-enter it in the sidebar."
            )
        if r.ok:
            quotes.update(r.json().get("data", {}))
        _cb(0.12 + 0.60 * (i + 1) / n_batches,
            f"Fetching option quotes… {min(start + batch_size, total)}/{total}")

    # 6. Analyse each stock
    valid_quotes = sum(1 for q in quotes.values() if int(q.get("oi", 0)) > 0)
    _cb(0.75, f"Analysing signals… ({valid_quotes} instruments with live OI)")
    results = []

    for name in stock_names:
        spot_key  = f"NSE:{name}"
        spot_data = spots.get(spot_key, {})
        spot      = float(spot_data.get("last_price", 0))
        if spot <= 0:
            continue

        stock_opts = opts[opts["name"] == name]
        rows_dict: dict = {}
        for _, row in stock_opts.iterrows():
            ts_key = f"NFO:{row['tradingsymbol']}"
            q      = quotes.get(ts_key, {})
            strike = float(row["strike"])
            itype  = row["instrument_type"]
            if strike not in rows_dict:
                rows_dict[strike] = {
                    "Strike": strike,
                    "CE OI": 0, "PE OI": 0,
                    "CE LTP": 0.0, "PE LTP": 0.0,
                }
            rows_dict[strike][f"{itype} OI"]  = int(q.get("oi", 0))
            rows_dict[strike][f"{itype} LTP"] = float(q.get("last_price", 0))

        if not rows_dict:
            continue

        df = (pd.DataFrame(list(rows_dict.values()))
                .sort_values("Strike")
                .reset_index(drop=True))

        score, signal, atm, pcr, max_pain = _score_stock(df, spot)
        # Include all stocks — user can filter in the UI

        is_ce  = "CE" in signal
        strong = "STRONG" in signal
        strikes    = sorted(df["Strike"].unique())
        strike_gap = min(b - a for a, b in zip(strikes, strikes[1:])) if len(strikes) > 1 else 1
        rec_strike = atm if strong else (atm + strike_gap if is_ce else atm - strike_gap)

        row_df = df[df["Strike"] == rec_strike]
        if row_df.empty:
            rec_strike = atm
            row_df = df[df["Strike"] == atm]

        ltp_col = "CE LTP" if is_ce else "PE LTP"
        ltp     = float(row_df.iloc[0][ltp_col]) if not row_df.empty else 0.0
        if ltp <= 0.5 and rec_strike != atm:
            rec_strike = atm
            row_df = df[df["Strike"] == atm]
            ltp = float(row_df.iloc[0][ltp_col]) if not row_df.empty else 0.0

        sl     = round(ltp * 0.65, 1) if ltp > 0.5 else None
        target = round(ltp * 1.65, 1) if ltp > 0.5 else None
        rr     = round((target - ltp) / (ltp - sl), 1) if (ltp > 0.5 and sl) else None

        mp_diff = round((spot - max_pain) / max_pain * 100, 2)

        results.append({
            "Symbol":      name,
            "Spot":        round(spot, 2),
            "ATM":         int(atm),
            "PCR":         pcr,
            "Max Pain":    int(max_pain),
            "MP Diff %":   mp_diff,
            "Score":       score,
            "Signal":      signal,
            "Rec Strike":  int(rec_strike),
            "Type":        "CE" if is_ce else "PE",
            "LTP":         round(ltp, 2) if ltp > 0.5 else None,
            "SL":          sl,
            "Target":      target,
            "R:R":         rr,
            "Expiry":      expiry_str,
            "NSE_Symbol":  f"{name}.NS",
        })

    if not results:
        _cb(1.0, "⚠️ No results — check that Kite token is valid and market is open")
        return pd.DataFrame()

    _cb(0.98, f"Done! Found signals for {len(results)} stocks")

    _order = {
        "STRONG BUY CE 📈": 0,
        "BUY CE 📈":        1,
        "BUY PE 📉":        2,
        "STRONG BUY PE 📉": 3,
        "NEUTRAL ⚖️":       4,
    }
    df_out = pd.DataFrame(results)
    df_out["_rank"] = df_out["Signal"].map(_order).fillna(4)
    df_out = (df_out.sort_values(["_rank", "Score"], ascending=[True, False])
                    .drop(columns=["_rank"])
                    .reset_index(drop=True))
    return df_out
