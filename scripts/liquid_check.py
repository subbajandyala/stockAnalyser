"""
Liquid-list check — read-only. What OI_LIQUID_TOP / OI_MAX_SPREAD would have removed.

For every saved day: the stocks ranked by the previous day's traded value of their ATM ±2
options (CE + PE, from the previous day's last snapshot), then every signal in oi_monitor.log
(or --replay) marked KEPT / REMOVED, with how its option went (premium +20% before −20%).

Usage (Subba's machine):
  /root/venv/bin/python scripts/liquid_check.py                 # top 120, spread 2%
  /root/venv/bin/python scripts/liquid_check.py --top 100 --spread 1.5
"""

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).parent))

import pandas as pd

import oi_signals_report as rep
from screener import oi_monitor as om


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=120)
    ap.add_argument("--spread", type=float, default=2.0, help="max ATM spread %% at the signal")
    ap.add_argument("--target", type=float, default=20.0)
    ap.add_argument("--stop", type=float, default=20.0)
    ap.add_argument("--replay", action="store_true", help="rebuild signals from snapshots (slow)")
    a = ap.parse_args()

    days = rep.day_dirs()
    ranks = {}
    for i, d in enumerate(days):
        prev = [x for x in days[:i] if rep.market_files(x) or rep.mon.snap_files(x)]
        if not prev:
            continue
        files = rep.mon.snap_files(prev[-1])
        r = om.liquid_rank(pd.read_csv(files[-1])) if files else None
        if r is not None:
            ranks[d] = r
            top = r[r["rank"] <= a.top]
            print(f"{d}: list from {prev[-1]} {files[-1].name} · {len(r)} stocks · "
                  f"#{a.top} trades ₹{top['value_cr'].iloc[-1]:.2f} Cr of ATM±2 options · "
                  f"bottom 10: {', '.join(r['name'].tail(10))}")
    if a.replay:
        sigs = [s for d in days for s in rep.replay(d)]
    else:
        log = rep.mon.ROOT / "oi_monitor.log"
        sigs = [s for s in rep.from_log(log) if s.get("ts")] if log.exists() else []
    rows = []
    for s in sigs:
        r = ranks.get(s["day"])
        if r is None:
            continue
        hit = r[r["name"] == s["name"]]
        rank = int(hit["rank"].iloc[0]) if len(hit) else None
        spread = None
        for f in rep.market_files(s["day"]):
            if f.name[:4] >= s["time"].replace(":", ""):
                x = rep.read(f)
                x = x[x["ts"] == s["ts"]]
                if len(x) and x.iloc[0]["ltp"]:
                    o = x.iloc[0]
                    spread = (o["ask"] - o["bid"]) / o["ltp"] * 100 if o["bid"] and o["ask"] else 999
                break
        why = []
        if rank is None or rank > a.top:
            why.append(f"rank {rank or '—'}")
        if spread is not None and spread > a.spread:
            why.append(f"spread {spread:.1f}%")
        later = [x for x in days if x > s["day"] and rep.market_files(x)]
        f = rep.follow(s, a.target, a.stop, later[0] if later else None)
        rows.append({**s, "rank": rank, "spread": spread, "keep": not why, "why": ", ".join(why), **f})
    if not rows:
        print("no signals on days with a previous-day snapshot")
        return
    df = pd.DataFrame(rows)
    df.to_csv(rep.mon.ROOT / "data" / "liquid_check.csv", index=False)
    print(f"\nsignals · KEPT = in yesterday's top {a.top} and ATM spread ≤ {a.spread:g}%\n")
    for _, x in df.iterrows():
        close = f"close {x['close_pct']:+.0f}%" if pd.notna(x["close_pct"]) else ""
        print(f"  {x['day']} {x['time']} {x['name']:<11} {x['side']} @ ₹{x['entry']:g}  "
              f"rank {x['rank'] if pd.notna(x['rank']) else '—'}  "
              f"{'KEPT   ' if x['keep'] else 'REMOVED'} {x['why']:<22} → {x['result']} {close}")
    for name, g in (("KEPT", df[df["keep"]]), ("REMOVED", df[~df["keep"]])):
        s_, f_ = int((g["result"] == "SUCCESS").sum()), int((g["result"] == "FAILURE").sum())
        c = g["close_pct"].dropna().mean()
        print(f"\n{name}: {len(g)} signals · ✅ {s_} · ❌ {f_}" + (f" · close {c:+.1f}% avg" if pd.notna(c) else ""))


if __name__ == "__main__":
    main()
