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

Variants:
  oi        futures-OI side required (the strategy)
  no_oi     same chart rules, either side, no OI filter (does the OI side help?)
Each also capped at 5 trades a day (first by time), like OIS_MAX_TRADES.

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


def sides(stock: pd.DataFrame, fut: pd.DataFrame) -> pd.Series:
    """Futures-OI side per candle: 'CE', 'PE' or None (vs the previous day's last candle)."""
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
    ok = same & f["close"].notna() & fpc.notna() & oi_chg.notna() & (oi_chg != 0)
    out = pd.Series([None] * len(stock), index=stock.index, dtype=object)
    out[ok & (price_pct >= MIN_PRICE_PCT) & (fut_pct >= MIN_FUT_PCT)] = "CE"   # long buildup / short covering
    out[ok & (price_pct <= -MIN_PRICE_PCT) & (fut_pct <= -MIN_FUT_PCT)] = "PE"  # short buildup / long unwinding
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


def simulate(name: str, df: pd.DataFrame, side_s: pd.Series, use_oi: bool) -> list[dict]:
    """One trade per stock per day, live entry / exit rules on closed 5-min candles."""
    lv = levels_by_day(df)
    trades = []
    idx = df.index
    o = df.to_dict("list")
    side_v = side_s.values
    days = np.array(idx.date)
    end_t = [(t + datetime.timedelta(minutes=5)).time() for t in idx]
    traded = set()
    i = 60
    while i < len(df):
        d = days[i]
        L = lv.get(d)
        if L is None or d in traded or idx[i].time() <= datetime.time(9, 15) \
                or not (sc.ENTRY_FROM <= end_t[i] <= sc.ENTRY_UNTIL):
            i += 1
            continue
        oi_side = side_v[i]
        if use_oi and oi_side == "NA":
            i += 1
            continue
        cands = [oi_side] if use_oi else ["CE", "PE"]
        sig = None
        for side in cands:
            if side not in ("CE", "PE"):
                continue
            cl, lo, hi, e20, e50 = o["close"][i], o["low"][i], o["high"][i], o["ema20"][i], o["ema50"][i]
            if side == "CE":
                if not (e20 > e50 and cl > L["top"]):
                    continue
                touched = next((n for n, e in (("20 EMA", e20), ("50 EMA", e50))
                                if lo <= e * (1 + sc.TOUCH_PCT) and cl >= e and (n == "50 EMA" or lo > e50)), None)
                tgt = L["r1"]
                if touched and tgt > cl:
                    sig = (side, touched, tgt)
            else:
                if not (e20 < e50 and cl < L["bottom"]):
                    continue
                touched = next((n for n, e in (("20 EMA", e20), ("50 EMA", e50))
                                if hi >= e * (1 - sc.TOUCH_PCT) and cl <= e and (n == "50 EMA" or hi < e50)), None)
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
        entry, entry_i = o["close"][i], i
        col = "ema20" if ema_name == "20 EMA" else "ema50"
        trailing, why, j = False, None, i + 1
        while j < len(df) and days[j] == d:
            cl = o["close"][j]
            if end_t[j] >= sc.SQUARE_OFF:
                why = "15:15"
            elif use_oi and side_v[j] == ("PE" if ce else "CE"):
                why = "OI flip"
            elif not trailing:
                if (cl < o[col][j]) if ce else (cl > o[col][j]):
                    why = "stop"
                elif (o["high"][j] >= tgt) if ce else (o["low"][j] <= tgt):
                    trailing = True
            else:
                a, b = o["close"][j - 1], o["sma10"][j - 1]
                if ((cl < o["sma10"][j] and a < b) if ce else (cl > o["sma10"][j] and a > b)):
                    why = "trail"
            if why:
                break
            j += 1
        if not why:                       # data ended inside the day
            j, why = min(j, len(df) - 1), "data end"
        exit_px = o["close"][j]
        pct = ((exit_px / entry - 1) if ce else (1 - exit_px / entry)) * 100
        trades.append({"name": name, "day": d, "side": side, "ema": ema_name, "entry_t": idx[entry_i],
                       "exit_t": idx[j], "entry": round(entry, 2), "exit": round(exit_px, 2),
                       "pct": round(pct, 3), "why": why, "trailed": trailing,
                       "oi_side": side_v[entry_i]})
        traded.add(d)
        i = j + 1
    return trades


