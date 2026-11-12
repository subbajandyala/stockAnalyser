#!/usr/bin/env python3
"""
Structure-only backtest of the index setups (core/ios_strategy.py) on Kite history — read-only.

Kite has no option-OI history for expired contracts, so this tests the STRUCTURE part only
(setups A / B, structure stop, target at the next obstacle, breakeven at +1R, trail / change-of-
character exits, 15:15). Results are in index points and R (points ÷ risk); option P&L is roughly
points × delta (ATM ≈ 0.5). Stop exits are on a 5-min close beyond the stop (like the live bot).

Usage (droplet, credentials from .env):
  /root/venv/bin/python scripts/ios_backtest.py                       # 60 days, NIFTY BANKNIFTY SENSEX, 1 lot
  /root/venv/bin/python scripts/ios_backtest.py --days 90 --lots 2 --syms NIFTY
"""

from __future__ import annotations

import argparse
import datetime
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd

from core import ios_strategy as ios
from core import kite_order as ko

TOKENS = {"NIFTY": 256265, "BANKNIFTY": 260105, "SENSEX": 265}
CHUNK = {"5minute": 95, "15minute": 190, "day": 1900}


def load_env() -> dict:
    env = {}
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.split(" #")[0].strip().strip('"').strip("'")
    return env


def history(kite, token: int, interval: str, start: datetime.date, end: datetime.date) -> pd.DataFrame:
    rows, a = [], start
    while a <= end:
        b = min(end, a + datetime.timedelta(days=CHUNK[interval]))
        rows += kite.historical_data(token, datetime.datetime.combine(a, datetime.time(9, 0)),
                                     datetime.datetime.combine(b, datetime.time(15, 30)), interval)
        time.sleep(0.4)
        a = b + datetime.timedelta(days=1)
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    idx = pd.to_datetime(df.pop("date"))
    if getattr(idx.dt, "tz", None) is not None:
        idx = idx.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    df.index = pd.DatetimeIndex(idx)
    return df[~df.index.duplicated()].sort_index()


def run(sym: str, m5: pd.DataFrame, m15: pd.DataFrame, daily: pd.DataFrame, days: list, lots: int,
        max_trades: int, max_losses: int) -> list[dict]:
    out = []
    for day in days:
        d5 = m5[m5.index.date == day]
        n_tr = n_loss = 0
        tr = None
        for k in range(len(d5)):
            t = d5.index[k]
            upto = m5[m5.index <= t]
            if tr:
                act, why = ios.manage_step(tr, upto)
                if act == "book_half":
                    tr["half_pts"] = (tr["target"] - tr["entry"]) * (1 if tr["side"] == "CE" else -1)
                elif act == "exit":
                    c = float(d5["close"].iloc[k])
                    px = tr["target"] if why.startswith("T1") else c
                    pts = (px - tr["entry"]) * (1 if tr["side"] == "CE" else -1)
                    if "half_pts" in tr:
                        pts = (tr["half_pts"] + pts) / 2
                    out.append({"sym": sym, "day": day, "in": tr["t_entry"][11:16], "out": f"{t:%H:%M}",
                                "side": tr["side"], "kind": tr["kind"], "level": tr["level"],
                                "entry": round(tr["entry"], 1), "stop": round(tr["stop0"], 1),
                                "target": round(tr["target"], 1), "risk": round(tr["risk"], 1),
                                "pts": round(pts, 1), "R": round(pts / tr["risk"], 2), "exit": why})
                    n_loss += pts < 0
                    tr = None
                continue
            if n_tr >= max_trades or n_loss >= max_losses:
                continue
            now_end = t + pd.Timedelta(minutes=5)
            m15c = m15[m15.index + pd.Timedelta(minutes=15) <= now_end]
            setups, _ = ios.find_setups(sym, upto, m15c, daily, day)
            if setups:
                s = max(setups, key=lambda x: x.rr)
                tr = ios.new_trade(s, lots)
                n_tr += 1
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--syms", nargs="+", default=list(TOKENS))
    ap.add_argument("--lots", type=int, default=1, help="1 = all out at T1; 2 = half at T1, rest trails")
    ap.add_argument("--max-trades", type=int, default=3)
    ap.add_argument("--max-losses", type=int, default=2)
    a = ap.parse_args()
    env = load_env()
    kite = ko.kite_for(env.get("KITE_API_KEY", ""), env.get("KITE_ACCESS_TOKEN", ""))
    kite.profile()
    end = datetime.date.today()
    start = end - datetime.timedelta(days=a.days)
    rows = []
    for sym in a.syms:
        tok = TOKENS[sym]
        print(f"{sym}: downloading …", flush=True)
        daily = history(kite, tok, "day", start - datetime.timedelta(days=200), end)
        daily.index = [x.date() for x in daily.index]
        m15 = history(kite, tok, "15minute", start - datetime.timedelta(days=10), end)
        m5 = history(kite, tok, "5minute", start - datetime.timedelta(days=5), end)
        days = sorted({d for d in m5.index.date if d >= start})
        print(f"{sym}: {len(days)} days, {len(m5)} 5-min candles — replaying …", flush=True)
        rows += run(sym, m5, m15, daily, days, a.lots, a.max_trades, a.max_losses)
    if not rows:
        print("no trades")
        return
    df = pd.DataFrame(rows)
    out = ROOT / "data" / "ios"
    out.mkdir(parents=True, exist_ok=True)
    f = out / f"backtest_{end}_{a.days}d_{a.lots}lot.csv"
    df.to_csv(f, index=False)
    pd.set_option("display.width", 220)
    print(f"\n{len(df)} trades · {a.days} days · {a.lots} lot(s) · R = points ÷ risk\n")
    g = df.groupby(["sym", "kind"]).agg(trades=("R", "size"), win_pct=("R", lambda x: round(100 * (x > 0).mean())),
                                        avg_R=("R", "mean"), total_R=("R", "sum"), avg_pts=("pts", "mean"),
                                        avg_risk=("risk", "mean")).round(2)
    print(g.to_string())
    print("\nby side:\n" + df.groupby(["sym", "side"])["R"].agg(["size", "mean", "sum"]).round(2).to_string())
    kinds = ["T1", "structure stop", "breakeven stop", "trail stop", "5m structure broke", "15:15", "OI against"]
    ex = df["exit"].map(lambda s: next((k for k in kinds if s.startswith(k)), s))
    print("\nexits (count, avg R):\n" + df.groupby(ex)["R"].agg(["size", "mean"]).round(2).to_string())
    print(f"\nALL: {len(df)} trades · win {100 * (df['R'] > 0).mean():.0f}% · avg {df['R'].mean():+.2f}R · "
          f"total {df['R'].sum():+.1f}R\nfull list → {f}")


if __name__ == "__main__":
    main()
