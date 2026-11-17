"""
Straddle Tracker — implied (ATM straddle) vs actual expiry move.

Idea
----
The ATM straddle price on the trading day before expiry (T-1, 15:20) is the
market's estimate of the *average absolute* move to expiry. Track it over
many expiries and compare with what actually happened:

    straddle %   = (CE + PE) / spot × 100
    |move| %     = |expiry close − T-1 spot| / T-1 spot × 100
    ratio        = |move| % / straddle %      (<1 market over-estimated,
                                                >1 market under-estimated)

Note: a fairly priced straddle ≈ 0.8 σ, so even with fair pricing the close
lands outside ±straddle roughly 40 % of the time. Judge the AVERAGE ratio
across many expiries, not a single breach.

How data is captured
--------------------
Everything is rebuilt from Kite historical minute candles **on expiry day**
(after 15:30), while the weekly contracts are still listed. Kite drops
expired contracts from the instrument dump the next day, so a missed day
can't be recovered from Kite — use the manual-entry form in the app.

    T-1 15:20   spot, ATM strike, CE, PE, India VIX   (candle 15:19 close)
    Expiry 11:00  SAME strikes re-priced (pure decay view) + new ATM straddle
    Expiry day  open / high / low / close of the index, VIX close

Records are stored as JSON at data/straddle_tracker.json
(override with env STRADDLE_TRACKER_FILE).
"""

from __future__ import annotations

import datetime
import json
import math
import os
import pathlib
import tempfile
from io import StringIO

import pandas as pd
import requests

_KITE_BASE = "https://api.kite.trade"

# Kite tokens for index spot + India VIX
_INDEX_TOKEN = {"NIFTY": 256265, "BANKNIFTY": 260105, "SENSEX": 265}
_VIX_TOKEN = 264969

INSTRUMENTS: dict[str, dict] = {
    "NIFTY":     {"exchange": "NFO", "step": 50,  "lot": 65},
    "BANKNIFTY": {"exchange": "NFO", "step": 100, "lot": 30},
    "SENSEX":    {"exchange": "BFO", "step": 100, "lot": 20},
}

ENTRY_TIME = "15:20"   # T-1 snapshot
MID_TIME = "11:00"     # expiry-day decay snapshot

_DEFAULT_FILE = pathlib.Path(__file__).resolve().parent.parent / "data" / "straddle_tracker.json"


# ── Storage ───────────────────────────────────────────────────────────────────

def data_path() -> pathlib.Path:
    return pathlib.Path(os.environ.get("STRADDLE_TRACKER_FILE", str(_DEFAULT_FILE)))


def load_records() -> list[dict]:
    p = data_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text())
        return data.get("records", []) if isinstance(data, dict) else []
    except Exception:
        return []


def save_records(records: list[dict]) -> None:
    p = data_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    records = sorted(records, key=lambda r: (r.get("symbol", ""), r.get("expiry", "")))
    payload = json.dumps({"version": 1, "records": records}, indent=2, default=str)
    # atomic write — app and bot may touch the file at the same time
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".straddle_", suffix=".json")
    with os.fdopen(fd, "w") as f:
        f.write(payload)
    os.replace(tmp, p)


def upsert_record(rec: dict) -> list[dict]:
    """Insert or replace by (symbol, expiry); keeps user tag/notes if present."""
    records = load_records()
    key = (rec["symbol"], rec["expiry"])
    out, replaced = [], False
    for r in records:
        if (r.get("symbol"), r.get("expiry")) == key:
            for keep in ("tag", "notes"):
                if r.get(keep) and not rec.get(keep):
                    rec[keep] = r[keep]
            out.append(rec)
            replaced = True
        else:
            out.append(r)
    if not replaced:
        out.append(rec)
    save_records(out)
    return out


def update_annotations(symbol: str, expiry: str, tag: str, notes: str) -> None:
    records = load_records()
    for r in records:
        if r.get("symbol") == symbol and r.get("expiry") == expiry:
            r["tag"], r["notes"] = tag, notes
    save_records(records)


def delete_record(symbol: str, expiry: str) -> None:
    save_records([r for r in load_records()
                  if not (r.get("symbol") == symbol and r.get("expiry") == expiry)])


