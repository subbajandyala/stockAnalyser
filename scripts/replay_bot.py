"""
Replay of the LIVE chart_bot rules on Kite history — run before deploying a change.

Two engines, side by side on the same days (--engine both, the default):

  setups   (live default) regime + setups on closed 3m candles (screener/setups.py);
           enter on a fresh setup, either side; whole position booked at +20%;
           SL = setup stop → premium (5–15%) minus 15 pts NIFTY / 30 pts SENSEX;
           opposite setup exits and flips; 15:15 square-off
  score    the old ±5 score on 5m candles (details below)

The score engine, as before:

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

Exit tests (replay only — the live bot does not have these):
  --mid-stop        exit when a 3m candle CLOSES back past the middle of the entry
                    candle (below it for CE, above it for PE)
  --be-lock 6,18    once the premium is up 6 pts (NIFTY) / 18 pts (SENSEX), move
                    the stop to entry +1 (break-even)
  --compare-exits   run live, mid-stop, BE-lock and both side by side
"""

import argparse
import datetime
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import pandas as pd

from screener.chart_analysis import _INDEX_TOKENS, analyze_df, front_future_token
from screener import candles as _candles
from screener import setups as _setups
from screener.kite_order import (BOOK_PROFIT_PCT, LOT_SIZE, SL_MAX_PCT, SL_MIN_PCT, T1_MIN_PCT,
                                 _spot_to_option_levels, _tick, book_levels)
from scripts import backtest_chart as _bt
from scripts.backtest_chart import _fetch
from scripts import chart_bot as cb

_bt._MAX_DAYS_PER_CALL.setdefault("3minute", 90)

_ROOT = pathlib.Path(__file__).parent.parent
_IST  = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
_PREMIUM = {"NIFTY": 150.0, "SENSEX": 350.0}   # typical ATM premium for % levels
_DELTA = 0.5
_TRAIL_PCT, _TRAIL_FLOOR = 0.12, 0.10
_SQUARE_OFF = cb.SQUARE_OFF_TIME
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


# ── Setups engine ────────────────────────────────────────────────────────────

def build_setup_signals(symbol: str, df3: pd.DataFrame, days: list) -> pd.DataFrame:
    """The setups engine's read at every 3m close (same detect() the bot calls)."""
    rows = []
    ends = df3.index + pd.Timedelta(minutes=3)
    for di, day in enumerate(days):
        steps = pd.date_range(f"{day} 09:21", f"{day} 15:27", freq="3min", tz="Asia/Kolkata")
        for t in steps:
            hist = df3[ends <= t].tail(600)
            if len(hist) < 30 or hist.index[-1].date() != day:
                continue
            r = _setups.detect(hist)
            rows.append({"t": t, "day": day, "symbol": symbol, "signal": r["signal"], "setup": r["setup"],
                         "regime": r["regime"], "stop": r["stop"], "spot": r["spot"], "vwap": r["vwap"]})
        print(f"  {symbol}: setups {day} ({di + 1}/{len(days)})", end="\r", flush=True)
    print()
    return pd.DataFrame(rows)


