"""
Backtest: VWAP cross candle in the direction of the trend (Srikanth's idea).
Read-only: fetches Kite history, places NO orders.

Rule (CE; PE is the mirror)
  1. Trend up   — Supertrend(10,2) green on the candles before the cross candle
                  and/or EMA 7 > EMA 25 (see TREND variants below)
  2. Cross      — candle opened below VWAP (or the previous close was below it)
                  and closes above VWAP by at least BUF points
  3. RSI(14)    — 51..65 for CE, 35..49 for PE (above 65 / below 35 = too stretched)
  4. Time       — cross candle closes 09:30..15:00; entry at its close
  Exit (index)  — a candle closes back below VWAP (exit at that close), or the
                  cross candle's low is broken (exit at that low), or 15:15.
                  "tgt" variant also books at a fixed index-point target.
  One trade at a time; a new trade needs a fresh cross.

VWAP: index price (H+L+C)/3 weighted by the futures volume of the same candle,
reset each day — what TradingView shows on a stock. Index candles have no volume.
Kite only serves history for futures that are still listed, so the first weeks of a
long --days run may have no futures volume: those days are skipped.

Results are in INDEX points. Rough option ₹ = points × 0.5 delta × lot (no theta/IV).

Usage (on the droplet):
  /root/venv/bin/python scripts/bt_vwap_cross.py                  # NIFTY + SENSEX, 3m + 5m, 60 days
  /root/venv/bin/python scripts/bt_vwap_cross.py NIFTY --days 90 --tf 3m --trades
  /root/venv/bin/python scripts/bt_vwap_cross.py --push           # also send the summary to ntfy
"""

import argparse
import datetime
import pathlib
import sys
import time
from io import StringIO

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import numpy as np
import pandas as pd
import requests

_ROOT = pathlib.Path(__file__).parent.parent
_KITE = "https://api.kite.trade"
_INDEX = {"NIFTY": 256265, "SENSEX": 265}
_FUT_SRC = {"NIFTY": "NFO", "SENSEX": "BFO"}
_LOT = {"NIFTY": 65, "SENSEX": 20}
_BUF = {"NIFTY": 5.0, "SENSEX": 15.0}        # close must clear VWAP by this many points
_TGT = {"NIFTY": 40.0, "SENSEX": 130.0}      # ≈ +20% on an ATM option at 0.5 delta
_KITE_TF = {"3m": "3minute", "5m": "5minute"}
_CHUNK_DAYS = 60

TRENDS = ("st+ema", "st", "ema", "none")
EXITS = ("ride", "tgt")


# ── setup ─────────────────────────────────────────────────────────────────────

def _env() -> dict:
    env = {}
    p = _ROOT / ".env"
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _hdrs(env: dict) -> dict:
    return {"X-Kite-Version": "3",
            "Authorization": f"token {env.get('KITE_API_KEY', '')}:{env.get('KITE_ACCESS_TOKEN', '')}"}


def _fetch(token: int, tf: str, start: datetime.date, end: datetime.date, env: dict) -> pd.DataFrame:
    frames, cur = [], start
    while cur <= end:
        to = min(end, cur + datetime.timedelta(days=_CHUNK_DAYS))
        r = requests.get(f"{_KITE}/instruments/historical/{token}/{_KITE_TF[tf]}",
                         headers=_hdrs(env),
                         params={"from": f"{cur} 09:15:00", "to": f"{to} 15:30:00",
                                 "continuous": 0, "oi": 0}, timeout=30)
        if not r.ok:
            raise RuntimeError(f"Kite {r.status_code}: {r.text[:200]}")
        c = r.json().get("data", {}).get("candles", [])
        if c:
            frames.append(pd.DataFrame(c, columns=["dt", "Open", "High", "Low", "Close", "Volume"]))
        cur = to + datetime.timedelta(days=1)
        time.sleep(0.4)
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames)
    df["dt"] = pd.to_datetime(df["dt"])
    df["dt"] = (df["dt"].dt.tz_convert("Asia/Kolkata") if df["dt"].dt.tz is not None
                else df["dt"].dt.tz_localize("Asia/Kolkata")).dt.tz_localize(None)
    return df.drop_duplicates("dt").set_index("dt").sort_index().between_time("09:15", "15:29")