# ── Black-Scholes IV (r≈0, fine for a 1–2 day option) ────────────────────────

def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bs_price(spot: float, strike: float, t: float, vol: float, opt: str) -> float:
    if t <= 0 or vol <= 0:
        return max(0.0, spot - strike) if opt == "CE" else max(0.0, strike - spot)
    sd = vol * math.sqrt(t)
    d1 = (math.log(spot / strike) + 0.5 * sd * sd) / sd
    d2 = d1 - sd
    if opt == "CE":
        return spot * _norm_cdf(d1) - strike * _norm_cdf(d2)
    return strike * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def implied_vol(price: float, spot: float, strike: float, t: float, opt: str) -> float | None:
    """Annualised IV in % via bisection. None if price is below intrinsic."""
    intrinsic = max(0.0, spot - strike) if opt == "CE" else max(0.0, strike - spot)
    if price <= intrinsic or t <= 0 or spot <= 0:
        return None
    lo, hi = 0.001, 5.0
    for _ in range(100):
        mid = (lo + hi) / 2
        if _bs_price(spot, strike, t, mid, opt) > price:
            hi = mid
        else:
            lo = mid
    return round((lo + hi) / 2 * 100, 2)


def years_to_expiry(start: datetime.datetime, expiry: datetime.date) -> float:
    end = datetime.datetime.combine(expiry, datetime.time(15, 30))
    return max((end - start).total_seconds(), 0) / (365.0 * 86400)


# ── Kite helpers ──────────────────────────────────────────────────────────────

class _Kite:
    def __init__(self, api_key: str, access_token: str, session: requests.Session | None = None):
        self.hdrs = {"X-Kite-Version": "3", "Authorization": f"token {api_key}:{access_token}"}
        self.s = session or requests.Session()

    def instruments(self, exchange: str) -> pd.DataFrame:
        r = self.s.get(f"{_KITE_BASE}/instruments/{exchange}", headers=self.hdrs, timeout=30)
        r.raise_for_status()
        df = pd.read_csv(StringIO(r.text))
        df["expiry_dt"] = pd.to_datetime(df["expiry"], errors="coerce").dt.date
        return df

    def candles(self, token: int, interval: str, frm: datetime.datetime,
                to: datetime.datetime) -> pd.DataFrame:
        r = self.s.get(
            f"{_KITE_BASE}/instruments/historical/{int(token)}/{interval}",
            headers=self.hdrs,
            params={"from": frm.strftime("%Y-%m-%d %H:%M:%S"),
                    "to": to.strftime("%Y-%m-%d %H:%M:%S")},
            timeout=30,
        )
        r.raise_for_status()
        rows = r.json().get("data", {}).get("candles", [])
        if not rows:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        df = pd.DataFrame([c[:6] for c in rows],
                          columns=["ts", "open", "high", "low", "close", "volume"])
        # Kite returns "+0530" offsets → keep IST wall-clock, drop tz
        df["ts"] = pd.to_datetime(df["ts"].astype(str).str.slice(0, 19))
        return df.set_index("ts")


def _price_at(df: pd.DataFrame, at: datetime.datetime) -> float | None:
    """Close of the 1-min candle that ends at `at` (i.e. starts at at-1m)."""
    if df is None or df.empty:
        return None
    upto = df[df.index <= at - datetime.timedelta(minutes=1)]
    if upto.empty:
        return None
    return float(upto["close"].iloc[-1])


def _day_minutes(kite: _Kite, token: int, day: datetime.date) -> pd.DataFrame:
    return kite.candles(token, "minute",
                        datetime.datetime.combine(day, datetime.time(9, 15)),
                        datetime.datetime.combine(day, datetime.time(15, 30)))


def nearest_expiry(opts: pd.DataFrame, today: datetime.date) -> datetime.date | None:
    future = opts[opts["expiry_dt"] >= today]["expiry_dt"]
    return future.min() if not future.empty else None


def _option_rows(instr: pd.DataFrame, symbol: str) -> pd.DataFrame:
    return instr[(instr["name"] == symbol) & (instr["instrument_type"].isin(["CE", "PE"]))]


