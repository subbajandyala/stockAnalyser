"""
OI Buildup History — tracks strike-level OI accumulation over a date range.

Uses Kite historical API (oi=1) to pull daily OI for every strike of
a given index/expiry, then shows which strikes had fresh writing,
unwinding, or long buildup.
"""

from __future__ import annotations

import datetime
from io import StringIO

import numpy as np
import pandas as pd
import requests

_KITE_BASE = "https://api.kite.trade"
_IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

_INSTRUMENT_CONFIG = {
    "NIFTY":      {"exchange": "NFO", "tick": 50,  "spot_sym": "NSE:NIFTY 50",          "lot": 75},
    "BANKNIFTY":  {"exchange": "NFO", "tick": 100, "spot_sym": "NSE:NIFTY BANK",         "lot": 30},
    "FINNIFTY":   {"exchange": "NFO", "tick": 50,  "spot_sym": "NSE:NIFTY FIN SERVICE",  "lot": 40},
    "MIDCPNIFTY": {"exchange": "NFO", "tick": 25,  "spot_sym": "NSE:NIFTY MID SELECT",   "lot": 75},
    "SENSEX":     {"exchange": "BFO", "tick": 100, "spot_sym": "BSE:SENSEX",             "lot": 10},
}

ALL_INSTRUMENTS = list(_INSTRUMENT_CONFIG.keys())


def _hdrs(api_key: str, access_token: str) -> dict:
    return {
        "X-Kite-Version": "3",
        "Authorization": f"token {api_key}:{access_token}",
    }


def get_expiries(api_key: str, access_token: str, symbol: str) -> list[datetime.date]:
    """Return sorted list of available expiry dates for symbol."""
    cfg = _INSTRUMENT_CONFIG.get(symbol, _INSTRUMENT_CONFIG["NIFTY"])
    r = requests.get(
        f"{_KITE_BASE}/instruments/{cfg['exchange']}",
        headers=_hdrs(api_key, access_token), timeout=30,
    )
    r.raise_for_status()
    df = pd.read_csv(StringIO(r.text))
    df["expiry_dt"] = pd.to_datetime(df["expiry"], errors="coerce")
    opts = df[(df["name"] == symbol) & (df["instrument_type"].isin(["CE", "PE"]))]
    expiries = sorted(opts["expiry_dt"].dropna().dt.date.unique())
    return expiries


