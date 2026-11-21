#!/usr/bin/env python3
"""
Backtest of the unified strategy (core/unified.py — super candle → inside bar / pin bar, 15-min Supertrend,
pivots, Bollinger Bands) against the current index bot (core/ios_strategy.py) on the same Kite history.
Read-only. Price only: Kite has no option-OI history for expired contracts, so the OI / VIX checks of the live
bot are left out. VWAP: index candles carry no volume, so the session's average price is used.

Results in index points and R (points ÷ risk). Option ₹ ≈ points × 0.5 (ATM delta) × lot size, minus ₹60.
2 lots: half is booked at T1 and the rest trails (as the live bot with IOS_LOTS=2).

Usage (droplet):
  /root/venv/bin/python scripts/unified_backtest.py                   # 90 days, NIFTY + SENSEX
  /root/venv/bin/python scripts/unified_backtest.py --days 180 --syms NIFTY
"""

from __future__ import annotations

import argparse
import datetime
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import pandas as pd

import ios_backtest as ib
from core import ios_strategy as ios
from core import kite_order as ko
from core import unified as un

LOTS = {"NIFTY": 65, "SENSEX": 20, "BANKNIFTY": 30}
CHARGES = 60.0


def run_unified(sym: str, m5: pd.DataFrame, m15: pd.DataFrame, daily: pd.DataFrame, days: list, lots: int,
                max_trades: int, max_losses: int) -> list[dict]:
    f_all = un.frame(m5)
    out = []
    for day in days:
        pv = un.pivots(daily, day)
        if pv is None:
            continue
        idx = [i for i, t in enumerate(f_all.index) if t.date() == day]
        if not idx:
            continue
        state, tr, n_tr, n_loss = {}, None, 0, 0
        for i in idx:
            t = f_all.index[i]
            upto = f_all.iloc[:i + 1]
            if tr:
                why = un.extra_exit(tr, upto, m15, pv)
                act = "exit" if why else None
                if not act:
                    act, why = ios.manage_step(tr, upto)
                if act == "book_half":
                    tr["half_pts"] = (tr["target"] - tr["entry"]) * (1 if tr["side"] == "CE" else -1)
                elif act == "exit":
                    c = float(upto["close"].iloc[-1])
                    px = tr["target"] if why.startswith("T1") else c
                    pts = (px - tr["entry"]) * (1 if tr["side"] == "CE" else -1)
                    if "half_pts" in tr:
                        pts = (tr["half_pts"] + pts) / 2
                    out.append({"engine": "unified", "sym": sym, "day": day, "in": tr["t_entry"][11:16],
                                "out": f"{t:%H:%M}", "side": tr["side"], "kind": tr["kind"],
                                "entry": round(tr["entry"], 1), "stop": round(tr["stop0"], 1),
                                "target": round(tr["target"], 1), "risk": round(tr["risk"], 1),
                                "pts": round(pts, 1), "R": round(pts / tr["risk"], 2), "exit": why})
                    n_loss += pts < 0
                    tr = None
                continue
            if n_tr >= max_trades or n_loss >= max_losses:
                continue
            kind, d = un.step(sym, f_all, i, m15, pv, state)
            if kind == "enter":
                tr = ios.new_trade(un.to_setup(sym, d), lots)
                n_tr += 1
    return out


def summary(df: pd.DataFrame, label: str) -> dict:
    if df.empty:
        return {"strategy": label, "trades": 0}
    w, l_ = df[df["R"] > 0], df[df["R"] <= 0]
    rs = df["R"].cumsum()
    return {"strategy": label, "trades": len(df), "win %": round(100 * len(w) / len(df)),
            "avg win R": round(w["R"].mean(), 2) if len(w) else 0, "avg loss R": round(l_["R"].mean(), 2) if len(l_) else 0,
            "total R": round(df["R"].sum(), 1), "max drawdown R": round((rs - rs.cummax()).min(), 1),
            "≈ ₹ (1 lot)": round(df["rs"].sum())}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--syms", nargs="+", default=["NIFTY", "SENSEX"])
    ap.add_argument("--lots", type=int, default=2, help="2 = half at T1 + trail (live IOS_LOTS=2); 1 = all out at T1")
    ap.add_argument("--max-trades", type=int, default=3)
    ap.add_argument("--max-losses", type=int, default=2)
    a = ap.parse_args()
    env = ib.load_env()
    kite = ko.kite_for(env.get("KITE_API_KEY", ""), env.get("KITE_ACCESS_TOKEN", ""))
    kite.profile()
    end = datetime.date.today()
    start = end - datetime.timedelta(days=a.days)
    rows = []
    for sym in a.syms:
        tok = ib.TOKENS[sym]
        print(f"{sym}: downloading …", flush=True)
        daily = ib.history(kite, tok, "day", start - datetime.timedelta(days=200), end)
        daily.index = [x.date() for x in daily.index]
        m15 = ib.history(kite, tok, "15minute", start - datetime.timedelta(days=15), end)
        m5 = ib.history(kite, tok, "5minute", start - datetime.timedelta(days=5), end)
        days = sorted({d for d in m5.index.date if d >= start})
        print(f"{sym}: {len(days)} days — unified …", flush=True)
        rows += run_unified(sym, m5, m15, daily, days, a.lots, a.max_trades, a.max_losses)
        print(f"{sym}: current index bot …", flush=True)
        for r in ib.run(sym, m5, m15, daily, days, a.lots, a.max_trades, a.max_losses):
            rows.append({"engine": "current", **r})
    if not rows:
        print("no trades")
        return
    df = pd.DataFrame(rows)
    df["rs"] = df.apply(lambda r: r["pts"] * 0.5 * LOTS.get(r["sym"], 1) - CHARGES, axis=1)
    out = ROOT / "data" / "ios"
    out.mkdir(parents=True, exist_ok=True)
    fpath = out / f"unified_backtest_{end}_{a.days}d.csv"
    df.to_csv(fpath, index=False)
    pd.set_option("display.width", 220)
    lines = []
    for eng, lab in (("unified", "UNIFIED (new)"), ("current", "current index bot")):
        for sym in a.syms:
            lines.append(summary(df[(df["engine"] == eng) & (df["sym"] == sym)], f"{lab} · {sym}"))
        lines.append(summary(df[df["engine"] == eng], f"{lab} · ALL"))
    print(f"\n{a.days} days · {a.lots} lot(s) · R = points ÷ risk · ₹ ≈ points × 0.5 × lot − ₹60\n")
    print(pd.DataFrame(lines).to_string(index=False))
    u = df[df["engine"] == "unified"]
    if len(u):
        kinds = ["T1", "structure stop", "breakeven stop", "trail stop", "5m structure broke", "15:15",
                 "closed back", "15-min Supertrend"]
        ex = u["exit"].map(lambda s: next((k for k in kinds if s.startswith(k)), s))
        print("\nUNIFIED exits (count, avg R):\n" + u.groupby(ex)["R"].agg(["size", "mean"]).round(2).to_string())
        print("\nUNIFIED by trigger:\n" + u.groupby("kind")["R"].agg(["size", "mean", "sum"]).round(2).to_string())
        print("\nUNIFIED last 10 trades:\n" + u.tail(10)[["sym", "day", "in", "out", "side", "kind", "entry", "stop",
                                                          "target", "pts", "R", "exit"]].to_string(index=False))
    print(f"\nall trades → {fpath}")


if __name__ == "__main__":
    main()
