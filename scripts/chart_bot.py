"""
Chart Analysis Signal Bot
=========================
Every minute, on closed candles, for NIFTY / SENSEX (BANKNIFTY alerts only).

Engine "setups" (default, CHART_ENGINE in .env):
  screener/setups.py reads today's 3-minute chart — regime (trend / range) and four
  setups: trend pullback, opening-range breakout, VWAP reclaim / reject, range fade.
  A fresh setup on a flat index buys the ATM option (CE or PE, either side):
    qty     95% of the Kite balance (min 1 lot, so a rejection is visible)
    SL      setup stop → premium (5–15% below entry), then −15 pts NIFTY / −30 SENSEX
    target  +20% — the GTT books the WHOLE position; the next fresh setup re-enters
    exits   GTT target / SL, opposite setup (exit, then flip), 15:15 square-off

Engine "score" (CHART_ENGINE=score): the old ±5 score on 5m, R:R ≥ 1.5,
half booked at T1 and the rest trailed.

Set KITE_AUTO_TRADE=1 to enable auto order execution.

Run (start_droplet.sh):
  /root/venv/bin/python -u scripts/chart_bot.py --loop >> chart_bot.log 2>&1

Checks / tests:
  /root/venv/bin/python scripts/chart_bot.py --preflight          # no orders
  /root/venv/bin/python -u scripts/chart_bot.py --test-notify
  /root/venv/bin/python scripts/replay_bot.py --days 20           # both engines on history
"""

import argparse
import datetime
import json
import logging
import os
import pathlib
import re
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import requests

from kiteconnect import KiteConnect

from screener.chart_analysis import analyze_chart
from screener import setups as _setups
from screener.kite_order import (
    BOOK_PROFIT_PCT,
    LOT_SIZE,
    contract_pnl,
    delete_gtts_for,
    has_open_position,
    modify_oco_gtt,
    open_qty,
    place_oco_gtt,
    place_order_for_signal as _place_order,
    sell_and_confirm,
)
from screener.kite_order import SL_EXTRA_PTS as _SL_EXTRA
from screener.sharekhan_order import place_sk_order as _place_sk_order

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
_LOG = logging.getLogger("chart_bot")

_IST         = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
MARKET_OPEN  = datetime.time(9, 20)   # give 5 min after open to avoid noise
MARKET_CLOSE = datetime.time(15, 25)  # stop 5 min before close

SYMBOLS    = ["NIFTY", "BANKNIFTY", "SENSEX"]
_TRADE_SYMBOLS = {"NIFTY", "SENSEX"}   # BANKNIFTY excluded from auto-trade

def _env_setting(name: str) -> str:
    """Value from the environment, else from .env; "" if unset."""
    v = os.environ.get(name, "")
    if v:
        return v.strip()
    env_path = pathlib.Path(__file__).parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, val = line.split("=", 1)
                if k.strip() == name:
                    return val.strip().strip('"').strip("'")
    return ""


# Signal timeframe (orders come from this chart) and optional confirmation chart.
# Override in .env, e.g. CHART_PRIMARY_TF=15m and CHART_CONFIRM_TF=5m for the 15 Oct setup.
PRIMARY_TF = _env_setting("CHART_PRIMARY_TF") or "5m"
CONFIRM_TF = _env_setting("CHART_CONFIRM_TF") or "none"

# Order engine. "setups" (default): regime + setups on 3m candles, book 100% at
# +20%, re-enter on the next fresh setup either side. "score": the old ±5 score
# with half-booking + trailing. Switch back with CHART_ENGINE=score in .env.
ENGINE   = (_env_setting("CHART_ENGINE") or "setups").lower()
SETUP_TF = _env_setting("CHART_SETUP_TF") or "3m"
# Setups engine: losing trades in one direction before that direction stops for
# the day — 0 = never stop (grab every setup). Set CHART_MAX_LOSSES=2 to cap.
SETUP_MAX_LOSSES = int(_env_setting("CHART_MAX_LOSSES") or 0)

# Minimum score to send a notification (BUY=3, STRONG BUY=5)
MIN_NOTIFY_SCORE = 3
# Minimum score to auto-execute an order
MIN_TRADE_SCORE  = 5
# Minimum R:R ratio on the underlying to fire a signal
MIN_RR = 1.5
# Clean signal window: no new signals outside 9:30–15:00 IST
SIGNAL_OPEN  = datetime.time(9, 30)
SIGNAL_CLOSE = datetime.time(15, 0)
# Signals are scored on completed candles (no flicker), so one scan is enough.
# Raise this to require the signal on N consecutive 1-minute scans.
CONFIRM_SCANS = 1
# After this many losing trades in one direction on one index, stop that
# direction for the day (the other side keeps trading)
MAX_LOSSES_PER_DIR = 2
# Exit an open trade when the signal chart turns this strongly against it
REVERSAL_EXIT_SCORE = 3


def _loss_limit() -> int:
    """Losses per direction per index before that direction stops for the day (0 = no limit)."""
    return SETUP_MAX_LOSSES if ENGINE == "setups" else MAX_LOSSES_PER_DIR


def _minutes_between(hhmm: str, now_ist: datetime.datetime) -> float:
    """Minutes from today's HH:MM to now; large if unparseable."""
    try:
        hh, mm = hhmm.split(":")
        t = now_ist.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        return (now_ist - t).total_seconds() / 60
    except Exception:
        return 1e9


def _auto_trade_enabled() -> bool:
    """Check KITE_AUTO_TRADE in env vars first, then .env file."""
    for key in ("CHART_AUTO_TRADE", "KITE_AUTO_TRADE"):
        if os.environ.get(key) == "1":
            return True
    env_path = pathlib.Path(__file__).parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                if k.strip() in ("KITE_AUTO_TRADE", "CHART_AUTO_TRADE"):
                    if v.strip().strip('"').strip("'") == "1":
                        return True
    return False

_AUTO_TRADE = _auto_trade_enabled()
print(f"[chart_bot] AUTO_TRADE={'ON' if _AUTO_TRADE else 'OFF (set KITE_AUTO_TRADE=1 in .env to enable)'}"
      + (f" | engine setups on {SETUP_TF} | book 100% at +{BOOK_PROFIT_PCT:.0%}" if ENGINE == "setups" else
         f" | engine score | signal chart {PRIMARY_TF} | confirm {CONFIRM_TF} | orders at score ±{MIN_TRADE_SCORE}"),
      flush=True)


# ── Credentials ───────────────────────────────────────────────────────────────

def _kite_creds() -> tuple[str, str]:
    """Priority: env vars → .env → ~/.kite_creds.json."""
    api_key = os.environ.get("KITE_API_KEY", "")
    token   = os.environ.get("KITE_ACCESS_TOKEN", "")
    # .env first — auto_token.py always writes the fresh token here
    if not (api_key and token):
        env_path = pathlib.Path(__file__).parent.parent / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    v = v.strip().strip('"').strip("'")
                    if k.strip() == "KITE_API_KEY":
                        api_key = api_key or v
                    elif k.strip() == "KITE_ACCESS_TOKEN":
                        token = token or v
    # ~/.kite_creds.json as fallback
    if not (api_key and token):
        p = pathlib.Path.home() / ".kite_creds.json"
        if p.exists():
            try:
                c = json.loads(p.read_text())
                api_key = api_key or c.get("api_key", "")
                token   = token   or c.get("access_token", "")
            except Exception:
                pass
    return api_key, token


def _ntfy_topic() -> str:
    t = os.environ.get("NTFY_TOPIC", "")
    if not t:
        for src in [
            pathlib.Path.home() / ".kite_creds.json",
            pathlib.Path(__file__).parent.parent / ".env",
        ]:
            if src.exists():
                try:
                    if src.suffix == ".json":
                        t = json.loads(src.read_text()).get("ntfy_agent_topic", "")
                    else:
                        for line in src.read_text().splitlines():
                            if "NTFY_TOPIC" in line and "=" in line:
                                t = line.split("=", 1)[1].strip().strip('"').strip("'")
                except Exception:
                    pass
            if t:
                break
    return t


# ── Notification ──────────────────────────────────────────────────────────────

