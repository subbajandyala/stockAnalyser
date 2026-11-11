"""
Exit replay — read-only. How would different profit-booking / exit plans have done on the OI
monitor's signals? No orders, no pushes.

Entry: each logged signal (oi_monitor.log; --replay rebuilds them from the snapshots) at its scan,
buying the ATM option at the logged price (ask). Signals at / after 15:00 are skipped.
Then every later 10-min snapshot of that day is walked (exit price = the option's bid, else LTP):

  fixed 40/20      premium +40 % target / −20 % stop (the live OI_TARGET_PCT / OI_SL_PCT)
  fixed 20/20      premium +20 % / −20 %
  plan (1 lot)     target on the STOCK = the nearer of k × daily ATR(14) and just before the next
                   level / OI wall ahead (screener/levels.py), capped by the day's range left
                   (ATR − today's high-low so far; < 0.25 ATR left → no trade)
                   stop on the stock = 0.3 × daily ATR · breakeven: once the premium is +12 %,
                   exit if it falls back to the entry · OI turn: the signal's score ≤ 2 · futures
                   turn against the trade · time stop: not +10 % within 90 min · 15:10 exit
  plan 2 lots …    the same, but at the target half is booked and half trails
                   (Chandelier: highest 10-min high since entry − 2 × 15-min ATR(14); or VWAP:
                   a 10-min close back across the stock's VWAP), out at 15:10 at the latest
  ablations        the 1-lot plan with one rule changed, to see what each rule is worth

Snapshots are 10 minutes apart: a stop or target crossed and recovered inside one gap is missed,
and an exit is priced at the snapshot, not at the exact trigger (a real GTT fills nearer it).

Usage (Subba's machine):
  /root/venv/bin/python scripts/exit_replay.py
  /root/venv/bin/python scripts/exit_replay.py --ordered        # only signals Kite filled
"""

import argparse
import datetime
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).parent))

import pandas as pd

import oi_signals_report as rep
from screener import levels
from screener import oi_monitor as om

EXIT_BY = "1510"
BE_ARM, TIME_MIN, TIME_GAIN = 12.0, 90, 10.0
STOP_ATR, MIN_ROOM_ATR = 0.3, 0.25
AGAINST = {"CE": ("short buildup", "long unwinding"), "PE": ("long buildup", "short covering")}

BASE_PLAN = {"k": 0.5, "level": True, "room": True, "be": True, "oi": True, "fut": True, "time": True,
             "trail": None}
PLANS = {
    "fixed 40/20": {"fixed": (40, 20)},
    "fixed 20/20": {"fixed": (20, 20)},
    "plan (1 lot)": BASE_PLAN,
    "plan 2 lots Chandelier": {**BASE_PLAN, "trail": "chandelier"},
    "plan 2 lots VWAP": {**BASE_PLAN, "trail": "vwap"},
    "· target 0.3 ATR": {**BASE_PLAN, "k": 0.3},
    "· target 0.75 ATR": {**BASE_PLAN, "k": 0.75},
    "· ATR only (no level)": {**BASE_PLAN, "level": False},
    "· no room skip": {**BASE_PLAN, "room": False},
    "· no breakeven": {**BASE_PLAN, "be": False},
    "· no OI turn": {**BASE_PLAN, "oi": False},
    "· no futures turn": {**BASE_PLAN, "fut": False},
    "· no time stop": {**BASE_PLAN, "time": False},
}


def hm_dt(day: datetime.date, hm: str) -> datetime.datetime:
    return datetime.datetime.combine(day, datetime.time(int(hm[:2]), int(hm[2:4])))


def closed(df: pd.DataFrame, upto: datetime.datetime, minutes: int) -> pd.DataFrame:
    return df[df.index + datetime.timedelta(minutes=minutes) <= upto] if len(df) else df


def mean_tr(df: pd.DataFrame, n: int = 14) -> float | None:
    if len(df) < 2:
        return None
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1).iloc[1:]
    return float(tr.tail(n).mean())