def _pick(opts: pd.DataFrame, expiry: datetime.date, strike: float, typ: str) -> dict | None:
    m = opts[(opts["expiry_dt"] == expiry) & (opts["strike"] == strike)
             & (opts["instrument_type"] == typ)]
    if m.empty:
        return None
    row = m.iloc[0]
    return {"token": int(row["instrument_token"]), "tradingsymbol": str(row["tradingsymbol"])}


def _nearest_listed_strike(opts: pd.DataFrame, expiry: datetime.date, target: float) -> float:
    strikes = opts[opts["expiry_dt"] == expiry]["strike"].unique()
    return float(min(strikes, key=lambda s: abs(s - target)))


# ── Core: build one expiry record ─────────────────────────────────────────────

def build_expiry_record(
    api_key: str,
    access_token: str,
    symbol: str = "NIFTY",
    expiry: datetime.date | None = None,
    now: datetime.datetime | None = None,
    session: requests.Session | None = None,
) -> dict:
    """
    Rebuild the full T-1 → expiry record from Kite candles.

    Run on expiry day after 15:30 for a complete record. Earlier in the day
    it returns a partial record (status="partial").
    """
    cfg = INSTRUMENTS[symbol]
    now = now or datetime.datetime.now(
        datetime.timezone(datetime.timedelta(hours=5, minutes=30))).replace(tzinfo=None)
    kite = _Kite(api_key, access_token, session)

    opts = _option_rows(kite.instruments(cfg["exchange"]), symbol)
    if opts.empty:
        raise RuntimeError(f"No {symbol} options in Kite instruments")
    expiry = expiry or nearest_expiry(opts, now.date())
    if expiry is None or expiry not in set(opts["expiry_dt"]):
        raise RuntimeError(f"Expiry {expiry} not listed on Kite (expired contracts can't be fetched)")
    if expiry > now.date():
        raise RuntimeError(f"{symbol} expiry {expiry} is in the future — run on expiry day")

    idx_token = _INDEX_TOKEN[symbol]

    # T-1 = last trading day before expiry (handles holidays automatically)
    daily = kite.candles(idx_token, "day",
                         datetime.datetime.combine(expiry - datetime.timedelta(days=12), datetime.time(0, 0)),
                         datetime.datetime.combine(expiry, datetime.time(23, 59)))
    prior_days = sorted(d for d in {ts.date() for ts in daily.index} if d < expiry)
    if not prior_days:
        raise RuntimeError("Could not find the trading day before expiry")
    t1 = prior_days[-1]

    hh, mm = map(int, ENTRY_TIME.split(":"))
    entry_at = datetime.datetime.combine(t1, datetime.time(hh, mm))

    idx_t1 = _day_minutes(kite, idx_token, t1)
    entry_spot = _price_at(idx_t1, entry_at)
    if entry_spot is None:
        raise RuntimeError(f"No index candles for {t1}")

    step = cfg["step"]
    strike = _nearest_listed_strike(opts, expiry, round(entry_spot / step) * step)
    ce, pe = _pick(opts, expiry, strike, "CE"), _pick(opts, expiry, strike, "PE")
    if not ce or not pe:
        raise RuntimeError(f"ATM {strike} CE/PE not found for {expiry}")

    ce_t1 = _day_minutes(kite, ce["token"], t1)
    pe_t1 = _day_minutes(kite, pe["token"], t1)
    ce_entry, pe_entry = _price_at(ce_t1, entry_at), _price_at(pe_t1, entry_at)
    if ce_entry is None or pe_entry is None:
        raise RuntimeError(f"No option candles for {strike} on {t1}")

    vix_t1 = _day_minutes(kite, _VIX_TOKEN, t1)
    vix_entry = _price_at(vix_t1, entry_at)

    straddle = ce_entry + pe_entry
    t_years = years_to_expiry(entry_at, expiry)
    ce_iv = implied_vol(ce_entry, entry_spot, strike, t_years, "CE")
    pe_iv = implied_vol(pe_entry, entry_spot, strike, t_years, "PE")
    ivs = [v for v in (ce_iv, pe_iv) if v is not None]

    rec: dict = {
        "symbol": symbol,
        "expiry": expiry.isoformat(),
        "entry_date": t1.isoformat(),
        "entry_time": ENTRY_TIME,
        "hours_to_expiry": round(t_years * 365 * 24, 1),
        "entry_spot": round(entry_spot, 2),
        "atm_strike": strike,
        "ce_symbol": ce["tradingsymbol"],
        "pe_symbol": pe["tradingsymbol"],
        "ce_entry": round(ce_entry, 2),
        "pe_entry": round(pe_entry, 2),
        "straddle_entry": round(straddle, 2),
        "straddle_pct": round(straddle / entry_spot * 100, 3),
        "atm_iv": round(sum(ivs) / len(ivs), 2) if ivs else None,
        "vix_entry": round(vix_entry, 2) if vix_entry else None,
        "source": "kite",
        "recorded_at": now.strftime("%Y-%m-%d %H:%M"),
    }

    # ── Expiry-day 11:00 decay snapshot (same strikes) ────────────────────────
    hh, mm = map(int, MID_TIME.split(":"))
    mid_at = datetime.datetime.combine(expiry, datetime.time(hh, mm))
    if now >= mid_at:
        idx_e = _day_minutes(kite, idx_token, expiry)
        ce_e = _day_minutes(kite, ce["token"], expiry)
        pe_e = _day_minutes(kite, pe["token"], expiry)
        mid_spot = _price_at(idx_e, mid_at)
        ce_mid, pe_mid = _price_at(ce_e, mid_at), _price_at(pe_e, mid_at)
        if mid_spot and ce_mid is not None and pe_mid is not None:
            mid_same = ce_mid + pe_mid
            rec.update({
                "mid_time": MID_TIME,
                "mid_spot": round(mid_spot, 2),
                "mid_same_strike_straddle": round(mid_same, 2),
                "decay_pts": round(straddle - mid_same, 2),
                "decay_pct": round((straddle - mid_same) / straddle * 100, 2),
                "spot_drift_pct_at_mid": round((mid_spot - entry_spot) / entry_spot * 100, 3),
            })
            # current ATM at 11:00 (may differ if spot moved)
            mid_strike = _nearest_listed_strike(opts, expiry, round(mid_spot / step) * step)
            rec["mid_atm_strike"] = mid_strike
            if mid_strike == strike:
                rec["mid_atm_straddle"] = rec["mid_same_strike_straddle"]
            else:
                ce2, pe2 = _pick(opts, expiry, mid_strike, "CE"), _pick(opts, expiry, mid_strike, "PE")
                if ce2 and pe2:
                    c2 = _price_at(_day_minutes(kite, ce2["token"], expiry), mid_at)
                    p2 = _price_at(_day_minutes(kite, pe2["token"], expiry), mid_at)
                    if c2 is not None and p2 is not None:
                        rec["mid_atm_straddle"] = round(c2 + p2, 2)

        # ── Expiry-day OHLC ───────────────────────────────────────────────────
        if not idx_e.empty:
            rec.update({
                "exp_open": round(float(idx_e["open"].iloc[0]), 2),
                "exp_high": round(float(idx_e["high"].max()), 2),
                "exp_low": round(float(idx_e["low"].min()), 2),
            })
            # high/low between T-1 entry and expiry (includes rest of T-1 session)
            after_entry = idx_t1[idx_t1.index >= entry_at]
            hi = max(rec["exp_high"], float(after_entry["high"].max()) if not after_entry.empty else 0)
            lo = min(rec["exp_low"], float(after_entry["low"].min()) if not after_entry.empty else 1e12)
            rec["path_high"], rec["path_low"] = round(hi, 2), round(lo, 2)

    closed = now >= datetime.datetime.combine(expiry, datetime.time(15, 30))
    if closed:
        day_row = daily[[ts.date() == expiry for ts in daily.index]]
        exp_close = float(day_row["close"].iloc[-1]) if not day_row.empty else None
        if exp_close is None and "mid_spot" in rec:
            exp_close = float(_day_minutes(kite, idx_token, expiry)["close"].iloc[-1])
        vix_e = _day_minutes(kite, _VIX_TOKEN, expiry)
        if exp_close:
            rec["exp_close"] = round(exp_close, 2)
            rec["vix_close"] = round(float(vix_e["close"].iloc[-1]), 2) if not vix_e.empty else None
    rec["status"] = "complete" if "exp_close" in rec else "partial"
    return enrich(rec)


