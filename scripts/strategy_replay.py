"""
Strategy replay — read-only, all saved days. Which entry / filters / exits make money?

For every day in data/oi/ the saved snapshots are walked in order and the START trigger (screener/oi_start.py)
is run exactly as the live bot does, with the live filters of Subba's machine: yesterday's top OI_LIQUID_TOP
liquid stocks, orders from 10:00 to 14:30, the day-type rule (OI_AGAINST_WRITE 1.5, NIFTY from Kite), one
start per stock and side a day. Each start is then traded under several variants:

  entry    start   buy the ATM option at the START snapshot (today's bot)
           retest  arm it; buy on a later snapshot when the stock is back within 0.15 % of the broken level
                   with a new floor there (screener/oi_entry.retest_step); cancel on failure / 45 min / 13:00
  filters  —       none
           ex+pr   no buy when the squeeze is used up (oi_entry.exhausted) or the premium is inflated
                   (oi_entry.premium_inflated)
  exits    old     today's: move failed · −25 % · take profit on the turn from +5 % · lock from +20 % · 15:15
           new     + SL −15 % (filled at ~trigger − 3 %, as the SL order in Kite would) · take profit only from
                   +15 % with the OI sign · lock from +30 % · out if not +5 % after 45 min

Entry = the option's ask, exits at the bid (the stop-loss at its trigger − 3 %), one lot (today's lot sizes
from Kite), ₹60 charges a trade. Snapshots before 14 Nov have no VWAP / stock volume (those two checks are
skipped there) and are 10 min apart.

Usage (Subba's machine, a few minutes):
  /root/venv/bin/python scripts/strategy_replay.py
  /root/venv/bin/python scripts/strategy_replay.py --from 2026-11-12
"""

from __future__ import annotations

import argparse
import datetime
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).parent))

import pandas as pd

import oi_monitor_bot as mon
from screener import oi_entry, oi_exit, oi_monitor as om, oi_rules as rules, oi_start

COLS = ["name", "strike", "type", "ts", "oi", "ltp", "bid", "ask", "spot", "spot_close", "fut_ltp", "fut_close",
        "fut_oi", "vwap", "day_high", "day_low", "stock_volume"]
ORDER_FROM, LAST_ORDER = "1000", "1430"
AGAINST = 1.5
CHARGES = 60.0
OLD = dict(SL_PCT=0.0, TP_MIN=0.0, TP_NEED_OI=False, TIME_STOP=0, LOCK_AT_PCT=20.0)
NEW = dict(SL_PCT=15.0, TP_MIN=15.0, TP_NEED_OI=True, TIME_STOP=45, LOCK_AT_PCT=30.0)
VARIANTS = [("start", False, "old"), ("start", False, "new"), ("start", True, "new"),
            ("retest", False, "old"), ("retest", False, "new"), ("retest", True, "new")]


class F:
    """Stands in for a snapshot path (oi_start only reads .name)."""
    def __init__(self, hm: str):
        self.name = f"{hm}.csv.gz"


class Ctx(dict):
    """name → om.analyse_stock() vs yesterday, computed only when oi_start asks for it."""
    def __init__(self, now: dict, base: dict, recent: dict, prev_hm: str):
        super().__init__(_prev_hm=prev_hm)
        self.now, self.base, self.recent = now, base, recent

    def get(self, name, default=None):
        if name.startswith("_"):
            return super().get(name, default)
        if name not in self:
            try:
                g = self.now[name]
                self[name] = om.analyse_stock(g, self.base.get(name, g), self.recent.get(name))
            except Exception:
                self[name] = None
        return dict.get(self, name)


def load(p: pathlib.Path) -> pd.DataFrame:
    return pd.read_csv(p, usecols=lambda c: c in COLS)


def by_name(df: pd.DataFrame) -> dict:
    return {n: g for n, g in df.groupby("name")}


