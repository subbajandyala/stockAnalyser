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

By default it takes the signals the monitor actually pushed (and their order lines) from
oi_monitor.log; --replay re-runs every saved scan instead (slow on the 512 MB machine).

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
import re
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).parent))

import pandas as pd

import oi_monitor_bot as mon
from screener import oi_monitor as om
from screener import oi_rules as rules

RUN_FROM, RUN_TO = "0920", "1531"
_CACHE: dict = {}


COLS = ["name", "strike", "type", "ts", "oi", "ltp", "bid", "ask", "spot", "spot_close",
        "fut_ltp", "fut_close", "fut_oi"]
DTYPES = {"name": "category", "type": "category", "ts": "category", "strike": "float32", "oi": "float32",
          "ltp": "float32", "bid": "float32", "ask": "float32", "spot": "float32", "spot_close": "float32",
          "fut_ltp": "float32", "fut_close": "float32", "fut_oi": "float32"}


def read(p: pathlib.Path) -> pd.DataFrame:
    """Each snapshot is read once, only the needed columns, compact types (512 MB machine)."""
    if p not in _CACHE:
        df = pd.read_csv(p, usecols=lambda c: c in COLS, dtype=DTYPES)
        for c in ("name", "type", "ts"):
            df[c] = df[c].astype(str)
        _CACHE[p] = df
    return _CACHE[p]


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
    files = market_files(day)
    t0 = time.time()
    for n, f in enumerate(files, 1):
        hm = f.name[:4]
        print(f"  replay {day} {hm[:2]}:{hm[2:]} ({n}/{len(files)}) · {len(out)} signals · {time.time() - t0:.0f}s",
              flush=True)
        snap = read(f)
        base_p, recent_p = mon.base_and_recent(day, hm)
        base, recent = (read(base_p) if base_p else None), (read(recent_p) if recent_p else None)
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


SIG_RE = re.compile(r"^(🟢|🔴) (\S+) ([\d.]+) [▲▼]([\d.]+)% — (?:BUY|signal) ([\d.]+) (CE|PE) @ ₹([\d.]+)")
ORDER_RE = re.compile(r"^\s+order (✓|✗|…) (\S+) (\d+) @ ₹([\d.]+)\s*(\S*) — (.*)$")
SCAN_RE = re.compile(r"^\[(\d\d):(\d\d)\] \d+ stocks")
FUT_RE = re.compile(r"fut (long buildup|short covering|short buildup|long unwinding|no OI base)")
SR_RE = re.compile(r"(support|resistance) ([\d.]+)")
WALL_RE = re.compile(r"next wall ([\d.]+)")


def from_log(log: pathlib.Path) -> list[dict]:
    """Signals the monitor pushed, with their order lines, from oi_monitor.log.

    The log has times but no dates: a scan time earlier than the one before starts a new
    day, and the days are matched (newest last) to the dates that have saved snapshots."""
    days, cur, last_t, sig = [], None, None, None
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        m = SCAN_RE.match(line)
        if m:
            t = m.group(1) + ":" + m.group(2)
            if last_t is None or t < last_t:
                cur = []
                days.append(cur)
            last_t, sig = t, None
            continue
        m = SIG_RE.match(line)
        if m and cur is not None:
            if any(x["name"] == m.group(2) and x["side"] == m.group(6) for x in cur):
                sig = None              # same signal again (an EARLY one decided later) — keep the first
                continue
            sig = {"time": last_t, "side": m.group(6), "name": m.group(2), "spot": float(m.group(3)),
                   "price_pct": float(m.group(4)) * (1 if m.group(1) == "🟢" else -1),
                   "strike": float(m.group(5)), "signal_ltp": float(m.group(7)), "ts": None,
                   "order": "no order line", "entry": float(m.group(7)), "score": ""}
            cur.append(sig)
            continue
        if sig is not None:
            m = ORDER_RE.match(line)
            if m:
                sig.update({"ts": m.group(2), "entry": float(m.group(4)),
                            "order": f"{m.group(1)} {m.group(6)}".strip()})
            elif "no order" in line:
                sig["order"] = line.strip()
            elif "score " in line:
                sig["score"] = line.strip().split("score ")[-1].split("/")[0]
            m2 = FUT_RE.search(line)
            if m2:
                sig["fut"] = m2.group(1)
            m3, m4 = SR_RE.search(line), WALL_RE.search(line)
            if m3 or m4:
                near = float(m3.group(2)) if m3 else None
                nxt = float(m4.group(1)) if m4 else None
                if sig["side"] == "CE":
                    sig["put_wall"], sig["call_wall"] = near, nxt
                else:
                    sig["call_wall"], sig["put_wall"] = near, nxt
    dates = [d for d in day_dirs() if market_files(d)]
    days = [d for d in days if d]
    out = []
    for date, sigs in zip(dates[-len(days):] if days else [], days[-len(dates):] if dates else []):
        for x in sigs:
            x["day"] = date
            if not x["ts"]:            # no order line: find the contract in that day's snapshot
                f = market_files(date)
                s = read(f[0]) if f else None
                if s is not None:
                    y = s[(s["name"] == x["name"]) & (s["strike"] == x["strike"]) & (s["type"] == x["side"])]
                    x["ts"] = y.iloc[0]["ts"] if len(y) else None
            out.append(x)
    return out