# ── Derived metrics ───────────────────────────────────────────────────────────

def enrich(rec: dict) -> dict:
    """Compute derived fields from the raw ones (works for manual records too)."""
    r = dict(rec)
    spot, straddle = r.get("entry_spot"), r.get("straddle_entry")
    if spot and straddle:
        r["straddle_pct"] = round(straddle / spot * 100, 3)
        r["range_low"] = round(spot - straddle, 2)
        r["range_high"] = round(spot + straddle, 2)
    close = r.get("exp_close")
    if spot and close:
        move = (close - spot) / spot * 100
        r["move_pct"] = round(move, 3)
        r["abs_move_pct"] = round(abs(move), 3)
        if r.get("straddle_pct"):
            r["move_ratio"] = round(abs(move) / r["straddle_pct"], 3)
            r["closed_outside"] = abs(close - spot) > straddle
        strike = r.get("atm_strike") or spot
        settle = abs(close - strike)
        r["settle_value"] = round(settle, 2)
        r["seller_pnl_pts"] = round(straddle - settle, 2)
        lot = INSTRUMENTS.get(r.get("symbol", "NIFTY"), {}).get("lot", 1)
        r["seller_pnl_per_lot"] = round((straddle - settle) * lot, 0)
    if r.get("exp_open") and spot:
        r["gap_pct"] = round((r["exp_open"] - spot) / spot * 100, 3)
    hi = r.get("path_high") or r.get("exp_high")
    lo = r.get("path_low") or r.get("exp_low")
    if hi and lo and spot and straddle:
        r["max_excursion_pct"] = round(max(hi - spot, spot - lo) / spot * 100, 3)
        r["touched_outside"] = (hi > spot + straddle) or (lo < spot - straddle)
    return r