def candidates(day: datetime.date, snaps: dict, groups: dict, base: dict, liquid: set | None, kd) -> list[dict]:
    hms = sorted(snaps)
    started, out = set(), []
    dts: dict = {}
    for i, hm in enumerate(hms):
        upto = [F(h) for h in hms[:i + 1]]
        pf = oi_start.snap_ago(upto, hm, oi_start.WINDOW_MIN)
        if pf is None:
            continue
        prev_hm = pf.name[:4]
        window = [snaps[f.name[:4]][["name", "spot"]] for f in oi_start.range_files(upto, hm)]
        rf = oi_start.snap_ago(upto, hm, mon.RECENT_MIN)
        ctx = Ctx(groups[hm], base, groups[rf.name[:4]] if rf else {}, prev_hm)
        for s in oi_start.starts(snaps[hm], snaps[prev_hm], window, ctx, hm):
            key = f"{s['name']}|{s['signal']}"
            if key in started:
                continue
            started.add(key)
            if liquid is not None and s["name"] not in liquid:
                continue
            if not ORDER_FROM <= hm < LAST_ORDER:
                continue
            if hm not in dts:
                try:
                    dts[hm] = rules.day_type(day, datetime.datetime.combine(day, datetime.time(int(hm[:2]), int(hm[2:]))), kd)[0]
                except Exception:
                    dts[hm] = "flat"
            ce = s["signal"] == "CE"
            need = dts[hm] in (("down", "flat") if ce else ("up", "flat"))
            wrote = s.get("oi_puts" if ce else "oi_calls")
            if need and (wrote is None or wrote < AGAINST):
                continue
            out.append({**s, "hm": hm, "day_type": dts[hm]})
    return out


def prev_of(hms: list, hm: str) -> str | None:
    upto = [F(h) for h in hms if h <= hm]
    pf = oi_start.snap_ago(upto, hm, oi_start.WINDOW_MIN)
    return pf.name[:4] if pf else None