def send_ntfy(title: str, body: str, topic: str, priority: str = "high") -> bool:
    if not topic:
        return False
    try:
        r = requests.post(
            f"https://ntfy.sh/{topic}",
            data=body.encode("utf-8"),
            headers={
                "Title":        title.encode("utf-8"),
                "Priority":     priority,
                "Tags":         "chart,robot",
                "Content-Type": "text/plain; charset=utf-8",
            },
            timeout=10,
        )
        return r.ok
    except Exception as e:
        print(f"  ntfy error: {e}")
        return False


# ── Signal helpers ────────────────────────────────────────────────────────────

def _verdict_direction(verdict: str) -> str:
    """STRONG BUY/BUY → CE, STRONG SELL/SELL → PE, else WAIT."""
    if "BUY" in verdict:
        return "CE"
    if "SELL" in verdict:
        return "PE"
    return "WAIT"


def _stars(score: int) -> str:
    abs_s = abs(score)
    if abs_s >= 5: return "★★★★★"
    if abs_s >= 4: return "★★★★"
    if abs_s >= 3: return "★★★"
    return "★★"


# ── Alert formatter ───────────────────────────────────────────────────────────

def _fmt_alert(symbol: str, p_res: dict, c_res: dict) -> tuple[str, str]:
    verdict = p_res["verdict"]["verdict"]
    score   = p_res["verdict"]["score"]
    conf    = p_res["verdict"]["confidence"]
    entry   = p_res["verdict"].get("entry_zone")
    sl      = p_res["verdict"].get("stop_loss")
    targets = p_res["verdict"].get("targets", [])
    rr      = p_res["verdict"].get("rr")
    reasons = p_res["verdict"].get("reasons", [])
    summary = p_res["summary"]
    cpr     = p_res.get("cpr", {})
    direction = _verdict_direction(verdict)
    now_ist   = datetime.datetime.now(_IST)
    stars     = _stars(score)

    title = f"Chart Signal: {verdict} {symbol} | {stars} ({conf}%)"

    lines = [
        f"{'🚀' if abs(score) >= 5 else '⚡'} CHART SIGNAL — {symbol}",
        f"Time     : {now_ist.strftime('%H:%M IST')}",
        f"",
        f"Verdict  : {verdict}  {stars}",
        f"Direction: BUY {'CALL (CE)' if direction == 'CE' else 'PUT (PE)'}",
        f"Score    : {score:+d} / 7   Confidence: {conf}%",
        f"",
        f"Spot     : {summary['close']:,.1f}",
        f"Trend    : {summary['trend']}",
        f"Structure: {summary['structure']}",
        f"RSI      : {summary['rsi']}   ADX: {summary['adx']}",
    ]

    if cpr:
        lines.append(f"CPR      : {cpr.get('pos_label', '')}")

    if entry:
        lines.append(f"")
        lines.append(f"Entry    : {entry[0]:,.1f} – {entry[1]:,.1f}")
    if sl:
        lines.append(f"Stop Loss: {sl:,.1f}")
    if targets:
        t_str = " / ".join(f"{t:,.1f}" for t in targets)
        lines.append(f"Targets  : {t_str}")
    if rr:
        lines.append(f"R:R      : {rr}x")

    # Confirmation timeframe summary
    c_verdict = c_res.get("verdict", {}).get("verdict", "WAIT")
    c_score   = c_res.get("verdict", {}).get("score", 0)
    lines += ["", f"{PRIMARY_TF:<4}: {verdict} ({score:+d})"]
    if CONFIRM_TF != "none":
        lines.append(f"{CONFIRM_TF:<4}: {c_verdict} ({c_score:+d})")
    lines.append("")

    # reason["ok"] is True for bullish evidence; for a PE signal bearish evidence supports it
    supports = direction == "CE"
    ok_reasons  = [r["text"] for r in reasons if r.get("ok") is supports][:4]
    bad_reasons = [r["text"] for r in reasons if r.get("ok") is (not supports)][:2]
    if ok_reasons:
        lines.append("Signals:")
        for r in ok_reasons:
            lines.append(f"  ✓ {r}")
    if bad_reasons:
        lines.append("Risks:")
        for r in bad_reasons:
            lines.append(f"  ✗ {r}")

    lines += ["", "Verify before executing. Not financial advice."]
    return title, "\n".join(lines)


# ── State management ──────────────────────────────────────────────────────────

def _load_state(path: str) -> dict:
    try:
        return json.loads(pathlib.Path(path).read_text())
    except Exception:
        return {}


def _save_state(path: str, state: dict) -> None:
    pathlib.Path(path).write_text(json.dumps(state, indent=2))


# ── Signal log (for EOD report) ───────────────────────────────────────────────

def _chart_log_path(date_str: str) -> pathlib.Path:
    return pathlib.Path(f"/tmp/chart_bot_log_{date_str}.json")


def _append_chart_log(date_str: str, entry: dict) -> None:
    path = _chart_log_path(date_str)
    try:
        log = json.loads(path.read_text()) if path.exists() else {"date": date_str, "signals": []}
    except Exception:
        log = {"date": date_str, "signals": []}
    # Update existing entry for same symbol+time if re-processed (e.g. trade details added)
    existing = next((s for s in log["signals"] if s.get("_key") == entry.get("_key")), None)
    if existing:
        existing.update(entry)
    else:
        log["signals"].append(entry)
    try:
        path.write_text(json.dumps(log, indent=2))
    except Exception as e:
        print(f"  [log] Failed to write chart log: {e}")


# ── Trailing take profit ─────────────────────────────────────────────────────
# At T1 (the chart / +25% target) the bot books half (when holding ≥ 2 lots) and
# trails the rest: stop 12% below the highest premium seen, never below entry +10%.
# The GTT keeps a far-away target the whole time, so profit-taking is the bot's job.

TRAIL_PCT       = 0.12              # stop sits this far below the peak premium
TRAIL_FLOOR_PCT = 0.10              # once trailing, the stop never goes below entry +10%
TRAIL_MIN_STEP  = 0.02              # modify the GTT only when the stop rises ≥ 2%
SQUARE_OFF_TIME = datetime.time(15, 15)
_EXCHANGE_MAP = {"NIFTY": "NFO", "SENSEX": "BFO", "BANKNIFTY": "NFO"}


def _tick(x: float) -> float:
    return round(round(x / 0.05) * 0.05, 2)


def _bot_ltp(kite, symbol: str, ts: str) -> float | None:
    exchange = _EXCHANGE_MAP.get(symbol, "NFO")
    try:
        return float(kite.ltp([f"{exchange}:{ts}"])[f"{exchange}:{ts}"]["last_price"])
    except Exception as e:
        _LOG.warning(f"[TRAIL] LTP fetch failed for {ts}: {e}")
        return None


def _clear_gtts(kite, symbol: str, ts: str, ntfy_topic: str) -> None:
    """Delete every active GTT on a contract the bot is now flat in; alert if any remain."""
    deleted, failed = delete_gtts_for(kite, ts)
    if deleted:
        print(f"  [MONITOR] {symbol}: deleted GTT {', '.join(map(str, deleted))} on {ts}")
    if failed:
        print(f"  [MONITOR] {symbol}: GTT cleanup failed — {failed}")
        if ntfy_topic:
            send_ntfy(f"⚠️ Chart Bot: delete GTT on {ts} manually",
                      f"Position is flat but the GTT could not be removed ({failed}). "
                      f"If it triggers it will SELL an option you don't hold.",
                      ntfy_topic, priority="urgent")