def run_setups(sig: pd.DataFrame, bars: dict, lots: int, max_losses: int, verbose: bool) -> pd.DataFrame:
    """Bar-by-bar on 3m candles, same rules as chart_bot's setups engine.

    The setup in force stays tradeable after a +20% booking while it is still
    valid (setups.still_valid); a stop-out ends it. SL is checked before the
    target inside a bar (conservative).
    """
    trades = []
    for (symbol, day), g in sig.groupby(["symbol", "day"]):
        if symbol not in cb._TRADE_SYMBOLS:
            continue
        lot = LOT_SIZE.get(symbol, 1)
        b = bars[symbol]
        b = b[b.index.date == day]
        g = g.set_index("t")
        losses = {"CE": 0, "PE": 0}
        pos, pending, active, failed = None, None, None, None

        def _closed(tr):
            nonlocal active, failed
            trades.append(tr)
            if tr["pnl"] < 0:
                losses[tr["dir"]] += 1
                if active and active["side"] == tr["dir"]:
                    failed, active = active["trig"], None
            if verbose:
                print(f"    {tr['exit_t']} {symbol} EXIT {tr['dir']} ({tr['exit_reason']}) → ₹{tr['pnl']:,.0f}")

        for t, bar in b.iterrows():
            close_t = t + pd.Timedelta(minutes=3)
            if pos and (t.time() >= _SQUARE_OFF or pos.get("exit_next")):
                why = "15:15 square-off" if t.time() >= _SQUARE_OFF else pos["exit_next"]
                px = _premium(pos["entry"], pos["spot"], bar["Open"], pos["d"])
                p_, pos = pos, None
                _closed(_close(p_, px, t, why, symbol, day, lot))
            if pending is not None and pos is None and t.time() < _SQUARE_OFF:
                a = pending
                entry = _PREMIUM.get(symbol, 150.0)
                sl, tgt = book_levels(symbol, a["side"], a["spot"], entry, a["stop"], BOOK_PROFIT_PCT)
                pos = {"d": a["side"], "t": t, "spot": bar["Open"], "entry": entry, "sl": sl, "tgt": tgt,
                       "qty": lots * lot, "setup": a["setup"], "exit_next": None}
                if verbose:
                    print(f"    {t:%H:%M} {symbol} ENTER {a['side']} ({a['setup']}{', re-entry' if a['re'] else ''}) "
                          f"₹{entry:.0f}  SL ₹{sl}  book ₹{tgt}")
            pending = None
            if pos:
                d = pos["d"]
                adverse, favour = (bar["Low"], bar["High"]) if d == "CE" else (bar["High"], bar["Low"])
                p_open = _premium(pos["entry"], pos["spot"], bar["Open"], d)
                px = why = None
                if _premium(pos["entry"], pos["spot"], adverse, d) <= pos["sl"]:
                    px, why = min(pos["sl"], p_open), "stop loss"
                elif _premium(pos["entry"], pos["spot"], favour, d) >= pos["tgt"]:
                    px, why = max(pos["tgt"], p_open), "booked +20%"
                if px is not None:
                    p_, pos = pos, None
                    _closed(_close(p_, px, t, why, symbol, day, lot))
            if close_t not in g.index:
                continue
            s = g.loc[close_t]
            fresh = isinstance(s["signal"], str)
            if fresh:
                trig = (s["signal"], close_t)
                if trig != failed and (not active or active["trig"] != trig):
                    active = {"side": s["signal"], "setup": s["setup"], "stop": s["stop"], "trig": trig}
            elif active and not _setups.still_valid(active, {"spot": s["spot"], "vwap": s["vwap"]}):
                active = None
            if pos:
                if fresh and s["signal"] != pos["d"]:
                    pos["exit_next"] = f"reversal ({s['setup']} {s['signal']})"
                    if active and active["side"] == s["signal"] and _SIG_OPEN <= close_t.time() <= _SIG_CLOSE:
                        pending = {**active, "spot": s["spot"], "re": False}
                continue
            if not active or not (_SIG_OPEN <= close_t.time() <= _SIG_CLOSE):
                continue
            if max_losses and losses[active["side"]] >= max_losses:
                continue
            pending = {**active, "spot": s["spot"], "re": not fresh or active["trig"] != (s["signal"], close_t)}
    return pd.DataFrame(trades)


# ── Candles engine ───────────────────────────────────────────────────────────

def build_candle_signals(symbol: str, df3: pd.DataFrame, days: list) -> pd.DataFrame:
    """candles.read() on each CLOSED 3m candle (history has no forming candles, so
    live entries come earlier, mid-candle, than this replay assumes)."""
    rows = []
    for day in days:
        hist_all = df3[df3.index.date <= day]
        for i in range(len(hist_all)):
            t = hist_all.index[i]
            if t.date() != day:
                continue
            r = _candles.read(hist_all.iloc[max(0, i - 39): i + 1])
            rows.append({"t": t + pd.Timedelta(minutes=3), "day": day, "symbol": symbol, "signal": r["signal"],
                         "setup": r["pattern"], "stop": r["stop"], "spot": r["spot"],
                         "range_mult": r.get("range_mult", 0.0),
                         "mid": (hist_all["High"].iloc[i] + hist_all["Low"].iloc[i]) / 2})
    return pd.DataFrame(rows)


