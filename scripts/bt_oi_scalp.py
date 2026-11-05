"""
Backtest of the OI + MA pullback scalp on F&O stocks — read-only, places no orders.

Kite keeps no history of option OI by strike, so the OI side comes from the stock's FUTURES
(price + OI on 5-min candles, oi=1), compared with the previous day's close:
  CE side  long buildup / short covering (futures price up ≥ 0.3 %) and the stock up ≥ 0.5 %
  PE side  short buildup / long unwinding (futures down ≥ 0.3 %) and the stock down ≥ 0.5 %
Entry and exits are the live rules of screener/oi_scalp.py on the stock's 5-min chart
(20 EMA > 50 EMA, beyond the CPR, touch of the 20 / 50 EMA closing back, target R1 / S1
→ trail on 2 closes past the 10 SMA, stop on a close past the touched EMA, OI flip, 15:15).
No option-OI wall exists in history, so target 1 is R1 / S1 (the live bot uses the wall).

Variants (each also capped at 5 trades a day, first by time, like OIS_MAX_TRADES):
  base      the live rules: futures-OI side, 20 / 50 EMA touch, stop = close past the touched EMA,
            target R1 / S1 then trail on 2 closes past the 10 SMA, entries 09:30–14:45
  no_oi     base without the OI side (either side) — does the OI side help?
  strong    side needs the stock ±1.0 % (not 0.5) and futures OI moved ≥ 3 % vs yesterday
  confirm   the candle after the touch must also close on the trade's side (entry at its close)
  stop50    stop = close past the 50 EMA (more room than the touched 20 EMA)
  trail20   after target 1, trail on 2 closes past the 20 EMA (not the 10 SMA)
  hold      after target 1, hold to 15:15 (only an OI flip exits)
  window    entries 10:00–13:30 only
  liquid50  base on the 50 most traded stocks (average daily turnover)
  combo     strong + confirm + stop50 + trail20 + window;  combo_liq = combo on the 50 liquid stocks
  combo_hold  strong + confirm + stop50 + hold + window
Downloaded candles are cached in data/bt_cache/<date>/ (re-runs the same day need no Kite calls).

Results are in % of the stock price, entry and exit at candle closes, no costs.
Rough ₹ per lot = move × 0.5 delta × lot size, minus ₹100 costs — option premium itself
isn't in history (expired option contracts aren't served).
Kite serves futures history only for contracts still listed, so days before the oldest
listed contract's start have no OI and are skipped (the run prints how many days it used).

Usage (droplet):
  /root/venv/bin/python scripts/bt_oi_scalp.py --days 90                 # all F&O stocks
  /root/venv/bin/python scripts/bt_oi_scalp.py --days 90 --symbols SBIN,TCS --trades
  /root/venv/bin/python scripts/bt_oi_scalp.py --days 90 --push           # summary to OI_NTFY_TOPIC
"""

import argparse
import datetime
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).parent))

import numpy as np
import pandas as pd
from kiteconnect import KiteConnect

import oi_monitor_bot as mon
from screener import oi_scalp as sc

MIN_PRICE_PCT, MIN_FUT_PCT = 0.5, 0.3
LIQUID_N = 50
CAP_PER_DAY = 5
COST_RS = 100.0
CHUNK_DAYS = 95


def hist(kite, token: int, start: datetime.date, end: datetime.date, oi: bool = False) -> pd.DataFrame:
    frames, cur = [], start
    while cur <= end:
        to = min(end, cur + datetime.timedelta(days=CHUNK_DAYS))
        for attempt in range(3):
            try:
                rows = kite.historical_data(token, datetime.datetime.combine(cur, datetime.time(9, 15)),
                                            datetime.datetime.combine(to, datetime.time(15, 30)),
                                            "5minute", oi=oi)
                break
            except Exception as ex:
                rows = []
                if attempt == 2:
                    print(f"  history failed {token}: {ex}")
                time.sleep(1.5)
        time.sleep(0.35)
        if rows:
            frames.append(pd.DataFrame(rows))
        cur = to + datetime.timedelta(days=1)
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames)
    df["date"] = pd.to_datetime(df["date"])
    if df["date"].dt.tz is not None:
        df["date"] = df["date"].dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    return df.drop_duplicates("date").set_index("date").sort_index()