def _exit_bot_position(kite, symbol: str, state: dict, trade_key: str, reason: str,
                       ntfy_topic: str) -> bool:
    """Sell the bot's whole position and only then remove its GTT.

    The GTT is the only stop loss, so it is kept until the sell is confirmed;
    a failed sell is retried on the next scan instead of leaving no protection.
    """
    k = lambda name: f"{trade_key}:{name}"
    ts, qty, gtt_id = state.get(k("ts")), int(state.get(k("qty")) or 0), state.get(k("gtt_id"))
    # Sell the bot's quantity, less any part-fill of an earlier cancelled sell — never
    # more than the bot bought (a manual position in the same contract stays untouched)
    held = open_qty(kite, ts)
    if held is not None:
        qty = max(0, min(qty, held))
        state[k("qty")] = qty
    ok, info = (True, "already flat") if qty <= 0 else sell_and_confirm(kite, symbol, ts, qty, discount=0.02)
    if not ok:
        fails = int(state.get(k("exit_fails")) or 0) + 1
        state[k("exit_fails")] = fails
        print(f"  [EXIT] {symbol}: {reason} — sell not filled ({info}); GTT kept, retrying next scan")
        if fails in (1, 3) and ntfy_topic:
            send_ntfy(f"⚠️ Chart Bot: {symbol} exit not filled yet",
                      f"{reason}\n{ts}: {info}\nGTT stop loss kept; retrying every minute.",
                      ntfy_topic, priority="urgent")
        return False
    _clear_gtts(kite, symbol, ts, ntfy_topic)
    state[k("gtt_id")] = ""
    state[k("exit_reason")] = reason
    print(f"  [EXIT] {symbol}: {reason} — {info}")
    if ntfy_topic:
        send_ntfy(f"Chart Bot: {symbol} exited", f"{reason}\n{ts}: {info}", ntfy_topic, priority="default")
    return True


def _manage_trade(kite, symbol: str, state: dict, trade_key: str,
                  now_ist: datetime.datetime, api_key: str, access_token: str,
                  ntfy_topic: str) -> None:
    """Square-off at 15:15, book half at T1, then trail the stop on the rest."""
    k = lambda name: f"{trade_key}:{name}"
    ts      = state.get(k("ts"))
    gtt_id  = state.get(k("gtt_id"))
    entry   = float(state.get(k("entry_price")) or 0)
    qty     = int(state.get(k("qty")) or 0)
    t1      = float(state.get(k("t1")) or 0)
    sl      = float(state.get(k("sl")) or 0)
    far     = float(state.get(k("target_price")) or 0)
    if not (ts and entry > 0 and qty > 0):
        return

    if state.get(k("exit_reason")):
        return  # sold; the close-out runs once Kite shows the position flat

    # 1. End-of-day square-off — NRML positions would otherwise be carried overnight
    if now_ist.time() >= SQUARE_OFF_TIME:
        _exit_bot_position(kite, symbol, state, trade_key, "15:15 square-off", ntfy_topic)
        return

    ltp = _bot_ltp(kite, symbol, ts)
    if ltp is None:
        return

    # A position without a GTT has no stop loss — (re)place one, alert if it fails
    if not gtt_id:
        sl  = sl or _tick(entry * 0.80)
        far = far or _tick(entry * 3)
        try:
            gtt_id = place_oco_gtt(kite, symbol, ts, qty, sl, far, ltp)
            state.update({k("gtt_id"): gtt_id, k("sl"): sl, k("target_price"): far})
            print(f"  [MONITOR] {symbol}: GTT was missing — placed {gtt_id} (SL ₹{sl})")
        except Exception as e:
            if not state.get(k("gtt_alerted")):
                state[k("gtt_alerted")] = True
                send_ntfy(f"⚠️ Chart Bot: {symbol} has NO stop loss",
                          f"{ts}: GTT placement failed ({e}). Set a stop loss in Kite manually.",
                          ntfy_topic, priority="urgent")
            return
    if state.get(k("mode")) == "book":
        print(f"  [MONITOR] {symbol}: LTP ₹{ltp:.2f}  entry ₹{entry:.2f}  SL ₹{sl:.2f}  "
              f"book ₹{far:.2f} (+{(far / entry - 1) * 100:.0f}%, GTT)")
        return  # the GTT books the whole position at the target
    trailing = bool(state.get(k("trailing")))
    stage = f"trailing, peak ₹{state.get(k('peak'))}" if trailing else f"T1 ₹{t1:.2f}"
    print(f"  [MONITOR] {symbol}: LTP ₹{ltp:.2f}  entry ₹{entry:.2f}  SL ₹{sl:.2f}  {stage}")

    # 2. T1 reached → book half, start trailing the rest
    if not trailing:
        if not (t1 and ltp >= t1):
            return
        lot       = LOT_SIZE.get(symbol, 1)
        half      = (qty // lot // 2) * lot if qty // lot >= 2 else 0
        remaining = qty - half
        new_sl    = _tick(max(entry * (1 + TRAIL_FLOOR_PCT), ltp * (1 - TRAIL_PCT)))
        far       = max(far, _tick(ltp * 2))
        # Shrink the GTT first so it can never sell more than we will still hold
        try:
            modify_oco_gtt(kite, symbol, ts, gtt_id, remaining, new_sl, far, ltp)
        except Exception as e:
            _LOG.warning(f"[TRAIL] {symbol}: GTT modify failed ({e}) — will retry next scan")
            return
        sold_ok, sold_info = (sell_and_confirm(kite, symbol, ts, half) if half else (True, ""))
        if not sold_ok:
            held = open_qty(kite, ts)
            held = qty if held is None else min(qty, held)   # < qty if the sell part-filled
            print(f"  [TRAIL] {symbol}: partial sell not filled ({sold_info}) — GTT back to {held} units")
            try:   # partial sell failed — protect what is actually held
                modify_oco_gtt(kite, symbol, ts, gtt_id, held, new_sl, far, ltp)
            except Exception as e:
                send_ntfy(f"⚠️ Chart Bot: {symbol} check GTT quantity",
                          f"Partial sell failed and GTT reset failed: {e}", ntfy_topic, priority="urgent")
            remaining, half = held, qty - held
        state.update({k("qty"): remaining, k("sl"): new_sl, k("target_price"): far,
                      k("trailing"): True, k("peak"): ltp})
        booked = f"Booked {half} units at ~₹{ltp:.1f}.\n" if half else ""
        print(f"  [TRAIL] {symbol}: T1 hit at ₹{ltp:.1f} — booked {half}, trailing {remaining} with SL ₹{new_sl}")
        if ntfy_topic:
            send_ntfy(f"💰 Chart Bot: {symbol} target hit — trailing",
                      f"{ts}\n{booked}Trailing {remaining} units: SL ₹{new_sl:.2f} "
                      f"(12% below peak, never below entry +10%).", ntfy_topic, priority="default")
        return

    # 3. Trailing — raise the stop as the premium makes new highs
    peak = max(float(state.get(k("peak")) or ltp), ltp)
    state[k("peak")] = peak
    cand = _tick(max(entry * (1 + TRAIL_FLOOR_PCT), peak * (1 - TRAIL_PCT)))
    if cand >= sl * (1 + TRAIL_MIN_STEP) and cand < ltp:
        far = max(far, _tick(peak * 2))
        try:
            modify_oco_gtt(kite, symbol, ts, gtt_id, qty, cand, far, ltp)
        except Exception as e:
            _LOG.warning(f"[TRAIL] {symbol}: GTT modify failed ({e}) — will retry next scan")
            return
        state[k("sl")] = cand
        state[k("target_price")] = far
        print(f"  [TRAIL] {symbol}: peak ₹{peak:.1f} → SL raised to ₹{cand:.2f} "
              f"(locks {((cand / entry) - 1) * 100:+.0f}%)")


# ── Position monitor ─────────────────────────────────────────────────────────

def _monitor_positions(
    symbols: list[str],
    state: dict,
    api_key: str,
    access_token: str,
    ntfy_topic: str,
    now_ist: datetime.datetime,
) -> None:
    """Check every symbol that has a recorded trade today.

    For each open-trade state key:
    - If the Kite position is gone (GTT hit SL or Target) → clear the trade_key
      so the next strong signal can re-enter.
    - If the position is still open AND the current 15m signal has reversed →
      send an ntfy alert prompting the user to review the position.
    """
    today = now_ist.strftime("%Y-%m-%d")

    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)

    for symbol in symbols:
        trade_key = f"{today}:{symbol}:trade"
        order_id  = state.get(trade_key)
        if not order_id:
            continue  # no recorded trade for this symbol today
        direction = state.get(f"{trade_key}:direction")

        # Use the exact contract the bot placed — never touch manual positions
        bot_ts = state.get(f"{trade_key}:ts")  # e.g. "NIFTY2541524950CE"

        # Look up original direction from the alerted state key
        # state[symbol] = "YYYYMMDD:symbol:direction:verdict"
        state_val = state.get(symbol, "")
        parts = state_val.split(":")
        recorded_direction = parts[2] if len(parts) >= 4 else None

        position_open = has_open_position(kite, symbol, only_ts=bot_ts)
        if position_open is None:
            # Kite API error — never treat it as "closed" (that would delete the GTT)
            print(f"  [MONITOR] {symbol}: position check failed — leaving the trade as is")
            continue

        if not position_open:
            print(f"  [MONITOR] {symbol}: position closed")
            pnl = contract_pnl(kite, bot_ts) if bot_ts else None
            closed_dir = direction or recorded_direction
            lost = pnl is not None and pnl < 0 and closed_dir
            n_losses = 0
            if lost:
                loss_key = f"{today}:{symbol}:losses:{closed_dir}"
                n_losses = int(state.get(loss_key) or 0) + 1
                state[loss_key] = n_losses
            # Remove any leftover GTT so it can't sell an option we no longer hold
            if bot_ts:
                _clear_gtts(kite, symbol, bot_ts, ntfy_topic)
            for _k in [x for x in state if x == trade_key or x.startswith(f"{trade_key}:")]:
                state.pop(_k, None)
            # Clear the alerted key too — prevents falling through to immediate re-entry
            state.pop(symbol, None)
            pnl_txt = f"P&L ₹{pnl:,.0f}" if pnl is not None else "P&L unknown"
            if ntfy_topic:
                send_ntfy(
                    f"Chart Bot: {symbol} position closed ({pnl_txt})",
                    (
                        f"Target / stop loss, reversal exit or 15:15 square-off.\n"
                        + (f"{n_losses} {closed_dir} loss(es) today — no more {symbol} {closed_dir} trades today; "
                           f"opposite side allowed." if lost and _loss_limit() and n_losses >= _loss_limit()
                           else f"{n_losses} {closed_dir} loss(es) today. Ready for the next signal." if lost
                           else "Ready for the next signal.")
                    ),
                    ntfy_topic,
                    priority="default",
                )
            continue

        # Position open — square-off / take profit / trailing stop
        _manage_trade(kite, symbol, state, trade_key, now_ist, api_key, access_token, ntfy_topic)
        if state.get(f"{trade_key}:exit_reason"):
            continue

        # Reversal exit: the signal chart has turned against the trade
        recorded_direction = direction or recorded_direction
        if not recorded_direction:
            continue
        if state.get(f"{trade_key}:mode") == "book":
            sres = _setups.scan(symbol, SETUP_TF, api_key, access_token)
            if sres.get("error") or not sres.get("signal") or sres["signal"] == recorded_direction:
                continue
            _exit_bot_position(
                kite, symbol, state, trade_key,
                f"reversal exit — {sres['label']} {sres['signal']} against the {recorded_direction} trade",
                ntfy_topic)
            continue
        try:
            p_res = analyze_chart(symbol, PRIMARY_TF, api_key, access_token, closed_only=True)
        except Exception as e:
            print(f"  [MONITOR] {symbol}: chart fetch failed — {e}")
            continue

        if p_res.get("error"):
            continue

        current_verdict   = p_res["verdict"]["verdict"]
        current_direction = _verdict_direction(current_verdict)
        current_score     = p_res["verdict"]["score"]

        if current_direction == "WAIT":
            continue

        if current_direction != recorded_direction and abs(current_score) >= REVERSAL_EXIT_SCORE:
            _exit_bot_position(
                kite, symbol, state, trade_key,
                f"reversal exit — {PRIMARY_TF} now {current_verdict} ({current_score:+d}) against the "
                f"{recorded_direction} trade", ntfy_topic)