def run_candles(sig: pd.DataFrame, bars: dict, lots: int, book_pct: float, max_min: int,
                verbose: bool, ride_mult: float = 0.0, mid_stop: bool = False,
                be_lock: dict | None = None) -> pd.DataFrame:
    """ride_mult > 0: strong candles ≥ ride_mult × avg range don't book at +book_pct —
    the stop moves to entry +RIDE_LOCK_PCT there (+10% at the default +20% booking)
    and trails 12% below the peak premium.

    Exit tests (replay only): mid_stop exits at the close of a 3m candle that closes
    back past the middle of the entry candle; be_lock {symbol: pts} moves the stop
    to entry +1 once the premium has been up that many points."""
    trades = []
    for (symbol, day), g in sig.groupby(["symbol", "day"]):
        if symbol not in cb._TRADE_SYMBOLS:
            continue
        lot = LOT_SIZE.get(symbol, 1)
        b = bars[symbol]
        b = b[b.index.date == day]
        g = g.set_index("t")
        pos, pending, lost_candle = None, None, None

        def _done(p_, px, t, why):
            nonlocal lost_candle
            tr = _close(p_, px, t, why, symbol, day, lot)
            tr["engine"] = "candles"
            trades.append(tr)
            if tr["pnl"] < 0:
                lost_candle = p_["candle"]
            if verbose:
                print(f"    {t:%H:%M} {symbol} EXIT {p_['d']} ({why}) → ₹{tr['pnl']:,.0f}")

        for t, bar in b.iterrows():
            close_t = t + pd.Timedelta(minutes=3)
            if pos:
                p_open = _premium(pos["entry"], pos["spot"], bar["Open"], pos["d"])
                held = (t - pos["t"]).total_seconds() / 60
                why = ("15:15 square-off" if t.time() >= _SQUARE_OFF else pos["exit_next"]
                       or ("not working" if held >= max_min and p_open < pos["entry"] * 1.02
                           and not pos["trail"] else None))
                if why:
                    p_, pos = pos, None
                    _done(p_, p_open, t, why)
            if pending is not None and pos is None and t.time() < _SQUARE_OFF:
                a = pending
                entry = _PREMIUM.get(symbol, 150.0)
                pts = cb.CANDLE_BOOK_PTS.get(symbol)
                sl, tgt = book_levels(symbol, a["signal"], a["spot"], entry, a["stop"], book_pct, pts,
                                      cb.CANDLE_SL_PCT)
                ride = bool(ride_mult and a["setup"] in ("strong_bull", "strong_bear")
                            and a.get("range_mult", 0) >= ride_mult)
                pos = {"d": a["signal"], "t": t, "spot": bar["Open"], "entry": entry, "sl": sl, "tgt": tgt,
                       "qty": lots * lot, "setup": a["setup"] + (" (ride)" if ride else ""), "exit_next": None,
                       "candle": a["candle"], "ride": ride, "trail": False, "peak": entry,
                       "lock": pts / 2 / entry if pts else cb.RIDE_LOCK_PCT, "mid": a.get("mid"),
                       "be": False}
                if verbose:
                    print(f"    {t:%H:%M} {symbol} ENTER {a['signal']} ({pos['setup']}) ₹{entry:.0f}  SL ₹{sl}  "
                          + (f"ride from ₹{tgt}" if ride else f"book ₹{tgt}"))
            pending = None
            if pos:
                d = pos["d"]
                adverse, favour = (bar["Low"], bar["High"]) if d == "CE" else (bar["High"], bar["Low"])
                p_open = _premium(pos["entry"], pos["spot"], bar["Open"], d)
                p_fav = _premium(pos["entry"], pos["spot"], favour, d)
                if _premium(pos["entry"], pos["spot"], adverse, d) <= pos["sl"]:
                    p_, pos = pos, None
                    _done(p_, min(p_["sl"], p_open), t, "trailing stop" if p_["trail"] else
                          "break-even stop" if p_["be"] else "stop loss")
                elif (mid_stop and pd.notna(pos["mid"]) and not pos["trail"]
                      and (bar["Close"] < pos["mid"] if d == "CE" else bar["Close"] > pos["mid"])
                      and not p_fav >= pos["tgt"]):
                    p_, pos = pos, None
                    _done(p_, _premium(p_["entry"], p_["spot"], bar["Close"], d), close_t, "candle-mid stop")
                elif pos["ride"]:
                    if pos["trail"] or p_fav >= pos["tgt"]:     # +book_pct reached → lock, trail 12%
                        pos["trail"] = True
                        pos["peak"] = max(pos["peak"], p_fav)
                        pos["sl"] = max(pos["sl"], _tick(max(pos["entry"] * (1 + pos["lock"]),
                                                             pos["peak"] * 0.88)))
                elif p_fav >= pos["tgt"]:
                    p_, pos = pos, None
                    _done(p_, max(p_["tgt"], p_open), t, "booked target")
                if pos and be_lock and not pos["be"] and p_fav - pos["entry"] >= be_lock.get(symbol, 1e9):
                    pos["be"] = True                         # takes effect from the next candle
                    pos["sl"] = max(pos["sl"], _tick(pos["entry"] + 1))
            if close_t not in g.index:
                continue
            s = g.loc[close_t]
            if not isinstance(s["signal"], str):
                continue
            cand = (s["signal"], close_t)
            if pos:
                if s["signal"] != pos["d"]:
                    pos["exit_next"] = f"opposite candle ({s['setup']})"
                    pending = {**s.to_dict(), "candle": cand}
                continue
            if cand == lost_candle or not (_SIG_OPEN <= close_t.time() <= _SIG_CLOSE):
                continue
            pending = {**s.to_dict(), "candle": cand}
    return pd.DataFrame(trades)