def futures_series(kite, futs: list[dict], start, end) -> pd.DataFrame:
    """Per candle, the nearest-expiry listed future that traded then (close, oi, contract)."""
    parts = []
    for f in sorted(futs, key=lambda r: r["expiry"]):
        d = hist(kite, f["instrument_token"], start, min(end, f["expiry"]), oi=True)
        if len(d):
            d = d[d.index.date <= f["expiry"]][["close", "oi"]].copy()
            d["contract"] = f["tradingsymbol"]
            parts.append(d)
    if not parts:
        return pd.DataFrame()
    out = pd.concat(parts)
    return out[~out.index.duplicated(keep="first")].sort_index()


def sides(stock: pd.DataFrame, fut: pd.DataFrame, min_price: float = MIN_PRICE_PCT,
          min_oi_pct: float = 0.0) -> pd.Series:
    """Futures-OI side per candle: 'CE', 'PE', None, or 'NA' (no futures OI to compare with)."""
    f = fut.reindex(stock.index)
    day = pd.Series(stock.index.date, index=stock.index)
    last_close = stock["close"].groupby(day).last().shift(1)
    f_last = fut.groupby(fut.index.date).agg({"close": "last", "oi": "last", "contract": "last"})
    f_prev = f_last.shift(1)
    pc = day.map(last_close)
    fpc, foi, fcon = day.map(f_prev["close"]), day.map(f_prev["oi"]), day.map(f_prev["contract"])
    same = f["contract"] == fcon                      # OI change only within one contract
    price_pct = (stock["close"] / pc - 1) * 100
    fut_pct = (f["close"] / fpc - 1) * 100
    oi_chg = f["oi"] - foi
    oi_pct = (oi_chg / foi.replace(0, np.nan)) * 100
    ok = same & f["close"].notna() & fpc.notna() & oi_chg.notna() & (oi_chg != 0)
    big = oi_pct.abs() >= min_oi_pct
    out = pd.Series([None] * len(stock), index=stock.index, dtype=object)
    out[ok & big & (price_pct >= min_price) & (fut_pct >= MIN_FUT_PCT)] = "CE"   # long buildup / short covering
    out[ok & big & (price_pct <= -min_price) & (fut_pct <= -MIN_FUT_PCT)] = "PE"  # short buildup / long unwinding
    out[~ok] = "NA"
    return out


def levels_by_day(df: pd.DataFrame) -> dict:
    out, days = {}, sorted(set(df.index.date))
    for i, d in enumerate(days[1:], 1):
        p = df[df.index.date == days[i - 1]]
        h, l, c = float(p["high"].max()), float(p["low"].min()), float(p["close"].iloc[-1])
        piv = (h + l + c) / 3
        bc = (h + l) / 2
        tc = 2 * piv - bc
        out[d] = {"top": max(tc, bc), "bottom": min(tc, bc), "r1": 2 * piv - l, "s1": 2 * piv - h}
    return out


BASE = {"use_oi": True, "side": "base", "confirm": False, "stop": "touched", "trail": "sma10",
        "window": (sc.ENTRY_FROM, sc.ENTRY_UNTIL), "liquid": False}
W2 = (datetime.time(10, 0), datetime.time(13, 30))
VARIANTS = {
    "base": {},
    "no_oi": {"use_oi": False},
    "strong": {"side": "strong"},
    "confirm": {"confirm": True},
    "stop50": {"stop": "50"},
    "trail20": {"trail": "ema20"},
    "hold": {"trail": "hold"},
    "window": {"window": W2},
    "liquid50": {"liquid": True},
    "combo": {"side": "strong", "confirm": True, "stop": "50", "trail": "ema20", "window": W2},
    "combo_liq": {"side": "strong", "confirm": True, "stop": "50", "trail": "ema20", "window": W2, "liquid": True},
    "combo_hold": {"side": "strong", "confirm": True, "stop": "50", "trail": "hold", "window": W2},
}