def _futures(symbol: str, env: dict) -> pd.DataFrame:
    """Listed futures of symbol: instrument_token, expiry (oldest first)."""
    r = requests.get(f"{_KITE}/instruments/{_FUT_SRC[symbol]}", headers=_hdrs(env), timeout=30)
    r.raise_for_status()
    df = pd.read_csv(StringIO(r.text))
    m = df[(df["name"] == symbol) & (df["instrument_type"] == "FUT")].copy()
    m["expiry"] = pd.to_datetime(m["expiry"]).dt.date
    return m.sort_values("expiry")[["instrument_token", "tradingsymbol", "expiry"]]


def futures_volume(symbol: str, tf: str, start, end, env: dict) -> pd.Series:
    """Per candle, the volume of the nearest-expiry listed future that traded then."""
    vols = []
    for _, f in _futures(symbol, env).iterrows():
        d = _fetch(int(f["instrument_token"]), tf, start, min(end, f["expiry"]), env)
        if not d.empty:
            v = d["Volume"]
            vols.append(v[v.index.date <= f["expiry"]])
    if not vols:
        return pd.Series(dtype=float)
    out = pd.concat(vols)
    return out[~out.index.duplicated(keep="first")].sort_index()   # nearest expiry wins


# ── indicators ────────────────────────────────────────────────────────────────

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    c = df["Close"]
    df["ema7"] = c.ewm(span=7, adjust=False).mean()
    df["ema25"] = c.ewm(span=25, adjust=False).mean()
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    df["rsi"] = 100 - 100 / (1 + up / dn.replace(0, np.nan))

    tp = (df["High"] + df["Low"] + df["Close"]) / 3
    v = df["Volume"].astype(float)
    day = df.index.date
    df["vwap"] = (tp * v).groupby(day).cumsum() / v.groupby(day).cumsum().replace(0, np.nan)

    # Supertrend(10, 2), Wilder ATR — same as screener/trend_filter.supertrend_up
    h, l, cl = df["High"].values, df["Low"].values, c.values
    n, mult = 10, 2.0
    st = np.full(len(df), np.nan)
    if len(df) > n + 1:
        tr = np.r_[h[0] - l[0], np.maximum.reduce([h[1:] - l[1:], abs(h[1:] - cl[:-1]), abs(l[1:] - cl[:-1])])]
        atr = tr[:n].mean()
        upper = lower = None
        upt = True
        for i in range(n - 1, len(df)):
            if i >= n:
                atr = (atr * (n - 1) + tr[i]) / n
            mid = (h[i] + l[i]) / 2
            bu, bl = mid + mult * atr, mid - mult * atr
            if upper is None:
                upper, lower, upt = bu, bl, bool(cl[i] > bu)
            else:
                upper = bu if (bu < upper or cl[i - 1] > upper) else upper
                lower = bl if (bl > lower or cl[i - 1] < lower) else lower
                if upt and cl[i] < lower:
                    upt = False
                elif not upt and cl[i] > upper:
                    upt = True
            st[i] = 1.0 if upt else -1.0
    df["st"] = st
    return df


# ── simulation ────────────────────────────────────────────────────────────────

def _trend_ok(row_prev, row, side: str, trend: str) -> bool:
    want = 1 if side == "CE" else -1
    st_ok = row_prev["st"] == want                       # Supertrend on completed candles before
    ema_ok = (row["ema7"] > row["ema25"]) if side == "CE" else (row["ema7"] < row["ema25"])
    return {"st+ema": st_ok and ema_ok, "st": st_ok, "ema": ema_ok, "none": True}[trend]


