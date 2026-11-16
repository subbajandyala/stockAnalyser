"""
START trades report (equity) — read-only. How would each START trade of a day have ended under the bot's own exit
rules, whether Kite filled it or rejected it?

This machine trades the STOCK (MIS): a CE start BUYs the stock, a PE start SELLs it short, for OIEQ_RS_PER_TRADE
rupees (quantity = rupees ÷ price). The START lines and their order lines are read from oi_monitor.log (the day's
block). Each trade is followed through that day's saved snapshots exactly as the live bot does every 5 minutes:
screener/oi_exit.check() in equity mode on the newest snapshot and the one ~10 min older (move failed / hard stop
−1 % / take profit on the turn from +0.3 % / profit lock from +1 % / 15:15).
  entry   the real fill when Kite filled it; else the price the order would have got (its LIMIT ÷ 1.002 for a buy,
          ÷ 0.998 for a short = the best ask / bid then), or the START price when there was no order price
  exit    the stock's price in the snapshot where an exit fires
P&L in rupees for the trade size, money made on winners / lost on losers, net, equity intraday charges
(brokerage 0.03 % capped ₹20 a side, STT 0.025 % on the sell, exchange, GST, stamp), and the most money in open
trades at one time (full value, and the MIS margin ≈ a fifth of it).

Usage (Srikanth's / Manjula's machine):
  /root/venv/bin/python scripts/start_trades_report.py                  # the newest day in the log
  /root/venv/bin/python scripts/start_trades_report.py --date 2026-11-16 --rs 20000
"""

import argparse
import datetime
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).parent))

import pandas as pd

import oi_monitor_bot as mon
from screener import oi_exit, oi_start

SCAN_RE = re.compile(r"^\[(\d\d):(\d\d)\] \d+ stocks")
START_RE = re.compile(r"^(🟢|🔴) (\S+) ([\d.]+).* — START (CE|PE): broke (?:above|below) the last hour's (?:high|low) "
                      r"([\d.]+) .*— (?:BUY|SHORT) the stock")
STOP_RE = re.compile(r"stop: stock back (?:below|above) ([\d.]+)")
ORDER_RE = re.compile(r"^\s+order (✓|✗) (\S+) (\d+) @ ₹([\d.]+)\s*\S* — (.*)$")


def log_days(log: pathlib.Path) -> list[list[dict]]:
    """START lines with their order lines, one list per day (a scan time earlier than the last = a new day)."""
    days, cur, last_t, s = [], None, None, None
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        m = SCAN_RE.match(line)
        if m:
            t = m.group(1) + m.group(2)
            if last_t is None or t < last_t:
                cur = []
                days.append(cur)
            last_t, s = t, None
            continue
        m = START_RE.match(line)
        if m and cur is not None:
            s = {"hm": last_t, "name": m.group(2), "side": m.group(4), "spot": float(m.group(3)),
                 "level": float(m.group(5)), "order": "", "price": 0.0}
            st = STOP_RE.search(line)
            if st:
                s["stop"] = float(st.group(1))
            cur.append(s)
            continue
        if s is not None:
            st = STOP_RE.search(line)
            if st and "stop" not in s:
                s["stop"] = float(st.group(1))
            m = ORDER_RE.match(line)
            if m:
                s.update({"order": m.group(1), "price": float(m.group(4)), "kite_why": m.group(5)[:70]})
            elif "no order" in line or "WATCH" in line:
                s["order"] = s["order"] or line.strip()[:70]
    return [d for d in days if d]


def charges(buy_val: float, sell_val: float) -> float:
    """Zerodha equity intraday, both legs: brokerage, STT, exchange, SEBI, GST, stamp."""
    brk = min(20.0, 0.0003 * buy_val) + min(20.0, 0.0003 * sell_val)
    stt = 0.00025 * sell_val
    exch = 0.0000297 * (buy_val + sell_val)
    sebi = 0.000001 * (buy_val + sell_val)
    gst = 0.18 * (brk + exch + sebi)
    stamp = 0.00003 * buy_val
    return brk + stt + exch + sebi + gst + stamp