def vix_bucket(v) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "n/a"
    if v < 12:
        return "<12"
    if v < 15:
        return "12–15"
    if v < 18:
        return "15–18"
    return "18+"


def records_df(symbol: str | None = None) -> pd.DataFrame:
    recs = [enrich(r) for r in load_records() if symbol is None or r.get("symbol") == symbol]
    if not recs:
        return pd.DataFrame()
    df = pd.DataFrame(recs).sort_values("expiry").reset_index(drop=True)
    df["vix_bucket"] = df.get("vix_entry", pd.Series([None] * len(df))).apply(vix_bucket)
    for c in ("tag", "notes"):
        if c not in df.columns:
            df[c] = ""
        df[c] = df[c].fillna("")
    return df


def summarize(df: pd.DataFrame) -> dict:
    """Headline stats over completed expiries."""
    done = df[df.get("move_ratio").notna()] if "move_ratio" in df.columns else df.iloc[0:0]
    n = len(done)
    out = {"n": n, "n_total": len(df)}
    if n == 0:
        return out
    out.update({k: float(v) for k, v in {
        "avg_straddle_pct": round(done["straddle_pct"].mean(), 3),
        "avg_abs_move_pct": round(done["abs_move_pct"].mean(), 3),
        # ratio of averages is the cleaner test; mean of ratios shown too
        "ratio_of_avgs": round(done["abs_move_pct"].mean() / done["straddle_pct"].mean(), 3),
        "median_ratio": round(done["move_ratio"].median(), 3),
        "pct_closed_inside": round((~done["closed_outside"].astype(bool)).mean() * 100, 1),
        "seller_win_rate": round((done["seller_pnl_pts"] > 0).mean() * 100, 1),
        "seller_total_pts": round(done["seller_pnl_pts"].sum(), 1),
        "seller_avg_pts": round(done["seller_pnl_pts"].mean(), 1),
        "seller_worst_pts": round(done["seller_pnl_pts"].min(), 1),
    }.items()})
    if "touched_outside" in done.columns and done["touched_outside"].notna().any():
        t = done["touched_outside"].dropna().astype(bool)
        out["pct_touched_outside"] = float(round(t.mean() * 100, 1))
    if "decay_pct" in done.columns and done["decay_pct"].notna().any():
        out["avg_decay_pct"] = float(round(done["decay_pct"].mean(), 1))
    return out