def follow(sig: dict, target: float, stop: float, next_day: datetime.date | None) -> dict:
    """Track the signal's option through the day's later snapshots."""
    day, hm = sig["day"], sig["time"].replace(":", "")
    path = []
    for f in market_files(day):
        if f.name[:4] <= hm:
            continue
        s = read(f)
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
            s = read(nf[-1])
            x = s[s["ts"] == sig["ts"]]
            if len(x):
                res["next_pct"] = round((float(x.iloc[0]["ltp"]) / e - 1) * 100, 1)
    return res


# ── New rules: screener/oi_rules.py (the same rules the live monitor uses) ─────────────────────
FIRST_ORDER, ORDER_UNTIL = rules.FIRST_ORDER, rules.ORDER_UNTIL


def Kite() -> "rules.KiteData":
    from kiteconnect import KiteConnect
    e = mon.env()
    k = KiteConnect(e.get("KITE_API_KEY", ""))
    k.set_access_token(e.get("KITE_ACCESS_TOKEN", ""))
    k.profile()                      # fails fast on an expired token
    return rules.KiteData(k)


def option_at(day: datetime.date, hm: str, name: str, side: str) -> dict | None:
    """The ATM option of `side` in the first saved scan at / after hm: contract, entry (ask), spot."""
    for f in market_files(day):
        if f.name[:4] < hm:
            continue
        s = read(f)
        g = s[s["name"] == name]
        if g.empty:
            return None
        spot = float(g["spot"].iloc[0])
        k = min(g["strike"].unique(), key=lambda x: abs(x - spot))
        row = g[(g["strike"] == k) & (g["type"] == side)]
        if row.empty:
            return None
        r = row.iloc[0]
        return {"time": f"{f.name[:2]}:{f.name[2:4]}", "ts": r["ts"], "strike": float(k), "spot": spot,
                "entry": float(r["ask"] or r["ltp"])}
    return None


def still_strong(day: datetime.date, name: str, side: str, from_hm: str) -> dict | None:
    """First scan at / after from_hm where the stock is still STRONG on that side (only this stock read)."""
    for f in market_files(day):
        hm = f.name[:4]
        if hm < from_hm or hm > ORDER_UNTIL:
            continue
        base_p, recent_p = mon.base_and_recent(day, hm)
        one = lambda d: d[d["name"] == name] if d is not None else None
        r = om.analyse_all(one(read(f)), one(read(base_p)) if base_p else None,
                           one(read(recent_p)) if recent_p else None)
        if r and r[0].get("signal") == side:
            return {"time": f"{hm[:2]}:{hm[2:]}", "fut": r[0]["fut"], "call_wall": r[0]["call_wall"],
                    "put_wall": r[0]["put_wall"]}
    return None