def follow(s: dict, entry: float, files: list) -> dict:
    tr = {"side": s["side"], "entry": entry, "stop": s["stop"], "entry_spot": s["spot"], "level": s["level"],
          "mode": "equity", "ts": s["name"]}
    last = None
    for f in [f for f in files if f.name[:4] > s["hm"]]:
        hm = f.name[:4]
        g = pd.read_csv(f)
        g = g[g["name"] == s["name"]]
        pf = oi_start.snap_ago([x for x in files if x.name[:4] <= hm], hm, oi_start.WINDOW_MIN)
        gp = pd.read_csv(pf) if pf else None
        gp = gp[gp["name"] == s["name"]] if gp is not None else None
        kind, why, px = oi_exit.check(tr, g, gp, hm)
        if tr.get("last_spot"):
            last = (hm, tr["last_spot"])
        if kind:
            return {"exit_hm": hm, "exit": px, "kind": kind, "why": why}
    if last:
        return {"exit_hm": last[0], "exit": last[1], "kind": "end", "why": "no exit by the last snapshot"}
    return {"exit_hm": "", "exit": None, "kind": "n/a", "why": "stock not in the snapshots"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD (default: the newest day in the log)")
    ap.add_argument("--rs", type=float, help="rupees per trade (default: OIEQ_RS_PER_TRADE in .env, else 20000)")
    ap.add_argument("--log", default=str(mon.ROOT / "oi_monitor.log"))
    a = ap.parse_args()
    rs = a.rs or float(mon.env().get("OIEQ_RS_PER_TRADE") or 20000)
    days = log_days(pathlib.Path(a.log))
    dates = sorted(d.name for d in mon.DATA.iterdir() if d.is_dir() and d.name[:2] == "20" and mon.snap_files(
        datetime.date.fromisoformat(d.name)))
    pairs = dict(zip(dates[-len(days):], days[-len(dates):]))
    day = a.date or (max(pairs) if pairs else None)
    if not day or day not in pairs:
        print(f"no START lines for {day} in {a.log}")
        return
    files = mon.snap_files(datetime.date.fromisoformat(day))
    starts = [s for s in pairs[day] if s["order"] in ("✓", "✗") and s.get("stop")]
    skipped = [s for s in pairs[day] if s not in starts]
    rows = []
    for s in starts:
        long = s["side"] == "CE"
        if s["order"] == "✓":
            entry = s["price"]
        elif s["price"] > 0:
            entry = round(s["price"] / (1.002 if long else 0.998), 2)
        else:
            entry = s["spot"]
        qty = max(1, int(rs // entry))
        r = follow(s, entry, files)
        d = 1 if long else -1
        pnl = d * (r["exit"] - entry) * qty if r["exit"] else None
        ch = charges(*((entry * qty, r["exit"] * qty) if long else (r["exit"] * qty, entry * qty))) if r["exit"] else 0
        rows.append({"time": f"{s['hm'][:2]}:{s['hm'][2:]}", "stock": s["name"],
                     "trade": "BUY" if long else "SHORT", "qty": qty, "value": round(entry * qty),
                     "kite": "filled" if s["order"] == "✓" else "rejected", "entry": entry,
                     "exit_at": f"{r['exit_hm'][:2]}:{r['exit_hm'][2:]}" if r["exit_hm"] else "",
                     "exit": r["exit"], "pct": round(d * (r["exit"] / entry - 1) * 100, 2) if r["exit"] else None,
                     "pnl": round(pnl) if pnl is not None else None, "charges": round(ch),
                     "result": r["kind"], "why": r["why"][:90], "_in": s["hm"], "_out": r["exit_hm"] or "1530"})
    df = pd.DataFrame(rows)
    if df.empty:
        print(f"{day}: no START orders")
        return
    out = mon.ROOT / "data" / f"start_trades_{day}.csv"
    df.drop(columns=["_in", "_out"]).to_csv(out, index=False)
    pd.set_option("display.width", 250)
    print(f"\n{day}: {len(df)} START trades (Kite: {sum(df['kite'] == 'filled')} filled, "
          f"{sum(df['kite'] == 'rejected')} rejected) — ₹{rs:,.0f} each, followed with the bot's exit rules\n")
    print(df.drop(columns=["why", "_in", "_out"]).to_string(index=False))
    ok = df.dropna(subset=["pnl"])
    w, l_ = ok[ok["pnl"] > 0], ok[ok["pnl"] <= 0]
    made, lost, chg = w["pnl"].sum(), l_["pnl"].sum(), ok["charges"].sum()
    times = sorted(set(ok["_in"]) | set(ok["_out"]))
    peak = max((ok[(ok["_in"] <= t) & (ok["_out"] > t)]["value"].sum() for t in times), default=0)
    print(f"\n₹{rs:,.0f} per trade:")
    print(f"  winners {len(w):>3}   money made  ₹{made:>+10,.0f}   (avg ₹{w['pnl'].mean() if len(w) else 0:+,.0f})")
    print(f"  losers  {len(l_):>3}   money lost  ₹{lost:>+10,.0f}   (avg ₹{l_['pnl'].mean() if len(l_) else 0:+,.0f})")
    print(f"  net before charges   ₹{made + lost:>+10,.0f}")
    print(f"  charges ({len(ok)} round trips) ₹{-chg:>+10,.0f}")
    print(f"  NET                  ₹{made + lost - chg:>+10,.0f}")
    print(f"  invested: {len(ok)} trades × ₹{rs:,.0f} = ₹{ok['value'].sum():,.0f} traded in the day · most open at one "
          f"time ₹{peak:,.0f} (MIS margin ≈ ₹{peak / 5:,.0f})")
    print("by exit: " + " · ".join(f"{k} {len(g)} (₹{g['pnl'].sum():+,.0f})" for k, g in ok.groupby("result")))
    print("\nwhy each exit fired:")
    for _, r in df.iterrows():
        print(f"  {r['stock']:<12} {r['trade']:<5} {r['result']:<7} {r['why']}")
    if skipped:
        print(f"\nnot ordered: {len(skipped)} START line(s) (after 14:30 / NIFTY against / no order line)")
    print(f"\nfull list → {out}")


if __name__ == "__main__":
    main()
