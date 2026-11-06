"""
OI + PCR for the chandu scan — one Kite read per scan, compared with yesterday's close.

Per symbol (indices: nearest expiry; stocks: the monthly; on expiry day the next one):
  PCR        put OI ÷ call OI over ATM ±10 strikes, and its change since yesterday's last scan
  OI change  near ATM — calls ATM−1 … ATM+3, puts ATM−3 … ATM+1 — as % of yesterday's OI
             (puts added / calls unwinding = bullish; calls added / puts unwinding = bearish)
  futures    long buildup / short covering (bullish) · short buildup / long unwinding (bearish)
  walls      biggest call OI above the price = resistance, biggest put OI below = support

OI bias, −4 … +4 (one point each, bullish minus bearish):
  bullish  PCR up ≥ PCR_STEP · puts added ≥ CHG_PCT % · calls unwinding ≥ CHG_PCT % · futures bullish
  bearish  PCR down ≥ PCR_STEP · calls added ≥ CHG_PCT % · puts unwinding ≥ CHG_PCT % · futures bearish
OI agrees with a bullish entry at bias ≥ +AGREE, with a bearish one at bias ≤ −AGREE.

Snapshots: data/chandu_oi/<date>/<HHMM>.csv.gz (30 days kept). The day's last one is the next
day's base; with no base yet (first day) the OI read is "n/a" and the scan does not filter on it.
"""

from __future__ import annotations

import datetime
import gzip
import io
import json
import pathlib
import shutil
import time

import pandas as pd
import requests

from core import kite_data as _kd

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "chandu_oi"
INDEXES = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"}
STRIKES_EACH_SIDE = 10
NEAR_LO, NEAR_HI = 3, 3          # puts ATM−3 … ATM+1, calls ATM−1 … ATM+3
CHG_PCT = 2.0
PCR_STEP = 0.05
AGREE = 2
KEEP_DAYS = 30


def _get(url: str, params=None, timeout: int = 30):
    r = requests.get(url, headers=_kd._hdrs(), params=params, timeout=timeout)
    r.raise_for_status()
    return r


def contracts(names: list[str], today: datetime.date) -> dict:
    """{name: {"exch", "expiry", "fut", "opts": [[strike, CE ts, PE ts], ...]}} — cached per day."""
    DATA.mkdir(parents=True, exist_ok=True)
    cache = DATA / f"contracts_{today}.json"
    have = json.loads(cache.read_text()) if cache.exists() else {}
    want = [n for n in names if n not in have]
    if want:
        for exch in ("NFO", "BFO"):
            need = [n for n in want if (n == "SENSEX") == (exch == "BFO")]
            if not need:
                continue
            df = pd.read_csv(io.StringIO(_get(f"{_kd._KITE_BASE}/instruments/{exch}").text))
            df = df[df["name"].isin(need)].copy()
            df["expiry"] = pd.to_datetime(df["expiry"]).dt.date
            df = df[df["expiry"] > today]          # on expiry day: the next one
            for n, g in df.groupby("name"):
                opt = g[g["instrument_type"].isin(["CE", "PE"])]
                fut = g[g["instrument_type"] == "FUT"].sort_values("expiry")
                if opt.empty:
                    continue
                exp = opt["expiry"].min()
                legs: dict = {}
                for _, r in opt[opt["expiry"] == exp].iterrows():
                    legs.setdefault(float(r["strike"]), {})[r["instrument_type"]] = r["tradingsymbol"]
                have[n] = {"exch": exch, "expiry": str(exp),
                           "fut": fut.iloc[0]["tradingsymbol"] if len(fut) else None,
                           "opts": [[k, v["CE"], v["PE"]] for k, v in sorted(legs.items()) if "CE" in v and "PE" in v]}
        for n in want:
            have.setdefault(n, None)               # no options on Kite (not an F&O symbol)
        cache.write_text(json.dumps(have))
        for old in DATA.glob("contracts_*.json"):
            if old != cache:
                old.unlink()
    return have


