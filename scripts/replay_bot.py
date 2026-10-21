"""
Replay of the LIVE chart_bot rules on Kite history — run before deploying a change.

At every 5-minute candle close (09:30–15:00) it scores the closed 5m candles with
the same analyze_df pipeline chart_bot uses, then applies the bot's rules:

  entry      score ≥ +5 → CE, ≤ −5 → PE (NIFTY / SENSEX), R:R ≥ MIN_RR,
             one open trade per index, no direction after MAX_LOSSES_PER_DIR losses
  stop       chart SL → option premium (delta 0.5), kept 20–25% below entry
  T1         chart target (≥ +15%) → book half, trail the rest 12% below the peak,
             never below entry +10%
  exits      GTT stop / trailing stop, reversal exit (opposite score ≥ 3),
             15:15 square-off

It answers two questions:
  1. Will the bot trade at all?  (signals, entries and the reason each STRONG
     signal was skipped, day by day — a day with 0 trades is flagged)
  2. Roughly how did those trades do?  (option ₹ via delta 0.5, no theta/IV,
     so treat P&L as directional only)

Variants run side by side on the same signals:
  live        the rules chart_bot runs now
  no_rr       without the R:R gate
  no_2loss    without the 2-losses-per-direction stop

Known gaps vs live: no GEX (no historical option chains); futures volume for
VWAP only where the current front-month future has history; fills at the next
5m open; option premium from delta, not real option prices.

Usage (on the droplet):
  /root/venv/bin/python scripts/replay_bot.py                 # NIFTY + SENSEX, last 20 days
  /root/venv/bin/python scripts/replay_bot.py SENSEX --days 40
  /root/venv/bin/python scripts/replay_bot.py --day 2026-10-21 -v   # one day, every decision
"""

import argparse
import datetime
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import pandas as pd

from screener.chart_analysis import _INDEX_TOKENS, analyze_df, front_future_token
from screener.kite_order import (LOT_SIZE, SL_MAX_PCT, SL_MIN_PCT, T1_MIN_PCT,
                                 _spot_to_option_levels, _tick)
from scripts.backtest_chart import _fetch
from scripts import chart_bot as cb

_ROOT = pathlib.Path(__file__).parent.parent
_IST  = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
_PREMIUM = {"NIFTY": 150.0, "SENSEX": 350.0}   # typical ATM premium for % levels
_DELTA = 0.5
_TRAIL_PCT, _TRAIL_FLOOR = 0.12, 0.10
_SQUARE_OFF = datetime.time(15, 15)
_SIG_OPEN, _SIG_CLOSE = cb.SIGNAL_OPEN, cb.SIGNAL_CLOSE

VARIANTS = {
    "live":     dict(rr=True,  max_losses=cb.MAX_LOSSES_PER_DIR),
    "no_rr":    dict(rr=False, max_losses=cb.MAX_LOSSES_PER_DIR),
    "no_2loss": dict(rr=True,  max_losses=99),
}


def _direction(verdict: str) -> str:
    return cb._verdict_direction(verdict)


def build_signals(symbol: str, df5: pd.DataFrame, days: list) -> pd.DataFrame:
    """The bot's view at every 5m close: score, R:R, chart SL/T1 on closed candles."""
    rows = []
    ends = df5.index + pd.Timedelta(minutes=5)          # candle close times
    for di, day in enumerate(days):
        steps = pd.date_range(f"{day} 09:20", f"{day} 15:25", freq="5min", tz="Asia/Kolkata")
        for t in steps:
            hist = df5[ends <= t].tail(750)               # same window as fetch_candles
            if len(hist) < 30 or hist.index[-1].date() != day:
                continue
            v = analyze_df(hist, "5m")["verdict"]
            rows.append({
                "t": t, "day": day, "symbol": symbol, "verdict": v["verdict"],
                "direction": _direction(v["verdict"]), "score": int(v["score"]),
                "rr": v.get("rr"), "sl": v.get("stop_loss"),
                "t1": (v.get("targets") or [None])[0], "spot": float(hist["Close"].iloc[-1]),
            })
        print(f"  {symbol}: scored {day} ({di + 1}/{len(days)})", end="\r", flush=True)
    print()
    return pd.DataFrame(rows)