def build(sig: dict, kd) -> dict | None:
    """Everything the plans need for one signal, or None (with sig['skip'] set)."""
    day, side, name = sig["day"], sig["side"], sig["name"]
    hm0 = sig["time"].replace(":", "")
    if hm0 >= rep.ORDER_UNTIL:
        sig["skip"] = "15:00 or later"
        return None
    files = rep.market_files(day)
    one = lambda d: d[d["name"] == name] if d is not None else None
    scans = []
    walls = None
    for f in files:
        hm = f.name[:4]
        if hm < hm0 or hm > EXIT_BY:
            continue
        s = rep.read(f)
        g = s[s["name"] == name]
        o = g[g["ts"] == sig["ts"]]
        if g.empty or o.empty:
            continue
        base_p, recent_p = rep.mon.base_and_recent(day, hm)
        a = om.analyse_all(one(s), one(rep.read(base_p)) if base_p else None,
                           one(rep.read(recent_p)) if recent_p else None)
        a = a[0] if a and "error" not in a[0] else {}
        o = o.iloc[0]
        px = float(o["bid"]) if o["bid"] else float(o["ltp"])
        if walls is None:
            walls = (a.get("call_wall"), a.get("put_wall"))
        scans.append({"hm": hm, "t": hm_dt(day, hm), "spot": float(g["spot"].iloc[0]), "px": px,
                      "ltp": float(o["ltp"]), "score": a.get("ce_score" if side == "CE" else "pe_score"),
                      "fut": a.get("fut")})
    if len(scans) < 2:
        sig["skip"] = "no later snapshot"
        return None
    daily = kd.candles(name, "day", day - datetime.timedelta(days=40), day - datetime.timedelta(days=1))
    m15 = kd.candles(name, "15minute", day - datetime.timedelta(days=6), day)
    m10 = kd.candles(name, "10minute", day, day)
    if daily is None or len(daily) < 15 or m10 is None or m10.empty:
        sig["skip"] = "no Kite candles"
        return None
    atr = mean_tr(daily)
    t0 = scans[0]["t"]
    so_far = closed(m10, t0, 10)
    rng = float(so_far["high"].max() - so_far["low"].min()) if len(so_far) else 0.0
    d2 = daily.copy()
    d2.index = [pd.Timestamp(x).date() for x in d2.index]
    lv = levels.build(d2, m15, day, t0, walls[0], walls[1]) if walls else []
    spot0 = scans[0]["spot"]
    if side == "CE":
        ahead = [z.lo for z in lv if z.lo > spot0 * 1.001]
        lvl_target = min(ahead) * 0.999 if ahead else None
    else:
        ahead = [z.hi for z in lv if z.hi < spot0 * 0.999]
        lvl_target = max(ahead) * 1.001 if ahead else None
    return {"sig": sig, "scans": scans, "entry": float(sig["entry"]), "spot0": spot0, "atr": atr, "range": rng,
            "lvl_target": lvl_target, "m10": m10, "m15": m15}


def trail_hit(kind: str, side: str, c: dict, since: datetime.datetime, now: datetime.datetime) -> bool:
    m10 = closed(c["m10"], now, 10)
    if m10.empty:
        return False
    last = float(m10["close"].iloc[-1])
    if kind == "vwap":
        tp = (m10["high"] + m10["low"] + m10["close"]) / 3
        v = m10["volume"].astype(float)
        if v.sum() <= 0:
            return False
        vw = float((tp * v).sum() / v.sum())
        return last < vw if side == "CE" else last > vw
    after = m10[m10.index >= since - datetime.timedelta(minutes=10)]
    a15 = mean_tr(closed(c["m15"], now, 15))
    if after.empty or not a15:
        return False
    if side == "CE":
        return last < float(after["high"].max()) - 2 * a15
    return last > float(after["low"].min()) + 2 * a15


