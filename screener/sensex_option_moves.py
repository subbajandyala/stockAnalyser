"""
Sensex Expiry Option Moves Analyser
Finds options that moved 500%+ in the 2:15–3:15 PM window on each Friday expiry.

For already-expired options (prior weeks), prices are estimated using an
expiry-day model: intrinsic value dominates with tiny time premium at 2:15 PM.
For the current-week expiry still in the BFO instruments master (Kite required),
actual historical candle data is fetched.
"""

import datetime
import math
import time
from io import StringIO

import numpy as np
import pandas as pd
import requests
import yfinance as yf

_KITE_BASE  = "https://api.kite.trade"
_SENSEX_YF  = "^BSESN"
_IST        = "Asia/Kolkata"

# Known Kite instrument token for BSE SENSEX index (used for historical candles)
_SENSEX_KITE_TOKEN = 260105


# ── Date helpers ──────────────────────────────────────────────────────────────

def get_sensex_expiry_fridays(n: int = 5) -> list:
    """Return the last n Fridays (most recent first), all strictly before today."""
    today = datetime.date.today()
    fridays = []
    d = today - datetime.timedelta(days=1)
    while len(fridays) < n:
        if d.weekday() == 4:       # 4 = Friday
            fridays.append(d)
        d -= datetime.timedelta(days=1)
    return fridays


# ── Index data fetching ───────────────────────────────────────────────────────

def _kite_headers(api_key: str, access_token: str) -> dict:
    return {
        "X-Kite-Version": "3",
        "Authorization":  f"token {api_key}:{access_token}",
    }


def _fetch_sensex_via_kite(date: datetime.date, api_key: str, access_token: str) -> pd.DataFrame:
    """5-min SENSEX candles via Kite historical API."""
    date_str = date.strftime("%Y-%m-%d")
    resp = requests.get(
        f"{_KITE_BASE}/instruments/historical/{_SENSEX_KITE_TOKEN}/5minute",
        headers=_kite_headers(api_key, access_token),
        params={"from": f"{date_str} 09:15:00", "to": f"{date_str} 15:30:00"},
        timeout=30,
    )
    resp.raise_for_status()
    candles = resp.json().get("data", {}).get("candles", [])
    if not candles:
        return pd.DataFrame()
    df = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    if df["timestamp"].dt.tz is None:
        df["timestamp"] = df["timestamp"].dt.tz_localize(_IST)
    else:
        df["timestamp"] = df["timestamp"].dt.tz_convert(_IST)
    return df.set_index("timestamp")