def new_rules(sig: dict, kite: "rules.KiteData") -> dict:
    """What the rules would have done with this signal: decision + the entry they'd use."""
    day, side, name = sig["day"], sig["side"], sig["name"]
    t, fut = sig["time"].replace(":", ""), sig.get("fut")
    if t >= ORDER_UNTIL:
        return {"new": "⏰ 15:00 or later — no order"}
    walls = {"call_wall": sig.get("call_wall"), "put_wall": sig.get("put_wall")}
    note = ""
    if t < FIRST_ORDER:                                   # early → must still be STRONG at 09:40+
        later = still_strong(day, name, side, FIRST_ORDER)
        if not later:
            return {"new": "⏱ EARLY — not STRONG again after 09:40, no trade"}
        t, fut = later["time"].replace(":", ""), later["fut"]
        walls = {k: later[k] for k in walls}
        note = f"early {sig['time']} → still STRONG at {later['time']} · "
    opt = option_at(day, t, name, side)
    if not opt:
        return {"new": "no snapshot at the entry time"}
    upto = datetime.datetime.combine(day, datetime.time(int(t[:2]), int(t[2:])))
    d = rules.decide(name, side, day, upto, opt["spot"], walls["call_wall"], walls["put_wall"], kite, fut)
    if d["kind"] == "WATCH":
        return {"new": f"👀 WATCH — {d['text']}"}
    if d["kind"] == "BUY":
        return {"new": f"✅ BUY — {note}{d['text']}", **opt}
    end_of_day = datetime.datetime.combine(day, datetime.time(15, 0))
    kind, txt, at = rules.watch_break(name, side, d["level"], day, upto, end_of_day, kite)
    if kind == "BREAK":
        o = option_at(day, f"{at:%H%M}", name, side)
        if o:
            return {"new": f"✅ BREAK → BUY — {note}{d['note']} · {txt}", **o}
    if kind == "HELD":
        return {"new": f"❌ CANCELLED — {txt}", "level": d["level"].text()}
    return {"new": f"❌ EXPIRED — {d['level'].text()} never broke by 15:00"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="one day, YYYY-MM-DD (default: every saved day)")
    ap.add_argument("--target", type=float, default=20.0, help="success: premium up this %% (default 20)")
    ap.add_argument("--stop", type=float, default=20.0, help="failure: premium down this %% (default 20)")
    ap.add_argument("--push", action="store_true", help="send the summary to OI_NTFY_TOPIC")
    ap.add_argument("--no-rules", action="store_true", help="skip the new-rules comparison (no Kite calls)")
    ap.add_argument("--replay", action="store_true",
                    help="re-run every saved scan instead of reading the pushed signals from oi_monitor.log")
    a = ap.parse_args()

    days = day_dirs()
    if a.date:
        days = [d for d in days if str(d) == a.date]
    rows = []
    all_days = day_dirs()
    if a.replay:
        found = [sig for d in days for sig in replay(d)]
    else:
        log = mon.ROOT / "oi_monitor.log"
        if not log.exists():
            print(f"{log} not found — run with --replay to rebuild the signals from the snapshots")
            return
        found = [x for x in from_log(log) if x.get("ts") and x["day"] in days]
    kite = None
    if not a.no_rules:
        try:
            kite = Kite()
        except Exception as ex:
            print(f"  new rules skipped — Kite login failed ({ex})")
    print(f"  following {len(found)} signals …", flush=True)
    for sig in found:
        later = [x for x in all_days if x > sig["day"] and market_files(x)]
        nd = later[0] if later else None
        row = {**sig, **follow(sig, a.target, a.stop, nd)}
        if kite:
            try:
                nr = new_rules(sig, kite)
            except Exception as ex:
                nr = {"new": f"rules failed: {ex}"}
            row["new"] = nr["new"]
            if nr.get("ts"):
                res = follow({**sig, "time": nr["time"], "ts": nr["ts"], "entry": nr["entry"]}, a.target, a.stop, nd)
                row.update({"new_time": nr["time"], "new_strike": nr["strike"], "new_entry": nr["entry"],
                            "new_result": res["result"], "new_close_pct": res["close_pct"]})
        rows.append(row)
    if not rows:
        print("no signals in the saved snapshots" + (f" for {a.date}" if a.date else ""))
        return

    df = pd.DataFrame(rows)
    if "new" in df:
        for c in ("new_time", "new_strike", "new_entry", "new_result", "new_close_pct"):
            if c not in df:
                df[c] = pd.NA
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
                  f"\n        → {r['result']}{' at ' + r['hit_time'] if r['hit_time'] else ''} · {rng}{nd}"
                  + (f"\n        order {r['order']}" if 'order' in r and pd.notna(r.get('order')) else "")
                  + (f"\n        NEW RULES: {r['new']}" if 'new' in r and pd.notna(r.get('new')) else "")
                  + (f" · {r['new_time']} {r['new_strike']:g} {r['side']} @ ₹{r['new_entry']:g} → {r['new_result']}"
                     f" (close {r['new_close_pct']:+.0f}%)" if 'new_result' in r and pd.notna(r.get('new_result')) else ""))
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
    if "new" in df:
        for c in ("new_result", "new_close_pct"):
            if c not in df:
                df[c] = pd.NA
        ob = df[~df["order"].astype(str).str.startswith("no order")] if "order" in df else df
        os_, of_ = int((ob["result"] == "SUCCESS").sum()), int((ob["result"] == "FAILURE").sum())
        oc = ob["close_pct"].dropna().mean() if len(ob) else float("nan")
        nb = df[df["new_result"].notna()]
        ns, nf = int((nb["new_result"] == "SUCCESS").sum()), int((nb["new_result"] == "FAILURE").sum())
        nc = pd.to_numeric(nb["new_close_pct"], errors="coerce").dropna().mean() if len(nb) else float("nan")
        lines += ["", f"OLD rules (signals that went to Kite): {len(ob)} trades · ✅ {os_} · ❌ {of_}"
                  + (f" · close {oc:+.1f}% avg" if pd.notna(oc) else ""),
                  f"NEW rules: {len(nb)} trades · ✅ {ns} · ❌ {nf}" + (f" · close {nc:+.1f}% avg" if pd.notna(nc) else "")]
        kinds = df["new"].str.extract(r"^(\S+ [A-Z]+)")[0].value_counts()
        lines.append("decisions: " + " · ".join(f"{k} {v}" for k, v in kinds.items()))
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