# ── Setups engine scan ───────────────────────────────────────────────────────

def _setup_log_entry(now_ist, symbol: str, res: dict, status: str, trade: dict | None = None) -> dict:
    today = now_ist.strftime("%Y-%m-%d")
    trade = trade or {}
    sig = res.get("signal") or "WAIT"
    return {
        "_key": f"{today}:{symbol}:{now_ist.strftime('%H%M')}",
        "time": now_ist.strftime("%H:%M IST"), "symbol": symbol,
        "direction": sig, "verdict": res.get("label") or res.get("regime", "").replace("_", " "),
        "score": "", "conf": "", "spot": res.get("spot", ""),
        "tf_15m": f"{SETUP_TF} {res.get('regime', '')}", "tf_5m": "",
        "entry_low": "", "entry_high": "", "stop_loss": res.get("stop") or "",
        "targets": "", "rr": "", "auto_trade": status,
        "tradingsymbol": trade.get("tradingsymbol", ""), "strike": trade.get("strike", ""),
        "qty": trade.get("qty", ""), "entry_premium": trade.get("entry_price", ""),
        "sl_premium": trade.get("sl", ""), "target_premium": trade.get("target", ""),
        "order_id": trade.get("order_id", ""),
    }


def _scan_setups(symbols: list[str], state: dict, api_key: str, access_token: str,
                 ntfy_topic: str, now_ist: datetime.datetime) -> None:
    """One pass of the setups engine: a fresh setup on a flat index → order (book at +20%)."""
    today = now_ist.strftime("%Y-%m-%d")
    log_tick = now_ist.minute % 5 == 0
    for symbol in symbols:
        res = _setups.scan(symbol, SETUP_TF, api_key, access_token)
        if res.get("error"):
            print(f"  {symbol} {SETUP_TF}: ERROR — {res['error']}")
            continue
        sig, regime = res.get("signal"), res.get("regime", "")
        info = res["reasons"][0] if res.get("reasons") else ""
        marker = "🚀" if sig else "  "
        print(f"  {marker} {symbol:10s} | {SETUP_TF} | {regime:10s} | "
              f"{(res['label'] + ' ' + sig) if sig else 'no setup':28s} | {info}")
        if not sig:
            if log_tick:
                _append_chart_log(today, _setup_log_entry(now_ist, symbol, res, "no setup"))
            continue

        trig = f"{sig}@{res['trigger_time']:%H:%M}"
        trade_key = f"{today}:{symbol}:trade"
        done_key, alert_key = f"{today}:{symbol}:setup_done", f"{today}:{symbol}:setup_alerted"
        if state.get(done_key) == trig:
            continue   # this setup candle already traded / handled

        if state.get(alert_key) != trig:
            state[alert_key] = trig
            if ntfy_topic:
                send_ntfy(f"Chart Bot: {symbol} {res['label']} → BUY {sig}",
                          "\n".join(res["reasons"]) + f"\nSpot {res['spot']:,.1f}  ({SETUP_TF}, {regime})",
                          ntfy_topic)

        if state.get(trade_key):
            print(f"         [{symbol}] bot position open — no new entry "
                  f"(an opposite setup exits it first)")
            continue
        if SETUP_MAX_LOSSES:
            n_lost = int(state.get(f"{today}:{symbol}:losses:{sig}") or 0)
            if n_lost >= SETUP_MAX_LOSSES:
                print(f"         [{symbol}] {n_lost} {sig} losses today — no more {sig} entries")
                state[done_key] = trig
                continue
        if not (_AUTO_TRADE and symbol in _TRADE_SYMBOLS):
            why = "no auto-trade for this symbol" if symbol not in _TRADE_SYMBOLS else "auto-trade off"
            state[done_key] = trig
            _append_chart_log(today, _setup_log_entry(now_ist, symbol, res, f"signal only ({why})"))
            continue

        print(f"  [AUTO-TRADE] {res['label']} → {sig} on {symbol} @ {res['spot']:.0f} "
              f"(index stop {res['stop']})")
        trade = _place_order(symbol, sig, res["spot"], api_key, access_token,
                             chart_sl=res["stop"], book_pct=BOOK_PROFIT_PCT)
        if "error" in trade:
            # Retried every minute while this setup candle is the latest one
            print(f"  [AUTO-TRADE] NOT PLACED — {trade['error']}")
            if ntfy_topic and state.get(f"{alert_key}:err") != trig:
                state[f"{alert_key}:err"] = trig
                send_ntfy(f"Chart Auto-Trade NOT PLACED: {symbol}",
                          f"Setup: {res['label']} {sig}\nReason: {trade['error']}\n"
                          f"Retrying each minute while the setup is current.",
                          ntfy_topic, priority="default")
            if log_tick:
                _append_chart_log(today, _setup_log_entry(now_ist, symbol, res, f"skipped: {trade['error']}",
                                                          trade))
            continue

        state[done_key] = trig
        state.update({
            trade_key: trade["order_id"],
            f"{trade_key}:ts": trade["tradingsymbol"],
            f"{trade_key}:gtt_id": trade.get("gtt_id") or "",
            f"{trade_key}:entry_price": trade.get("entry_price", 0),
            f"{trade_key}:target_price": trade.get("target", 0),
            f"{trade_key}:qty": trade.get("qty", 0),
            f"{trade_key}:sl": trade.get("sl", 0),
            f"{trade_key}:t1": trade.get("t1", 0),
            f"{trade_key}:trailing": False,
            f"{trade_key}:direction": sig,
            f"{trade_key}:mode": "book",
            f"{trade_key}:setup": res["setup"],
        })
        state[symbol] = f"{today}:{symbol}:{sig}:{res['label']}"
        entry, sl, tgt = trade["entry_price"], trade["sl"], trade["target"]
        print(f"  [AUTO-TRADE] ORDER — {trade['tradingsymbol']} | qty {trade['qty']} | entry ₹{entry} "
              f"| SL ₹{sl} | book ₹{tgt}")
        _append_chart_log(today, _setup_log_entry(now_ist, symbol, res, "placed", trade))
        if ntfy_topic:
            send_ntfy(f"Chart ORDER PLACED: {trade['tradingsymbol']}",
                      (f"Setup   : {res['label']} ({regime})\n"
                       f"Qty     : {trade['qty']}\n"
                       f"Entry   : ₹{entry}\n"
                       f"SL      : ₹{sl} (structure − {int(_SL_EXTRA.get(symbol, 15))} pts buffer)\n"
                       f"Book    : ₹{tgt} (+{BOOK_PROFIT_PCT:.0%}, whole position)\n"
                       f"Order ID: {trade['order_id']}"),
                      ntfy_topic)
        try:
            sk = _place_sk_order(symbol, sig, res["spot"], option_ltp=entry, option_sl=sl, option_target=tgt)
            print(f"  [SHAREKHAN] " + (f"SKIPPED — {sk['error']}" if "error" in sk else f"ORDER — {sk['order_id']}"))
        except Exception as _sk_err:
            print(f"  [SHAREKHAN] ERROR — {_sk_err}")


