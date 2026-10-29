"""
CE/PE Signal Bot — 5-minute rule engine + backtest log
======================================================
Branch: chart-screen-logic

Runs screener/ce_pe_engine.py on NIFTY (or other indices) and appends every
snapshot + verdict to a JSONL log. No notifications and no orders — this bot
only records signals so the thresholds can be tuned before going live.

Modes
-----
Live (cron, every 5 min, ~10s after each candle close):
  */5 9-15 * * 1-5 sleep 10; cd /root/stockAnalyser && /root/venv/bin/python scripts/ce_pe_bot.py --once >> ce_pe_bot.log 2>&1

Replay / backtest over history (same engine, completed candles only, no look-ahead):
  /root/venv/bin/python scripts/ce_pe_bot.py --replay 2026-10-01 2026-10-29
  /root/venv/bin/python scripts/ce_pe_bot.py --replay 2026-10-29 2026-10-29 --symbols NIFTY SENSEX

Offline replay from CSV files (5m.csv, 15m.csv, 1H.csv, 1D.csv with
columns datetime,Open,High,Low,Close,Volume):
  python scripts/ce_pe_bot.py --replay 2026-10-29 2026-10-29 --csv-dir data/nifty

Log line format (one JSON object per evaluation):
  {"mode": "live"|"replay", "snapshot": {...}, "verdict": {...}}

Educational tooling — not financial advice.
"""

from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import pandas as pd

from screener.ce_pe_engine import (
    ALL_TFS, analyze_frames, build_snapshot, candles_to_df, evaluate, fetch_range,
)

ROOT        = pathlib.Path(__file__).parent.parent
DEFAULT_LOG = ROOT / "logs" / "ce_pe_signals.jsonl"
REPLAY_LOG  = ROOT / "logs" / "ce_pe_replay.jsonl"
STATE_FILE  = "/tmp/ce_pe_bot_state.json"
SYMBOLS     = ["NIFTY"]

# history needed before the first replay day (calendar days), per timeframe
_LOOKBACK = {"5m": 8, "15m": 20, "1H": 70, "1D": 760}


# ── IO helpers ────────────────────────────────────────────────────────────────

def _append_jsonl(path: pathlib.Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, default=str) + "\n")


def _load_state(path: str) -> dict:
    try:
        return json.loads(pathlib.Path(path).read_text())
    except Exception:
        return {}


def _save_state(path: str, state: dict) -> None:
    pathlib.Path(path).write_text(json.dumps(state, indent=2, default=str))


def _line(v: dict) -> str:
    t   = v["timestamp_ist"][11:16]
    act = v["action"]
    sc  = "" if v.get("score") is None else f" score {v['score']:>3}"
    if act.startswith(("STRONG", "MODERATE")):
        p = v["plan"]
        return (f"[{t}] {v['symbol']:9s} {act:12s}{sc} | {p['entry_type']} {p['entry']:,.1f} "
                f"SL {p['stop']:,.1f} T {p['target']:,.1f} R:R {p['rr']}")
    if act == "EXIT":
        pts = "" if v.get("points") is None else f" @ {v['exit_price']:,.1f} ({v['points']:+} pts)"
        return f"[{t}] {v['symbol']:9s} EXIT {v['side']} — {v.get('exit_reason')}{pts}"
    reason = v.get("failed_reason") or (v.get("reasoning") or [""])[0]
    return f"[{t}] {v['symbol']:9s} {act:12s}{sc} | {reason}"


# ── Live ──────────────────────────────────────────────────────────────────────

def run_once(symbols: list[str], state_file: str, log_path: pathlib.Path) -> None:
    from scripts.chart_bot import _kite_creds
    from screener.ce_pe_engine import run_live

    api_key, token = _kite_creds()
    if not (api_key and token):
        print("ERROR: KITE_API_KEY and KITE_ACCESS_TOKEN required.")
        sys.exit(1)

    now   = pd.Timestamp.now(tz="Asia/Kolkata")
    if not (datetime.time(9, 20) <= now.time() <= datetime.time(15, 35)):
        print(f"[{now:%H:%M}] Market closed — skipping.")
        return

    state = _load_state(state_file)
    today = now.date().isoformat()
    for sym in symbols:
        sym_state = state.get(sym, {})
        if sym_state.get("date") != today:           # intraday: reset every session
            sym_state = {"date": today}
        try:
            snap, verdict, new_state = run_live(sym, api_key, token, sym_state, now)
        except Exception as e:
            print(f"[{now:%H:%M}] {sym}: ERROR — {e}")
            continue
        new_state["date"] = today
        state[sym] = new_state
        _append_jsonl(log_path, {"mode": "live", "snapshot": snap, "verdict": verdict})
        print(_line(verdict))
    _save_state(state_file, state)


# ── Replay ────────────────────────────────────────────────────────────────────

def load_frames_kite(symbol: str, start: datetime.date, end: datetime.date) -> dict:
    from scripts.chart_bot import _kite_creds
    from screener.chart_analysis import resolve_token
    api_key, token = _kite_creds()
    if not (api_key and token):
        print("ERROR: KITE_API_KEY and KITE_ACCESS_TOKEN required for replay.")
        sys.exit(1)
    inst, _ = resolve_token(symbol, api_key, token)
    frames = {}
    for tf in ALL_TFS:
        frames[tf] = fetch_range(inst, tf, start - datetime.timedelta(days=_LOOKBACK[tf]),
                                 end, api_key, token)
        time.sleep(0.4)                                # stay under Kite's 3 req/s
    return frames