def simulate(name: str, df: pd.DataFrame, side_s: pd.Series, cfg: dict, lv: dict | None = None) -> list[dict]:
    """One trade per stock per day; live entry / exit rules (or the variant's) on closed 5-min candles."""
    lv = lv if lv is not None else levels_by_day(df)
    use_oi, w_from, w_to = cfg["use_oi"], cfg["window"][0], cfg["window"][1]
    trades = []
    idx = df.index
    o = df.to_dict("list")
    side_v = side_s.values
    days = np.array(idx.date)
    end_t = [(t + datetime.timedelta(minutes=5)).time() for t in idx]
    traded = set()
    n = len(df)
    i = 60
    while i < n:
        d = days[i]
        L = lv.get(d)
        if L is None or d in traded or idx[i].time() <= datetime.time(9, 15) or not (w_from <= end_t[i] <= w_to):
            i += 1
            continue
        oi_side = side_v[i]
        if use_oi and oi_side not in ("CE", "PE"):
            i += 1
            continue
        if not use_oi and oi_side == "NA":
            i += 1
            continue
        sig = None
        for side in ([oi_side] if use_oi else ["CE", "PE"]):
            cl, lo, hi, e20, e50 = o["close"][i], o["low"][i], o["high"][i], o["ema20"][i], o["ema50"][i]
            if side == "CE":
                if not (e20 > e50 and cl > L["top"]):
                    continue
                touched = next((nm for nm, e in (("20 EMA", e20), ("50 EMA", e50))
                                if lo <= e * (1 + sc.TOUCH_PCT) and cl >= e and (nm == "50 EMA" or lo > e50)), None)
                tgt = L["r1"]
                if touched and tgt > cl:
                    sig = (side, touched, tgt)
            else:
                if not (e20 < e50 and cl < L["bottom"]):
                    continue
                touched = next((nm for nm, e in (("20 EMA", e20), ("50 EMA", e50))
                                if hi >= e * (1 - sc.TOUCH_PCT) and cl <= e and (nm == "50 EMA" or hi < e50)), None)
                tgt = L["s1"]
                if touched and tgt < cl:
                    sig = (side, touched, tgt)
            if sig:
                break
        if not sig:
            i += 1
            continue
        side, ema_name, tgt = sig
        ce = side == "CE"
        if cfg["confirm"]:                 # the next candle must close on the trade's side too
            k = i + 1
            if k >= n or days[k] != d or not ((o["close"][k] > o["close"][i]) if ce else (o["close"][k] < o["close"][i])) \
                    or (use_oi and side_v[k] != side):
                i += 1
                continue
            i = k
        entry, entry_i = o["close"][i], i
        stop_col = "ema50" if cfg["stop"] == "50" else ("ema20" if ema_name == "20 EMA" else "ema50")
        trailing, why, j = False, None, i + 1
        while j < n and days[j] == d:
            cl = o["close"][j]
            if end_t[j] >= sc.SQUARE_OFF:
                why = "15:15"
            elif use_oi and side_v[j] == ("PE" if ce else "CE"):
                why = "OI flip"
            elif not trailing:
                if (cl < o[stop_col][j]) if ce else (cl > o[stop_col][j]):
                    why = "stop"
                elif (o["high"][j] >= tgt) if ce else (o["low"][j] <= tgt):
                    trailing = True
            elif cfg["trail"] != "hold":
                col = "sma10" if cfg["trail"] == "sma10" else "ema20"
                a, b = o["close"][j - 1], o[col][j - 1]
                if (cl < o[col][j] and a < b) if ce else (cl > o[col][j] and a > b):
                    why = "trail"
            if why:
                break
            j += 1
        if not why:                       # data ended inside the day
            j, why = min(j, n - 1), "data end"
        exit_px = o["close"][j]
        pct = ((exit_px / entry - 1) if ce else (1 - exit_px / entry)) * 100
        trades.append({"name": name, "day": d, "side": side, "ema": ema_name, "entry_t": idx[entry_i],
                       "exit_t": idx[j], "entry": round(entry, 2), "exit": round(exit_px, 2),
                       "pct": round(pct, 3), "why": why, "trailed": trailing})
        traded.add(d)
        i = j + 1
    return trades