def _close(pos: dict, px: float, t, why: str, symbol: str, day, lot: int) -> dict:
    pnl = pos["qty"] * (px - pos["entry"])
    return {"engine": "setups", "symbol": symbol, "day": day, "dir": pos["d"], "setup": pos["setup"],
            "entry_t": pos["t"].strftime("%H:%M"), "exit_t": t.strftime("%H:%M"),
            "entry": round(pos["entry"], 1), "exit": round(px, 1), "sl": pos["sl"], "target": pos["tgt"],
            "exit_reason": why, "pnl": round(pnl), "win": pnl > 0}


def _engine_summary(tr: pd.DataFrame, all_days: list, cost: float) -> dict:
    n = len(tr)
    if not n:
        return {"trades": 0, "trades/day": 0, "days w/o trade": len(all_days), "win %": "—",
                "avg win ₹": "—", "avg loss ₹": "—", "P&L after costs ₹": 0, "worst day ₹": "—"}
    net = tr["pnl"] - cost
    by_day = net.groupby([tr["symbol"], tr["day"]]).sum()
    traded = set(zip(tr["symbol"], tr["day"]))
    return {"trades": n, "trades/day": round(n / max(1, len(all_days)), 1),
            "days w/o trade": sum(1 for k in all_days if k not in traded),
            "win %": f"{(net > 0).mean() * 100:.0f}%",
            "avg win ₹": f"{net[net > 0].mean():,.0f}" if (net > 0).any() else "—",
            "avg loss ₹": f"{net[net <= 0].mean():,.0f}" if (net <= 0).any() else "—",
            "P&L after costs ₹": f"{net.sum():,.0f}", "worst day ₹": f"{by_day.min():,.0f}"}