def load_frames_csv(folder: pathlib.Path) -> dict:
    frames = {}
    for tf in ALL_TFS:
        p = folder / f"{tf}.csv"
        if not p.exists():
            raise FileNotFoundError(f"missing {p}")
        raw = pd.read_csv(p)
        frames[tf] = candles_to_df(raw[["datetime", "Open", "High", "Low", "Close", "Volume"]]
                                   .values.tolist(), tf)
    return frames


def replay(symbol: str, frames: dict, start: datetime.date, end: datetime.date,
           log_path: pathlib.Path | None, verbose: bool = True) -> list[dict]:
    """Walk every completed 5m candle in [start, end] and evaluate. Returns trades."""
    df5   = frames["5m"]
    days  = sorted({d for d in df5.index.date if start <= d <= end})
    cache: dict = {}
    trades: list[dict] = []
    for day in days:
        state: dict = {}
        open_sig: dict | None = None
        for bar in df5[df5.index.date == day].index:
            now = bar + pd.Timedelta(minutes=5)
            results  = analyze_frames(symbol, frames, now, cache)
            snap     = build_snapshot(symbol, results, now)
            verdict, state = evaluate(snap, state)
            if log_path:
                _append_jsonl(log_path, {"mode": "replay", "snapshot": snap, "verdict": verdict})
            act = verdict["action"]
            if act.startswith(("STRONG", "MODERATE")):
                open_sig = {"day": str(day), "time": verdict["timestamp_ist"][11:16],
                            "action": act, "grade": verdict["grade"], "side": verdict["side"],
                            "score": verdict["score"], **verdict["plan"]}
            elif act == "EXIT" and open_sig:
                open_sig.update(result="UNFILLED" if verdict.get("unfilled") else "EXIT",
                                exit_time=verdict["timestamp_ist"][11:16],
                                reason=verdict.get("exit_reason"), points=verdict.get("points"))
                trades.append(open_sig); open_sig = None
            if verbose and (act not in ("WAIT", "NO_TRADE", "HOLD")):
                print(_line(verdict))
        if open_sig:                                   # should not happen (square-off), but be safe
            open_sig.update(result="OPEN", reason="end of data", points=None)
            trades.append(open_sig)
    return trades


def print_summary(trades: list[dict]) -> None:
    print("\n" + "=" * 78)
    print(f"{'Day':10s} {'Time':5s} {'Signal':12s} {'Sc':>3s} {'Type':8s} {'Entry':>9s} "
          f"{'SL':>9s} {'Tgt':>9s}  Result")
    print("-" * 78)
    for t in trades:
        res = t["result"] if t["result"] != "EXIT" else f"{t['reason']} {t['points']:+.1f}"
        print(f"{t['day']:10s} {t['time']:5s} {t['action']:12s} {t['score']:>3} {t['entry_type']:8s} "
              f"{t['entry']:>9,.1f} {t['stop']:>9,.1f} {t['target']:>9,.1f}  {res} ({t.get('exit_time', '')})")
    print("-" * 78)
    for grade in ("STRONG", "MODERATE"):
        g  = [t for t in trades if t["grade"] == grade]
        ex = [t for t in g if t["result"] == "EXIT"]
        if not g:
            continue
        wins = [t for t in ex if t["points"] > 0]
        pts  = sum(t["points"] for t in ex)
        print(f"{grade:9s} signals {len(g):3d} | filled+closed {len(ex):3d} | "
              f"win rate {len(wins) / len(ex) * 100 if ex else 0:5.1f}% | net {pts:+.1f} index pts")
    if not trades:
        print("No STRONG/MODERATE signals in this range.")
    print("=" * 78)
    print("Points are NIFTY spot points, not option P&L. Educational backtest only.")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="CE/PE 5-minute rule engine + signal log")
    ap.add_argument("--symbols", nargs="*", default=SYMBOLS)
    ap.add_argument("--once", action="store_true", help="Single live evaluation (cron mode)")
    ap.add_argument("--replay", nargs=2, metavar=("START", "END"), help="Backtest YYYY-MM-DD YYYY-MM-DD")
    ap.add_argument("--csv-dir", help="Replay from local CSVs instead of Kite")
    ap.add_argument("--log-file", help="JSONL log path")
    ap.add_argument("--no-log", action="store_true", help="Replay without writing the JSONL log")
    ap.add_argument("--state-file", default=STATE_FILE)
    args = ap.parse_args()
    symbols = [s.upper() for s in args.symbols]

    if args.replay:
        start, end = (datetime.date.fromisoformat(d) for d in args.replay)
        log = None if args.no_log else pathlib.Path(args.log_file) if args.log_file else REPLAY_LOG
        all_trades: list[dict] = []
        for sym in symbols:
            print(f"\nReplaying {sym} {start} → {end}")
            frames = load_frames_csv(pathlib.Path(args.csv_dir)) if args.csv_dir \
                else load_frames_kite(sym, start, end)
            all_trades += replay(sym, frames, start, end, log)
        print_summary(all_trades)
        if log:
            print(f"Log: {log}")
    elif args.once:
        run_once(symbols, args.state_file,
                 pathlib.Path(args.log_file) if args.log_file else DEFAULT_LOG)
    else:
        print("Use --once (live cron mode) or --replay START END (backtest).")
        sys.exit(1)