def _fetch_sensex_via_yf(date: datetime.date) -> pd.DataFrame:
    """5-min SENSEX candles via yfinance."""
    start = date.strftime("%Y-%m-%d")
    end   = (date + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    df = yf.download(_SENSEX_YF, start=start, end=end, interval="5m",
                     progress=False, auto_adjust=True)
    if df.empty:
        return df
    # Flatten potential MultiIndex columns
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    # Normalise to IST
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert(_IST)
    # Normalise column names to lowercase
    df.columns = [c.lower() for c in df.columns]
    return df


def fetch_sensex_candles(date: datetime.date,
                          api_key: str = "",
                          access_token: str = "") -> tuple:
    """
    Returns (df, source_label).  df has lowercase columns open/high/low/close/volume.
    """
    if api_key and access_token:
        try:
            df = _fetch_sensex_via_kite(date, api_key, access_token)
            if not df.empty:
                return df, "Kite"
        except Exception:
            pass
    try:
        df = _fetch_sensex_via_yf(date)
        if not df.empty:
            return df, "yfinance"
    except Exception:
        pass
    return pd.DataFrame(), "none"


# ── Option price estimation (expiry-day model) ───────────────────────────────

def _est_option_open_price(spot: float, strike: float, opt_type: str) -> float:
    """
    Rough price estimate for a Sensex option at 2:15 PM on expiry day.
    At 2:15 PM with ~45 min left, time value is tiny.  We model it as
    intrinsic_value + decaying_time_premium, where the time premium is
    capped at ~10% of ATM value and decays exponentially with OTM distance.
    """
    moneyness = (spot - strike) if opt_type == "CE" else (strike - spot)
    intrinsic = max(0.0, moneyness)

    # Time premium: rough lookup based on OTM distance
    otm_dist = max(0.0, -moneyness)           # 0 when ITM
    atm_tp   = max(spot * 0.0008, 15.0)       # ~0.08% of Sensex ≈ ₹65 for 81k
    time_prem = atm_tp * math.exp(-otm_dist / 120.0)

    return max(0.10, intrinsic + time_prem)


def _est_option_close_price(spot_close: float, strike: float, opt_type: str) -> float:
    """At 3:15 PM on expiry, price ≈ intrinsic value (time premium ≈ 0)."""
    if opt_type == "CE":
        return max(0.0, spot_close - strike)
    return max(0.0, strike - spot_close)


def compute_rocket_options(spot_open: float,
                            spot_close: float,
                            pct_threshold: float = 500.0,
                            strike_range: int = 20) -> pd.DataFrame:
    """
    Given Sensex levels at 2:15 and 3:15 PM on expiry day, compute
    estimated option moves and return those >= pct_threshold%.
    """
    atm = round(spot_open / 100) * 100
    rows = []
    for i in range(-strike_range, strike_range + 1):
        strike = atm + i * 100
        if strike <= 0:
            continue
        for opt_type in ("CE", "PE"):
            p_open  = _est_option_open_price(spot_open, strike, opt_type)
            p_close = _est_option_close_price(spot_close, strike, opt_type)
            pct     = (p_close - p_open) / p_open * 100 if p_open > 0 else 0.0

            if pct >= pct_threshold:
                otm_flag = ""
                moneyness_at_open = (spot_open - strike) if opt_type == "CE" else (strike - spot_open)
                if moneyness_at_open < 0:
                    otm_flag = f"OTM {abs(moneyness_at_open):.0f}pts"
                elif moneyness_at_open > 0:
                    otm_flag = f"ITM {moneyness_at_open:.0f}pts"
                else:
                    otm_flag = "ATM"

                rows.append({
                    "Type":            opt_type,
                    "Strike":          int(strike),
                    "Moneyness":       otm_flag,
                    "Est. @ 2:15 PM":  round(p_open, 2),
                    "Est. @ 3:15 PM":  round(p_close, 2),
                    "Est. % Move":     round(pct, 1),
                })

    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).sort_values("Est. % Move", ascending=False).reset_index(drop=True)
    return df


# ── Kite BFO actual option scan (for non-expired options) ────────────────────

def _try_kite_actual_scan(api_key: str, access_token: str,
                           target_dates: set,
                           pct_threshold: float,
                           progress_cb=None) -> pd.DataFrame:
    """
    Tries to find SENSEX options in the BFO instruments master that expire
    on target_dates (already-expired options are usually absent, but we try).
    Returns actual 500%+ movers if any found; empty DataFrame otherwise.
    """
    try:
        resp = requests.get(
            f"{_KITE_BASE}/instruments/BFO",
            headers=_kite_headers(api_key, access_token),
            timeout=30,
        )
        resp.raise_for_status()
        instr = pd.read_csv(StringIO(resp.text))
    except Exception:
        return pd.DataFrame()

    instr["expiry_dt"]  = pd.to_datetime(instr["expiry"], errors="coerce")
    instr["expiry_str"] = instr["expiry_dt"].dt.strftime("%Y-%m-%d")

    opts = instr[
        (instr["name"] == "SENSEX") &
        (instr["instrument_type"].isin(["CE", "PE"])) &
        (instr["expiry_str"].isin(target_dates))
    ].copy()

    if opts.empty:
        return pd.DataFrame()

    total   = len(opts)
    results = []

    for i, (_, row) in enumerate(opts.iterrows()):
        if progress_cb:
            progress_cb(i / total, f"Scanning {row['tradingsymbol']} ({i+1}/{total})…")

        exp_date = row["expiry_str"]
        try:
            hist_resp = requests.get(
                f"{_KITE_BASE}/instruments/historical/{int(row['instrument_token'])}/5minute",
                headers=_kite_headers(api_key, access_token),
                params={"from": f"{exp_date} 14:15:00",
                        "to":   f"{exp_date} 15:20:00"},
                timeout=30,
            )
            hist_resp.raise_for_status()
            candles = hist_resp.json().get("data", {}).get("candles", [])
        except Exception:
            time.sleep(0.2)
            continue

        if not candles:
            time.sleep(0.1)
            continue

        df_c = pd.DataFrame(candles, columns=["ts", "open", "high", "low", "close", "vol"])
        p_open = float(df_c["open"].iloc[0])
        if p_open <= 0:
            time.sleep(0.1)
            continue

        p_peak   = float(df_c["high"].max())
        pct_move = (p_peak - p_open) / p_open * 100

        if pct_move >= pct_threshold:
            results.append({
                "Expiry":      exp_date,
                "Type":        row["instrument_type"],
                "Strike":      int(float(row["strike"])),
                "Symbol":      row["tradingsymbol"],
                "Open @ 2:15": round(p_open, 2),
                "Peak":        round(p_peak, 2),
                "% Move":      round(pct_move, 1),
                "Volume":      int(df_c["vol"].sum()),
                "Source":      "Kite Actual",
            })

        time.sleep(0.12)          # Kite allows ~3 req/s for historical

    if not results:
        return pd.DataFrame()
    return (pd.DataFrame(results)
            .sort_values(["Expiry", "% Move"], ascending=[False, False])
            .reset_index(drop=True))