def simulate(df: pd.DataFrame, symbol: str, trend: str, exit_mode: str, tf_min: int) -> list[dict]:
    buf, tgt = _BUF[symbol], _TGT[symbol]
    trades, pos = [], None
    rows = df.rename_axis("dt").reset_index().to_dict("records")
    for i in range(1, len(rows)):
        r, p = rows[i], rows[i - 1]
        t_close = (r["dt"] + pd.Timedelta(minutes=tf_min)).time()
        same_day = p["dt"].date() == r["dt"].date()

        if pos:   # manage the open trade on this candle
            ce = pos["side"] == "CE"
            px = None
            if ce and r["Low"] <= pos["stop"]:
                px, why = pos["stop"], "candle low broken"
            elif not ce and r["High"] >= pos["stop"]:
                px, why = pos["stop"], "candle high broken"
            elif exit_mode == "tgt" and ce and r["High"] >= pos["entry"] + tgt:
                px, why = pos["entry"] + tgt, "target"
            elif exit_mode == "tgt" and not ce and r["Low"] <= pos["entry"] - tgt:
                px, why = pos["entry"] - tgt, "target"
            elif not np.isnan(r["vwap"]) and (r["Close"] < r["vwap"] if ce else r["Close"] > r["vwap"]):
                px, why = r["Close"], "close back across VWAP"
            elif t_close >= datetime.time(15, 15) or not same_day:
                px, why = (r["Close"] if same_day else p["Close"]), "15:15"
            if px is not None:
                pts = (px - pos["entry"]) if ce else (pos["entry"] - px)
                trades.append({**pos, "exit_t": r["dt"] + pd.Timedelta(minutes=tf_min), "exit": round(px, 2),
                               "pts": round(pts, 2), "why": why})
                pos = None
            continue

        if not same_day or np.isnan(r["vwap"]) or np.isnan(p["st"]) or np.isnan(r["rsi"]):
            continue
        if not (datetime.time(9, 30) <= t_close <= datetime.time(15, 0)):
            continue
        vw, prev_vw = r["vwap"], p["vwap"]
        below_before = r["Open"] < vw or (not np.isnan(prev_vw) and p["Close"] < prev_vw)
        above_before = r["Open"] > vw or (not np.isnan(prev_vw) and p["Close"] > prev_vw)
        side = None
        if below_before and r["Close"] >= vw + buf and 51 <= r["rsi"] <= 65:
            side = "CE"
        elif above_before and r["Close"] <= vw - buf and 35 <= r["rsi"] <= 49:
            side = "PE"
        if side and _trend_ok(p, r, side, trend):
            pos = {"day": r["dt"].date(), "entry_t": r["dt"] + pd.Timedelta(minutes=tf_min),
                   "side": side, "entry": round(r["Close"], 2), "vwap": round(vw, 2),
                   "stop": r["Low"] if side == "CE" else r["High"], "rsi": round(r["rsi"], 1)}
    return trades


def stats(trades: list[dict], lot: int) -> dict:
    if not trades:
        return {"n": 0, "win": 0, "pts": 0.0, "avg": 0.0, "worst_day": 0.0, "dd": 0.0, "rs": 0}
    pts = pd.Series([t["pts"] for t in trades])
    by_day = pd.Series([t["pts"] for t in trades], index=[t["day"] for t in trades]).groupby(level=0).sum()
    eq = pts.cumsum()
    return {"n": len(pts), "win": round(100 * float((pts > 0).mean())), "pts": round(float(pts.sum()), 1),
            "avg": round(float(pts.mean()), 1), "worst_day": round(float(by_day.min()), 1),
            "dd": round(float((eq - eq.cummax()).min()), 1), "rs": int(pts.sum() * 0.5 * lot)}