def trade(c: dict, entry: str, filt: bool, exits: str, hms: list, groups: dict, lots: dict) -> dict | None:
    name, side = c["name"], c["signal"]
    g_of = lambda h: groups[h].get(name) if h else None
    buy_hm, why_in = c["hm"], "start"
    if entry == "retest":
        arm = {"side": side, "level": c["level"], "hm": c["hm"]}
        buy_hm = None
        for h in [h for h in hms if h > c["hm"]]:
            if h >= LAST_ORDER:
                break
            act, why = oi_entry.retest_step(arm, h, g_of(h), g_of(prev_of(hms, h)))
            if act == "cancel":
                return {"result": "cancelled", "why": why}
            if act == "buy":
                buy_hm, why_in = h, why
                break
        if buy_hm is None:
            return {"result": "cancelled", "why": "no pullback before the cut-off"}
    g = g_of(buy_hm)
    if g is None:
        return None
    spot = float(g["spot"].iloc[0])
    strikes = sorted(g["strike"].unique())
    k = min(strikes, key=lambda x: abs(x - spot))
    row = g[(g["strike"] == k) & (g["type"] == side)]
    if row.empty:
        return None
    ask = float(row.iloc[0]["ask"] or row.iloc[0]["ltp"] or 0)
    if ask <= 0:
        return None
    if filt:
        gp = g_of(prev_of(hms, buy_hm))
        peaks = oi_entry.day_peaks([groups[h][name] for h in hms if h <= buy_hm and name in groups[h]])
        w = oi_entry.exhausted(side, g, gp, peaks) or oi_entry.premium_inflated(side, k, g, gp)
        if w:
            return {"result": "filtered", "why": w}
    for a, v in (NEW if exits == "new" else OLD).items():
        setattr(oi_exit, a, v)
    tr = {"side": side, "ts": row.iloc[0]["ts"], "entry": ask, "stop": c["stop"], "entry_spot": spot,
          "level": c["level"], "opened": buy_hm}
    kind, why, px, out_hm = None, "", None, None
    for h in [h for h in hms if h > buy_hm]:
        kind, why, px = oi_exit.check(tr, g_of(h), g_of(prev_of(hms, h)), h)
        if kind:
            out_hm = h
            break
    if not kind:
        if not tr.get("last_prem"):
            return None
        kind, why, px, out_hm = "end", "last snapshot", tr["last_prem"], hms[-1]
    if why.startswith("stop-loss"):
        px = ask * (1 - oi_exit.SL_PCT / 100) * 0.97
    lot = lots.get(name, 0)
    if not lot:
        return None
    return {"result": kind, "why": why[:80], "in": buy_hm, "out": out_hm, "entry": ask, "exit": round(px, 2),
            "pct": round((px / ask - 1) * 100, 1), "pnl": round((px - ask) * lot - CHARGES), "lot": lot}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="frm", help="first day, YYYY-MM-DD")
    a = ap.parse_args()
    e = mon.env()
    from kiteconnect import KiteConnect
    k = KiteConnect(e.get("KITE_API_KEY", ""))
    k.set_access_token(e.get("KITE_ACCESS_TOKEN", ""))
    lots = {}
    for r in k.instruments("NFO"):
        if r["instrument_type"] == "FUT" and r["name"] not in lots:
            lots[r["name"]] = int(r.get("lot_size") or 0)
    kd = rules.KiteData(k)
    liquid_top = int(e.get("OI_LIQUID_TOP") or 0)
    days = sorted(d.name for d in mon.DATA.iterdir() if d.is_dir() and d.name[:2] == "20")
    days = [d for d in days if not a.frm or d >= a.frm]
    rows, prev_last = [], None
    all_dirs = sorted(d.name for d in mon.DATA.iterdir() if d.is_dir() and d.name[:2] == "20")
    t0 = time.time()
    for dname in days:
        day = datetime.date.fromisoformat(dname)
        files = [f for f in mon.snap_files(day) if "0915" <= f.name[:4] <= "1530"]
        if len(files) < 20 or day.weekday() >= 5:
            print(f"{dname}: {len(files)} snapshots — skipped")
            continue
        older = [x for x in all_dirs if x < dname]
        base_df = None
        for x in reversed(older):
            fs = mon.snap_files(datetime.date.fromisoformat(x))
            if fs:
                base_df = load(fs[-1])
                break
        snaps = {f.name[:4]: load(f) for f in files}
        groups = {h: by_name(df) for h, df in snaps.items()}
        base = by_name(base_df) if base_df is not None else {}
        liquid = None
        lq = mon.DATA / dname / "liquid.csv"
        if liquid_top and lq.exists():
            r = pd.read_csv(lq)
            liquid = set(r[r["rank"] <= liquid_top]["name"])
        cands = candidates(day, snaps, groups, base, liquid, kd)
        hms = sorted(snaps)
        for c in cands:
            for entry, filt, exits in VARIANTS:
                res = trade(c, entry, filt, exits, hms, groups, lots)
                if res:
                    rows.append({"day": dname, "start": c["hm"], "stock": c["name"], "side": c["signal"],
                                 "variant": f"{entry}{' +filters' if filt else ''} · {exits} exits", **res})
        print(f"{dname}: {len(files)} snapshots · {len(cands)} starts after the live filters · {time.time() - t0:.0f}s",
              flush=True)
    if not rows:
        print("no trades")
        return
    df = pd.DataFrame(rows)
    out = mon.ROOT / "data" / "strategy_replay.csv"
    df.to_csv(out, index=False)
    t = df[df["pnl"].notna()]
    lines = []
    order = [f"{e_}{' +filters' if f_ else ''} · {x_} exits" for e_, f_, x_ in VARIANTS]
    for v in order:
        s = t[t["variant"] == v]
        w, l_ = s[s["pnl"] > 0], s[s["pnl"] <= 0]
        skipped = df[(df["variant"] == v) & df["pnl"].isna()]
        line = {"variant": v, "trades": len(s), "skipped": len(skipped),
                "win %": round(100 * len(w) / len(s)) if len(s) else 0,
                "avg win ₹": round(w["pnl"].mean()) if len(w) else 0,
                "avg loss ₹": round(l_["pnl"].mean()) if len(l_) else 0,
                "NET ₹": round(s["pnl"].sum())}
        for d in sorted(t["day"].unique()):
            line[d[5:]] = round(s[s["day"] == d]["pnl"].sum())
        lines.append(line)
    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 30)
    print("\nONE LOT per trade, after ₹60 charges (skipped = cancelled / filtered, no trade):\n")
    print(pd.DataFrame(lines).to_string(index=False))
    for v in order:
        s = t[t["variant"] == v]
        print(f"\n{v}: " + " · ".join(f"{k_} {len(g)} (₹{g['pnl'].sum():+,.0f})" for k_, g in s.groupby("result")))
    print(f"\nall trades → {out}")


if __name__ == "__main__":
    main()