def _with_future_volume(symbol: str, df5: pd.DataFrame, start, end, api_key, access_token,
                        interval: str = "5minute") -> tuple:
    tok = front_future_token(symbol, api_key, access_token)
    if not tok:
        return df5, 0.0
    try:
        fut = _fetch(tok, interval, start, end, api_key, access_token)
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
    ap.add_argument("--engine", choices=["all", "both", "candles", "setups", "score"], default="all",
                    help="all = candles + setups + score; both = setups + score")
    ap.add_argument("--lots", type=int, default=2, help="Lots per trade (default 2)")
    ap.add_argument("--cost", type=float, default=100.0,
                    help="₹ brokerage + taxes per round trip, deducted per trade (default 100)")
    ap.add_argument("--max-losses", type=int, default=cb.SETUP_MAX_LOSSES,
                    help="Setups: losses per direction before it stops for the day (0 = off)")
    ap.add_argument("--ride-mult", type=float, default=cb.RIDE_MULT,
                    help="Candles: strong candles ≥ this × avg range ride instead of booking (0 = off)")
    ap.add_argument("--mid-stop", action="store_true",
                    help="Candles test: exit when a 3m candle closes back past the entry candle's middle")
    ap.add_argument("--be-lock", metavar="NIFTY,SENSEX",
                    help="Candles test: stop to entry +1 once the premium is up these pts, e.g. 6,18")
    ap.add_argument("--compare-exits", action="store_true",
                    help="Candles: run live, mid-stop, BE-lock and both side by side (BE-lock 6,18 if not given)")
    ap.add_argument("-v", "--verbose", action="store_true", help="Print every entry / exit")
    args = ap.parse_args()
    be = None
    if args.be_lock or args.compare_exits:
        n_, s_ = (float(x) for x in (args.be_lock or "6,18").split(","))
        be = {"NIFTY": n_, "BANKNIFTY": n_, "SENSEX": s_}

    api_key, access_token = cb._kite_creds()
    if not (api_key and access_token):
        sys.exit("KITE_API_KEY / KITE_ACCESS_TOKEN missing")
    now = datetime.datetime.now(_IST)
    today = now.date()
    end = datetime.date.fromisoformat(args.day) if args.day else today
    start = end - datetime.timedelta(days=int(args.days * 1.5) + 20)
    want_candles = args.engine in ("all", "candles")
    want_setups = args.engine in ("all", "both", "setups")
    want_score = args.engine in ("all", "both", "score")

    def _days(df):
        all_d = sorted(set(df.index.date))
        if args.day:
            return [end]
        return [d for d in all_d if d < today or now.time() > datetime.time(15, 30)][-args.days:]

    sig3, sigc, sig5, bars3, bars5, sym_days = [], [], [], {}, {}, []
    for sym in [s.upper() for s in args.symbols]:
        tok = _INDEX_TOKENS.get(sym)
        if not tok:
            print(f"{sym}: only NIFTY / BANKNIFTY / SENSEX are supported")
            continue
        if want_setups or want_candles:
            # CHART_CANDLE_SOURCE=future: the current contract's own 3m history (it was the
            # 2nd month before the last expiry), as the live bot reads it
            fut = front_future_token(sym, api_key, access_token) if cb._candles.SOURCE == "future" else None
            if cb._candles.SOURCE == "option":
                print("  CHART_CANDLE_SOURCE=option: Kite keeps no history for expired options — replaying the index chart")
            print(f"Fetching {sym}{' FUT (front month)' if fut else ''} 3m history…")
            df3 = _fetch(fut or tok, "3minute", start, end, api_key, access_token)
            if df3.empty:
                print(f"{sym}: no 3m data")
            else:
                if fut:
                    cov = float((df3["Volume"] > 0).mean())
                    print(f"  futures chart from {df3.index[0]:%d %b}")
                else:
                    df3, cov = _with_future_volume(sym, df3, start, end, api_key, access_token, "3minute")
                    print(f"  futures volume on {cov:.0%} of 3m candles")
                days = _days(df3)
                sym_days += [(sym, d) for d in days if sym in cb._TRADE_SYMBOLS]
                bars3[sym] = df3
                if want_setups:
                    sig3.append(build_setup_signals(sym, df3, days))
                if want_candles:
                    sigc.append(build_candle_signals(sym, df3, days))
        if want_score:
            print(f"Fetching {sym} 5m history…")
            df5 = _fetch(tok, "5minute", start, end, api_key, access_token)
            if df5.empty:
                print(f"{sym}: no 5m data")
                continue
            df5, cov = _with_future_volume(sym, df5, start, end, api_key, access_token)
            print(f"  futures volume on {cov:.0%} of 5m candles")
            days = _days(df5)
            if not (want_setups or want_candles):
                sym_days += [(sym, d) for d in days if sym in cb._TRADE_SYMBOLS]
            bars5[sym] = df5
            sig5.append(build_signals(sym, df5, days))

    out = _ROOT / "data"
    out.mkdir(exist_ok=True)
    engines, trades_by = {}, {}
    if sigc:
        if args.verbose:
            print("\nCandles engine, decision by decision:")
        allc = pd.concat(sigc, ignore_index=True)
        tr = run_candles(allc, bars3, args.lots, cb.CANDLE_BOOK_PCT, cb.SCALP_MAX_MIN, args.verbose,
                         ride_mult=args.ride_mult)
        trades_by["candles (live)"] = tr
        engines["candles (live)"] = _engine_summary(tr, sym_days, args.cost)
        tr.to_csv(out / "replay_candles_trades.csv", index=False)
        if args.ride_mult and not (args.mid_stop or args.be_lock or args.compare_exits):
            tr2 = run_candles(allc, bars3, args.lots, cb.CANDLE_BOOK_PCT, cb.SCALP_MAX_MIN, False, ride_mult=0)
            trades_by["candles, no ride"] = tr2
            engines["candles, no ride"] = _engine_summary(tr2, sym_days, args.cost)
        tests = ([("mid-stop", True, None), ("BE-lock", False, be), ("mid+BE", True, be)] if args.compare_exits
                 else [("mid+BE" if args.mid_stop and be else "mid-stop" if args.mid_stop else "BE-lock",
                        args.mid_stop, be)] if (args.mid_stop or be) else [])
        for name, ms, bl in tests:
            trt = run_candles(allc, bars3, args.lots, cb.CANDLE_BOOK_PCT, cb.SCALP_MAX_MIN, False,
                              ride_mult=args.ride_mult, mid_stop=ms, be_lock=bl)
            trades_by[name] = trt
            engines[name] = _engine_summary(trt, sym_days, args.cost)
            trt.to_csv(out / f"replay_candles_{name.replace('+', '_')}_trades.csv", index=False)
    if sig3:
        if args.verbose and not sigc:
            print("\nSetups engine, decision by decision:")
        tr = run_setups(pd.concat(sig3, ignore_index=True), bars3, args.lots, args.max_losses,
                        args.verbose and not sigc)
        trades_by["setups"] = tr
        engines["setups"] = _engine_summary(tr, sym_days, args.cost)
        tr.to_csv(out / "replay_setups_trades.csv", index=False)
    if sig5:
        sig = pd.concat(sig5, ignore_index=True)
        tr, sk = run_variant("live", VARIANTS["live"], sig, bars5, args.lots, args.verbose and not (sig3 or sigc))
        trades_by["score (old)"] = tr
        engines["score (old)"] = _engine_summary(tr, sym_days, args.cost)
        tr.to_csv(out / "replay_score_trades.csv", index=False)

    if not engines:
        return
    print(f"\n{'=' * 78}\nDay by day ({args.lots} lots/trade, ₹{args.cost:.0f} costs per trade)\n{'=' * 78}")
    rows = []
    for sym, day in sym_days:
        row = {"day": str(day), "symbol": sym}
        for name, tr in trades_by.items():
            t_ = tr[(tr["symbol"] == sym) & (tr["day"] == day)] if not tr.empty else tr
            short = name.split()[0]
            row[f"{short} trades"] = len(t_)
            row[f"{short} ₹"] = int((t_["pnl"] - args.cost).sum()) if len(t_) else 0
        rows.append(row)
    print(pd.DataFrame(rows).to_string(index=False))

    print(f"\n{'=' * 78}\nEngines compared\n{'=' * 78}")
    print(pd.DataFrame(engines).T.to_string())
    for name in ("candles (live)", "setups", "mid-stop", "BE-lock", "mid+BE"):
        if name not in trades_by or trades_by[name].empty:
            continue
        st = trades_by[name]
        if name in ("candles (live)", "setups"):
            print(f"\n{name} by pattern / setup:")
            print(st.groupby("setup").agg(trades=("pnl", "size"), win_pct=("win", "mean"),
                                          pnl=("pnl", "sum")).assign(win_pct=lambda x: (x.win_pct * 100).round(0))
                  .to_string())
        else:
            print(f"\n{name}:")
        print("Exits:", ", ".join(f"{k} {c}" for k, c in st["exit_reason"].str.replace(r" \(.*", "", regex=True)
                                     .value_counts().items()))
    print(f"\nTrades → {out}/replay_candles_trades.csv, replay_setups_trades.csv, replay_score_trades.csv")
    print("P&L uses delta 0.5 on a typical ATM premium (NIFTY ₹150, SENSEX ₹350), no theta/IV — "
          "compare engines, don't read it as exact rupees.")


if __name__ == "__main__":
    main()