def quotes(keys: list[str]) -> dict:
    out = {}
    for i in range(0, len(keys), 500):
        for attempt in range(3):
            try:
                out.update(_get(f"{_kd._KITE_BASE}/quote", params=[("i", k) for k in keys[i:i + 500]]).json()["data"])
                break
            except Exception as ex:
                if attempt == 2:
                    print(f"  OI quote batch failed: {ex}")
                time.sleep(2)
        time.sleep(1.05)
    return out


def snapshot(prices: dict[str, float], today: datetime.date) -> pd.DataFrame:
    """One row per option contract (ATM ±10) with the future's price / OI, for the given symbols."""
    con = contracts(list(prices), today)
    want, pick = [], {}
    for n, px in prices.items():
        c = con.get(n)
        if not c or not c["opts"]:
            continue
        opts = c["opts"]
        i = min(range(len(opts)), key=lambda j: abs(opts[j][0] - px))
        pick[n] = opts[max(0, i - STRIKES_EACH_SIDE): i + STRIKES_EACH_SIDE + 1]
        want += [f"{c['exch']}:{t}" for _, ce, pe in pick[n] for t in (ce, pe)]
        if c["fut"]:
            want.append(f"{c['exch']}:{c['fut']}")
    q = quotes(want)
    rows = []
    for n, legs in pick.items():
        c = con[n]
        f = q.get(f"{c['exch']}:{c['fut']}", {}) if c["fut"] else {}
        for k, ce, pe in legs:
            for typ, ts in (("CE", ce), ("PE", pe)):
                o = q.get(f"{c['exch']}:{ts}")
                if o:
                    rows.append({"name": n, "strike": k, "type": typ, "oi": float(o.get("oi") or 0),
                                 "spot": prices[n], "expiry": c["expiry"],
                                 "fut_ltp": float(f.get("last_price") or 0),
                                 "fut_close": float((f.get("ohlc") or {}).get("close") or 0),
                                 "fut_oi": float(f.get("oi") or 0)})
    return pd.DataFrame(rows)


def save(snap: pd.DataFrame, today: datetime.date, hm: str) -> None:
    d = DATA / str(today)
    d.mkdir(parents=True, exist_ok=True)
    with gzip.open(d / f"{hm}.csv.gz", "wt") as fh:
        snap.to_csv(fh, index=False)
    cut = str(today - datetime.timedelta(days=KEEP_DAYS))
    for old in DATA.iterdir():
        if old.is_dir() and old.name < cut:
            shutil.rmtree(old, ignore_errors=True)


def base(today: datetime.date) -> pd.DataFrame | None:
    """Yesterday's (latest earlier day's) last snapshot."""
    if not DATA.exists():
        return None
    for d in sorted((x for x in DATA.iterdir() if x.is_dir() and x.name < str(today)), reverse=True):
        files = sorted(d.glob("*.csv.gz"))
        if files:
            return pd.read_csv(files[-1])
    return None


def fut_label(price_pct: float, oi_chg: float) -> str | None:
    if not oi_chg:
        return None
    if price_pct >= 0:
        return "long buildup" if oi_chg > 0 else "short covering"
    return "short buildup" if oi_chg > 0 else "long unwinding"