def segment(df: pd.DataFrame, by: str) -> pd.DataFrame:
    if df.empty or "move_ratio" not in df.columns or by not in df.columns:
        return pd.DataFrame()
    done = df[df["move_ratio"].notna()].copy()
    if done.empty:
        return pd.DataFrame()
    done[by] = done[by].replace("", "untagged")
    g = done.groupby(by)
    return pd.DataFrame({
        "expiries": g.size(),
        "avg straddle %": g["straddle_pct"].mean().round(3),
        "avg |move| %": g["abs_move_pct"].mean().round(3),
        "ratio": (g["abs_move_pct"].mean() / g["straddle_pct"].mean()).round(2),
        "closed inside %": g["closed_outside"].apply(lambda s: round((~s.astype(bool)).mean() * 100, 1)),
        "seller avg pts": g["seller_pnl_pts"].mean().round(1),
    }).reset_index()


def manual_record(symbol: str, expiry: datetime.date, entry_date: datetime.date,
                  entry_spot: float, atm_strike: float, ce_entry: float, pe_entry: float,
                  exp_close: float, exp_high: float | None = None, exp_low: float | None = None,
                  vix_entry: float | None = None, mid_same_strike_straddle: float | None = None,
                  tag: str = "", notes: str = "") -> dict:
    straddle = ce_entry + pe_entry
    rec = {
        "symbol": symbol, "expiry": expiry.isoformat(), "entry_date": entry_date.isoformat(),
        "entry_time": ENTRY_TIME, "entry_spot": entry_spot, "atm_strike": atm_strike,
        "ce_entry": ce_entry, "pe_entry": pe_entry, "straddle_entry": round(straddle, 2),
        "exp_close": exp_close, "exp_high": exp_high or None, "exp_low": exp_low or None,
        "vix_entry": vix_entry or None, "tag": tag, "notes": notes,
        "source": "manual", "status": "complete",
        "recorded_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    t = years_to_expiry(datetime.datetime.combine(entry_date, datetime.time(15, 20)), expiry)
    ivs = [v for v in (implied_vol(ce_entry, entry_spot, atm_strike, t, "CE"),
                       implied_vol(pe_entry, entry_spot, atm_strike, t, "PE")) if v]
    rec["atm_iv"] = round(sum(ivs) / len(ivs), 2) if ivs else None
    rec["hours_to_expiry"] = round(t * 365 * 24, 1)
    if mid_same_strike_straddle:
        rec["mid_time"] = MID_TIME
        rec["mid_same_strike_straddle"] = mid_same_strike_straddle
        rec["decay_pts"] = round(straddle - mid_same_strike_straddle, 2)
        rec["decay_pct"] = round((straddle - mid_same_strike_straddle) / straddle * 100, 2)
    return enrich(rec)


def format_summary_text(rec: dict) -> str:
    """Short text for push notifications."""
    lines = [
        f"{rec['symbol']} expiry {rec['expiry']} (entry {rec['entry_date']} {rec['entry_time']})",
        f"Spot {rec['entry_spot']:,.0f} · ATM {rec['atm_strike']:.0f} · "
        f"Straddle ₹{rec['straddle_entry']:.1f} ({rec['straddle_pct']:.2f}%)",
        f"Range {rec.get('range_low', 0):,.0f} – {rec.get('range_high', 0):,.0f}",
    ]
    if rec.get("exp_close"):
        lines.append(f"Close {rec['exp_close']:,.0f} · move {rec['move_pct']:+.2f}% · "
                     f"ratio {rec['move_ratio']:.2f} · "
                     f"{'OUTSIDE' if rec['closed_outside'] else 'inside'} range")
        lines.append(f"Seller P&L {rec['seller_pnl_pts']:+.1f} pts "
                     f"(₹{rec['seller_pnl_per_lot']:+,.0f}/lot)")
    if rec.get("decay_pct") is not None:
        lines.append(f"11:00 same-strike straddle ₹{rec['mid_same_strike_straddle']:.1f} "
                     f"(−{rec['decay_pct']:.1f}%)")
    return "\n".join(lines)