def _premium(entry_prem: float, entry_spot: float, spot: float, d: str) -> float:
    move = (spot - entry_spot) if d == "CE" else (entry_spot - spot)
    return max(0.05, entry_prem + _DELTA * move)


def _levels(symbol: str, d: str, spot: float, chart_sl, chart_t1) -> tuple[float, float, float]:
    """Entry premium, option SL and T1 exactly as place_order_for_signal sets them."""
    p = _PREMIUM.get(symbol, 150.0)
    lv = _spot_to_option_levels(spot, p, chart_sl, chart_t1, d) if (chart_sl and chart_t1) else None
    sl, t1 = lv if lv else (_tick(p * 0.80), _tick(p * 1.25))
    sl = _tick(min(max(sl, p * (1 - SL_MAX_PCT)), p * (1 - SL_MIN_PCT)))
    t1 = _tick(max(t1, p * (1 + T1_MIN_PCT)))
    return p, sl, t1


def run_variant(name: str, v: dict, sig: pd.DataFrame, bars: dict, lots: int, verbose: bool) -> tuple:
    trades, skips = [], []
    for (symbol, day), g in sig.groupby(["symbol", "day"]):
        if symbol not in cb._TRADE_SYMBOLS:
            continue
        lot = LOT_SIZE.get(symbol, 1)
        b = bars[symbol]
        b = b[b.index.date == day]
        losses = {"CE": 0, "PE": 0}
        pos = None
        g = g.set_index("t")
        for t, bar in b.iterrows():
            close_t = t + pd.Timedelta(minutes=5)
            # ── manage the open position on this bar ──
            if pos:
                d = pos["d"]
                lo_spot, hi_spot = (bar["Low"], bar["High"]) if d == "CE" else (bar["High"], bar["Low"])
                p_open = _premium(pos["entry"], pos["spot"], bar["Open"], d)
                p_lo = _premium(pos["entry"], pos["spot"], lo_spot, d)
                p_hi = _premium(pos["entry"], pos["spot"], hi_spot, d)
                exit_px, why = None, None
                if t.time() >= _SQUARE_OFF:
                    exit_px, why = p_open, "15:15 square-off"
                elif pos["exit_next"]:
                    exit_px, why = p_open, pos["exit_next"]
                elif p_lo <= pos["sl"]:                    # stop first (conservative)
                    exit_px, why = min(pos["sl"], p_open), ("trailing stop" if pos["trail"] else "stop loss")
                else:
                    if not pos["trail"] and p_hi >= pos["t1"]:
                        half = (pos["qty"] // lot // 2) * lot if pos["qty"] // lot >= 2 else 0
                        pos["booked"] += half * (pos["t1"] - pos["entry"])
                        pos["qty"] -= half
                        pos["trail"], pos["peak"] = True, pos["t1"]
                        pos["sl"] = _tick(max(pos["entry"] * (1 + _TRAIL_FLOOR), pos["t1"] * (1 - _TRAIL_PCT)))
                    if pos["trail"]:
                        pos["peak"] = max(pos["peak"], p_hi)
                        pos["sl"] = max(pos["sl"], _tick(max(pos["entry"] * (1 + _TRAIL_FLOOR),
                                                             pos["peak"] * (1 - _TRAIL_PCT))))
                if exit_px is not None:
                    pnl = pos["booked"] + pos["qty"] * (exit_px - pos["entry"])
                    trades.append({"variant": name, "symbol": symbol, "day": day, "dir": d,
                                   "entry_t": pos["t"].strftime("%H:%M"), "exit_t": t.strftime("%H:%M"),
                                   "entry": round(pos["entry"], 1), "exit": round(exit_px, 1),
                                   "sl": pos["sl0"], "t1": pos["t1"], "exit_reason": why,
                                   "pnl": round(pnl), "win": pnl > 0})
                    if pnl < 0:
                        losses[d] += 1
                    if verbose:
                        print(f"    {t:%H:%M} {symbol} EXIT {d} ({why}) ₹{exit_px:.1f} → P&L ₹{pnl:,.0f}")
                    pos = None
            # ── the bot's decision at this candle's close ──
            if close_t not in g.index:
                continue
            s = g.loc[close_t]
            if pos:
                if s["direction"] not in ("WAIT", pos["d"]) and abs(s["score"]) >= cb.REVERSAL_EXIT_SCORE:
                    pos["exit_next"] = f"reversal ({s['verdict']} {s['score']:+d})"
                continue
            if not (_SIG_OPEN <= close_t.time() <= _SIG_CLOSE):
                continue
            if s["direction"] == "WAIT" or abs(s["score"]) < cb.MIN_TRADE_SCORE:
                continue
            reason = None
            if v["rr"] and s["rr"] is not None and s["rr"] < cb.MIN_RR:
                reason = f"R:R {s['rr']} < {cb.MIN_RR}"
            elif losses[s["direction"]] >= v["max_losses"]:
                reason = f"{losses[s['direction']]} {s['direction']} losses today"
            if reason:
                skips.append({"variant": name, "symbol": symbol, "day": day, "t": close_t.strftime("%H:%M"),
                              "dir": s["direction"], "score": s["score"], "reason": reason})
                if verbose:
                    print(f"    {close_t:%H:%M} {symbol} {s['verdict']} {s['score']:+d} — skipped: {reason}")
                continue
            entry, sl, t1 = _levels(symbol, s["direction"], s["spot"], s["sl"], s["t1"])
            pos = {"d": s["direction"], "t": close_t, "spot": s["spot"], "entry": entry, "sl": sl, "sl0": sl,
                   "t1": t1, "qty": lots * lot, "trail": False, "peak": entry, "booked": 0.0,
                   "exit_next": None}
            if verbose:
                print(f"    {close_t:%H:%M} {symbol} ENTER {s['direction']} ({s['verdict']} {s['score']:+d}, "
                      f"R:R {s['rr']}) premium ₹{entry:.0f}  SL ₹{sl}  T1 ₹{t1}")
    return pd.DataFrame(trades), pd.DataFrame(skips)


def _with_future_volume(symbol: str, df5: pd.DataFrame, start, end, api_key, access_token) -> tuple:
    tok = front_future_token(symbol, api_key, access_token)
    if not tok:
        return df5, 0.0
    try:
        fut = _fetch(tok, "5minute", start, end, api_key, access_token)
    except Exception:
        return df5, 0.0
    vol = fut["Volume"].reindex(df5.index).fillna(0) if not fut.empty else None
    if vol is None or vol.sum() <= 0:
        return df5, 0.0
    df5 = df5.copy()
    df5["Volume"] = vol.values
    return df5, float((vol > 0).mean())


def main() -> None:
    ap = argparse.ArgumentParser(description="Replay the live chart_bot rules on Kite history")
    ap.add_argument("symbols", nargs="*", default=["NIFTY", "SENSEX"])
    ap.add_argument("--days", type=int, default=20, help="Trading days to replay (default 20)")
    ap.add_argument("--day", help="Replay only this date (YYYY-MM-DD)")
    ap.add_argument("--lots", type=int, default=2, help="Lots per trade (default 2, so T1 books half)")
    ap.add_argument("-v", "--verbose", action="store_true", help="Print every entry / skip / exit (live rules)")
    args = ap.parse_args()

    api_key, access_token = cb._kite_creds()
    if not (api_key and access_token):
        sys.exit("KITE_API_KEY / KITE_ACCESS_TOKEN missing")
    today = datetime.datetime.now(_IST).date()
    end = datetime.date.fromisoformat(args.day) if args.day else today
    start = end - datetime.timedelta(days=int(args.days * 1.5) + 30)

    sigs, bars = [], {}
    for sym in [s.upper() for s in args.symbols]:
        tok = _INDEX_TOKENS.get(sym)
        if not tok:
            print(f"{sym}: only NIFTY / BANKNIFTY / SENSEX are supported")
            continue
        print(f"Fetching {sym} 5m history…")
        df5 = _fetch(tok, "5minute", start, end, api_key, access_token)
        if df5.empty:
            print(f"{sym}: no data")
            continue
        df5, cov = _with_future_volume(sym, df5, start, end, api_key, access_token)
        print(f"  futures volume on {cov:.0%} of candles (rest: price-only VWAP)")
        all_days = sorted(set(df5.index.date))
        days = [end] if args.day else [d for d in all_days if d < today or
                                       datetime.datetime.now(_IST).time() > datetime.time(15, 30)][-args.days:]
        bars[sym] = df5
        sigs.append(build_signals(sym, df5, days))
    if not sigs:
        return
    sig = pd.concat(sigs, ignore_index=True)

    summary, all_trades, all_skips = {}, [], []
    for name, v in VARIANTS.items():
        if args.verbose and name == "live":
            print("\nLive rules, decision by decision:")
        tr, sk = run_variant(name, v, sig, bars, args.lots, args.verbose and name == "live")
        all_trades.append(tr)
        all_skips.append(sk)
        n = len(tr)
        summary[name] = {
            "trades": n,
            "win %": f"{tr['win'].mean() * 100:.0f}%" if n else "—",
            "est P&L ₹": f"{tr['pnl'].sum():,.0f}" if n else "0",
            "worst ₹": f"{tr['pnl'].min():,.0f}" if n else "—",
            "STRONG skipped": len(sk),
        }

    strong = sig[(sig["direction"] != "WAIT") & (sig["score"].abs() >= cb.MIN_TRADE_SCORE)
                 & sig["symbol"].isin(cb._TRADE_SYMBOLS)]
    live_tr, live_sk = all_trades[0], all_skips[0]

    print(f"\n{'=' * 72}\nDay by day — live rules ({args.lots} lots/trade)\n{'=' * 72}")
    rows = []
    for (sym, day), g in sig[sig["symbol"].isin(cb._TRADE_SYMBOLS)].groupby(["symbol", "day"]):
        t_ = live_tr[(live_tr["symbol"] == sym) & (live_tr["day"] == day)] if not live_tr.empty else live_tr
        s_ = live_sk[(live_sk["symbol"] == sym) & (live_sk["day"] == day)] if not live_sk.empty else live_sk
        st_ = strong[(strong["symbol"] == sym) & (strong["day"] == day)]
        rows.append({"day": str(day), "symbol": sym,
                     "max |score|": int(g["score"].abs().max()),
                     "STRONG scans": len(st_), "trades": len(t_),
                     "est P&L ₹": int(t_["pnl"].sum()) if len(t_) else 0,
                     "skipped (why)": "; ".join(f"{k} ×{c}" for k, c in s_["reason"].str.replace(r"[\d.]+ <", "<", regex=True)
                                                .value_counts().items()) if len(s_) else "",
                     "flag": "NO TRADE" if not len(t_) else ""})
    print(pd.DataFrame(rows).to_string(index=False))

    print(f"\n{'=' * 72}\nVariants (same signals)\n{'=' * 72}")
    print(pd.DataFrame(summary).T.to_string())
    if not live_tr.empty:
        print("\nLive-rule exits:", ", ".join(f"{k} {c}" for k, c in live_tr["exit_reason"]
                                              .str.replace(r" \(.*", "", regex=True).value_counts().items()))
    no_trade = sum(1 for r in rows if r["flag"])
    print(f"\n{no_trade} of {len(rows)} symbol-days had no trade under the live rules.")

    out = _ROOT / "data"
    out.mkdir(exist_ok=True)
    pd.concat(all_trades, ignore_index=True).to_csv(out / "replay_trades.csv", index=False)
    pd.concat(all_skips, ignore_index=True).to_csv(out / "replay_skips.csv", index=False)
    sig.to_csv(out / "replay_signals.csv", index=False)
    print(f"Details → {out}/replay_trades.csv, replay_skips.csv, replay_signals.csv")
    print("P&L uses delta 0.5 on a typical ATM premium and ignores theta/IV — compare variants, "
          "don't read it as exact rupees.")


if __name__ == "__main__":
    main()