def fetch_oi_history(
    api_key: str,
    access_token: str,
    symbol: str,
    expiry: datetime.date,
    from_date: datetime.date,
    to_date: datetime.date,
    strikes_around_atm: int = 20,
) -> dict:
    """
    Fetch daily OI for every strike of the given expiry from from_date to to_date.

    Returns
    -------
    {
        "spot": float,
        "expiry": str,
        "from_date": str,
        "to_date": str,
        "pivot": DataFrame  — index=Strike, columns=dates (CE_OI / PE_OI for each date),
        "summary": DataFrame — one row per strike with key metrics,
        "daily_chain": DataFrame — long-form (date, strike, type, oi, ltp, volume),
    }
    """
    cfg  = _INSTRUMENT_CONFIG.get(symbol, _INSTRUMENT_CONFIG["NIFTY"])
    exch = cfg["exchange"]
    hdrs = _hdrs(api_key, access_token)

    # Instruments master
    r = requests.get(f"{_KITE_BASE}/instruments/{exch}", headers=hdrs, timeout=30)
    r.raise_for_status()
    instr = pd.read_csv(StringIO(r.text))
    instr["expiry_dt"] = pd.to_datetime(instr["expiry"], errors="coerce")

    # Filter to target expiry
    target_dt = pd.Timestamp(expiry)
    opts = instr[
        (instr["name"] == symbol) &
        (instr["instrument_type"].isin(["CE", "PE"])) &
        (instr["expiry_dt"] == target_dt)
    ].copy()

    if opts.empty:
        raise RuntimeError(f"No {symbol} options for expiry {expiry} in {exch}.")

    # Spot
    ltp_r = requests.get(
        f"{_KITE_BASE}/quote/ltp", headers=hdrs,
        params={"i": cfg["spot_sym"]}, timeout=10,
    )
    ltp_r.raise_for_status()
    spot = float(ltp_r.json()["data"][cfg["spot_sym"]]["last_price"])

    # Limit to strikes around ATM
    tick = cfg["tick"]
    atm = round(spot / tick) * tick
    strike_range = strikes_around_atm * tick
    opts = opts[abs(opts["strike"] - atm) <= strike_range].copy()

    # Date strings for Kite historical API
    from_str = from_date.strftime("%Y-%m-%d")
    to_str   = to_date.strftime("%Y-%m-%d")

    # Fetch daily history for each instrument token
    records: list[dict] = []
    tokens = opts["instrument_token"].tolist()
    meta   = opts.set_index("instrument_token")[["strike", "instrument_type", "tradingsymbol"]].to_dict("index")

    for token in tokens:
        m = meta[token]
        url = f"{_KITE_BASE}/instruments/historical/{token}/day"
        params = {
            "from": from_str,
            "to":   to_str,
            "oi":   "1",
        }
        try:
            hr = requests.get(url, headers=hdrs, params=params, timeout=20)
            if not hr.ok:
                continue
            candles = hr.json().get("data", {}).get("candles", [])
            for c in candles:
                # [date, open, high, low, close, volume, oi]
                if len(c) >= 7:
                    records.append({
                        "date":   pd.Timestamp(c[0]).date(),
                        "strike": float(m["strike"]),
                        "type":   m["instrument_type"],
                        "ltp":    float(c[4]),
                        "volume": int(c[5]),
                        "oi":     int(c[6]),
                        "symbol": m["tradingsymbol"],
                    })
        except Exception:
            continue

    if not records:
        raise RuntimeError("No historical data returned from Kite. Verify credentials and date range.")

    daily = pd.DataFrame(records)
    daily["date"] = pd.to_datetime(daily["date"])

    # Build summary: per-strike, OI on first day vs last day, and max OI
    dates_sorted = sorted(daily["date"].unique())
    first_dt = dates_sorted[0]
    last_dt  = dates_sorted[-1]

    summary_rows = []
    for (strike, otype), grp in daily.groupby(["strike", "type"]):
        grp = grp.sort_values("date")
        oi_start  = int(grp[grp["date"] == first_dt]["oi"].values[0]) if first_dt in grp["date"].values else 0
        oi_end    = int(grp[grp["date"] == last_dt]["oi"].values[-1]) if last_dt in grp["date"].values else 0
        oi_max    = int(grp["oi"].max())
        oi_change = oi_end - oi_start
        ltp_start = float(grp[grp["date"] == first_dt]["ltp"].values[0]) if first_dt in grp["date"].values else 0
        ltp_end   = float(grp[grp["date"] == last_dt]["ltp"].values[-1]) if last_dt in grp["date"].values else 0
        ltp_chg   = round(ltp_end - ltp_start, 2)
        total_vol = int(grp["volume"].sum())

        # Interpret signal
        if oi_change > 0 and ltp_chg > 0:
            signal = "📈 Long Buildup"
        elif oi_change > 0 and ltp_chg < 0:
            signal = "🐻 Short Buildup"
        elif oi_change < 0 and ltp_chg > 0:
            signal = "🔼 Short Covering"
        elif oi_change < 0 and ltp_chg < 0:
            signal = "🔽 Long Unwinding"
        else:
            signal = "➖ Neutral"

        summary_rows.append({
            "Strike":    int(strike),
            "Type":      otype,
            "OI Start":  oi_start,
            "OI End":    oi_end,
            "OI Change": oi_change,
            "OI Max":    oi_max,
            "LTP Start": ltp_start,
            "LTP End":   ltp_end,
            "LTP Chg":   ltp_chg,
            "Volume":    total_vol,
            "Signal":    signal,
        })

    summary_df = (
        pd.DataFrame(summary_rows)
        .sort_values(["Type", "Strike"])
        .reset_index(drop=True)
    )

    # Pivot: rows=strike, cols=date-string, values=OI for CE and PE
    def _date_str(ts) -> str:
        return pd.Timestamp(ts).strftime("%Y-%m-%d")

    ce_raw = (
        daily[daily["type"] == "CE"]
        .pivot_table(index="strike", columns="date", values="oi", aggfunc="last")
    )
    ce_raw.columns = [f"CE_{_date_str(d)}" for d in ce_raw.columns]

    pe_raw = (
        daily[daily["type"] == "PE"]
        .pivot_table(index="strike", columns="date", values="oi", aggfunc="last")
    )
    pe_raw.columns = [f"PE_{_date_str(d)}" for d in pe_raw.columns]

    pivot = ce_raw.join(pe_raw, how="outer").fillna(0).astype(int)

    return {
        "spot":       spot,
        "atm":        atm,
        "expiry":     expiry.strftime("%d %b %Y"),
        "from_date":  from_str,
        "to_date":    to_str,
        "dates":      [pd.Timestamp(d).date() for d in dates_sorted],
        "pivot":      pivot,
        "summary":    summary_df,
        "daily":      daily,
    }