# ── Main public function ──────────────────────────────────────────────────────

def run_sensex_option_moves_scan(
    n_weeks: int = 5,
    pct_threshold: float = 500.0,
    api_key: str = "",
    access_token: str = "",
    progress_cb=None,
) -> tuple:
    """
    Analyse the last n_weeks Sensex weekly expiry Fridays.

    Returns
    -------
    summary_df   : pd.DataFrame  — one row per expiry, index movement + best option
    weekly_data  : list[dict]    — detailed per-date results including rocket_df
    actual_df    : pd.DataFrame  — actual option data from Kite (may be empty)
    """
    expiry_dates  = get_sensex_expiry_fridays(n_weeks)
    target_dates  = {d.strftime("%Y-%m-%d") for d in expiry_dates}
    total_steps   = len(expiry_dates) + 1   # +1 for optional Kite scan

    summary_rows = []
    weekly_data  = []

    for i, date in enumerate(expiry_dates):
        if progress_cb:
            progress_cb(i / total_steps, f"Fetching Sensex data for {date.strftime('%d %b %Y')}…")

        df_candles, source = fetch_sensex_candles(date, api_key, access_token)

        if df_candles.empty:
            summary_rows.append({
                "Expiry Date":    date.strftime("%d %b %Y (%A)"),
                "Sensex @ 2:15":  "—",
                "Sensex @ 3:15":  "—",
                "Pts Move":       "—",
                "% Move":         "—",
                "Direction":      "—",
                "Best Option":    "—",
                "Est. Move":      "—",
                "ATM @ 2:15":     "—",
                "Data Source":    "No data",
            })
            weekly_data.append({"date": date, "error": "No data"})
            time.sleep(0.5)
            continue

        # Filter 2:15–3:15 PM IST window
        window = df_candles.between_time("14:15", "15:15")
        if window.empty:
            summary_rows.append({
                "Expiry Date":    date.strftime("%d %b %Y (%A)"),
                "Sensex @ 2:15":  "—",
                "Sensex @ 3:15":  "—",
                "Pts Move":       "—",
                "% Move":         "—",
                "Direction":      "—",
                "Best Option":    "—",
                "Est. Move":      "—",
                "ATM @ 2:15":     "—",
                "Data Source":    source,
            })
            weekly_data.append({"date": date, "error": "No data in 2:15-3:15 window"})
            time.sleep(0.5)
            continue

        spot_open  = float(window["open"].iloc[0])
        spot_close = float(window["close"].iloc[-1])
        spot_high  = float(window["high"].max())
        spot_low   = float(window["low"].min())
        pts_move   = spot_close - spot_open
        pct_move   = pts_move / spot_open * 100 if spot_open else 0.0
        atm        = int(round(spot_open / 100) * 100)
        direction  = "▲ Bullish" if pts_move > 0 else ("▼ Bearish" if pts_move < 0 else "Flat")

        rocket_df  = compute_rocket_options(spot_open, spot_close, pct_threshold)

        if not rocket_df.empty:
            top       = rocket_df.iloc[0]
            best_opt  = f"{top['Type']} {top['Strike']}"
            best_est  = f"{top['Est. % Move']:,.0f}%"
        else:
            best_opt  = "None (< 500% est.)"
            best_est  = "—"

        summary_rows.append({
            "Expiry Date":    date.strftime("%d %b %Y (%A)"),
            "Sensex @ 2:15":  f"{spot_open:,.2f}",
            "Sensex @ 3:15":  f"{spot_close:,.2f}",
            "Pts Move":       f"{pts_move:+,.2f}",
            "% Move":         f"{pct_move:+.3f}%",
            "Direction":      direction,
            "Best Option":    best_opt,
            "Est. Move":      best_est,
            "ATM @ 2:15":     f"{atm:,}",
            "Data Source":    source,
        })

        weekly_data.append({
            "date":        date,
            "source":      source,
            "spot_open":   spot_open,
            "spot_close":  spot_close,
            "spot_high":   spot_high,
            "spot_low":    spot_low,
            "pts_move":    pts_move,
            "pct_move":    pct_move,
            "atm":         atm,
            "rocket_df":   rocket_df,
            "candles":     window,
        })

        time.sleep(0.5)

    # Optionally try actual Kite BFO scan for any surviving instruments
    actual_df = pd.DataFrame()
    if api_key and access_token:
        if progress_cb:
            progress_cb((total_steps - 1) / total_steps,
                        "Checking BFO instruments master for actual option data…")
        actual_df = _try_kite_actual_scan(
            api_key, access_token, target_dates, pct_threshold
        )

    if progress_cb:
        progress_cb(1.0, "Done!")

    summary_df = pd.DataFrame(summary_rows)
    return summary_df, weekly_data, actual_df


