"""
Backtest / replay of chart_bot rules on Kite history.

Replays the live pipeline (analyze_df on closed 15m candles, 5m confirmation,
Noise Area state) every 5 minutes of past sessions, then simulates trades on
5m spot bars for several rule sets side by side:

  old       score ≥ 3, no 5m requirement, no noise gate, SL/target/EOD exits
  new       score ≥ 4, 5m confirms, noise breakout, time stop, partial at T1,
            2-loss halt, max 4 trades/day   (what chart_bot does now)
  new_adx   like "new" but ADX ≥ 20 instead of the noise gate

Results are in R (1R = distance from entry to the chart stop loss, on the
index) so they don't depend on option delta. A rough option ₹ estimate uses
delta 0.5 × lot size and ignores theta/IV, so treat it as directional only.

Known gaps vs live: no GEX (no historical option chains), VWAP without futures
volume (expired futures history isn't available), spot-level SL/targets.

Usage:
  python scripts/backtest_chart.py                    # NIFTY + SENSEX, last 30 days
  python scripts/backtest_chart.py NIFTY --days 60
"""

import argparse
import datetime
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import numpy as np
import pandas as pd
import requests

from screener.chart_analysis import _INDEX_TOKENS, _KITE_BASE, _hdrs, analyze_df
from screener.noise_area import compute_noise_state

_ROOT = pathlib.Path(__file__).parent.parent
_IST  = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
_LOT  = {"NIFTY": 65, "SENSEX": 20, "BANKNIFTY": 30}
_MAX_DAYS_PER_CALL = {"5minute": 90, "15minute": 180}

SIGNAL_OPEN, SIGNAL_CLOSE, EOD_EXIT = "09:30", "15:00", "15:20"

# atr_sl: SL is the wider of the chart SL and 1.5× 5m ATR, T1 ≥ 1.5R — the spot-level
# equivalent of the live bot's option-ATR stop (option ATR ≈ delta × spot ATR).
VARIANTS = {
    "old":     dict(min_score=3, need_5m=False, gate="none",  time_stop=False, partial=False, halt=99, max_trades=99, atr_sl=False),
    "new":     dict(min_score=4, need_5m=True,  gate="noise", time_stop=True,  partial=True,  halt=2,  max_trades=4,  atr_sl=True),
    "new_adx": dict(min_score=4, need_5m=True,  gate="adx",   time_stop=True,  partial=True,  halt=2,  max_trades=4,  atr_sl=True),
}


def _fetch(token: int, interval: str, start: datetime.date, end: datetime.date,
           api_key: str, access_token: str) -> pd.DataFrame:
    frames, cur = [], start
    step = datetime.timedelta(days=_MAX_DAYS_PER_CALL[interval])
    while cur <= end:
        to = min(end, cur + step)
        r = requests.get(
            f"{_KITE_BASE}/instruments/historical/{token}/{interval}",
            headers=_hdrs(api_key, access_token),
            params={"from": f"{cur} 09:15:00", "to": f"{to} 15:30:00", "continuous": 0, "oi": 0},
            timeout=30,
        )
        r.raise_for_status()
        c = r.json().get("data", {}).get("candles", [])
        if c:
            frames.append(pd.DataFrame(c, columns=["datetime", "Open", "High", "Low", "Close", "Volume"]))
        cur = to + datetime.timedelta(days=1)
        time.sleep(0.4)   # Kite historical rate limit
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames)
    df["datetime"] = pd.to_datetime(df["datetime"])
    if df["datetime"].dt.tz is None:
        df["datetime"] = df["datetime"].dt.tz_localize("Asia/Kolkata")
    else:
        df["datetime"] = df["datetime"].dt.tz_convert("Asia/Kolkata")
    df = df.drop_duplicates("datetime").set_index("datetime").sort_index()
    return df.between_time("09:15", "15:25")


