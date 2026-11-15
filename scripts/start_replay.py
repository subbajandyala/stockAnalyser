"""
Start-trigger replay — read-only. Would the move-START trigger (screener/oi_start.py) have bought
earlier and cheaper than the old STRONG trigger on the saved days?

For every saved day: walk the snapshots in order and call oi_start.starts() exactly as the live bot
does (snapshots saved before 14 Nov have no VWAP / stock volume, so those two checks are skipped
there, and they are 10 min apart, so the "last ~10 min" is one snapshot back). Each start is
followed like oi_signals_report.py does (premium +TARGET % before −STOP %, close of day).
The STRONG signals the monitor logged (oi_monitor.log) are followed the same way, and the same
stock + side is matched to show the time / price difference.

Usage (Subba's machine):  /root/venv/bin/python scripts/start_replay.py [--target 20 --stop 20]
"""

import argparse
import datetime
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).parent))

import pandas as pd

import oi_signals_report as rep
from screener import oi_monitor as om
from screener import oi_start


def replay_day(day: datetime.date, base: pd.DataFrame | None) -> list[dict]:
    files = rep.market_files(day)
    snaps = {f.name[:4]: f for f in files}
    seen, out = set(), []
    for f in files:
        hm = f.name[:4]
        if hm >= "1500":
            break
        upto = [x for x in files if x.name[:4] <= hm]
        prev_p = oi_start.snap_ago(upto, hm, oi_start.WINDOW_MIN)
        if prev_p is None:
            continue
        now = pd.read_csv(f)
        prev = pd.read_csv(prev_p)
        window = [pd.read_csv(x, usecols=["name", "spot"]) for x in oi_start.range_files(upto, hm)]
        ctx = {a["name"]: a for a in om.analyse_all(now, base, None) if "error" not in a} if base is not None else {}
        ctx["_prev_hm"] = prev_p.name[:4]
        for s in oi_start.starts(now, prev, window, ctx, hm):
            key = (s["name"], s["signal"])
            if key in seen:
                continue
            seen.add(key)
            out.append({"day": day, "time": f"{hm[:2]}:{hm[2:]}", "name": s["name"], "side": s["signal"],
                        "strike": s["option"]["strike"], "ts": s["ts"], "entry": s["entry"], "spot": s["spot"],
                        "level": s["level"], "beyond": s["beyond_pct"], "why": " · ".join(s["why"])})
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=float, default=20.0)
    ap.add_argument("--stop", type=float, default=20.0)
    a = ap.parse_args()
    days = [d for d in rep.day_dirs() if rep.market_files(d)]
    rows = []
    for i, d in enumerate(days):
        base = pd.read_csv(rep.mon.snap_files(days[i - 1])[-1]) if i else None
        print(f"  replaying {d} …", flush=True)
        rows += replay_day(d, base)
    for r in rows:
        later = [x for x in days if x > r["day"]]
        r.update(rep.follow(r, a.target, a.stop, later[0] if later else None))
    log = rep.mon.ROOT / "oi_monitor.log"
    strong = [s for s in rep.from_log(log) if s.get("ts") and s["day"] in days] if log.exists() else []
    for s in strong:
        later = [x for x in days if x > s["day"]]
        s.update(rep.follow(s, a.target, a.stop, later[0] if later else None))
    if not rows:
        print("no starts found on the saved days")
        return
    df = pd.DataFrame(rows)
    df.to_csv(rep.mon.ROOT / "data" / "start_replay.csv", index=False)
    print(f"\nSTART trigger · {len(df)} trades · success = premium +{a.target:g}% before −{a.stop:g}%\n")
    by = {(s["day"], s["name"], s["side"]): s for s in strong}
    for _, r in df.iterrows():
        s = by.get((r["day"], r["name"], r["side"]))
        vs = (f"  | STRONG {s['time']} @ ₹{s['entry']:g} → {s['result']}" if s else "")
        close = f"close {r['close_pct']:+.0f}%" if pd.notna(r["close_pct"]) else ""
        print(f"  {r['day']} {r['time']} {r['name']:<11} {r['side']} @ ₹{r['entry']:g} (+{r['beyond']:.2f}% past "
              f"{r['level']:g}) → {r['result']} {close}{vs}")

    def summ(name: str, d: pd.DataFrame) -> str:
        s_, f_ = int((d["result"] == "SUCCESS").sum()), int((d["result"] == "FAILURE").sum())
        c = pd.to_numeric(d["close_pct"], errors="coerce").dropna().mean()
        mx = pd.to_numeric(d["max_pct"], errors="coerce").dropna().mean()
        mn = pd.to_numeric(d["min_pct"], errors="coerce").dropna().mean()
        f = lambda x: "n/a" if pd.isna(x) else f"{x:+.1f}%"
        return (f"{name:<16} {len(d):>4} trades · ✅ {s_} · ❌ {f_} · close {f(c)} avg · best {f(mx)} / "
                f"worst {f(mn)} avg after entry")
    print("\n" + summ("START trigger", df))
    if strong:
        print(summ("STRONG (logged)", pd.DataFrame(strong)))
        both = [(r, by[(r["day"], r["name"], r["side"])]) for _, r in df.iterrows()
                if (r["day"], r["name"], r["side"]) in by]
        if both:
            earlier = sum(r["time"] < s["time"] for r, s in both)
            print(f"same stock + side in both: {len(both)} · START came earlier in {earlier}")
    print(f"\nfull list → {rep.mon.ROOT / 'data' / 'start_replay.csv'}")


if __name__ == "__main__":
    main()