def _push(env: dict, title: str, body: str) -> None:
    topic = env.get("NTFY_TOPIC", "")
    if not topic:
        print("(no NTFY_TOPIC in .env — not pushed)")
        return
    tag = env.get("KITE_NAME", "")
    title = f"[{tag}] {title}" if tag else title
    try:
        requests.post(f"https://ntfy.sh/{topic}", data=body.encode(),
                      headers={"Title": title.encode(), "Priority": "default", "Tags": "chart"},
                      timeout=10)
        print("pushed to ntfy")
    except Exception as e:
        print(f"ntfy failed: {e}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("symbols", nargs="*", default=["NIFTY", "SENSEX"])
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--tf", nargs="*", default=["3m", "5m"], choices=list(_KITE_TF))
    ap.add_argument("--trades", action="store_true", help="list each trade of the st+ema / ride variant")
    ap.add_argument("--push", action="store_true", help="send the summary to ntfy")
    a = ap.parse_args()

    env = _env()
    end = datetime.date.today()
    start = end - datetime.timedelta(days=a.days)
    lines, csv_rows = [], []
    for sym in [s.upper() for s in a.symbols]:
        for tf in a.tf:
            print(f"\n{sym} {tf}: fetching {start} → {end} …")
            idx = _fetch(_INDEX[sym], tf, start, end, env)
            vol = futures_volume(sym, tf, start, end, env)
            idx["Volume"] = vol.reindex(idx.index).fillna(0).values
            has = idx.groupby(idx.index.date)["Volume"].transform("sum") > 0
            days_all, days_ok = len(set(idx.index.date)), len(set(idx[has].index.date))
            print(f"  {days_ok}/{days_all} days have futures volume (others skipped)")
            df = add_indicators(idx)
            df.loc[~has, "vwap"] = np.nan
            tf_min = int(tf[:-1])
            hdr = f"{sym} {tf} ({days_ok} days)"
            print(f"  {'trend':7} {'exit':5} {'trades':>6} {'win%':>5} {'points':>8} {'avg':>6} "
                  f"{'worstDay':>9} {'maxDD':>8} {'~₹/lot':>9}")
            lines.append(hdr)
            for trend in TRENDS:
                for ex in EXITS:
                    tr = simulate(df, sym, trend, ex, tf_min)
                    s = stats(tr, _LOT[sym])
                    print(f"  {trend:7} {ex:5} {s['n']:>6} {s['win']:>5} {s['pts']:>8} {s['avg']:>6} "
                          f"{s['worst_day']:>9} {s['dd']:>8} {s['rs']:>9}")
                    lines.append(f" {trend}/{ex}: {s['n']} tr, {s['win']}% win, {s['pts']:+} pts, "
                                 f"DD {s['dd']}, ~₹{s['rs']:,}/lot")
                    for t in tr:
                        csv_rows.append({"symbol": sym, "tf": tf, "trend": trend, "exit_mode": ex, **t})
                    if a.trades and trend == "st+ema" and ex == "ride":
                        for t in tr:
                            print(f"    {t['entry_t']:%d-%b %H:%M} {t['side']} @ {t['entry']} "
                                  f"(vwap {t['vwap']}, rsi {t['rsi']}) → {t['exit_t']:%H:%M} "
                                  f"{t['exit']}  {t['pts']:+}  [{t['why']}]")
    out = _ROOT / "data" / f"bt_vwap_cross_{end}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(csv_rows).to_csv(out, index=False)
    print(f"\ntrades → {out}")
    print("trend: st+ema = Supertrend(10,2) AND EMA7>25 · st = Supertrend only · ema = EMA only · none = every cross")
    print("exit:  ride = VWAP close-back / cross-candle low / 15:15 · tgt = same + fixed target "
          f"(NIFTY {_TGT['NIFTY']:g} / SENSEX {_TGT['SENSEX']:g} index pts)")
    if a.push:
        _push(env, f"VWAP cross backtest {a.days}d", "\n".join(lines))


if __name__ == "__main__":
    main()
