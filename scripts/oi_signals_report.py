"""
OI monitor signals report — every STRONG CE / PE signal and how it went. Read-only.

The OI monitor saves a snapshot every 10 minutes (data/oi/<date>/<HHMM>.csv.gz) with every
option's price. This script replays those scans with the monitor's own rules
(screener/oi_monitor.py, same base / "recent" snapshots), finds each NEW signal (first per
stock and side per day, as pushed), then follows that exact ATM option through the rest of
the day's snapshots:

  entry     the option's ask (else LTP) in the signal's snapshot — what the order would pay
  max / min the best and worst premium after the signal (10-min snapshots)
  close     the premium in the day's last snapshot
  result    SUCCESS  the premium reached +TARGET % before falling −STOP %
            FAILURE  it fell −STOP % first
            OPEN ▲ / OPEN ▼   neither by the close: still up / down at the last snapshot
            (snapshots are 10 minutes apart, so a target and a stop inside one gap are
            judged by the order the snapshots show them)
  next day  the premium in the next trading day's last snapshot, when one exists (NRML holds)

Usage (Subba's machine):
  /root/venv/bin/python scripts/oi_signals_report.py                  # all saved days
  /root/venv/bin/python scripts/oi_signals_report.py --date 2026-11-06
  /root/venv/bin/python scripts/oi_signals_report.py --target 30 --stop 25 --push
Kite only lists today's orders, so order fills are not part of this report — see the log
(grep order oi_monitor.log) for what Kite did with each order.
"""

import argparse
import datetime
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).parent))

import pandas as pd

import oi_monitor_bot as mon
from screener import oi_monitor as om

RUN_FROM, RUN_TO = "0920", "1531"


def day_dirs() -> list[datetime.date]:
    if not mon.DATA.exists():
        return []
    out = []
    for d in mon.DATA.iterdir():
        try:
            out.append(datetime.date.fromisoformat(d.name))
        except ValueError:
            pass
    return sorted(out)


def market_files(day: datetime.date) -> list[pathlib.Path]:
    return [f for f in mon.snap_files(day) if RUN_FROM <= f.name[:4] <= RUN_TO]


def replay(day: datetime.date) -> list[dict]:
    """Signals of one day, in order, with the scan they came from."""
    seen, out = set(), []
    for f in market_files(day):
        hm = f.name[:4]
        snap = pd.read_csv(f)
        base_p, recent_p = mon.base_and_recent(day, hm)
        base, recent = mon.load(base_p), mon.load(recent_p)
        for r in om.analyse_all(snap, base, recent):
            side = r.get("signal")
            if not side or (r["name"], side) in seen:
                continue
            seen.add((r["name"], side))
            row = snap[(snap["name"] == r["name"]) & (snap["strike"] == r["option"]["strike"])
                       & (snap["type"] == side)].iloc[0]
            out.append({"day": day, "time": f"{hm[:2]}:{hm[2:]}", "name": r["name"], "side": side,
                        "strike": float(r["option"]["strike"]), "ts": row["ts"], "spot": float(r["spot"]),
                        "price_pct": r["price_pct"], "score": r["ce_score"] if side == "CE" else r["pe_score"],
                        "entry": float(row["ask"] or row["ltp"]), "fut": r["fut"],
                        "call_pct": r["call_pct"], "put_pct": r["put_pct"]})
    return out


