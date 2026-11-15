"""
Backtest — does the 5 EMA alert-candle exit (and the "don't chase" entry filter) beat the current
exits of the START trades? Read-only, Kite 5-min history, price only (Kite has no OI history for
expired option contracts, so the OI parts of the START entry / exit are left out).

Entry (price-only START, as the live bot sees it every 5 min): a 5-min close breaks the highest close
of the previous hour (candles 60 → 10 min back) by ≥ 0.1 %, the close 10 min ago was not above it,
≤ 0.6 % past the level, on the right side of VWAP within 1 %, last 10 min volume ≥ 1.5× the day's
average 10 min. 09:35 – 14:30, one trade per stock and side a day, one open trade per stock.
P&L in % of the STOCK price (equity bots trade the stock; option P&L moves with it, ~×5–10).

Exits, checked on each closed 5-min candle, filled at its close (like the live 5-min scan):
  always      15:15 · stock back past the broken level by 0.3 % · stock 1 % against (hard stop)
  current     take profit at ≥ +0.3 % when the stock gave back ≥ 35 % of its move AND turned vs 10 min
              ago (the 2 price signs of screener/oi_exit.py; the OI sign needs OI history) · lock:
              once +1 %, out when half is given back
  alert       opposite alert candle broken (screener/alert_candle.py), any time
  alert_p     the same, but only once the trade is ≥ +0.3 % (alerts that break earlier are ignored)
  both        whichever comes first
Each exit set is run with and without the don't-chase filter (alert_candle.stretched on the entry).

Usage (Subba's machine, ~3–6 min):
  /root/venv/bin/python scripts/alert_exit_bt.py               # 80 F&O stocks, last 60 trading days
  /root/venv/bin/python scripts/alert_exit_bt.py --stocks 150 --days 90
"""

import argparse
import datetime
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).parent))

import pandas as pd

import oi_monitor_bot as mon
from screener import alert_candle as ac
from screener import oi_rules as rules

INDEXES = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}
T_FROM, T_UNTIL, T_EXIT = datetime.time(9, 35), datetime.time(14, 30), datetime.time(15, 15)


def entries(d: pd.DataFrame, e5: pd.Series, r14: pd.Series) -> list[dict]:
    """Price-only START entries of one stock on one day (d = that day's 5-min candles)."""
    out, done = [], set()
    c, v = d["close"].to_numpy(float), d["volume"].to_numpy(float)
    tp = (d["high"] + d["low"] + d["close"]) / 3
    vwap = ((tp * d["volume"]).cumsum() / d["volume"].cumsum().replace(0, float("nan"))).to_numpy(float)
    for i in range(14, len(d)):
        t_close = (d.index[i] + pd.Timedelta(minutes=5)).time()
        if not T_FROM <= t_close <= T_UNTIL:
            continue
        win = c[i - 12:i - 1]
        hi, lo = win.max(), win.min()
        vol10, before = v[i - 1] + v[i], v[:i - 1]
        avg10 = before.sum() / max(1, len(before) / 2)
        if not avg10 or vol10 < 1.5 * avg10:
            continue
        for side, lvl in (("CE", hi), ("PE", lo)):
            if side in done:
                continue
            if side == "CE":
                ok = c[i] >= lvl * 1.001 and c[i - 2] <= lvl and (c[i] / lvl - 1) * 100 <= 0.6
                off = (c[i] / vwap[i] - 1) * 100
                ok = ok and 0 <= off <= 1.0
            else:
                ok = c[i] <= lvl * 0.999 and c[i - 2] >= lvl and (1 - c[i] / lvl) * 100 <= 0.6
                off = (c[i] / vwap[i] - 1) * 100
                ok = ok and -1.0 <= off <= 0
            if not ok:
                continue
            done.add(side)
            row = d.iloc[i]
            e, r = float(e5.loc[d.index[i]]), float(r14.loc[d.index[i]])
            if side == "CE":
                why = "candle above 5 EMA" if float(row["low"]) > e else (f"RSI {r:.0f}" if r > ac.RSI_HI else None)
            else:
                why = "candle below 5 EMA" if float(row["high"]) < e else (f"RSI {r:.0f}" if r < ac.RSI_LO else None)
            out.append({"i": i, "side": side, "level": lvl, "entry": c[i], "time": d.index[i], "stretched": why})
    return out