def summary(tr: pd.DataFrame, lots: dict) -> dict:
    if tr.empty:
        return {"n": 0}
    rs = tr.apply(lambda r: r["entry"] * r["pct"] / 100 * 0.5 * lots.get(r["name"], 1) - COST_RS, axis=1)
    day = tr.groupby("day")["pct"].sum()
    win, loss = tr.loc[tr["pct"] > 0, "pct"], tr.loc[tr["pct"] <= 0, "pct"]
    return {"n": len(tr), "win": round(100 * (tr["pct"] > 0).mean()), "avg": round(tr["pct"].mean(), 3),
            "sum": round(tr["pct"].sum(), 1), "avg_win": round(float(win.mean()) if len(win) else 0.0, 2),
            "avg_loss": round(float(loss.mean()) if len(loss) else 0.0, 2),
            "rs": int(rs.sum()), "worst_day": round(day.min(), 2), "days": tr["day"].nunique()}


def load(kite, n: str, nse_tok: int, futs: list, warm, end, cache: pathlib.Path):
    sp, fp = cache / f"{n}_stock.pkl", cache / f"{n}_fut.pkl"
    if sp.exists() and fp.exists():
        return pd.read_pickle(sp), pd.read_pickle(fp)
    st = hist(kite, nse_tok, warm, end)
    fu = futures_series(kite, futs, warm, end) if len(st) >= 200 else pd.DataFrame()
    st.to_pickle(sp)
    fu.to_pickle(fp)
    return st, fu


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--symbols", help="comma list (default: all F&O stocks)")
    ap.add_argument("--variants", help="comma list of variants (default: all)")
    ap.add_argument("--trades", metavar="VARIANT", help="print every trade of one variant")
    ap.add_argument("--push", action="store_true")
    a = ap.parse_args()

    e = mon.env()
    kite = KiteConnect(e.get("KITE_API_KEY", ""))
    kite.set_access_token(e.get("KITE_ACCESS_TOKEN", ""))
    end = datetime.date.today()
    start = end - datetime.timedelta(days=a.days)
    warm = start - datetime.timedelta(days=7)          # EMA 50 warm-up + yesterday's CPR
    cache = mon.ROOT / "data" / "bt_cache" / f"{end}_{a.days}"
    cache.mkdir(parents=True, exist_ok=True)

    nfo = [r for r in kite.instruments("NFO") if r["instrument_type"] == "FUT" and r["expiry"] >= end
           and r["name"] not in mon.INDEXES]
    futs: dict = {}
    for r in nfo:
        futs.setdefault(r["name"], []).append(r)
    nse = {r["tradingsymbol"]: r["instrument_token"] for r in kite.instruments("NSE") if r["segment"] == "NSE"}
    names = sorted(n for n in futs if n in nse)
    if a.symbols:
        want = {s.strip().upper() for s in a.symbols.split(",")}
        names = [n for n in names if n in want]
    lots = {n: int(futs[n][0].get("lot_size") or 1) for n in names}
    variants = {k: {**BASE, **v} for k, v in VARIANTS.items()
                if not a.variants or k in a.variants.split(",")}
    print(f"{len(names)} stocks · {start} → {end} · {len(variants)} variants · cache {cache.name}")

    data, turnover, oi_days = {}, {}, set()
    t0 = time.time()
    for k, n in enumerate(names, 1):
        st, fu = load(kite, n, nse[n], futs[n], warm, end, cache)
        if len(st) < 200 or fu.empty:
            continue
        turnover[n] = float((st["close"] * st["volume"]).groupby(st.index.date).sum().mean()) if "volume" in st else 0.0
        st = sc.add_indicators(st[["open", "high", "low", "close"]])
        sd = {"base": sides(st, fu), "strong": sides(st, fu, 1.0, 3.0)}
        oi_days |= set(st.index[(sd["base"] != "NA").values & (st.index.date >= start)].date)
        data[n] = (st, sd, levels_by_day(st))
        if k % 20 == 0:
            print(f"  loaded {k}/{len(names)} · {time.time() - t0:.0f}s")
    liquid = set(sorted(turnover, key=turnover.get, reverse=True)[:LIQUID_N])

    all_tr = {v: [] for v in variants}
    for n, (st, sd, lv) in data.items():
        for v, cfg in variants.items():
            if cfg["liquid"] and n not in liquid:
                continue
            for t in simulate(n, st, sd[cfg["side"]], cfg, lv):
                if t["day"] >= start and t["day"] in oi_days:
                    all_tr[v].append(t)
    print(f"  simulated · {time.time() - t0:.0f}s")

    out_dir = mon.ROOT / "data"
    lines = [f"OI + MA scalp backtest · {len(data)} stocks · {start} → {end} · {len(oi_days)} days with futures OI"]
    print("\n" + lines[0])
    print(f"{'variant':<16}{'trades':>7}{'win%':>6}{'avg%':>8}{'sum%':>8}{'avgW%':>7}{'avgL%':>7}"
          f"{'worstDay%':>10}{'~₹/lot':>12}")
    for v in variants:
        tr = pd.DataFrame(all_tr[v])
        if not tr.empty:
            tr.to_csv(out_dir / f"bt_oi_scalp_{v}_{end}.csv", index=False)
        for label, t in ((v, tr), (v + " cap5", tr.sort_values("entry_t").groupby("day").head(CAP_PER_DAY)
                                   if not tr.empty else tr)):
            s = summary(t, lots)
            if not s["n"]:
                print(f"{label:<16}{0:>7}")
                continue
            print(f"{label:<16}{s['n']:>7}{s['win']:>6}{s['avg']:>8}{s['sum']:>8}{s['avg_win']:>7}"
                  f"{s['avg_loss']:>7}{s['worst_day']:>10}{s['rs']:>12,}")
            lines.append(f"{label}: {s['n']} tr, {s['win']}% win, avg {s['avg']:+.3f}%, ₹{s['rs']:,}/lot")
    if a.trades and a.trades in all_tr:
        tr = pd.DataFrame(all_tr[a.trades])
        if not tr.empty:
            print(f"\nby exit ({a.trades}):")
            print(tr.groupby("why")["pct"].agg(["count", "mean", "sum"]).round(2).to_string())
            tr["month"] = pd.to_datetime(tr["day"]).dt.strftime("%Y-%m")
            print(f"\nby month ({a.trades}):")
            print(tr.groupby("month")["pct"].agg(["count", "mean", "sum"]).round(2).to_string())
            print(f"\ntrades ({a.trades}):")
            for _, r in tr.sort_values("entry_t").iterrows():
                print(f"  {r['entry_t']:%d-%b %H:%M} {r['name']:<11} {r['side']} {r['ema']} @ {r['entry']:g} → "
                      f"{r['exit_t']:%H:%M} {r['exit']:g}  {r['pct']:+.2f}%  [{r['why']}]")
    print(f"\ntrades → data/bt_oi_scalp_<variant>_{end}.csv")
    if a.push:
        topic = e.get("OI_NTFY_TOPIC") or e.get("NTFY_TOPIC", "")
        if topic:
            mon.push(topic, "OI scalp backtest (variants)", "\n".join(lines))
            print("pushed")


if __name__ == "__main__":
    main()
