#!/usr/bin/env python3
"""
pulse_backtest.py — NIFTY Pulse (core/pulse.py) on Kite history. Read-only, no orders.

Same Strategy / manage_exit / guards / sizing as the live bot, run candle by candle:
  * signal on a closed 5-min candle → armed for 2 candles → entered at the trigger when a later candle
    trades through it (skipped if it opens > 0.3R past it; if one candle touches both the stop and the
    trigger, the stop is assumed first → cancelled)
  * open trade: stop first (if the candle reaches it), then T1 half / T2 at their prices, then the
    closed-candle trail / lock / time stop. 15:10 square-off.
Limits of the data: futures = the CURRENT NIFTY futures contract for the whole window (Kite keeps no
intraday history of expired futures), so the test is limited to that contract's life (~3 months);
max-OI strikes are left out (no option-OI history); the premium is modelled as entry + delta × spot move
(no theta / IV), entry premium PREM. India VIX from its own 5-min history.

Usage (droplet):
  /root/venv/bin/python scripts/pulse_backtest.py                 # 60 days
  /root/venv/bin/python scripts/pulse_backtest.py --days 85 --capital 300000 --risk 0.02
  /root/venv/bin/python scripts/pulse_backtest.py --compare          # baseline vs initial-balance variants
  /root/venv/bin/python scripts/pulse_backtest.py --ib-mode 2 --ib-early none
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from core import pulse as P   # noqa: E402

PREM = 150.0
LOT = 65


def run(cfg: P.Config, spot: list, fut: list, vix: dict, start: datetime.date, lot: int = LOT,
        log=print) -> list:
    strat = P.Strategy(cfg)
    spot, fut = P.align(spot, fut)
    x = strat.context(spot, fut)
    delta = cfg.delta_itm if cfg.itm_steps else cfg.delta_atm
    trades, st, day = [], None, None
    armed, pos, armed_left = None, None, 0
    sq = cfg.t(cfg.square_off)

    def prem_at(px: float) -> float:
        return max(0.05, pos.entry_prem + delta * (px - pos.entry_spot) * (1 if pos.sig.side == "CE" else -1))

    def close(px: float, why: str, b: P.Bar):
        nonlocal pos
        p = pos
        sell = prem_at(px)
        p.realised += (sell - p.entry_prem) * p.qty_open
        p.realised -= cfg.cost_per_lot_rt * p.lots
        risk_rs = cfg.capital * cfg.risk_pct
        st.pnl += p.realised
        st.pnl_r += p.realised / risk_rs
        if p.realised < 0:
            st.losses += 1
            st.last_loss_at = b.ts.isoformat()
        if st.pnl <= -cfg.daily_loss_limit_pct * cfg.capital:
            st.halted = True
        sgn = 1 if p.sig.side == "CE" else -1
        trades.append({"day": b.ts.date(), "in": p.opened_at[11:16], "out": f"{b.ts:%H:%M}", "side": p.sig.side,
                       "kind": p.sig.kind, "pattern": p.sig.pattern, "entry": round(p.entry_spot, 1),
                       "stop0": p.sig.stop, "risk": p.sig.risk, "exit_spot": round(px, 1),
                       "pts": round(sgn * (px - p.entry_spot), 1), "lots": p.lots,
                       "peak_R": round(p.peak_r, 2), "rs": round(p.realised), "R": round(p.realised / risk_rs, 2),
                       "partial": p.partial_done and p.lots > 1, "why": why})
        pos = None

    for i in range(max(cfg.ema_trend + 5, 4), len(spot)):
        b = spot[i]
        if b.ts.date() < start:
            continue
        if b.ts.date() != day:
            day = b.ts.date()
            st = P.DayState(day.isoformat())
            armed = None
        end_ts = b.ts + datetime.timedelta(minutes=cfg.candle_minutes)
        x_atr = x["atr"][i]

        # ---- open trade on this candle
        if pos:
            sgn = 1 if pos.sig.side == "CE" else -1
            adverse = b.low if sgn > 0 else b.high
            fav = b.high if sgn > 0 else b.low
            if sgn * (adverse - pos.stop_spot) <= 0:
                px = b.open if sgn * (b.open - pos.stop_spot) <= 0 else pos.stop_spot     # gap through the stop
                close(px, f"stop {pos.stop_spot:.1f}", b)
            else:
                pos.atr_now = x_atr
                acts = P.manage_exit(pos, b, prem_at(b.close), cfg, closed_bar=True)
                for a_ in acts:
                    if a_[0] == "partial":            # booked when spot touched entry + 1R inside the candle
                        q = a_[1]
                        pos.realised += (prem_at(pos.entry_spot + sgn * cfg.partial_at_r * pos.sig.risk)
                                         - pos.entry_prem) * q
                        pos.qty_open -= q
                # T2 touched inside the candle (the watcher sees it on LTP)
                if sgn * (fav - pos.sig.t2) >= 0 and not (pos.sig.strong and cfg.trail_runner_in_strong_trend):
                    close(pos.sig.t2, "target T2", b)
                else:
                    ex_ = [a_ for a_ in acts if a_[0] == "exit"]
                    if ex_:
                        close(b.close, ex_[0][1], b)
                    elif end_ts.time() >= sq:
                        close(b.close, "15:10 square-off", b)
            continue

        # ---- armed signal: did this candle trigger it?
        if armed:
            s = armed
            sgn = 1 if s.side == "CE" else -1
            adverse = b.low if sgn > 0 else b.high
            fav = b.high if sgn > 0 else b.low
            if b.ts.time() > cfg.last_entry:
                armed = None
            elif sgn * (b.open - s.trigger) > cfg.overshoot_r * s.risk:
                armed = None                                                         # gapped past: no chase
            elif sgn * (adverse - s.stop) <= 0:
                armed = None                                                         # stop side first
            elif sgn * (fav - s.trigger) >= 0:
                entry = b.open if sgn * (b.open - s.trigger) >= 0 else s.trigger
                half = st.pnl_r >= cfg.daily_profit_lock_r
                lots = P.size_lots(s.risk, PREM, delta, lot, cfg, half=half)
                armed = None
                if lots >= 1:
                    pos = P.Position(s, "SIM", lots, lot, PREM, entry, s.stop, delta, b.ts.isoformat(),
                                     peak_prem=PREM, best_spot=entry, qty_open=lots * lot, atr_now=x_atr)
                    st.trades += 1
                    # rest of this candle: stop / T1 / closed-candle management
                    if sgn * (b.close - pos.stop_spot) <= 0:
                        close(pos.stop_spot, f"stop {pos.stop_spot:.1f} (entry candle)", b)
                    else:
                        P.manage_exit(pos, b, prem_at(b.close), cfg, closed_bar=False)
                    continue
            else:
                armed_left -= 1
                if armed_left <= 0:
                    armed = None

        # ---- scan the closed candle i for a new signal
        strat.update_flow_blocks(i, spot, fut, x, st)
        if pos or armed:
            continue
        now = end_ts
        v = vix.get(b.ts)
        if P.can_trade(now, st, cfg, v, False, str(ROOT)):
            continue
        lv_from = max(0, i - 400)
        levels = P.build_levels(spot[lv_from: i + 1], cfg, x_atr)
        sig, _ = strat.evaluate(i, spot, fut, x, levels, st)
        if sig:
            armed, armed_left = sig, cfg.trigger_valid_bars
    return trades


def summary(rows: list, label: str) -> str:
    if not rows:
        return f"{label}: no trades"
    w = [r for r in rows if r["rs"] > 0]
    l_ = [r for r in rows if r["rs"] <= 0]
    eq, peak, dd = 0.0, 0.0, 0.0
    for r in rows:
        eq += r["R"]
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    return (f"{label}: {len(rows)} trades · win {100 * len(w) / len(rows):.0f}% · avg win "
            f"{sum(r['R'] for r in w) / max(len(w), 1):+.2f}R · avg loss {sum(r['R'] for r in l_) / max(len(l_), 1):+.2f}R"
            f" · total {sum(r['R'] for r in rows):+.1f}R (₹{sum(r['rs'] for r in rows):+,.0f}) · max drawdown {dd:.1f}R")


def main() -> None:
    import pulse_bot as pb
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--capital", type=float)
    ap.add_argument("--risk", type=float)
    ap.add_argument("--ib-mode", type=int, help="0 off · 1 IB levels · 2 IB levels + IB break filter")
    ap.add_argument("--ib-early", choices=["all", "strong", "none"], help="ib-mode 2: entries before 10:15")
    ap.add_argument("--compare", action="store_true", help="baseline vs the initial-balance variants")
    a = ap.parse_args()
    env = pb.load_env()
    cfg = P.Config.from_env(env)
    if a.capital:
        cfg.capital = a.capital
    if a.risk:
        cfg.risk_pct = a.risk
    if a.ib_mode is not None:
        cfg.ib_mode = a.ib_mode
    if a.ib_early:
        cfg.ib_early = a.ib_early
    bot = pb.Bot(env, dry=True)
    kite = bot.kite
    kite.profile()
    end = datetime.date.today()
    start = end - datetime.timedelta(days=a.days)
    fut_row = bot.future()

    def hist(token: int, frm: datetime.date) -> list:
        out, d = [], frm
        while d <= end:                                         # Kite: ≤ 100 days per 5-min request
            to = min(end, d + datetime.timedelta(days=60))
            for r in kite.historical_data(token, d, to, cfg.candle_interval):
                ts = r["date"]
                if getattr(ts, "tzinfo", None) is not None:
                    ts = ts.astimezone(pb.IST).replace(tzinfo=None)
                out.append(P.Bar(ts, float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]),
                                 float(r.get("volume") or 0)))
            d = to + datetime.timedelta(days=1)
        today_open = [b for b in out if b.ts + datetime.timedelta(minutes=cfg.candle_minutes) <= pb.now_ist()]
        return sorted({b.ts: b for b in today_open}.values(), key=lambda b: b.ts)

    warm = start - datetime.timedelta(days=10)
    print(f"downloading NIFTY spot, {fut_row['tradingsymbol']}, India VIX from {warm} …", flush=True)
    spot = hist(256265, warm)
    fut = hist(int(fut_row["instrument_token"]), warm)
    vix = {b.ts: b.close for b in hist(264969, warm)}
    if fut:
        first = fut[0].ts.date()
        if first > start:
            print(f"note: {fut_row['tradingsymbol']} history starts {first} — test window starts there")
            start = max(start, (fut[0].ts + datetime.timedelta(days=4)).date())
    if a.compare:
        variants = [("baseline (no IB)", 0, "all"), ("IB levels only", 1, "all"),
                    ("IB filter · before 10:15 strong trend only", 2, "strong"),
                    ("IB filter · nothing before 10:15", 2, "none"),
                    ("IB filter · before 10:15 all", 2, "all")]
        print(f"\nNIFTY Pulse · {start} → {end} · capital ₹{cfg.capital:,.0f} · risk {cfg.risk_pct:.1%}\n")
        allrows = []
        for name, mode, early in variants:
            v = dataclasses.replace(cfg, ib_mode=mode, ib_early=early)
            rs = run(v, spot, fut, vix, start)
            print(summary(rs, f"{name:<44}"))
            days = sorted({r["day"] for r in rs})
            losing = sum(1 for d in days if sum(r["rs"] for r in rs if r["day"] == d) < 0)
            print(f"{'':<46}days traded {len(days)} · losing days {losing} · "
                  f"ib retest trades {sum(1 for r in rs if r['kind'] == 'ib retest')}")
            allrows += [{"variant": name, **r} for r in rs]
        out = ROOT / "data" / "pulse"
        out.mkdir(parents=True, exist_ok=True)
        f = out / f"backtest_compare_{end}_{a.days}d.csv"
        if allrows:
            keys = list(allrows[0])
            f.write_text(",".join(keys) + "\n" + "\n".join(",".join(str(r[k]).replace(",", ";") for k in keys)
                                                           for r in allrows) + "\n")
            print(f"\nall trades → {f}")
        return
    rows = run(cfg, spot, fut, vix, start)
    print(f"\nNIFTY Pulse · IB mode {cfg.ib_mode} ({cfg.ib_early}) · {start} → {end} · capital ₹{cfg.capital:,.0f} · "
          f"risk {cfg.risk_pct:.1%} · premium "
          f"modelled at ₹{PREM:g}, delta {cfg.delta_itm}, lot {LOT}, ₹{cfg.cost_per_lot_rt:g}/lot costs\n")
    print(summary(rows, "ALL"))
    for side in ("CE", "PE"):
        print(summary([r for r in rows if r["side"] == side], side))
    for kind in ("pullback", "continuation", "retest", "ib retest"):
        print(summary([r for r in rows if r["kind"] == kind], kind))
    days = sorted({r["day"] for r in rows})
    if days:
        last5 = days[-5:]
        print(summary([r for r in rows if r["day"] in last5], f"last 5 trading days with trades ({last5[0]}…)"))
    ex: dict = {}
    for r in rows:
        k = r["why"].split(" ")[0] if not r["why"].startswith("time") else "time stop"
        ex.setdefault(k, []).append(r["R"])
    print("\nexits: " + " · ".join(f"{k} {len(v)} ({sum(v):+.1f}R)" for k, v in ex.items()))
    print("\nlast 15 trades:")
    for r in rows[-15:]:
        print(f"  {r['day']} {r['in']}-{r['out']} {r['side']} {r['kind']:<12} {r['pattern']:<20} entry {r['entry']:.0f} "
              f"R {r['risk']:.0f} · peak {r['peak_R']:+.1f}R · {r['R']:+.2f}R ₹{r['rs']:+,} ({r['lots']} lot) — {r['why']}")
    out = ROOT / "data" / "pulse"
    out.mkdir(parents=True, exist_ok=True)
    f = out / f"backtest_{end}_{a.days}d.csv"
    if rows:
        keys = list(rows[0])
        f.write_text(",".join(keys) + "\n" + "\n".join(",".join(str(r[k]).replace(",", ";") for k in keys)
                                                       for r in rows) + "\n")
        print(f"\nall trades → {f}")


if __name__ == "__main__":
    main()