# ── Single scan ───────────────────────────────────────────────────────────────

def scan_once(symbols: list[str], state_file: str, force: bool = False) -> None:
    api_key, access_token = _kite_creds()
    ntfy_topic = _ntfy_topic()

    if not (api_key and access_token):
        print("ERROR: KITE_API_KEY and KITE_ACCESS_TOKEN required.")
        sys.exit(1)

    now_ist = datetime.datetime.now(_IST)
    print(f"[{now_ist.strftime('%H:%M:%S IST')}] Chart bot scan — {', '.join(symbols)}")

    if not force and now_ist.weekday() >= 5:  # 5=Saturday, 6=Sunday
        print("Weekend — market closed, skipping.")
        return

    if not force and not (MARKET_OPEN <= now_ist.time() <= MARKET_CLOSE):
        print("Market closed — skipping.")
        return

    state = _load_state(state_file)
    today = now_ist.strftime("%Y-%m-%d")

    # Carry trades from earlier days forward so they are still managed (square-off,
    # trailing) — the monitor only looks at today's keys.
    for key in [k for k in state if re.fullmatch(r"\d{4}-\d{2}-\d{2}:\w+:trade", k)]:
        day, sym, _ = key.split(":")
        new_key = f"{today}:{sym}:trade"
        if day < today and new_key not in state:
            for old in [k for k in list(state) if k == key or k.startswith(key + ":")]:
                state[new_key + old[len(key):]] = state.pop(old)
            print(f"  [STATE] Carried open {sym} trade from {day} forward")
    # Prune everything else dated before today
    stale = [k for k in state if k[:10].count("-") == 2 and k[:10] < today]
    for k in stale:
        state.pop(k, None)
    if stale:
        print(f"  [STATE] Pruned {len(stale)} entries from previous days")

    # Monitor existing open positions before scanning for new signals
    _monitor_positions(symbols, state, api_key, access_token, ntfy_topic, now_ist)
    _save_state(state_file, state)  # persist any GTT-hit clearances immediately

    # Time filter: no new signals during opening noise or closing chop
    if not force and not (SIGNAL_OPEN <= now_ist.time() <= SIGNAL_CLOSE):
        print(f"Outside clean signal window ({SIGNAL_OPEN}–{SIGNAL_CLOSE} IST) — skipping new signals.")
        return

    if ENGINE == "setups":
        _scan_setups(symbols, state, api_key, access_token, ntfy_topic, now_ist)
        _save_state(state_file, state)
        print("Done.")
        return

    for symbol in symbols:
        try:
            p_res = analyze_chart(symbol, PRIMARY_TF, api_key, access_token, closed_only=True)
        except Exception as e:
            print(f"  {symbol} {PRIMARY_TF}: ERROR — {e}")
            continue

        if p_res.get("error"):
            print(f"  {symbol}: {p_res['error']}")
            continue

        verdict   = p_res["verdict"]["verdict"]
        score     = p_res["verdict"]["score"]
        conf      = p_res["verdict"]["confidence"]
        direction = _verdict_direction(verdict)

        marker = "🚀" if abs(score) >= 5 else ("⚡" if abs(score) >= 3 else "  ")
        print(f"  {marker} {symbol:12s} | {PRIMARY_TF} | {verdict:12s} | score {score:+d} | {conf}% conf")

        # R:R on the underlying: a target closer than the stop means price is already
        # sitting on support/resistance — the move is mostly done (21 Oct, 14:00–14:55)
        rr_val = p_res["verdict"].get("rr")
        _skip_reason = None
        if rr_val is not None and rr_val < MIN_RR:
            _skip_reason = f"R:R {rr_val} < {MIN_RR}"
            print(f"         {_skip_reason} — skipped")

        if direction == "WAIT" or abs(score) < MIN_NOTIFY_SCORE or _skip_reason:
            # Log WAIT/weak/filtered scans so the EOD Chart Bot sheet is never blank
            today = now_ist.strftime("%Y-%m-%d")
            vdict   = p_res["verdict"]
            entry_z = vdict.get("entry_zone") or []
            targets = vdict.get("targets") or []
            wait_entry: dict = {
                "_key":       f"{today}:{symbol}:{now_ist.strftime('%H%M')}",
                "time":       now_ist.strftime("%H:%M IST"),
                "symbol":     symbol,
                "direction":  direction,
                "verdict":    verdict,
                "score":      score,
                "conf":       conf,
                "spot":       p_res["summary"]["close"],
                "tf_15m":     f"{verdict} ({score:+d})",
                "tf_5m":      "",
                "entry_low":  round(entry_z[0], 1) if len(entry_z) >= 1 else "",
                "entry_high": round(entry_z[1], 1) if len(entry_z) >= 2 else "",
                "stop_loss":  vdict.get("stop_loss", ""),
                "targets":    " / ".join(str(round(t, 1)) for t in targets),
                "rr":         vdict.get("rr", ""),
                "auto_trade": _skip_reason or "no signal",
                "tradingsymbol": "", "strike": "", "qty": "",
                "entry_premium": "", "sl_premium": "", "target_premium": "",
                "order_id": "",
            }
            if now_ist.minute % 5 == 0:   # 1-min scans: keep the log at 5-min granularity
                _append_chart_log(today, wait_entry)
            continue

        # Optional confirmation chart (CONFIRM_TF="none" → signal chart only)
        c_res = {}
        if CONFIRM_TF != "none":
            try:
                c_res = analyze_chart(symbol, CONFIRM_TF, api_key, access_token, closed_only=True)
            except Exception as e:
                print(f"       {CONFIRM_TF} confirm failed: {e}")
                c_res = {}

        c_verdict   = c_res.get("verdict", {}).get("verdict", "WAIT") if c_res else "WAIT"
        c_direction = _verdict_direction(c_verdict)
        c_score     = c_res.get("verdict", {}).get("score", 0) if c_res else 0

        if CONFIRM_TF != "none":
            print(f"         {CONFIRM_TF} confirm: {c_verdict} ({c_score:+d})")

        # 5m confirmation gate: 5m score must not be negative for CE,
        # and must not be positive for PE. This prevents entering when
        # the shorter timeframe momentum actively opposes the 15m signal.
        if direction == "CE" and c_score < 0:
            print(f"         5m score {c_score:+d} opposes CE — skipping")
            vdict2 = p_res["verdict"]
            entry_z2 = vdict2.get("entry_zone") or []
            today2 = now_ist.strftime("%Y-%m-%d")
            if now_ist.minute % 5 == 0:
              _append_chart_log(today2, {
                "_key": f"{today2}:{symbol}:{now_ist.strftime('%H%M')}",
                "time": now_ist.strftime("%H:%M IST"), "symbol": symbol,
                "direction": direction, "verdict": verdict, "score": score, "conf": conf,
                "spot": p_res["summary"]["close"],
                "tf_15m": f"{verdict} ({score:+d})", "tf_5m": f"{c_verdict} ({c_score:+d})",
                "entry_low": round(entry_z2[0], 1) if len(entry_z2) >= 1 else "",
                "entry_high": round(entry_z2[1], 1) if len(entry_z2) >= 2 else "",
                "stop_loss": vdict2.get("stop_loss", ""),
                "targets": " / ".join(str(round(t, 1)) for t in (vdict2.get("targets") or [])),
                "rr": vdict2.get("rr", ""),
                "auto_trade": f"blocked: 5m score {c_score:+d} opposes CE",
                "tradingsymbol": "", "strike": "", "qty": "",
                "entry_premium": "", "sl_premium": "", "target_premium": "", "order_id": "",
            })
            continue
        if direction == "PE" and c_score > 0:
            print(f"         5m score {c_score:+d} opposes PE — skipping")
            vdict2 = p_res["verdict"]
            entry_z2 = vdict2.get("entry_zone") or []
            today2 = now_ist.strftime("%Y-%m-%d")
            if now_ist.minute % 5 == 0:
              _append_chart_log(today2, {
                "_key": f"{today2}:{symbol}:{now_ist.strftime('%H%M')}",
                "time": now_ist.strftime("%H:%M IST"), "symbol": symbol,
                "direction": direction, "verdict": verdict, "score": score, "conf": conf,
                "spot": p_res["summary"]["close"],
                "tf_15m": f"{verdict} ({score:+d})", "tf_5m": f"{c_verdict} ({c_score:+d})",
                "entry_low": round(entry_z2[0], 1) if len(entry_z2) >= 1 else "",
                "entry_high": round(entry_z2[1], 1) if len(entry_z2) >= 2 else "",
                "stop_loss": vdict2.get("stop_loss", ""),
                "targets": " / ".join(str(round(t, 1)) for t in (vdict2.get("targets") or [])),
                "rr": vdict2.get("rr", ""),
                "auto_trade": f"blocked: 5m score {c_score:+d} opposes PE",
                "tradingsymbol": "", "strike": "", "qty": "",
                "entry_premium": "", "sl_premium": "", "target_premium": "", "order_id": "",
            })
            continue

        # 1-min scans score a still-forming 15m candle, so a signal must appear on
        # CONFIRM_SCANS consecutive scans before alerting/ordering (filters 1-min spikes)
        today      = now_ist.strftime("%Y-%m-%d")
        streak_key = f"{today}:{symbol}:streak"
        prev       = state.get(streak_key) or {}
        sig_id     = f"{direction}:{verdict}"
        consecutive = (prev.get("sig") == sig_id
                       and _minutes_between(prev.get("t", ""), now_ist) <= 2)
        n_seen = prev.get("n", 0) + 1 if consecutive else 1
        state[streak_key] = {"sig": sig_id, "n": n_seen, "t": now_ist.strftime("%H:%M")}
        if n_seen < CONFIRM_SCANS:
            print(f"         {verdict} seen {n_seen}/{CONFIRM_SCANS} — confirming on next scan")
            continue

        # Dedup: skip if same date+direction+verdict already alerted AND order was placed.
        # Prefix with today's date so stale state from yesterday never blocks today.
        state_key = f"{today}:{symbol}:{direction}:{verdict}"
        trade_key = f"{today}:{symbol}:trade"
        already_alerted = state.get(symbol) == state_key
        already_traded  = bool(state.get(trade_key))

        if already_alerted and already_traded:
            print(f"         Already alerted+traded today — no change")
            continue
        # _monitor_positions clears trade_key once the bot's position is closed, so a
        # trade_key here means a bot position is still open (possibly the other side)
        if already_traded:
            print(f"         [{symbol}] Bot position still open — no new entry")
            continue
        n_lost = int(state.get(f"{today}:{symbol}:losses:{direction}") or 0)
        if n_lost >= MAX_LOSSES_PER_DIR:
            if now_ist.minute % 5 == 0:
                print(f"         [{symbol}] {n_lost} {direction} losses today — no more {direction} "
                      f"entries today (opposite side allowed)")
            continue
        if already_alerted:
            if not (_AUTO_TRADE and symbol in _TRADE_SYMBOLS and abs(score) >= MIN_TRADE_SCORE):
                why = ("no auto-trade for this symbol" if symbol not in _TRADE_SYMBOLS
                       else f"orders need score ±{MIN_TRADE_SCORE}")
                print(f"         Already alerted — signal only ({why}), no order")
                if now_ist.minute % 5 == 0:   # keep the EOD sheet at 5-min granularity
                    vd = p_res["verdict"]
                    _append_chart_log(today, {
                        "_key": f"{today}:{symbol}:{now_ist.strftime('%H%M')}",
                        "time": now_ist.strftime("%H:%M IST"), "symbol": symbol,
                        "direction": direction, "verdict": verdict, "score": score, "conf": conf,
                        "spot": p_res["summary"]["close"],
                        "tf_15m": f"{verdict} ({score:+d})", "tf_5m": "",
                        "entry_low": "", "entry_high": "",
                        "stop_loss": vd.get("stop_loss", ""),
                        "targets": " / ".join(str(round(t, 1)) for t in (vd.get("targets") or [])),
                        "rr": vd.get("rr", ""),
                        "auto_trade": f"signal only ({why})",
                        "tradingsymbol": "", "strike": "", "qty": "",
                        "entry_premium": "", "sl_premium": "", "target_premium": "", "order_id": "",
                    })
                continue
            print(f"         Already alerted (no trade yet) — retrying order")

        # Format and send notification (once per signal)
        title, message = _fmt_alert(symbol, p_res, c_res)
        if not already_alerted:
            print(f"\n{'='*55}\n{message}\n{'='*55}\n")

        if ntfy_topic and not already_alerted:
            ok = send_ntfy(title, message, ntfy_topic)
            print(f"  ntfy.sh: {'OK' if ok else 'FAILED'}")
        elif not ntfy_topic:
            print("  No ntfy topic configured.")

        # Build log entry for EOD report
        vdict   = p_res["verdict"]
        targets = vdict.get("targets") or []
        entry_z = vdict.get("entry_zone") or []
        log_entry: dict = {
            "_key":      f"{today}:{symbol}:{now_ist.strftime('%H%M')}",
            "time":      now_ist.strftime("%H:%M IST"),
            "symbol":    symbol,
            "direction": direction,
            "verdict":   verdict,
            "score":     score,
            "conf":      conf,
            "spot":      p_res["summary"]["close"],
            "tf_15m":    f"{verdict} ({score:+d})",
            "tf_5m":     f"{c_verdict} ({c_score:+d})",
            "entry_low":  round(entry_z[0], 1) if len(entry_z) >= 1 else "",
            "entry_high": round(entry_z[1], 1) if len(entry_z) >= 2 else "",
            "stop_loss":  vdict.get("stop_loss", ""),
            "targets":    " / ".join(str(round(t, 1)) for t in targets),
            "rr":         vdict.get("rr", ""),
            "auto_trade": "off",
            "tradingsymbol": "",
            "strike": "",
            "qty": "",
            "entry_premium": "",
            "sl_premium": "",
            "target_premium": "",
            "order_id": "",
        }

        # Auto-execute order
        if _AUTO_TRADE and symbol in _TRADE_SYMBOLS and abs(score) >= MIN_TRADE_SCORE:
            spot      = p_res["summary"]["close"]
            chart_sl  = p_res["verdict"].get("stop_loss")
            chart_tgt = (p_res["verdict"].get("targets") or [None])[0]
            print(
                f"  [AUTO-TRADE] {direction} order for {symbol} @ {spot:.0f}"
                f"  (chart SL={chart_sl}  T={chart_tgt})"
            )
            trade = _place_order(
                symbol, direction, spot, api_key, access_token,
                chart_sl=chart_sl, chart_target=chart_tgt, trailing=True,
            )
            if "error" in trade:
                print(f"  [AUTO-TRADE] SKIPPED — {trade['error']}")
                log_entry["auto_trade"] = f"skipped: {trade['error']}"
                log_entry["order_id"] = trade.get("order_id", "")
                if ntfy_topic and (trade.get("sent") or not already_alerted):
                    send_ntfy(
                        f"Chart Auto-Trade SKIPPED: {symbol}",
                        f"Signal: {verdict}\nReason: {trade['error']}",
                        ntfy_topic,
                        priority="default",
                    )
            else:
                print(
                    f"  [AUTO-TRADE] ORDER — {trade['tradingsymbol']}"
                    f" | Entry ₹{trade['entry_price']}"
                    f" | SL ₹{trade['sl']}"
                    f" | T1 ₹{trade.get('t1')} (book half + trail)"
                )
                log_entry.update({
                    "auto_trade":    "placed",
                    "tradingsymbol": trade["tradingsymbol"],
                    "strike":        trade.get("strike", ""),
                    "qty":           trade.get("qty", ""),
                    "entry_premium": trade.get("entry_price", ""),
                    "sl_premium":    trade.get("sl", ""),
                    "target_premium":trade.get("target", ""),
                    "order_id":      trade.get("order_id", ""),
                })
                if ntfy_topic:
                    send_ntfy(
                        f"Chart ORDER PLACED: {trade['tradingsymbol']}",
                        (
                            f"Symbol  : {symbol} {trade['opt_type']}\n"
                            f"Strike  : {trade['strike']}\n"
                            f"Qty     : {trade['qty']}\n"
                            f"Entry   : ₹{trade['entry_price']}\n"
                            f"SL      : ₹{trade['sl']}\n"
                            f"T1      : ₹{trade.get('t1')} → book half, trail rest 12% below peak\n"
                            f"Order ID: {trade['order_id']}"
                        ),
                        ntfy_topic,
                    )
                state[trade_key]                      = trade["order_id"]
                state[f"{trade_key}:ts"]             = trade["tradingsymbol"]
                state[f"{trade_key}:gtt_id"]         = trade.get("gtt_id") or ""
                state[f"{trade_key}:entry_price"]    = trade.get("entry_price", 0)
                state[f"{trade_key}:target_price"]   = trade.get("target", 0)
                state[f"{trade_key}:qty"]            = trade.get("qty", 0)
                state[f"{trade_key}:sl"]             = trade.get("sl", 0)
                state[f"{trade_key}:t1"]             = trade.get("t1", 0)
                state[f"{trade_key}:trailing"]       = False
                state[f"{trade_key}:direction"]      = direction

                # Mirror on Sharekhan
                try:
                    sk_result = _place_sk_order(
                        symbol, direction, spot,
                        option_ltp=trade.get("entry_price", 0),
                        option_sl=trade.get("sl", 0),
                        option_target=trade.get("t1", trade.get("target", 0)),
                    )
                    if "error" in sk_result:
                        print(f"  [SHAREKHAN] SKIPPED — {sk_result['error']}")
                        if ntfy_topic:
                            send_ntfy(
                                f"Sharekhan SKIPPED: {symbol}",
                                f"Kite order placed but Sharekhan failed.\nReason: {sk_result['error']}",
                                ntfy_topic,
                                priority="default",
                            )
                    else:
                        print(
                            f"  [SHAREKHAN] ORDER — {sk_result['tradingsymbol']}"
                            f" | ID: {sk_result['order_id']}"
                        )
                        log_entry["sk_order_id"]       = sk_result["order_id"]
                        log_entry["sk_tradingsymbol"]  = sk_result["tradingsymbol"]
                        if ntfy_topic:
                            send_ntfy(
                                f"Sharekhan ORDER: {sk_result['tradingsymbol']}",
                                (
                                    f"Symbol : {symbol} {sk_result['opt_type']}\n"
                                    f"Strike : {sk_result['strike']}\n"
                                    f"Qty    : {sk_result['qty']}\n"
                                    f"Price  : ₹{sk_result['price']}\n"
                                    f"Order  : {sk_result['order_id']}"
                                ),
                                ntfy_topic,
                            )
                except Exception as _sk_err:
                    print(f"  [SHAREKHAN] ERROR — {_sk_err}")

        elif _AUTO_TRADE and symbol not in _TRADE_SYMBOLS:
            log_entry["auto_trade"] = "signal only (no auto-trade for this symbol)"
        elif _AUTO_TRADE and abs(score) < MIN_TRADE_SCORE:
            log_entry["auto_trade"] = f"signal only (score {score:+d} < {MIN_TRADE_SCORE})"

        if not already_alerted or log_entry["auto_trade"] == "placed" or now_ist.minute % 5 == 0:
            _append_chart_log(today, log_entry)
        state[symbol] = state_key  # mark as alerted (date-prefixed)

    _save_state(state_file, state)
    print("Done.")