def simulate(c: dict, p: dict) -> dict:
    sig, side, e = c["sig"], c["sig"]["side"], c["entry"]
    ce = side == "CE"
    scans = c["scans"][1:]
    if "fixed" in p:
        tgt, stp = p["fixed"]
        for s in scans:
            if s["px"] >= e * (1 + tgt / 100):
                return {"pct": (s["px"] / e - 1) * 100, "why": "target", "at": s["hm"]}
            if s["px"] <= e * (1 - stp / 100):
                return {"pct": (s["px"] / e - 1) * 100, "why": "stop", "at": s["hm"]}
        s = scans[-1]
        return {"pct": (s["px"] / e - 1) * 100, "why": "15:10", "at": s["hm"]}
    atr = c["atr"]
    dist = p["k"] * atr
    if p["room"]:
        left = atr - c["range"]
        if left < MIN_ROOM_ATR * atr:
            return {"pct": None, "why": "no room", "at": ""}
        dist = min(dist, left)
    target = c["spot0"] + dist if ce else c["spot0"] - dist
    if p["level"] and c["lvl_target"]:
        target = min(target, c["lvl_target"]) if ce else max(target, c["lvl_target"])
    stop = c["spot0"] - STOP_ATR * atr if ce else c["spot0"] + STOP_ATR * atr
    armed, best, booked = False, e, None          # booked = (pct of the half sold at target, time)
    t0 = c["scans"][0]["t"]

    def done(pct, why, at):
        if booked:
            return {"pct": (booked[0] + pct) / 2, "why": f"target {booked[1]} + {why}", "at": at}
        return {"pct": pct, "why": why, "at": at}

    for s in scans:
        pct = (s["px"] / e - 1) * 100
        best = max(best, s["ltp"])
        if p["be"] and best >= e * (1 + BE_ARM / 100):
            armed = True
        if (s["spot"] <= stop) if ce else (s["spot"] >= stop):
            return done(pct, "stop", s["hm"])
        if armed and s["px"] <= e:
            return done(pct, "breakeven", s["hm"])
        if booked:                                   # trailing half: only the trail / 15:10 now
            if trail_hit(p["trail"], side, c, t0, s["t"]):
                return done(pct, f"{p['trail']} trail", s["hm"])
            continue
        if p["oi"] and s["score"] is not None and s["score"] <= 2:
            return done(pct, "OI turn", s["hm"])
        if p["fut"] and s["fut"] in AGAINST[side]:
            return done(pct, "futures turn", s["hm"])
        if (s["spot"] >= target) if ce else (s["spot"] <= target):
            if not p["trail"]:
                return done(pct, "target", s["hm"])
            booked = (pct, s["hm"])
            continue
        if p["time"] and s["t"] >= t0 + datetime.timedelta(minutes=TIME_MIN) and best < e * (1 + TIME_GAIN / 100):
            return done(pct, "time stop", s["hm"])
    s = scans[-1]
    return done((s["px"] / e - 1) * 100, "15:10", s["hm"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ordered", action="store_true", help="only signals whose order Kite filled (✓)")
    ap.add_argument("--replay", action="store_true", help="rebuild signals from the snapshots (slow)")
    ap.add_argument("--detail", default="plan (1 lot)", help="plan to list trade by trade")
    a = ap.parse_args()

    if a.replay:
        sigs = [s for d in rep.day_dirs() for s in rep.replay(d)]
    else:
        log = rep.mon.ROOT / "oi_monitor.log"
        sigs = [s for s in rep.from_log(log) if s.get("ts")] if log.exists() else []
    if a.ordered:
        sigs = [s for s in sigs if str(s.get("order", "")).startswith("✓")]
    kd = rep.Kite()
    print(f"{len(sigs)} signals · building price / OI paths and Kite candles …", flush=True)
    ctx, skipped = [], {}
    for n, s in enumerate(sigs, 1):
        try:
            c = build(s, kd)
        except Exception as ex:
            s["skip"], c = f"error {ex}", None
        if c:
            ctx.append(c)
        else:
            skipped[s["skip"]] = skipped.get(s["skip"], 0) + 1
        if n % 20 == 0:
            print(f"  {n}/{len(sigs)}", flush=True)
    print(f"{len(ctx)} signals replayed" + (f" · skipped: {skipped}" if skipped else ""))
    if not ctx:
        return

    rows, summary = [], []
    for name, p in PLANS.items():
        res = [simulate(c, p) for c in ctx]
        pcts = [r["pct"] for r in res if r["pct"] is not None]
        reasons = pd.Series([r["why"].split(" + ")[-1] for r in res]).value_counts()
        w = [x for x in pcts if x > 0]
        l_ = [x for x in pcts if x < 0]
        summary.append({"plan": name, "trades": len(pcts), "wins": len(w), "losses": len(l_),
                        "avg %": round(sum(pcts) / len(pcts), 1) if pcts else None,
                        "avg win": round(sum(w) / len(w), 1) if w else None,
                        "avg loss": round(sum(l_) / len(l_), 1) if l_ else None,
                        "exits": " · ".join(f"{k} {v}" for k, v in reasons.items())})
        for c, r in zip(ctx, res):
            s = c["sig"]
            rows.append({"plan": name, "day": s["day"], "time": s["time"], "name": s["name"], "side": s["side"],
                         "entry": c["entry"], "atr": round(c["atr"], 2), "range": round(c["range"], 2),
                         "lvl_target": c["lvl_target"], "pct": None if r["pct"] is None else round(r["pct"], 1),
                         "why": r["why"], "at": r["at"]})
    out = rep.mon.ROOT / "data" / f"exit_replay_{datetime.date.today()}.csv"
    pd.DataFrame(rows).to_csv(out, index=False)
    sm = pd.DataFrame(summary)
    pd.set_option("display.width", 250)
    pd.set_option("display.max_colwidth", 90)
    print("\n" + sm.to_string(index=False))
    det = [r for r in rows if r["plan"] == a.detail]
    if det:
        print(f"\n{a.detail}, trade by trade:")
        for r in det:
            pct = "  —  " if r["pct"] is None else f"{r['pct']:+5.1f}%"
            print(f"  {r['day']} {r['time']} {r['name']:<11} {r['side']} @ ₹{r['entry']:g}  ATR {r['atr']:g} "
                  f"moved {r['range']:g} → {pct} {r['why']} {r['at']}")
    print(f"\nfull table → {out}")


if __name__ == "__main__":
    main()
