"""
START trades report — read-only. How would each START order of a day have ended under the bot's own exit rules,
whether Kite filled it or rejected it (no money)?

The START lines and their order lines are read from oi_monitor.log (the day's block). Each one is then followed
through that day's saved snapshots exactly as the live bot does every 5 minutes: screener/oi_exit.check() on the
newest snapshot and the one ~10 min older (move failed / hard stop / take profit on the turn / profit lock /
15:15). Entry = the option's ask in the START line (what the order would have paid); exit = the option's bid in
the snapshot where an exit fires. P&L is for ONE LOT of each trade (lot size from Kite's instrument list that day), as if the account had enough
money for every order: money made on the winners, money lost on the losers, the net, estimated charges, and the
most money that was needed at one time (all open trades' premium together).

Usage (Subba's machine):
  /root/venv/bin/python scripts/start_trades_report.py                    # the newest day in the log
  /root/venv/bin/python scripts/start_trades_report.py --date 2026-11-16
"""

import argparse
import datetime
import json
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
RETEST_RE = re.compile(r"^(🟢|🔴) (\S+) (CE|PE) — ✅ RETEST BUY ([\d.]+) (?:CE|PE) @ ₹([\d.]+): pulled back to "
                       r"([\d.]+) \(now ([\d.]+)")
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
        m = RETEST_RE.match(line)
        if m and cur is not None:                   # OI_ENTRY=retest: the buy comes on a later scan
            s = {"hm": last_t, "name": m.group(2), "side": m.group(3), "spot": float(m.group(7)),
                 "level": float(m.group(6)), "strike": float(m.group(4)), "entry": float(m.group(5)),
                 "order": "", "ts": None, "qty": 0}
            cur.append(s)
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
          "level": s["level"], "opened": s["hm"]}
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
            if why.startswith("stop-loss"):             # the SL order in Kite fills near its trigger
                prem = s["entry"] * (1 - oi_exit.SL_PCT / 100) * 0.97
            return {"exit_hm": hm, "exit": round(prem, 2), "kind": kind, "why": why}
    if last:
        return {"exit_hm": last[0], "exit": last[1], "kind": "end", "why": "no exit by the last snapshot"}
    return {"exit_hm": "", "exit": None, "kind": "n/a", "why": "option not in the snapshots"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD (default: the newest day in the log)")
    ap.add_argument("--log", default=str(mon.ROOT / "oi_monitor.log"))
    a = ap.parse_args()
    e = mon.env()                                   # the same exit settings as the live bot
    oi_exit.SL_PCT = float(e.get("OI_SL_ORDER") or 0)
    oi_exit.TP_MIN = float(e.get("OI_TP_MIN") or 0)
    oi_exit.TP_NEED_OI = e.get("OI_TP_NEED_OI", "0") == "1"
    oi_exit.TIME_STOP = int(float(e.get("OI_TIME_STOP") or 0))
    oi_exit.LOCK_AT_PCT = float(e.get("OI_LOCK_AT") or oi_exit.LOCK_AT_PCT)
    days = log_days(pathlib.Path(a.log))
    dates = sorted(d.name for d in mon.DATA.iterdir() if d.is_dir() and d.name[:2] == "20" and mon.snap_files(
        datetime.date.fromisoformat(d.name)))
    pairs = dict(zip(dates[-len(days):], days[-len(dates):]))
    day = a.date or (max(pairs) if pairs else None)
    if not day or day not in pairs:
        print(f"no START lines for {day} in {a.log}")
        return
    files = mon.snap_files(datetime.date.fromisoformat(day))
    uni_p = mon.DATA / f"universe_{day}.json"
    lots = {n: int(u.get("lot") or 0) for n, u in json.loads(uni_p.read_text()).items()} if uni_p.exists() else {}
    starts = [s for s in pairs[day] if s["order"] in ("✓", "✗") and s.get("stop")]
    skipped = [s for s in pairs[day] if s not in starts]
    rows = []
    for s in starts:
        r = follow(s, files)
        pct = (r["exit"] / s["entry"] - 1) * 100 if r["exit"] else None
        lot = lots.get(s["name"]) or s["qty"]          # 1 lot (the bot orders OI_LOTS=1 lot anyway)
        rows.append({"time": f"{s['hm'][:2]}:{s['hm'][2:]}", "stock": s["name"],
                     "option": f"{s['strike']:g} {s['side']}", "lot": lot,
                     "cost": round(s["entry"] * lot), "_in": s["hm"], "_out": r["exit_hm"] or "1530",
                     "kite": "filled" if s["order"] == "✓" else "rejected", "entry": s["entry"],
                     "exit_at": f"{r['exit_hm'][:2]}:{r['exit_hm'][2:]}" if r["exit_hm"] else "",
                     "exit": r["exit"], "pct": round(pct, 1) if pct is not None else None,
                     "pnl": round((r["exit"] - s["entry"]) * lot) if r["exit"] else None,
                     "result": r["kind"], "why": r["why"][:90]})
    df = pd.DataFrame(rows)
    if df.empty:
        print(f"{day}: no START orders")
        return
    out = mon.ROOT / "data" / f"start_trades_{day}.csv"
    df.drop(columns=["_in", "_out"]).to_csv(out, index=False)
    pd.set_option("display.width", 250)
    pd.set_option("display.max_colwidth", 90)
    print(f"\n{day}: {len(df)} START orders (Kite: {sum(df['kite'] == 'filled')} filled, "
          f"{sum(df['kite'] == 'rejected')} rejected) — each followed with the bot's exit rules\n")
    print(df.drop(columns=["why", "_in", "_out"]).to_string(index=False))
    ok = df.dropna(subset=["pnl"])
    w, l_ = ok[ok["pnl"] > 0], ok[ok["pnl"] <= 0]
    made, lost = w["pnl"].sum(), l_["pnl"].sum()
    charges = 60 * len(ok)                               # ≈ brokerage ₹40 + STT / exchange / GST / stamp per round trip
    times = sorted(set(ok["_in"]) | set(ok["_out"]))
    peak = max((ok[(ok["_in"] <= t) & (ok["_out"] > t)]["cost"].sum() for t in times), default=0)
    print(f"\nONE LOT of each trade:")
    print(f"  winners {len(w):>3}   money made  ₹{made:>+12,.0f}   (avg ₹{w['pnl'].mean() if len(w) else 0:+,.0f})")
    print(f"  losers  {len(l_):>3}   money lost  ₹{lost:>+12,.0f}   (avg ₹{l_['pnl'].mean() if len(l_) else 0:+,.0f})")
    print(f"  net before charges   ₹{made + lost:>+12,.0f}")
    print(f"  charges (≈ ₹60 × {len(ok)})   ₹{-charges:>+12,.0f}")
    print(f"  NET                  ₹{made + lost - charges:>+12,.0f}")
    print(f"  money needed at one time (open trades' premium together, peak): ₹{peak:,.0f}"
          f" · all {len(ok)} buys together ₹{ok['cost'].sum():,.0f}")
    print("by exit: " + " · ".join(f"{k} {len(g)} (₹{g['pnl'].sum():+,.0f})" for k, g in ok.groupby("result")))
    print("\nwhy each exit fired:")
    for _, r in df.iterrows():
        print(f"  {r['stock']:<12} {r['result']:<7} {r['why']}")
    if skipped:
        print(f"\nnot ordered: {len(skipped)} START line(s) (after 14:30 / NIFTY against / no order line)")
    print(f"\nfull list → {out}")


if __name__ == "__main__":
    main()