def build_signals(symbol: str, df15: pd.DataFrame, df5: pd.DataFrame, days: list) -> pd.DataFrame:
    """One row per 5-minute step: the live bot's view at that moment."""
    rows = []
    pc = df5["Close"].shift(1)
    tr = pd.concat([df5["High"] - df5["Low"], (df5["High"] - pc).abs(), (df5["Low"] - pc).abs()], axis=1).max(axis=1)
    atr5 = tr.rolling(14).mean()
    for di, day in enumerate(days):
        steps = pd.date_range(f"{day} {SIGNAL_OPEN}", f"{day} {SIGNAL_CLOSE}", freq="5min", tz="Asia/Kolkata")
        last15, v15 = None, None
        hist5 = df5[(df5.index.date <= day)]
        hist5 = hist5[hist5.index.date >= sorted(set(hist5.index.date))[-16]] if len(set(hist5.index.date)) >= 16 else hist5
        for t in steps:
            c15 = df15[df15.index + pd.Timedelta(minutes=15) <= t].tail(300)
            if len(c15) < 60:
                continue
            if c15.index[-1] != last15:
                last15 = c15.index[-1]
                res = analyze_df(c15, "15m")
                v15 = res["verdict"]
                v15["adx"] = res["summary"]["adx"]
            score = v15["score"]
            verdict = v15["verdict"]
            direction = "CE" if "BUY" in verdict else ("PE" if "SELL" in verdict else "WAIT")
            row = {"t": t, "day": day, "symbol": symbol, "direction": direction, "score": score,
                   "adx": v15["adx"], "sl": v15.get("stop_loss"),
                   "t1": (v15.get("targets") or [None])[0], "s5": 0, "noise": "unknown",
                   "atr5": float(atr5[atr5.index + pd.Timedelta(minutes=5) <= t].iloc[-1])}
            if direction != "WAIT" and abs(score) >= 3:
                c5 = df5[df5.index + pd.Timedelta(minutes=5) <= t].tail(300)
                if len(c5) >= 60:
                    row["s5"] = analyze_df(c5, "5m")["verdict"]["score"]
                n5 = hist5[hist5.index + pd.Timedelta(minutes=5) <= t]
                row["noise"] = compute_noise_state(n5).get("state", "unknown")
            rows.append(row)
        print(f"  {symbol}: {day} ({di + 1}/{len(days)})", end="\r", flush=True)
    print()
    return pd.DataFrame(rows)


def _passes(row, v: dict) -> bool:
    d, s = row["direction"], row["score"]
    if d == "WAIT" or abs(s) < v["min_score"]:
        return False
    s5 = row["s5"]
    if (d == "CE" and s5 <= -3) or (d == "PE" and s5 >= 3):
        return False
    if v["need_5m"] and not ((d == "CE" and s5 >= 1) or (d == "PE" and s5 <= -1)):
        return False
    if v["gate"] == "noise":
        if row["noise"] in ("above", "below", "inside"):
            return row["noise"] == ("above" if d == "CE" else "below")
        return row["adx"] >= 20
    if v["gate"] == "adx":
        return row["adx"] >= 20
    return True


def simulate_trade(bars: pd.DataFrame, d: int, entry: float, sl: float, t1: float, v: dict) -> dict:
    """Walk 5m bars after entry. Returns {"r", "pts", "exit", "minutes"}."""
    risk = abs(entry - sl)
    reward = abs(t1 - entry)
    runner_t = entry + d * 2 * reward
    stop, booked, size = sl, 0.0, 1.0
    t_start = bars.index[0]
    for ts, b in bars.iterrows():
        minutes = (ts - t_start).total_seconds() / 60
        lo, hi, cl = b["Low"], b["High"], b["Close"]
        stop_hit = (lo <= stop) if d > 0 else (hi >= stop)
        if stop_hit:
            pts = booked + size * (stop - entry) * d
            return {"pts": pts, "exit": "sl" if stop == sl else "breakeven", "minutes": minutes}
        tgt = runner_t if (v["partial"] and size < 1) else t1
        tgt_hit = (hi >= tgt) if d > 0 else (lo <= tgt)
        if tgt_hit:
            if v["partial"] and size == 1.0:
                booked, size, stop = 0.5 * reward, 0.5, entry
                if ((hi >= runner_t) if d > 0 else (lo <= runner_t)):
                    return {"pts": booked + size * 2 * reward, "exit": "target", "minutes": minutes}
                continue
            pts = booked + size * abs(tgt - entry)
            return {"pts": pts, "exit": "target" if size == 1.0 or tgt == runner_t else "t1", "minutes": minutes}
        if v["time_stop"] and size == 1.0 and minutes >= 30 and (cl - entry) * d < 0.3 * risk:
            return {"pts": (cl - entry) * d, "exit": "time", "minutes": minutes}
        if ts.strftime("%H:%M") >= EOD_EXIT:
            return {"pts": booked + size * (cl - entry) * d, "exit": "eod", "minutes": minutes}
    cl = bars["Close"].iloc[-1]
    return {"pts": booked + size * (cl - entry) * d, "exit": "eod", "minutes": 0}