# ── Pre-expiry analysis: use today's live OI to find Friday rockets ───────────

def run_preexpiry_analysis(api_key: str, access_token: str) -> dict:
    """
    Fetch live SENSEX BFO option chain for the nearest upcoming Friday expiry.
    Uses current real LTP prices as entry points and estimates exit value at Max Pain.

    Returns a dict with:
      spot, expiry, max_pain, pcr, atm,
      ce_wall, pe_wall, direction, gap_pts,
      chain_df      – full option chain
      rockets_df    – options likely to move 500%+ if Sensex closes at Max Pain
    """
    hdrs = _kite_headers(api_key, access_token)

    # 1. BFO instruments master
    resp = requests.get(f"{_KITE_BASE}/instruments/BFO", headers=hdrs, timeout=30)
    resp.raise_for_status()
    instr = pd.read_csv(StringIO(resp.text))

    instr["expiry_dt"] = pd.to_datetime(instr["expiry"], errors="coerce")

    # 2. Filter SENSEX CE/PE, find nearest expiry
    opts = instr[
        (instr["name"] == "SENSEX") &
        (instr["instrument_type"].isin(["CE", "PE"]))
    ].copy()

    if opts.empty:
        raise RuntimeError("No SENSEX options found in BFO instruments master.")

    nearest_expiry = opts["expiry_dt"].min()
    opts = opts[opts["expiry_dt"] == nearest_expiry].copy()
    expiry_label = nearest_expiry.strftime("%d %b %Y (%A)")

    # 3. Live spot price
    ltp_resp = requests.get(
        f"{_KITE_BASE}/quote/ltp",
        headers=hdrs,
        params={"i": "BSE:SENSEX"},
        timeout=10,
    )
    ltp_resp.raise_for_status()
    spot = float(ltp_resp.json()["data"]["BSE:SENSEX"]["last_price"])

    # 4. Fetch live quotes for all options in batches
    bfo_syms = ("BFO:" + opts["tradingsymbol"]).tolist()
    quotes: dict = {}
    for i in range(0, len(bfo_syms), 400):
        batch = bfo_syms[i: i + 400]
        q_resp = requests.get(
            f"{_KITE_BASE}/quote",
            headers=hdrs,
            params={"i": batch},
            timeout=30,
        )
        if q_resp.ok:
            quotes.update(q_resp.json().get("data", {}))

    # 5. Build chain DataFrame
    rows: dict = {}
    for _, row in opts.iterrows():
        strike = float(row["strike"])
        itype  = row["instrument_type"]
        key    = f"BFO:{row['tradingsymbol']}"
        q      = quotes.get(key, {})
        if strike not in rows:
            rows[strike] = {"Strike": strike,
                            "CE OI": 0, "CE LTP": 0.0,
                            "PE OI": 0, "PE LTP": 0.0}
        rows[strike][f"{itype} OI"]  = int(q.get("oi", 0))
        rows[strike][f"{itype} LTP"] = float(q.get("last_price", 0.0))

    chain_df = (pd.DataFrame(list(rows.values()))
                .sort_values("Strike")
                .reset_index(drop=True))

    # 6. Max Pain
    s_arr = chain_df["Strike"].values
    ce_oi = chain_df["CE OI"].values.astype(float)
    pe_oi = chain_df["PE OI"].values.astype(float)
    pain  = [
        float((ce_oi * np.maximum(0, s - s_arr)).sum() +
              (pe_oi * np.maximum(0, s_arr - s)).sum())
        for s in s_arr
    ]
    max_pain = float(s_arr[int(np.argmin(pain))])

    # 7. PCR, ATM, walls
    total_ce  = float(chain_df["CE OI"].sum())
    total_pe  = float(chain_df["PE OI"].sum())
    pcr       = round(total_pe / total_ce, 2) if total_ce else 0.0
    atm       = float(chain_df.loc[(chain_df["Strike"] - spot).abs().idxmin(), "Strike"])
    ce_wall   = float(chain_df.loc[chain_df["CE OI"].idxmax(), "Strike"])
    pe_wall   = float(chain_df.loc[chain_df["PE OI"].idxmax(), "Strike"])
    gap_pts   = max_pain - spot          # +ve → market must rally; -ve → must fall
    direction = "▲ RALLY to Max Pain" if gap_pts > 0 else "▼ FALL to Max Pain"

    # 8. Rocket candidates — options that benefit if Sensex closes at Max Pain
    rockets = []
    for _, row in chain_df.iterrows():
        strike   = float(row["Strike"])
        ce_entry = float(row["CE LTP"])
        pe_entry = float(row["PE LTP"])

        # CE: profitable if market rallies to max_pain
        ce_exit = max(0.0, max_pain - strike)
        if ce_entry > 0.10:
            ce_pct = (ce_exit - ce_entry) / ce_entry * 100
            if ce_pct >= 200:
                rockets.append({
                    "Type":       "CE",
                    "Strike":     int(strike),
                    "Entry (LTP)": round(ce_entry, 2),
                    "Exit @ Max Pain": round(ce_exit, 2),
                    "Est. % Move": round(ce_pct, 1),
                    "Moneyness":  (
                        f"OTM {strike - spot:.0f}pts" if strike > spot
                        else f"ITM {spot - strike:.0f}pts" if strike < spot
                        else "ATM"
                    ),
                })

        # PE: profitable if market falls to max_pain
        pe_exit = max(0.0, strike - max_pain)
        if pe_entry > 0.10:
            pe_pct = (pe_exit - pe_entry) / pe_entry * 100
            if pe_pct >= 200:
                rockets.append({
                    "Type":       "PE",
                    "Strike":     int(strike),
                    "Entry (LTP)": round(pe_entry, 2),
                    "Exit @ Max Pain": round(pe_exit, 2),
                    "Est. % Move": round(pe_pct, 1),
                    "Moneyness":  (
                        f"OTM {spot - strike:.0f}pts" if strike < spot
                        else f"ITM {strike - spot:.0f}pts" if strike > spot
                        else "ATM"
                    ),
                })

    rockets_df = pd.DataFrame(rockets) if rockets else pd.DataFrame()
    if not rockets_df.empty:
        rockets_df = (rockets_df
                      .sort_values("Est. % Move", ascending=False)
                      .reset_index(drop=True))

    return {
        "spot":        spot,
        "expiry":      expiry_label,
        "expiry_dt":   nearest_expiry,
        "max_pain":    max_pain,
        "pcr":         pcr,
        "atm":         atm,
        "ce_wall":     ce_wall,
        "pe_wall":     pe_wall,
        "gap_pts":     gap_pts,
        "direction":   direction,
        "chain_df":    chain_df,
        "rockets_df":  rockets_df,
    }
