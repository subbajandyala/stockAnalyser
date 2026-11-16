"""
START trades report — read-only. How would each START order of a day have ended under the bot's own exit rules,
whether Kite filled it or rejected it (no money)?

The START lines and their order lines are read from oi_monitor.log (the day's block). Each one is then followed
through that day's saved snapshots exactly as the live bot does every 5 minutes: screener/oi_exit.check() on the
newest snapshot and the one ~10 min older (move failed / hard stop / take profit on the turn / profit lock /
15:15). Entry = the option's ask in the START line (what the order would have paid); exit = the option's bid in
the snapshot where an exit fires. P&L = (exit − entry) × the order's quantity, before charges.

Usage (Subba's machine):
  /root/venv/bin/python scripts/start_trades_report.py                    # the newest day in the log
  /root/venv/bin/python scripts/start_trades_report.py --date 2026-11-16
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
                      r"([\d.]+) .*— BUY ([\d.]+) (?:CE|PE) @ ₹([\d.]+)")
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
                 "level": float(m.group(5)), "strike": float(m.group(6)), "entry": float(m.group(7)),
                 "order": "", "ts": None, "qty": 0}
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
                s.update({"order": m.group(1), "ts": m.group(2), "qty": int(m.group(3)),
                          "why_order": m.group(5)[:60]})
                if m.group(1) == "✓":                   # filled: the real fill price is the entry
                    s["entry"] = float(m.group(4))
            elif "no order" in line or "WATCH" in line:
                s["order"] = s["order"] or line.strip()[:70]
    return [d for d in days if d]


def follow(s: dict, files: list) -> dict:
    tr = {"side": s["side"], "entry": s["entry"], "ts": s["ts"], "stop": s["stop"], "entry_spot": s["spot"],
          "level": s["level"]}
    later = [f for f in files if f.name[:4] > s["hm"]]
    last = None
    for f in later:
        hm = f.name[:4]
        g = pd.read_csv(f)
        g = g[g["name"] == s["name"]]
        pf = oi_start.snap_ago([x for x in files if x.name[:4] <= hm], hm, oi_start.WINDOW_MIN)
        gp = pd.read_csv(pf) if pf else None
        gp = gp[gp["name"] == s["name"]] if gp is not None else None
        kind, why, prem = oi_exit.check(tr, g, gp, hm)
        if tr.get("last_prem"):
            last = (hm, tr["last_prem"])
        if kind:
            return {"exit_hm": hm, "exit": prem, "kind": kind, "why": why}
    if last:
        return {"exit_hm": last[0], "exit": last[1], "kind": "end", "why": "no exit by the last snapshot"}
    return {"exit_hm": "", "exit": None, "kind": "n/a", "why": "option not in the snapshots"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD (default: the newest day in the log)")
    ap.add_argument("--log", default=str(mon.ROOT / "oi_monitor.log"))
    a = ap.parse_args()
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
        r = follow(s, files)
        pct = (r["exit"] / s["entry"] - 1) * 100 if r["exit"] else None
        rows.append({"time": f"{s['hm'][:2]}:{s['hm'][2:]}", "stock": s["name"],
                     "option": f"{s['strike']:g} {s['side']}", "qty": s["qty"],
                     "kite": "filled" if s["order"] == "✓" else "rejected", "entry": s["entry"],
                     "exit_at": f"{r['exit_hm'][:2]}:{r['exit_hm'][2:]}" if r["exit_hm"] else "",
                     "exit": r["exit"], "pct": round(pct, 1) if pct is not None else None,
                     "pnl": round((r["exit"] - s["entry"]) * s["qty"]) if r["exit"] else None,
                     "result": r["kind"], "why": r["why"][:90]})
    df = pd.DataFrame(rows)
    if df.empty:
        print(f"{day}: no START orders")
        return
    out = mon.ROOT / "data" / f"start_trades_{day}.csv"
    df.to_csv(out, index=False)
    pd.set_option("display.width", 250)
    pd.set_option("display.max_colwidth", 90)
    print(f"\n{day}: {len(df)} START orders (Kite: {sum(df['kite'] == 'filled')} filled, "
          f"{sum(df['kite'] == 'rejected')} rejected) — each followed with the bot's exit rules\n")
    print(df.drop(columns=["why"]).to_string(index=False))
    ok = df.dropna(subset=["pnl"])
    w, l_ = ok[ok["pnl"] > 0], ok[ok["pnl"] <= 0]
    print(f"\nwinners {len(w)} · losers {len(l_)} · total ₹{ok['pnl'].sum():+,.0f} before charges "
          f"(≈ ₹{60 * len(ok):,} charges for {len(ok)} round trips)")
    print("by exit: " + " · ".join(f"{k} {len(g)} (₹{g['pnl'].sum():+,.0f})" for k, g in ok.groupby("result")))
    print("\nwhy each exit fired:")
    for _, r in df.iterrows():
        print(f"  {r['stock']:<12} {r['result']:<7} {r['why']}")
    if skipped:
        print(f"\nnot ordered: {len(skipped)} START line(s) (after 14:30 / NIFTY against / no order line)")
    print(f"\nfull list → {out}")


if __name__ == "__main__":
    main()