def run_variant(name: str, v: dict, sig: pd.DataFrame, df5: pd.DataFrame, cost_pts: float) -> pd.DataFrame:
    trades = []
    for (symbol, day), g in sig.groupby(["symbol", "day"]):
        bars_day = df5[symbol][df5[symbol].index.date == day]
        busy_until = None   # open trade + 30-min cooldown, as in the live bot
        losses = n = 0
        for _, row in g.iterrows():
            t = row["t"]
            if busy_until is not None and t < busy_until:
                continue
            if losses >= v["halt"] or n >= v["max_trades"]:
                break
            if not _passes(row, v):
                continue
            d = 1 if row["direction"] == "CE" else -1
            after = bars_day[bars_day.index >= t]
            if after.empty or row["sl"] is None:
                continue
            entry = float(after["Open"].iloc[0])
            sl = float(row["sl"])
            if (sl - entry) * d >= 0:
                continue
            if v["atr_sl"] and row["atr5"] == row["atr5"]:
                sl = entry - d * max(abs(entry - sl), 1.5 * row["atr5"])
            risk = abs(entry - sl)
            t1 = row["t1"]
            if t1 is None or (float(t1) - entry) * d <= 0:
                t1 = entry + d * 1.5 * risk
            elif v["atr_sl"] and abs(float(t1) - entry) < 1.5 * risk:
                t1 = entry + d * 1.5 * risk
            res = simulate_trade(after, d, entry, sl, float(t1), v)
            pts = res["pts"] - cost_pts
            trades.append({"variant": name, "symbol": symbol, "day": day, "time": t.strftime("%H:%M"),
                           "direction": row["direction"], "score": row["score"], "entry": round(entry, 1),
                           "sl": round(sl, 1), "t1": round(float(t1), 1), "exit": res["exit"],
                           "minutes": res["minutes"], "pts": round(pts, 1), "r": round(pts / risk, 2),
                           "est_rs_per_lot": round(pts * 0.5 * _LOT.get(symbol, 1))})
            n += 1
            losses = losses + 1 if pts < 0 else 0
            busy_until = after.index[0] + pd.Timedelta(minutes=res["minutes"] + 30)
    return pd.DataFrame(trades)


def _summary(tr: pd.DataFrame) -> dict:
    if tr.empty:
        return {"trades": 0}
    eq = tr["r"].cumsum()
    return {
        "trades":   len(tr),
        "win%":     round(float((tr["r"] > 0).mean()) * 100),
        "avg R":    round(float(tr["r"].mean()), 2),
        "total R":  round(float(tr["r"].sum()), 1),
        "max DD R": round(float((eq.cummax() - eq).max()), 1),
        "SL exits": int((tr["exit"] == "sl").sum()),
        "avg min":  round(float(tr["minutes"].mean())),
        "est ₹/lot": int(tr["est_rs_per_lot"].sum()),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Replay chart_bot rules on history")
    ap.add_argument("symbols", nargs="*", default=["NIFTY", "SENSEX"])
    ap.add_argument("--days", type=int, default=30, help="Trading days to test")
    ap.add_argument("--cost-pts", type=float, default=3.0,
                    help="Index points deducted per trade for costs/slippage (default 3)")
    args = ap.parse_args()

    from scripts.chart_bot import _kite_creds
    api_key, access_token = _kite_creds()
    today = datetime.datetime.now(_IST).date()
    start = today - datetime.timedelta(days=int(args.days * 1.5) + 40)

    all_sig, df5_map = [], {}
    for sym in [s.upper() for s in args.symbols]:
        tok = _INDEX_TOKENS.get(sym)
        if not tok:
            print(f"{sym}: only NIFTY/BANKNIFTY/SENSEX are supported")
            continue
        print(f"Fetching {sym} history…")
        df15 = _fetch(tok, "15minute", start, today, api_key, access_token)
        df5  = _fetch(tok, "5minute",  start, today, api_key, access_token)
        if df15.empty or df5.empty:
            print(f"{sym}: no data")
            continue
        days = [d for d in sorted(set(df5.index.date)) if d < today][-args.days:]
        df5_map[sym] = df5
        all_sig.append(build_signals(sym, df15, df5, days))
    if not all_sig:
        return
    sig = pd.concat(all_sig, ignore_index=True)

    results, all_trades = {}, []
    for name, v in VARIANTS.items():
        tr = run_variant(name, v, sig, df5_map, args.cost_pts)
        results[name] = _summary(tr)
        all_trades.append(tr)
        if not tr.empty:
            for sym, g in tr.groupby("symbol"):
                results[f"{name} · {sym}"] = _summary(g)

    print(f"\nBacktest: {', '.join(df5_map)} | last {args.days} trading days | cost {args.cost_pts} pts/trade\n")
    print(pd.DataFrame(results).T.fillna("").to_string())
    out = _ROOT / "data" / "backtest_trades.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    pd.concat(all_trades, ignore_index=True).to_csv(out, index=False)
    print(f"\nTrades → {out}")
    print("R = profit ÷ (entry→SL distance). est ₹/lot uses delta 0.5 and ignores theta/IV — directional only.")


if __name__ == "__main__":
    main()