def analyse(now: pd.DataFrame, prev: pd.DataFrame | None) -> dict:
    """{name: OI read} — "na": True when there is no base to compare with."""
    out = {}
    for n, g in now.groupby("name"):
        spot = float(g["spot"].iloc[0])
        strikes = sorted(g["strike"].unique())
        atm_i = min(range(len(strikes)), key=lambda j: abs(strikes[j] - spot))
        calls, puts = g[g["type"] == "CE"], g[g["type"] == "PE"]
        c_oi, p_oi = calls["oi"].sum(), puts["oi"].sum()
        pcr = p_oi / c_oi if c_oi else None
        above, below = calls[calls["strike"] > spot], puts[puts["strike"] < spot]
        r = {"pcr": round(float(pcr), 2) if pcr else None,
             "call_wall": float(above.loc[above["oi"].idxmax(), "strike"]) if len(above) else None,
             "put_wall": float(below.loc[below["oi"].idxmax(), "strike"]) if len(below) else None,
             "expiry": g["expiry"].iloc[0], "na": True, "bias": 0}
        b = prev[(prev["name"] == n) & (prev["expiry"] == r["expiry"])] if prev is not None else None
        if b is not None and len(b):
            m = g.merge(b[["strike", "type", "oi"]], on=["strike", "type"], how="left", suffixes=("", "_b"))
            m["oi_b"] = m["oi_b"].fillna(m["oi"])
            near_c = m[(m["type"] == "CE") & m["strike"].isin(strikes[max(0, atm_i - 1): atm_i + NEAR_HI + 1])]
            near_p = m[(m["type"] == "PE") & m["strike"].isin(strikes[max(0, atm_i - NEAR_LO): atm_i + 2])]
            pct = lambda d: (d["oi"].sum() / d["oi_b"].sum() - 1) * 100 if d["oi_b"].sum() else 0.0
            bc, bp = m[m["type"] == "CE"]["oi_b"].sum(), m[m["type"] == "PE"]["oi_b"].sum()
            pcr_b = bp / bc if bc else None
            f0 = g.iloc[0]
            fb = b.iloc[0]
            fut_pct = (f0["fut_ltp"] / f0["fut_close"] - 1) * 100 if f0["fut_close"] else 0.0
            fut = fut_label(fut_pct, float(f0["fut_oi"]) - float(fb["fut_oi"])) if f0["fut_oi"] and fb["fut_oi"] else None
            c_pct, p_pct = pct(near_c), pct(near_p)
            d_pcr = (pcr - pcr_b) if (pcr and pcr_b) else 0.0
            bull = [d_pcr >= PCR_STEP, p_pct >= CHG_PCT, c_pct <= -CHG_PCT, fut in ("long buildup", "short covering")]
            bear = [d_pcr <= -PCR_STEP, c_pct >= CHG_PCT, p_pct <= -CHG_PCT, fut in ("short buildup", "long unwinding")]
            r.update({"na": False, "d_pcr": round(float(d_pcr), 2), "call_pct": round(float(c_pct), 1),
                      "put_pct": round(float(p_pct), 1), "fut": fut, "fut_pct": round(float(fut_pct), 2),
                      "bias": int(sum(bool(x) for x in bull) - sum(bool(x) for x in bear))})
        out[n] = r
    return out


def agrees(oi: dict | None, side: str) -> bool | None:
    """True / False when the OI read agrees / disagrees with a 'bull' or 'bear' entry; None = no read."""
    if not oi or oi.get("na"):
        return None
    return bool(oi["bias"] >= AGREE) if side == "bull" else bool(oi["bias"] <= -AGREE)


def text(oi: dict | None) -> str:
    """Short OI line for the push."""
    if not oi:
        return ""
    if oi.get("na"):
        return f"PCR {oi['pcr']} (no base yet)" if oi.get("pcr") else ""
    walls = " · ".join(x for x in (f"S {oi['put_wall']:g}" if oi.get("put_wall") else "",
                                   f"R {oi['call_wall']:g}" if oi.get("call_wall") else "") if x)
    return (f"PCR {oi['pcr']} ({oi['d_pcr']:+.2f}) · puts {oi['put_pct']:+.0f}% calls {oi['call_pct']:+.0f}%"
            + (f" · fut {oi['fut']}" if oi.get("fut") else "") + (f" · {walls}" if walls else "")
            + f" · OI {oi['bias']:+d}")


def read(prices: dict[str, float], now: datetime.datetime) -> dict:
    """Snapshot now, save it, compare with yesterday's close. {} on any failure (scan goes on)."""
    today, hm = now.date(), now.strftime("%H%M")
    snap = snapshot(prices, today)
    if snap.empty:
        return {}
    prev = base(today)
    save(snap, today, hm)
    return analyse(snap, prev)