def summary(tr: pd.DataFrame, lots: dict) -> dict:
    if tr.empty:
        return {"n": 0}
    rs = tr.apply(lambda r: r["entry"] * r["pct"] / 100 * 0.5 * lots.get(r["name"], 1) - COST_RS, axis=1)
    day = tr.groupby("day")["pct"].sum()
    return {"n": len(tr), "win": round(100 * (tr["pct"] > 0).mean()), "avg": round(tr["pct"].mean(), 3),
            "sum": round(tr["pct"].sum(), 1), "avg_win": round(float(tr.loc[tr["pct"] > 0, "pct"].mean()) if (tr["pct"] > 0).any() else 0.0, 2),
            "avg_loss": round(float(tr.loc[tr["pct"] <= 0, "pct"].mean()) if (tr["pct"] <= 0).any() else 0.0, 2),
            "rs": int(rs.sum()), "worst_day": round(day.min(), 2), "days": tr["day"].nunique()}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--symbols", help="comma list (default: all F&O stocks)")
    ap.add_argument("--trades", action="store_true", help="print every trade of the oi variant")
    ap.add_argument("--push", action="store_true")
    a = ap.parse_args()

    e = mon.env()
    kite = KiteConnect(e.get("KITE_API_KEY", ""))
    kite.set_access_token(e.get("KITE_ACCESS_TOKEN", ""))
    end = datetime.date.today()
    start = end - datetime.timedelta(days=a.days)
    warm = start - datetime.timedelta(days=7)          # EMA 50 warm-up + yesterday's CPR

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
    print(f"{len(names)} stocks · {start} → {end} · fetching 5-min stock + futures (OI) history …")

    all_tr = {"oi": [], "no_oi": []}
    oi_days = set()
    t0 = time.time()
    for k, n in enumerate(names, 1):
        st = hist(kite, nse[n], warm, end)
        if len(st) < 200:
            continue
        st = sc.add_indicators(st[["open", "high", "low", "close"]])
        fu = futures_series(kite, futs[n], warm, end)
        if fu.empty:
            continue
        s_side = sides(st, fu)
        keep = st.index.date >= start
        oi_days |= set(st.index[(s_side != "NA").values & keep].date)
        for var, use in (("oi", True), ("no_oi", False)):
            for t in simulate(n, st, s_side, use):
                if t["day"] >= start:
                    all_tr[var].append(t)
        if k % 20 == 0:
            print(f"  {k}/{len(names)} stocks · {time.time() - t0:.0f}s")

    out_dir = mon.ROOT / "data"
    out_dir.mkdir(exist_ok=True)
    lines = [f"OI + MA scalp backtest · {len(names)} stocks · {start} → {end} · "
             f"{len(oi_days)} days with futures OI"]
    print("\n" + lines[0])
    print(f"{'variant':<14}{'trades':>7}{'win%':>6}{'avg%':>8}{'sum%':>8}{'avgW%':>7}{'avgL%':>7}"
          f"{'worstDay%':>10}{'~₹/lot':>11}")
    for var in ("oi", "no_oi"):
        tr = pd.DataFrame(all_tr[var])
        if var == "no_oi" and not tr.empty:
            tr = tr[tr["day"].isin(oi_days)]          # same days as the oi variant
        tr.to_csv(out_dir / f"bt_oi_scalp_{var}_{end}.csv", index=False)
        for label, t in ((var, tr), (var + " cap5", tr.sort_values("entry_t").groupby("day").head(CAP_PER_DAY)
                                     if not tr.empty else tr)):
            s = summary(t, lots)
            if not s["n"]:
                print(f"{label:<14}{0:>7}")
                lines.append(f" {label}: no trades")
                continue
            print(f"{label:<14}{s['n']:>7}{s['win']:>6}{s['avg']:>8}{s['sum']:>8}{s['avg_win']:>7}"
                  f"{s['avg_loss']:>7}{s['worst_day']:>10}{s['rs']:>11,}")
            lines.append(f" {label}: {s['n']} trades, {s['win']}% win, avg {s['avg']:+.2f}%, "
                         f"~₹{s['rs']:,} per lot")
    tr = pd.DataFrame(all_tr["oi"])
    if not tr.empty:
        print("\nby exit (oi):")
        print(tr.groupby("why")["pct"].agg(["count", "mean", "sum"]).round(2).to_string())
        print("\nby side / EMA (oi):")
        print(tr.groupby(["side", "ema"])["pct"].agg(["count", "mean", "sum"]).round(2).to_string())
        tr["month"] = pd.to_datetime(tr["day"]).dt.strftime("%Y-%m")
        print("\nby month (oi):")
        print(tr.groupby("month")["pct"].agg(["count", "mean", "sum"]).round(2).to_string())
        if a.trades:
            print("\ntrades (oi):")
            for _, r in tr.sort_values("entry_t").iterrows():
                print(f"  {r['entry_t']:%d-%b %H:%M} {r['name']:<11} {r['side']} {r['ema']} @ {r['entry']:g} → "
                      f"{r['exit_t']:%H:%M} {r['exit']:g}  {r['pct']:+.2f}%  [{r['why']}]")
    print(f"\ntrades → data/bt_oi_scalp_oi_{end}.csv, data/bt_oi_scalp_no_oi_{end}.csv")
    if a.push:
        topic = e.get("OI_NTFY_TOPIC") or e.get("NTFY_TOPIC", "")
        if topic:
            mon.push(topic, "OI scalp backtest", "\n".join(lines))
            print("pushed")


if __name__ == "__main__":
    main()