def follow(sig: dict, target: float, stop: float, next_day: datetime.date | None) -> dict:
    """Track the signal's option through the day's later snapshots."""
    day, hm = sig["day"], sig["time"].replace(":", "")
    path = []
    for f in market_files(day):
        if f.name[:4] <= hm:
            continue
        s = pd.read_csv(f)
        x = s[s["ts"] == sig["ts"]]
        if len(x):
            path.append((f.name[:2] + ":" + f.name[2:4], float(x.iloc[0]["ltp"]), float(x.iloc[0]["spot"])))
    e = sig["entry"]
    res = {"result": "NO DATA", "max_pct": None, "min_pct": None, "close_pct": None, "hit_time": "",
           "spot_close": None, "next_pct": None}
    if not path or not e:
        return res
    hit = None
    for t, px, _ in path:
        if px >= e * (1 + target / 100):
            hit = ("SUCCESS", t)
            break
        if px <= e * (1 - stop / 100):
            hit = ("FAILURE", t)
            break
    prices = [p for _, p, _ in path]
    close = prices[-1]
    res.update({"max_pct": round((max(prices) / e - 1) * 100, 1), "min_pct": round((min(prices) / e - 1) * 100, 1),
                "close_pct": round((close / e - 1) * 100, 1), "spot_close": path[-1][2]})
    if hit:
        res["result"], res["hit_time"] = hit
    else:
        res["result"] = "OPEN ▲" if close >= e else "OPEN ▼"
    if next_day:
        nf = market_files(next_day)
        if nf:
            s = pd.read_csv(nf[-1])
            x = s[s["ts"] == sig["ts"]]
            if len(x):
                res["next_pct"] = round((float(x.iloc[0]["ltp"]) / e - 1) * 100, 1)
    return res


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="one day, YYYY-MM-DD (default: every saved day)")
    ap.add_argument("--target", type=float, default=20.0, help="success: premium up this %% (default 20)")
    ap.add_argument("--stop", type=float, default=20.0, help="failure: premium down this %% (default 20)")
    ap.add_argument("--push", action="store_true", help="send the summary to OI_NTFY_TOPIC")
    a = ap.parse_args()

    days = day_dirs()
    if a.date:
        days = [d for d in days if str(d) == a.date]
    rows = []
    all_days = day_dirs()
    for d in days:
        later = [x for x in all_days if x > d and market_files(x)]
        for sig in replay(d):
            rows.append({**sig, **follow(sig, a.target, a.stop, later[0] if later else None)})
    if not rows:
        print("no signals in the saved snapshots" + (f" for {a.date}" if a.date else ""))
        return

    df = pd.DataFrame(rows)
    out = mon.ROOT / "data" / f"oi_signals_report_{datetime.date.today()}.csv"
    df.to_csv(out, index=False)
    print(f"OI monitor signals · success = premium +{a.target:g}% before −{a.stop:g}%\n")
    for day, g in df.groupby("day"):
        print(f"── {day} ({len(g)} signals)")
        for _, r in g.iterrows():
            nd = f" · next day {r['next_pct']:+.0f}%" if pd.notna(r["next_pct"]) else ""
            rng = (f"max {r['max_pct']:+.0f}% min {r['min_pct']:+.0f}% close {r['close_pct']:+.0f}%"
                   if pd.notna(r["max_pct"]) else "no later snapshot")
            print(f"  {r['time']} {'🟢' if r['side'] == 'CE' else '🔴'} {r['name']:<11} {r['spot']:>9g} "
                  f"({r['price_pct']:+.1f}%) BUY {r['strike']:g} {r['side']} @ ₹{r['entry']:g} · score {r['score']}/6"
                  f"\n        → {r['result']}{' at ' + r['hit_time'] if r['hit_time'] else ''} · {rng}{nd}")
    done = df[df["result"].isin(["SUCCESS", "FAILURE"])]
    n_s, n_f = int((df["result"] == "SUCCESS").sum()), int((df["result"] == "FAILURE").sum())
    n_up, n_dn = int((df["result"] == "OPEN ▲").sum()), int((df["result"] == "OPEN ▼").sum())
    close_avg = df["close_pct"].dropna().mean()
    lines = [f"OI signals report · {len(df)} signals on {df['day'].nunique()} day(s) · "
             f"success = +{a.target:g}% before −{a.stop:g}%",
             f"✅ success {n_s} · ❌ failure {n_f} · open ▲ {n_up} · open ▼ {n_dn}"
             + (f" · hit rate {round(100 * n_s / len(done))}% of decided" if len(done) else ""),
             f"average premium change at the close: {close_avg:+.1f}%" if pd.notna(close_avg) else ""]
    by = df.groupby("side")["close_pct"].agg(["count", "mean"]).round(1)
    for side, r in by.iterrows():
        lines.append(f"{side}: {int(r['count'])} signals, close {r['mean']:+.1f}% on average")
    print("\n" + "\n".join(l for l in lines if l))
    print(f"\nfull table → {out}")
    if a.push:
        e = mon.env()
        topic = e.get("OI_NTFY_TOPIC") or e.get("NTFY_TOPIC", "")
        if topic:
            body = "\n".join(l for l in lines if l) + "\n\n" + "\n".join(
                f"{r['day']} {r['time']} {r['name']} {r['side']} {r['strike']:g} @ ₹{r['entry']:g} → {r['result']}"
                + (f" (close {r['close_pct']:+.0f}%)" if pd.notna(r['close_pct']) else "") for _, r in df.iterrows())
            mon.push(topic, "OI signals report", body)
            print("pushed")


if __name__ == "__main__":
    main()