# ── Preflight (no orders) ─────────────────────────────────────────────────────

def preflight(symbols: list[str], state_file: str, notify: bool = False) -> bool:
    """Check everything an order needs, without placing one. True if nothing FAILs."""
    import fcntl
    from screener.kite_order import _EXCHANGE, _atm_strike, _resolve_tradingsymbol

    results: list[tuple[str, str, str]] = []
    def add(level: str, name: str, info: str) -> None:
        results.append((level, name, info))
        print(f"  {level:4s}  {name:22s} {info}")

    now_ist = datetime.datetime.now(_IST)
    print(f"[{now_ist:%Y-%m-%d %H:%M IST}] chart_bot preflight — no orders are placed\n")
    add("PASS" if _AUTO_TRADE else "FAIL", "auto-trade", "ON" if _AUTO_TRADE else "OFF — set KITE_AUTO_TRADE=1 in .env")
    if ENGINE == "setups":
        add("PASS", "rules", f"setups engine on {SETUP_TF}, book 100% at +{BOOK_PROFIT_PCT:.0%}, SL buffer "
                             f"NIFTY {_SL_EXTRA['NIFTY']:.0f} / SENSEX {_SL_EXTRA['SENSEX']:.0f} pts, "
                             f"loss limit {SETUP_MAX_LOSSES or 'off'}, window {SIGNAL_OPEN:%H:%M}–{SIGNAL_CLOSE:%H:%M}")
    else:
        add("PASS", "rules", f"score engine, {PRIMARY_TF} chart, orders at ±{MIN_TRADE_SCORE}, R:R ≥ {MIN_RR}, "
                             f"{MAX_LOSSES_PER_DIR} losses/direction/day, window {SIGNAL_OPEN:%H:%M}–{SIGNAL_CLOSE:%H:%M}")
    topic = _ntfy_topic()
    add("PASS" if topic else "WARN", "ntfy topic", "set" if topic else "missing — no phone alerts")

    api_key, access_token = _kite_creds()
    if not (api_key and access_token):
        add("FAIL", "kite credentials", "KITE_API_KEY / KITE_ACCESS_TOKEN missing")
        return _preflight_report(results, topic, notify)
    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)
    try:
        prof = kite.profile()
        add("PASS", "kite token", f"valid ({prof.get('user_id', '?')})")
    except Exception as e:
        add("FAIL", "kite token", f"{e} — run auto_token.py")
        return _preflight_report(results, topic, notify)

    try:
        bal = float(kite.margins()["equity"]["available"]["live_balance"])
        add("PASS", "balance", f"₹{bal:,.0f} live balance")
    except Exception as e:
        bal = None
        add("WARN", "balance", f"margin fetch failed ({e}) — orders will use 1 lot")

    for sym in symbols:
        try:
            res = analyze_chart(sym, PRIMARY_TF, api_key, access_token, closed_only=True)
        except Exception as e:
            add("FAIL", f"{sym} chart", str(e))
            continue
        if res.get("error"):
            add("FAIL", f"{sym} chart", res["error"])
            continue
        df, v = res["df"], res["verdict"]
        last = df.index[-1]
        vol = "futures volume" if df["Volume"].tail(20).sum() > 0 else "NO volume (price-only VWAP)"
        add("PASS", f"{sym} chart", f"{len(df)} candles, last {last:%d-%b %H:%M}, {vol}; "
                                    f"now {v['verdict']} {v['score']:+d} R:R {v.get('rr')}")
        if ENGINE == "setups":
            sres = _setups.scan(sym, SETUP_TF, api_key, access_token)
            if sres.get("error"):
                add("FAIL", f"{sym} setups", sres["error"])
            else:
                add("PASS", f"{sym} setups", f"{SETUP_TF} {sres['regime']}: "
                    + (f"{sres['label']} {sres['signal']}" if sres["signal"] else "no setup now")
                    + (f" — {sres['reasons'][-1]}" if sres.get("reasons") else ""))
        if sym not in _TRADE_SYMBOLS:
            continue
        spot = float(res["summary"]["close"])
        for opt in ("CE", "PE"):
            try:
                strike = _atm_strike(spot, sym)
                ts = _resolve_tradingsymbol(kite, sym, strike, opt)
                if not ts:
                    add("FAIL", f"{sym} {opt} contract", f"no contract for strike {strike}")
                    continue
                ex = _EXCHANGE[sym]
                ltp = float(kite.ltp([f"{ex}:{ts}"])[f"{ex}:{ts}"]["last_price"])
                lot_cost = ltp * LOT_SIZE[sym]
                lvl = "PASS" if bal is None or bal >= lot_cost else "WARN"
                note = "" if lvl == "PASS" else f" — balance below 1 lot, Kite will reject"
                add(lvl, f"{sym} {opt} contract", f"{ts} LTP ₹{ltp:.2f}, 1 lot ₹{lot_cost:,.0f}{note}")
            except Exception as e:
                add("FAIL", f"{sym} {opt} contract", str(e))

    # Open positions vs GTTs — a GTT on a contract we don't hold would open a short
    try:
        held = {p["tradingsymbol"]: int(p["quantity"]) for p in kite.positions().get("net", [])
                if int(p.get("quantity", 0)) != 0}
        gtts = [g for g in kite.get_gtts() if g.get("status") == "active"]
        orphans = [g for g in gtts if g.get("condition", {}).get("tradingsymbol") not in held]
        unprotected = [ts for ts, q in held.items() if q > 0 and
                       not any(g.get("condition", {}).get("tradingsymbol") == ts for g in gtts)]
        add("PASS", "positions", ", ".join(f"{ts} {q}" for ts, q in held.items()) or "none open")
        add("FAIL" if orphans else "PASS", "orphan GTTs",
            ", ".join(f"{g['id']} {g['condition']['tradingsymbol']}" for g in orphans) + " — delete in Kite"
            if orphans else "none")
        add("WARN" if unprotected else "PASS", "positions w/o GTT",
            ", ".join(unprotected) + " — no stop loss" if unprotected else "none")
    except Exception as e:
        add("FAIL", "positions / GTTs", str(e))

    try:
        state = _load_state(state_file)
        open_trades = [k for k in state if re.fullmatch(r"\d{4}-\d{2}-\d{2}:\w+:trade", k)]
        add("PASS", "state file", f"{len(state)} keys; open bot trades: "
                                  + (", ".join(open_trades) if open_trades else "none"))
    except Exception as e:
        add("FAIL", "state file", str(e))

    try:
        with open("/tmp/chart_bot_loop.lock", "w") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(lf, fcntl.LOCK_UN)
        add("WARN", "chart_bot loop", "NOT running — bash start_droplet.sh (watchdog restarts it)")
    except OSError:
        add("PASS", "chart_bot loop", "running")

    return _preflight_report(results, topic, notify)