def run_exits(full: pd.DataFrame, d: pd.DataFrame, tr: dict) -> dict:
    """Exit time / price / reason per exit set for one trade."""
    long = tr["side"] == "CE"
    sgn = 1 if long else -1
    c = d["close"].to_numpy(float)
    e0 = tr["entry"]
    stop = tr["level"] * (0.997 if long else 1.003)
    upto = full[full.index <= d.index[-1]]
    alert = ac.exit_signal(upto, tr["side"], tr["time"] + pd.Timedelta(minutes=5))
    breaks = set(ac.exit_breaks(upto, tr["side"], tr["time"] + pd.Timedelta(minutes=5)))
    res = {}
    for mode in ("current", "alert", "alert_p", "both"):
        best, out = e0, None
        for j in range(tr["i"] + 1, len(d)):
            px, t = c[j], d.index[j]
            g = sgn * (px / e0 - 1) * 100
            best = max(best, px) if long else min(best, px)
            bg = sgn * (best / e0 - 1) * 100
            if (t + pd.Timedelta(minutes=5)).time() >= T_EXIT:
                out = (px, "15:15")
            elif (px < stop) if long else (px > stop):
                out = (px, "move failed")
            elif g <= -1.0:
                out = (px, "hard stop")
            elif mode in ("alert", "both") and alert and t >= alert["time"]:
                out = (px, "alert exit")
            elif mode == "alert_p" and g >= 0.3 and t in breaks:
                out = (px, "alert exit")
            elif mode in ("current", "both"):
                move = abs(best - e0)
                bounce = move / e0 * 100 >= 0.3 and abs(best - px) / move * 100 >= 35 if move else False
                turned = j >= 2 and ((px < c[j - 2]) if long else (px > c[j - 2]))
                if g >= 0.3 and bounce and turned:
                    out = (px, "turn")
                elif bg >= 1.0 and g <= bg / 2:
                    out = (px, "lock")
            if out:
                res[mode] = {"pct": sgn * (out[0] / e0 - 1) * 100, "why": out[1], "best": bg, "at": f"{t:%H:%M}"}
                break
        if mode not in res:
            px = c[-1]
            res[mode] = {"pct": sgn * (px / e0 - 1) * 100, "why": "day end", "best": sgn * (best / e0 - 1) * 100,
                         "at": "close"}
    return res


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stocks", type=int, default=80)
    ap.add_argument("--days", type=int, default=60, help="trading days (≈ calendar × 0.7)")
    a = ap.parse_args()
    e = mon.env()
    from kiteconnect import KiteConnect
    k = KiteConnect(e.get("KITE_API_KEY", ""))
    k.set_access_token(e.get("KITE_ACCESS_TOKEN", ""))
    k.profile()
    kd = rules.KiteData(k)
    names = sorted({r["name"] for r in k.instruments("NFO") if r["instrument_type"] == "FUT"} - INDEXES)
    rank = None
    files = sorted(mon.DATA.glob("20*/liquid.csv"))
    if files:
        rank = pd.read_csv(files[-1])
        names = [n for n in rank.sort_values("rank")["name"] if n in names]
    names = names[:a.stocks]
    end = datetime.date.today()
    start = end - datetime.timedelta(days=int(a.days / 0.7) + 7)
    print(f"{len(names)} stocks ({'top by option value' if rank is not None else 'A–Z'}), {start} → {end} …", flush=True)
    rows = []
    t0 = time.time()
    for n_i, name in enumerate(names, 1):
        try:
            full = kd.candles(name, "5minute", start, end)
        except Exception as ex:
            print(f"  {name}: {ex}")
            continue
        if full is None or full.empty or "volume" not in full:
            continue
        e5, r14 = ac.ema(full["close"]), ac.rsi(full["close"])
        for day in sorted(set(full.index.date)):
            d = full[full.index.date == day]
            if len(d) < 40:
                continue
            for tr in entries(d, e5, r14):
                ex = run_exits(full, d, tr)
                for mode, r in ex.items():
                    rows.append({"stock": name, "day": day, "time": f"{tr['time']:%H:%M}", "side": tr["side"],
                                 "stretched": tr["stretched"] or "", "exit_set": mode, **r})
        if n_i % 20 == 0:
            print(f"  {n_i}/{len(names)} stocks · {time.time() - t0:.0f}s", flush=True)
    if not rows:
        print("no trades")
        return
    df = pd.DataFrame(rows)
    out = mon.ROOT / "data" / f"alert_exit_bt_{end}.csv"
    df.to_csv(out, index=False)
    pd.set_option("display.width", 200)
    print(f"\n{df[df['exit_set'] == 'current'].shape[0]} trades · % = stock move in the trade's direction "
          f"(options ≈ ×5–10)\n")
    lines = []
    for filt in (False, True):
        sub = df[df["stretched"] == ""] if filt else df
        for mode in ("current", "alert", "alert_p", "both"):
            s = sub[sub["exit_set"] == mode]
            w, l_ = s[s["pct"] > 0], s[s["pct"] <= 0]
            kept = (s["pct"] / s["best"].where(s["best"] > 0)).dropna()
            lines.append({"don't-chase filter": "on" if filt else "off", "exit": mode, "trades": len(s),
                          "win %": round(100 * len(w) / len(s)) if len(s) else 0,
                          "avg %": round(s["pct"].mean(), 3), "total %": round(s["pct"].sum(), 1),
                          "avg win %": round(w["pct"].mean(), 2) if len(w) else 0,
                          "avg loss %": round(l_["pct"].mean(), 2) if len(l_) else 0,
                          "kept of best": f"{100 * kept.clip(-2, 1).mean():.0f}%" if len(kept) else "-"})
    print(pd.DataFrame(lines).to_string(index=False))
    sk = df[(df["exit_set"] == "current") & (df["stretched"] != "")]
    if len(sk):
        print(f"\ndon't-chase skipped {len(sk)} entries — they would have made {sk['pct'].mean():+.3f}% avg "
              f"(current exits), {100 * (sk['pct'] > 0).mean():.0f}% winners")
    for mode in ("current", "alert", "alert_p", "both"):
        s = df[(df["exit_set"] == mode) & (df["stretched"] == "")]
        print(f"exits ({mode}, filter on): " + " · ".join(
            f"{k} {v} ({s[s['why'] == k]['pct'].mean():+.2f}%)" for k, v in s["why"].value_counts().items()))
    print(f"\nfull list → {out}")


if __name__ == "__main__":
    main()