def _preflight_report(results: list, topic: str, notify: bool) -> bool:
    fails = [r for r in results if r[0] == "FAIL"]
    warns = [r for r in results if r[0] == "WARN"]
    verdict = "READY" if not fails else "NOT READY"
    print(f"\n{verdict}: {len(fails)} fail, {len(warns)} warn")
    if notify and topic:
        body = "\n".join(f"{lvl} {name}: {info}" for lvl, name, info in fails + warns) or "All checks passed."
        send_ntfy(f"Chart Bot preflight: {verdict}", body, topic,
                  priority="urgent" if fails else "default")
    return not fails


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Chart Analysis signal bot")
    ap.add_argument("symbols", nargs="*", default=SYMBOLS)
    ap.add_argument("--once", action="store_true", help="Single scan then exit")
    ap.add_argument("--state-file", default="/root/stockAnalyser/chart_bot_state.json")
    ap.add_argument("--loop",        action="store_true", help="Run as daemon, scanning every --interval seconds")
    ap.add_argument("--interval",    type=int, default=60, help="Loop interval in seconds, aligned to the clock (default: 60)")
    ap.add_argument("--test-notify", action="store_true", help="Send test notification and exit")
    ap.add_argument("--force", action="store_true", help="Run scan even outside market hours")
    ap.add_argument("--preflight", action="store_true", help="Check token, charts, contracts, GTTs — no orders")
    ap.add_argument("--notify", action="store_true", help="With --preflight: push the result to ntfy")
    args = ap.parse_args()

    symbols = [s.upper() for s in args.symbols]

    if args.preflight:
        sys.exit(0 if preflight(symbols, args.state_file, notify=args.notify) else 1)
    if args.test_notify:
        ntfy_topic = _ntfy_topic()
        now = datetime.datetime.now(_IST).strftime("%H:%M IST")
        title = "Chart Bot — Test Notification"
        body  = (
            f"Chart Analysis Bot is running!\n"
            f"Time     : {now}\n"
            f"Watching : {', '.join(symbols)}\n"
            f"Strategy : {PRIMARY_TF} signals" + (f" + {CONFIRM_TF} confirmation" if CONFIRM_TF != "none" else "") + "\n"
            f"Signals  : SMC structure + EMA + VWAP + RSI + ADX + OB + CPR\n\n"
            f"You will receive alerts when STRONG BUY or STRONG SELL fires on "
            f"during market hours (09:30–15:00 IST)."
        )
        print(body)
        ok = send_ntfy(title, body, ntfy_topic)
        print(f"ntfy.sh: {'OK' if ok else 'FAILED — check NTFY_TOPIC in .env'}")
    elif args.once:
        scan_once(symbols, args.state_file, force=args.force)
    elif args.loop:
        # Single instance: two loops would both place orders. Wait briefly so a
        # restart (old copy still exiting) doesn't fail; otherwise exit.
        import fcntl
        _lock_file = open("/tmp/chart_bot_loop.lock", "w")
        for _ in range(15):
            try:
                fcntl.flock(_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                time.sleep(1)
        else:
            print("Another chart_bot --loop is already running — exiting.")
            sys.exit(0)
        print(f"Chart bot loop started — scanning every {args.interval}s")
        while True:
            try:
                scan_once(symbols, args.state_file, force=args.force)
            except Exception as e:
                print(f"[loop] Unhandled error: {e}")
            # Sleep to the next clock boundary (+3 s for the candle to land) so scans
            # don't drift later each time by however long the scan took
            time.sleep(args.interval - (time.time() % args.interval) + 3)
    else:
        print("Use --once for cron mode, --loop for daemon mode, or --test-notify.")
        sys.exit(1)
