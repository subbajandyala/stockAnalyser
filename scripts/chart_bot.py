"""
Chart Analysis Signal Bot
=========================
Runs analyze_chart() for each symbol/timeframe and sends a push notification
via ntfy.sh when a tradeable signal fires (STRONG BUY / BUY / STRONG SELL / SELL).

Signal → Option direction:
  STRONG BUY / BUY  → BUY CE
  STRONG SELL / SELL → BUY PE

Signals are scored on CLOSED 15m candles and confirmed on closed 5m candles.

Auto-trade (KITE_AUTO_TRADE=1) gates, in order:
  score ≥ MIN_TRADE_SCORE → 5m confirms → Noise Area breakout
Orders are based on chart analysis only.

Position management each scan:
  partial booking at T1 → time stop → VWAP-cross exit → trailing SL → reversal alert

All tunables can be overridden in .env (see _SETTINGS below).

Cron (every 5 min, market hours):
  */5 9-15 * * 1-5 cd /root/stockAnalyser && /root/venv/bin/python -u scripts/chart_bot.py --once >> chart_bot.log 2>&1

Single test run:
  /root/venv/bin/python -u scripts/chart_bot.py --once
  /root/venv/bin/python -u scripts/chart_bot.py --test-notify
  /root/venv/bin/python -u scripts/chart_bot.py --loop --shadow   # paper mode, no orders
"""

import argparse
import datetime
import json
import logging
import os
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import requests

from kiteconnect import KiteConnect

from screener.chart_analysis import analyze_chart
from screener.kite_order import (
    contract_pnl,
    exit_position,
    has_open_position,
    place_oco_gtt,
    place_order_for_signal as _place_order,
    position_pnl_pct,
    sell_partial,
)
from screener.noise_area import noise_state
from screener.sharekhan_order import place_sk_order as _place_sk_order

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
_LOG = logging.getLogger("chart_bot")

_ROOT        = pathlib.Path(__file__).parent.parent
_IST         = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
MARKET_OPEN  = datetime.time(9, 20)   # give 5 min after open to avoid noise
MARKET_CLOSE = datetime.time(15, 25)  # stop 5 min before close

SYMBOLS    = ["NIFTY", "BANKNIFTY", "SENSEX"]
_TRADE_SYMBOLS = {"NIFTY", "SENSEX"}   # BANKNIFTY excluded from auto-trade

# Primary + confirmation timeframe
PRIMARY_TF = "15m"
CONFIRM_TF = "5m"

# Minimum score to send a notification (BUY=3, STRONG BUY=5)
MIN_NOTIFY_SCORE = 3
# Minimum score to auto-execute an order (at least 4/7 across indicator groups)
MIN_TRADE_SCORE  = 4
# R:R on the underlying — informational only
MIN_RR = 1.5
# Clean signal window: no new signals outside 9:30–15:00 IST
SIGNAL_OPEN  = datetime.time(9, 30)
SIGNAL_CLOSE = datetime.time(15, 0)


# ── Settings (.env overridable) ───────────────────────────────────────────────

def _env_value(name: str) -> str:
    v = os.environ.get(name, "")
    if v:
        return v
    env_path = _ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, val = line.split("=", 1)
                if k.strip() == name:
                    return val.strip().strip('"').strip("'")
    return ""


def _cfg_float(name: str, default: float) -> float:
    try:
        v = _env_value(name)
        return float(v) if v else default
    except ValueError:
        return default


def _cfg_bool(name: str, default: bool) -> bool:
    v = _env_value(name)
    return default if not v else v.lower() in ("1", "true", "yes", "on")


TIME_STOP_MIN        = _cfg_float("CHART_TIME_STOP_MIN", 30)          # exit if flat after N minutes
TIME_STOP_MIN_GAIN   = _cfg_float("CHART_TIME_STOP_MIN_GAIN", 10)     # "flat" = premium gain below N%
EXPIRY_DAY_MODE      = _env_value("CHART_EXPIRY_DAY_MODE") or "next"  # next | same | skip
PARTIAL_BOOKING      = _cfg_bool("CHART_PARTIAL_BOOKING", True)
NOISE_GATE           = _cfg_bool("CHART_NOISE_GATE", True)
VWAP_EXIT            = _cfg_bool("CHART_VWAP_EXIT", True)
VWAP_EXIT_MIN_HOLD   = 10   # minutes before the VWAP-cross exit is allowed


def _auto_trade_enabled() -> bool:
    return _env_value("CHART_AUTO_TRADE") == "1" or _env_value("KITE_AUTO_TRADE") == "1"


_AUTO_TRADE = _auto_trade_enabled()
_SHADOW     = _cfg_bool("CHART_SHADOW", False)   # also settable with --shadow


# ── Credentials ───────────────────────────────────────────────────────────────

def _kite_creds() -> tuple[str, str]:
    """Priority: env vars → .env → ~/.kite_creds.json."""
    api_key = os.environ.get("KITE_API_KEY", "")
    token   = os.environ.get("KITE_ACCESS_TOKEN", "")
    # .env first — auto_token.py always writes the fresh token here
    if not (api_key and token):
        api_key = api_key or _env_value("KITE_API_KEY")
        token   = token or _env_value("KITE_ACCESS_TOKEN")
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
            _ROOT / ".env",
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
    if not topic or _SHADOW:
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

def _fmt_alert(symbol: str, p_res: dict, c_res: dict, extra: list[str] | None = None) -> tuple[str, str]:
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
    if extra:
        lines += extra

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

    c_verdict = c_res.get("verdict", {}).get("verdict", "WAIT")
    c_score   = c_res.get("verdict", {}).get("score", 0)
    lines += [
        f"",
        f"15m : {verdict} ({score:+d})",
        f"5m  : {c_verdict} ({c_score:+d})",
        f"",
    ]

    # reason["ok"] is True for bullish evidence — for a PE signal, bearish evidence supports it
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


# ── Signal log (for EOD report + outcome tracker) ─────────────────────────────

def _chart_log_path(date_str: str) -> pathlib.Path:
    prefix = "chart_bot_shadow_log" if _SHADOW else "chart_bot_log"
    return pathlib.Path(f"/tmp/{prefix}_{date_str}.json")


def _append_chart_log(date_str: str, entry: dict) -> None:
    path = _chart_log_path(date_str)
    try:
        log = json.loads(path.read_text()) if path.exists() else {"date": date_str, "signals": []}
    except Exception:
        log = {"date": date_str, "signals": []}
    existing = next((s for s in log["signals"] if s.get("_key") == entry.get("_key")), None)
    if existing:
        existing.update(entry)
    else:
        log["signals"].append(entry)
    try:
        path.write_text(json.dumps(log, indent=2))
    except Exception as e:
        print(f"  [log] Failed to write chart log: {e}")


def _base_log_entry(symbol: str, p_res: dict, now_ist: datetime.datetime,
                    tf_5m: str = "", auto_trade: str = "") -> dict:
    today   = now_ist.strftime("%Y-%m-%d")
    vdict   = p_res["verdict"]
    entry_z = vdict.get("entry_zone") or []
    targets = vdict.get("targets") or []
    verdict = vdict["verdict"]
    return {
        "_key":       f"{today}:{symbol}:{now_ist.strftime('%H%M')}",
        "time":       now_ist.strftime("%H:%M IST"),
        "symbol":     symbol,
        "direction":  _verdict_direction(verdict),
        "verdict":    verdict,
        "score":      vdict["score"],
        "conf":       vdict["confidence"],
        "groups":     vdict.get("groups", {}),
        "spot":       p_res["summary"]["close"],
        "adx":        p_res["summary"].get("adx", ""),
        "tf_15m":     f"{verdict} ({vdict['score']:+d})",
        "tf_5m":      tf_5m,
        "entry_low":  round(entry_z[0], 1) if len(entry_z) >= 1 else "",
        "entry_high": round(entry_z[1], 1) if len(entry_z) >= 2 else "",
        "stop_loss":  vdict.get("stop_loss", ""),
        "targets":    " / ".join(str(round(t, 1)) for t in targets),
        "rr":         vdict.get("rr", ""),
        "auto_trade": auto_trade,
        "tradingsymbol": "", "strike": "", "qty": "",
        "entry_premium": "", "sl_premium": "", "target_premium": "",
        "order_id": "",
    }


# ── Trailing GTT adjuster ────────────────────────────────────────────────────

_BREAKEVEN_THRESHOLD = 0.40   # 40% premium gain → move SL to entry (breakeven)
_TRAIL1_THRESHOLD    = 0.65   # 65% premium gain → move SL to entry + 20%
_TRAIL1_SL_BUFFER    = 0.20   # locks in 20% of entry premium as profit
_EXCHANGE_MAP = {"NIFTY": "NFO", "SENSEX": "BFO", "BANKNIFTY": "NFO"}


def _tick(x: float) -> float:
    return round(round(x / 0.05) * 0.05, 2)


def _replace_gtt(kite, symbol: str, ts: str, old_gtt_id, qty: int,
                 sl: float, target: float) -> int | str | None:
    """Delete the old GTT and place a new one. Returns the new GTT id, or None.

    If the old GTT can't be deleted, nothing new is placed (avoids two live GTTs
    that could both sell → accidental short).
    """
    exchange = _EXCHANGE_MAP.get(symbol, "NFO")
    try:
        ltp = float(kite.ltp([f"{exchange}:{ts}"])[f"{exchange}:{ts}"]["last_price"])
    except Exception as e:
        _LOG.warning(f"[GTT] LTP fetch failed for {ts}: {e}")
        return None
    if old_gtt_id:
        try:
            kite.delete_gtt(int(old_gtt_id))
        except Exception as e:
            _LOG.warning(f"[GTT] Delete of {old_gtt_id} failed ({e}) — keeping existing GTT")
            return None
    try:
        return place_oco_gtt(kite, symbol, ts, qty, sl, target, ltp)
    except Exception as e:
        _LOG.error(f"[GTT] New GTT placement failed for {ts}: {e} — POSITION UNPROTECTED")
        return None


def _adjust_trailing_gtt(kite, symbol: str, state: dict, trade_key: str,
                         pnl_pct: float, ntfy_topic: str) -> None:
    """sl_step 0→1: SL to breakeven at +40%. sl_step 1→2: SL to entry+20% at +65%."""
    bot_ts       = state.get(f"{trade_key}:ts")
    gtt_id       = state.get(f"{trade_key}:gtt_id", "")
    entry_price  = float(state.get(f"{trade_key}:entry_price") or 0)
    target_price = float(state.get(f"{trade_key}:target_price") or 0)
    qty          = int(state.get(f"{trade_key}:qty") or 0)
    sl_step      = int(state.get(f"{trade_key}:sl_step") or 0)
    if not (gtt_id and entry_price > 0 and target_price > 0 and qty > 0 and bot_ts):
        return

    if sl_step < 1 and pnl_pct >= _BREAKEVEN_THRESHOLD:
        new_sl_step, new_sl, label = 1, _tick(entry_price), "BREAKEVEN"
    elif sl_step < 2 and pnl_pct >= _TRAIL1_THRESHOLD:
        new_sl_step, new_sl, label = 2, _tick(entry_price * (1 + _TRAIL1_SL_BUFFER)), "TRAIL +20%"
    else:
        return
    if new_sl >= target_price or entry_price * (1 + pnl_pct) <= new_sl:
        return

    new_gtt_id = _replace_gtt(kite, symbol, bot_ts, gtt_id, qty, new_sl, target_price)
    if not new_gtt_id:
        return
    state[f"{trade_key}:gtt_id"]  = new_gtt_id
    state[f"{trade_key}:sl_step"] = new_sl_step
    _LOG.info(f"[TRAIL] {symbol} SL → {label}: new_sl={new_sl} target={target_price} pnl={pnl_pct:+.1%}")
    send_ntfy(
        f"🛡 Chart Bot: {symbol} SL → {label}",
        (
            f"Trade is in profit ({pnl_pct:+.1%}) — SL adjusted.\n"
            f"Contract : {bot_ts}\n"
            f"Entry    : ₹{entry_price:.2f}\n"
            f"New SL   : ₹{new_sl:.2f}\n"
            f"Target   : ₹{target_price:.2f}\n"
            f"GTT ID   : {new_gtt_id}"
        ),
        ntfy_topic,
        priority="default",
    )


# ── Position monitor ─────────────────────────────────────────────────────────

def _clear_trade(state: dict, symbol: str, trade_key: str) -> None:
    for k in [k for k in state if k == trade_key or k.startswith(f"{trade_key}:")]:
        state.pop(k, None)
    state.pop(symbol, None)


def _close_out(state: dict, symbol: str, trade_key: str, today: str,
               now_ist: datetime.datetime, kite, ntfy_topic: str) -> None:
    """Report a closed trade, clear its state and start the cooldown."""
    bot_ts = state.get(f"{trade_key}:ts")
    pnl    = contract_pnl(kite, bot_ts) if (kite and bot_ts) else None
    reason = state.get(f"{trade_key}:exit_reason", "GTT triggered (SL or target)")
    _clear_trade(state, symbol, trade_key)
    state[f"{today}:{symbol}:sl_cooldown"] = now_ist.strftime("%H:%M")
    pnl_txt = f"₹{pnl:,.0f}" if pnl is not None else "unknown"
    print(f"  [MONITOR] {symbol}: position closed — {reason} | P&L {pnl_txt}")
    send_ntfy(
        f"Chart Bot: {symbol} position closed ({pnl_txt})",
        f"Reason   : {reason}\nP&L      : {pnl_txt}\n"
        f"Re-entry : immediate on STRONG BUY/SELL, otherwise after 30 min.",
        ntfy_topic,
        priority="default",
    )


def _exit_now(state: dict, symbol: str, trade_key: str, reason: str,
              api_key: str, access_token: str, now_ist: datetime.datetime,
              ntfy_topic: str) -> None:
    """Cancel the GTT and sell the bot's contract; position is closed out on a later scan."""
    res = exit_position(symbol, api_key, access_token,
                        gtt_id=state.get(f"{trade_key}:gtt_id") or None,
                        only_ts=state.get(f"{trade_key}:ts"))
    if "error" in res:
        print(f"  [MONITOR] {symbol}: exit ({reason}) FAILED — {res['error']}")
        send_ntfy(f"⚠️ Chart Bot: {symbol} exit failed", f"Reason: {reason}\nError: {res['error']}\nCheck the position manually.",
                  ntfy_topic, priority="urgent")
        return
    state[f"{trade_key}:exiting"]     = now_ist.strftime("%H:%M")
    state[f"{trade_key}:exit_reason"] = reason
    state[f"{trade_key}:gtt_id"]      = ""
    print(f"  [MONITOR] {symbol}: EXIT sent — {reason}")
    send_ntfy(f"Chart Bot: {symbol} exiting", f"Reason: {reason}", ntfy_topic, priority="default")


def _minutes_since(hhmm: str | None, now_ist: datetime.datetime) -> float:
    if not hhmm:
        return 0.0
    try:
        hh, mm = hhmm.split(":")
        t = now_ist.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        return (now_ist - t).total_seconds() / 60
    except Exception:
        return 0.0


def _monitor_positions(symbols: list[str], state: dict, api_key: str, access_token: str,
                       ntfy_topic: str, now_ist: datetime.datetime) -> None:
    """Manage every bot trade recorded today: close-out, partial booking, time stop,
    VWAP-cross exit, trailing SL and reversal alerts."""
    today = now_ist.strftime("%Y-%m-%d")
    kite = None if _SHADOW else KiteConnect(api_key=api_key)
    if kite:
        kite.set_access_token(access_token)

    for symbol in symbols:
        trade_key = f"{today}:{symbol}:trade"
        if not state.get(trade_key):
            continue

        if _SHADOW:
            # Paper trades are "held" for 60 min; outcomes are measured by signal_outcomes.py
            if _minutes_since(state.get(f"{trade_key}:entry_time"), now_ist) >= 60:
                _clear_trade(state, symbol, trade_key)
            continue

        bot_ts    = state.get(f"{trade_key}:ts")
        direction = state.get(f"{trade_key}:direction")

        if not has_open_position(kite, symbol, only_ts=bot_ts):
            _close_out(state, symbol, trade_key, today, now_ist, kite, ntfy_topic)
            continue

        exiting = state.get(f"{trade_key}:exiting")
        if exiting:
            waited = _minutes_since(exiting, now_ist)
            print(f"  [MONITOR] {symbol}: exit order pending for {waited:.0f} min")
            if waited >= 5 and not state.get(f"{trade_key}:exit_alerted"):
                state[f"{trade_key}:exit_alerted"] = True
                send_ntfy(f"⚠️ Chart Bot: {symbol} exit not filled",
                          f"{bot_ts} exit order still open after {waited:.0f} min and the GTT is cancelled.\n"
                          f"Check the position manually.", ntfy_topic, priority="urgent")
            continue

        entry_price  = float(state.get(f"{trade_key}:entry_price") or 0)
        qty          = int(state.get(f"{trade_key}:qty") or 0)
        t1           = float(state.get(f"{trade_key}:t1") or 0)
        partial_qty  = int(state.get(f"{trade_key}:partial_qty") or 0)
        partial_done = bool(state.get(f"{trade_key}:partial_done"))
        sl_step      = int(state.get(f"{trade_key}:sl_step") or 0)
        held_min     = _minutes_since(state.get(f"{trade_key}:entry_time"), now_ist)

        pnl_pct = position_pnl_pct(kite, symbol, only_ts=bot_ts)
        if pnl_pct is None or entry_price <= 0:
            continue
        ltp = entry_price * (1 + pnl_pct)
        print(f"  [MONITOR] {symbol}: {bot_ts} pnl={pnl_pct:+.1%} held={held_min:.0f}m "
              f"sl_step={sl_step} partial={'done' if partial_done else partial_qty}")

        # 1. Partial booking at T1 → book half, move the rest to breakeven
        if partial_qty and not partial_done and t1 > 0 and ltp >= t1:
            target_price = float(state.get(f"{trade_key}:target_price") or 0)
            gtt_id       = state.get(f"{trade_key}:gtt_id", "")
            remaining    = qty - partial_qty
            try:
                kite.delete_gtt(int(gtt_id))
                gtt_deleted = True
            except Exception as e:
                gtt_deleted = False
                _LOG.warning(f"[PARTIAL] {symbol}: GTT delete failed ({e}) — skipping partial this scan")
            if gtt_deleted:
                sold = sell_partial(kite, symbol, bot_ts, partial_qty)
                new_qty, new_sl = (remaining, _tick(entry_price)) if sold else (qty, float(state.get(f"{trade_key}:sl") or 0))
                try:
                    new_gtt = place_oco_gtt(kite, symbol, bot_ts, new_qty, new_sl, target_price, ltp)
                except Exception as e:
                    new_gtt = ""
                    send_ntfy(f"⚠️ Chart Bot: {symbol} UNPROTECTED", f"GTT re-placement failed after partial: {e}\nSet SL manually.",
                              ntfy_topic, priority="urgent")
                state[f"{trade_key}:gtt_id"] = new_gtt
                if sold:
                    state[f"{trade_key}:qty"]          = remaining
                    state[f"{trade_key}:partial_done"] = True
                    state[f"{trade_key}:sl_step"]      = max(sl_step, 1)
                    print(f"  [PARTIAL] {symbol}: booked {partial_qty} @ ~₹{ltp:.1f}, {remaining} left with SL at entry")
                    send_ntfy(f"💰 Chart Bot: {symbol} partial profit booked",
                              f"Sold {partial_qty} of {bot_ts} at ~₹{ltp:.1f} (T1 ₹{t1:.1f}).\n"
                              f"Remaining {remaining} — SL moved to entry ₹{entry_price:.1f}, target ₹{target_price:.1f}.",
                              ntfy_topic, priority="default")
            continue

        # 2. Time stop — option buyers lose to theta; exit trades that haven't moved
        if (sl_step == 0 and not partial_done and held_min >= TIME_STOP_MIN
                and pnl_pct < TIME_STOP_MIN_GAIN / 100):
            _exit_now(state, symbol, trade_key,
                      f"time stop — {held_min:.0f} min with {pnl_pct:+.1%} (needs ≥ +{TIME_STOP_MIN_GAIN:.0f}%)",
                      api_key, access_token, now_ist, ntfy_topic)
            continue

        # 3. VWAP-cross exit (Noise Area method) — the move has failed
        if VWAP_EXIT and direction and held_min >= VWAP_EXIT_MIN_HOLD:
            ns = noise_state(symbol, api_key, access_token)
            px, vwap = ns.get("price"), ns.get("vwap")
            if px and vwap and ((direction == "CE" and px < vwap) or (direction == "PE" and px > vwap)):
                _exit_now(state, symbol, trade_key,
                          f"spot {px:,.1f} crossed VWAP {vwap:,.1f} against the {direction} trade",
                          api_key, access_token, now_ist, ntfy_topic)
                continue

        # 4. Trailing SL
        _adjust_trailing_gtt(kite, symbol, state, trade_key, pnl_pct, ntfy_topic)

        # 5. Reversal alert
        if not direction:
            continue
        try:
            p_res = analyze_chart(symbol, PRIMARY_TF, api_key, access_token, closed_only=True)
        except Exception as e:
            print(f"  [MONITOR] {symbol}: chart fetch failed — {e}")
            continue
        if p_res.get("error"):
            continue
        cur_dir = _verdict_direction(p_res["verdict"]["verdict"])
        if cur_dir != "WAIT" and cur_dir != direction and not state.get(f"{trade_key}:reversal_alerted"):
            state[f"{trade_key}:reversal_alerted"] = True
            send_ntfy(
                f"⚠️ Chart Bot: {symbol} signal REVERSED",
                (
                    f"You have an open {direction} position ({bot_ts}).\n"
                    f"15m chart now shows: {p_res['verdict']['verdict']} ({p_res['verdict']['score']:+d})\n\n"
                    f"Consider reviewing / exiting your position."
                ),
                ntfy_topic,
                priority="urgent",
            )


# ── Single scan ───────────────────────────────────────────────────────────────

def scan_once(symbols: list[str], state_file: str, force: bool = False) -> None:
    api_key, access_token = _kite_creds()
    ntfy_topic = _ntfy_topic()

    if not (api_key and access_token):
        print("ERROR: KITE_API_KEY and KITE_ACCESS_TOKEN required.")
        sys.exit(1)

    now_ist = datetime.datetime.now(_IST)
    print(f"[{now_ist.strftime('%H:%M:%S IST')}] Chart bot scan{' [SHADOW]' if _SHADOW else ''} — {', '.join(symbols)}")

    if not force and now_ist.weekday() >= 5:  # 5=Saturday, 6=Sunday
        print("Weekend — market closed, skipping.")
        return

    if not force and not (MARKET_OPEN <= now_ist.time() <= MARKET_CLOSE):
        print("Market closed — skipping.")
        return

    state = _load_state(state_file)
    today = now_ist.strftime("%Y-%m-%d")

    # Prune date-prefixed keys from previous days
    stale_keys = [k for k in list(state.keys()) if k[:10].count("-") == 2 and k[:10] < today]
    if stale_keys:
        for k in stale_keys:
            del state[k]
        _save_state(state_file, state)
        print(f"  [STATE] Pruned {len(stale_keys)} stale entries from previous days")

    # Manage open positions before looking for new signals
    _monitor_positions(symbols, state, api_key, access_token, ntfy_topic, now_ist)
    _save_state(state_file, state)

    if not force and not (SIGNAL_OPEN <= now_ist.time() <= SIGNAL_CLOSE):
        print(f"Outside clean signal window ({SIGNAL_OPEN}–{SIGNAL_CLOSE} IST) — skipping new signals.")
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
        groups    = p_res["verdict"].get("groups", {})
        direction = _verdict_direction(verdict)

        marker = "🚀" if abs(score) >= 5 else ("⚡" if abs(score) >= 3 else "  ")
        print(f"  {marker} {symbol:12s} | {PRIMARY_TF} | {verdict:12s} | score {score:+d} | {conf}% conf | {groups}")

        rr_val = p_res["verdict"].get("rr")
        if rr_val is not None and rr_val < MIN_RR:
            print(f"         R:R {rr_val} < {MIN_RR} (info only)")

        if direction == "WAIT" or abs(score) < MIN_NOTIFY_SCORE:
            _append_chart_log(today, _base_log_entry(symbol, p_res, now_ist, auto_trade="no signal"))
            continue

        # Confirm on closed 5m candles
        try:
            c_res = analyze_chart(symbol, CONFIRM_TF, api_key, access_token, closed_only=True)
        except Exception as e:
            print(f"       5m confirm failed: {e}")
            c_res = {}

        c_verdict = c_res.get("verdict", {}).get("verdict", "WAIT") if c_res else "WAIT"
        c_score   = c_res.get("verdict", {}).get("score", 0) if c_res else 0
        tf_5m     = f"{c_verdict} ({c_score:+d})"
        print(f"         {CONFIRM_TF} confirm: {tf_5m}")

        # A genuine opposing 5m signal blocks even the notification
        if (direction == "CE" and c_score <= -MIN_NOTIFY_SCORE) or (direction == "PE" and c_score >= MIN_NOTIFY_SCORE):
            print(f"         5m score {c_score:+d} is an actual opposing signal — skipping {direction}")
            _append_chart_log(today, _base_log_entry(
                symbol, p_res, now_ist, tf_5m, f"blocked: 5m score {c_score:+d} is actual opposing signal"))
            continue

        # 30-min cooldown after a closed trade — skipped for STRONG BUY / STRONG SELL
        cooldown_key = f"{today}:{symbol}:sl_cooldown"
        if state.get(cooldown_key):
            left = 30 - _minutes_since(state[cooldown_key], now_ist)
            if left > 0 and abs(score) < 5:
                print(f"         [{symbol}] cooldown active — {left:.0f} min remaining")
                continue
            if left > 0:
                print(f"         [{symbol}] {verdict} — skipping cooldown, re-entry allowed")
            state.pop(cooldown_key, None)

        state_key = f"{today}:{symbol}:{direction}:{verdict}"
        trade_key = f"{today}:{symbol}:trade"
        if state.get(trade_key):
            print(f"         [{symbol}] bot position still open — managing it, no new entry")
            continue
        # Already notified for this exact signal: don't repeat the alert, but keep
        # re-checking the trade gates (e.g. noise breakout may come a few bars later)
        alerted = state.get(symbol) == state_key
        if alerted and not ((_AUTO_TRADE or _SHADOW) and symbol in _TRADE_SYMBOLS
                            and abs(score) >= MIN_TRADE_SCORE):
            print(f"         Already alerted for {direction} {verdict} — no change")
            continue

        ns = noise_state(symbol, api_key, access_token) if symbol in _TRADE_SYMBOLS else {"state": "n/a"}
        extra = [f"Noise    : {ns.get('state', 'unknown')}"
                 + (f" (band {ns['lower']:,.0f}–{ns['upper']:,.0f}, VWAP {ns['vwap']:,.0f})" if ns.get("upper") else "")]

        if alerted:
            print(f"         Already alerted — re-checking trade gates (noise={ns.get('state')})")
        else:
            title, message = _fmt_alert(symbol, p_res, c_res, extra)
            print(f"\n{'='*55}\n{message}\n{'='*55}\n")
            if ntfy_topic and not _SHADOW:
                print(f"  ntfy.sh: {'OK' if send_ntfy(title, message, ntfy_topic) else 'FAILED'}")

        log_entry = _base_log_entry(symbol, p_res, now_ist, tf_5m, "off")
        log_entry["noise"] = ns.get("state", "")

        if (_AUTO_TRADE or _SHADOW) and symbol in _TRADE_SYMBOLS and abs(score) >= MIN_TRADE_SCORE:
            skip = None
            sent_key = f"{state_key}:sent"
            if state.get(sent_key):
                skip = f"order for this signal already sent to Kite at {state[sent_key]} (rejected/unfilled)"
            elif not ((direction == "CE" and c_score >= 1) or (direction == "PE" and c_score <= -1)):
                skip = f"5m score {c_score:+d} not confirming {direction}"
            elif NOISE_GATE and ns.get("state") in ("above", "below", "inside"):
                want = "above" if direction == "CE" else "below"
                if ns["state"] != want:
                    skip = f"price {ns['state']} the noise band (needs {want}) — no trend breakout"
            elif p_res["summary"].get("adx", 0) < 20:
                # Noise data unavailable (or gate disabled) — fall back to the ADX trend filter
                skip = f"ADX {p_res['summary'].get('adx', 0):.0f} < 20 — no trend"

            if skip:
                print(f"  [AUTO-TRADE] SKIPPED — {skip}")
                log_entry["auto_trade"] = f"skipped: {skip}"
            else:
                spot      = p_res["summary"]["close"]
                chart_sl  = p_res["verdict"].get("stop_loss")
                chart_tgt = (p_res["verdict"].get("targets") or [None])[0]

                if _SHADOW:
                    print(f"  [SHADOW] would place {direction} for {symbol} @ {spot:.0f}")
                    log_entry["auto_trade"] = "shadow: would place"
                    state[trade_key] = "shadow"
                    state[f"{trade_key}:entry_time"] = now_ist.strftime("%H:%M")
                else:
                    print(f"  [AUTO-TRADE] {direction} order for {symbol} @ {spot:.0f} "
                          f"(chart SL={chart_sl} T={chart_tgt})")
                    trade = _place_order(
                        symbol, direction, spot, api_key, access_token,
                        chart_sl=chart_sl, chart_target=chart_tgt,
                        expiry_day_mode=EXPIRY_DAY_MODE,
                        partial_booking=PARTIAL_BOOKING, use_option_atr=True,
                    )
                    if "error" in trade:
                        print(f"  [AUTO-TRADE] FAILED — {trade['error']}")
                        log_entry["auto_trade"] = f"skipped: {trade['error']}"
                        if trade.get("sent"):
                            # Sent to Kite but rejected/unfilled — don't resend for the same signal
                            state[sent_key] = now_ist.strftime("%H:%M")
                            log_entry["order_id"] = trade.get("order_id", "")
                        if trade.get("sent") or not alerted:
                            send_ntfy(f"Chart Auto-Trade FAILED: {symbol}",
                                      f"Signal: {verdict}\nReason: {trade['error']}", ntfy_topic, priority="default")
                    else:
                        _record_trade(state, trade_key, trade, direction, now_ist)
                        log_entry.update({
                            "auto_trade":     "placed",
                            "tradingsymbol":  trade["tradingsymbol"],
                            "strike":         trade.get("strike", ""),
                            "qty":            trade.get("qty", ""),
                            "entry_premium":  trade.get("entry_price", ""),
                            "sl_premium":     trade.get("sl", ""),
                            "target_premium": trade.get("target", ""),
                            "t1_premium":     trade.get("t1", ""),
                            "expiry":         trade.get("expiry", ""),
                            "order_id":       trade.get("order_id", ""),
                        })
                        _notify_order(symbol, trade, ntfy_topic)
                        _mirror_sharekhan(symbol, direction, spot, trade, log_entry, ntfy_topic)

        elif _AUTO_TRADE and symbol not in _TRADE_SYMBOLS:
            log_entry["auto_trade"] = "signal only (no auto-trade for this symbol)"
        elif _AUTO_TRADE:
            log_entry["auto_trade"] = f"signal only (score {score:+d} < {MIN_TRADE_SCORE})"

        # Re-checks of an already-alerted signal are only logged when they lead to a trade
        if alerted and not (log_entry["auto_trade"] == "placed" or log_entry["auto_trade"].startswith("shadow")):
            continue
        _append_chart_log(today, log_entry)
        state[symbol] = state_key

    _save_state(state_file, state)
    print("Done.")


def _record_trade(state: dict, trade_key: str, trade: dict, direction: str,
                  now_ist: datetime.datetime) -> None:
    state[trade_key]                    = trade["order_id"]
    state[f"{trade_key}:ts"]           = trade["tradingsymbol"]
    state[f"{trade_key}:direction"]    = direction
    state[f"{trade_key}:gtt_id"]       = trade.get("gtt_id") or ""
    state[f"{trade_key}:entry_price"]  = trade.get("entry_price", 0)
    state[f"{trade_key}:sl"]           = trade.get("sl", 0)
    state[f"{trade_key}:t1"]           = trade.get("t1", 0)
    state[f"{trade_key}:target_price"] = trade.get("target", 0)
    state[f"{trade_key}:qty"]          = trade.get("qty", 0)
    state[f"{trade_key}:partial_qty"]  = trade.get("partial_qty", 0)
    state[f"{trade_key}:partial_done"] = False
    state[f"{trade_key}:sl_step"]      = 0
    state[f"{trade_key}:entry_time"]   = now_ist.strftime("%H:%M")


def _notify_order(symbol: str, trade: dict, ntfy_topic: str) -> None:
    print(f"  [AUTO-TRADE] ORDER — {trade['tradingsymbol']} | Entry ₹{trade['entry_price']} "
          f"| SL ₹{trade['sl']} | T1 ₹{trade.get('t1')} | Target ₹{trade['target']}")
    partial = (f"Book    : {trade['partial_qty']} at T1 ₹{trade['t1']}\n" if trade.get("partial_qty") else "")
    send_ntfy(
        f"Chart ORDER PLACED: {trade['tradingsymbol']}",
        (
            f"Symbol  : {symbol} {trade['opt_type']}\n"
            f"Strike  : {trade['strike']}  (expiry {trade.get('expiry', '')})\n"
            f"Qty     : {trade['qty']}\n"
            f"Entry   : ₹{trade['entry_price']}\n"
            f"SL      : ₹{trade['sl']}  ({trade.get('levels_source', '')})\n"
            f"{partial}"
            f"Target  : ₹{trade['target']}\n"
            f"Time stop: exit if < +{TIME_STOP_MIN_GAIN:.0f}% after {TIME_STOP_MIN:.0f} min\n"
            f"Order ID: {trade['order_id']}"
        ),
        ntfy_topic,
    )


def _mirror_sharekhan(symbol: str, direction: str, spot: float, trade: dict,
                      log_entry: dict, ntfy_topic: str) -> None:
    try:
        sk_result = _place_sk_order(
            symbol, direction, spot,
            option_ltp=trade.get("entry_price", 0),
            option_sl=trade.get("sl", 0),
            option_target=trade.get("target", 0),
        )
        if "error" in sk_result:
            print(f"  [SHAREKHAN] SKIPPED — {sk_result['error']}")
            send_ntfy(f"Sharekhan SKIPPED: {symbol}",
                      f"Kite order placed but Sharekhan failed.\nReason: {sk_result['error']}",
                      ntfy_topic, priority="default")
        else:
            print(f"  [SHAREKHAN] ORDER — {sk_result['tradingsymbol']} | ID: {sk_result['order_id']}")
            log_entry["sk_order_id"]      = sk_result["order_id"]
            log_entry["sk_tradingsymbol"] = sk_result["tradingsymbol"]
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


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Chart Analysis signal bot")
    ap.add_argument("symbols", nargs="*", default=SYMBOLS)
    ap.add_argument("--once", action="store_true", help="Single scan then exit")
    ap.add_argument("--state-file", default=None)
    ap.add_argument("--loop",        action="store_true", help="Run as daemon, scanning every --interval seconds")
    ap.add_argument("--interval",    type=int, default=300, help="Loop interval in seconds (default: 300)")
    ap.add_argument("--test-notify", action="store_true", help="Send test notification and exit")
    ap.add_argument("--force", action="store_true", help="Run scan even outside market hours")
    ap.add_argument("--shadow", action="store_true", help="Paper mode: full logic, no orders, no notifications")
    args = ap.parse_args()

    if args.shadow:
        _SHADOW = True
    state_file = args.state_file or str(_ROOT / ("chart_bot_shadow_state.json" if _SHADOW else "chart_bot_state.json"))
    symbols = [s.upper() for s in args.symbols]

    print(
        f"[chart_bot] AUTO_TRADE={'ON' if _AUTO_TRADE else 'OFF'}{' | SHADOW MODE' if _SHADOW else ''} | "
        f"qty: 95% of balance | no daily limits | "
        f"time stop {TIME_STOP_MIN:.0f}m | expiry {EXPIRY_DAY_MODE} | partial {PARTIAL_BOOKING} | "
        f"noise gate {NOISE_GATE} | vwap exit {VWAP_EXIT}",
        flush=True,
    )

    if args.test_notify:
        ntfy_topic = _ntfy_topic()
        now = datetime.datetime.now(_IST).strftime("%H:%M IST")
        body = (
            f"Chart Analysis Bot is running!\n"
            f"Time     : {now}\n"
            f"Watching : {', '.join(symbols)}\n"
            f"Strategy : closed 15m candles + 5m confirmation + Noise Area breakout\n"
            f"Quantity : 95% of available balance\n\n"
            f"Signals fire during market hours (09:30–15:00 IST)."
        )
        print(body)
        ok = send_ntfy("Chart Bot — Test Notification", body, ntfy_topic)
        print(f"ntfy.sh: {'OK' if ok else 'FAILED — check NTFY_TOPIC in .env'}")
    elif args.once:
        scan_once(symbols, state_file, force=args.force)
    elif args.loop:
        print(f"Chart bot loop started — scanning every {args.interval}s")
        while True:
            try:
                scan_once(symbols, state_file, force=args.force)
            except Exception as e:
                _LOG.exception(f"[loop] Unhandled error: {e}")
            time.sleep(args.interval)
    else:
        print("Use --once for cron mode, --loop for daemon mode, or --test-notify.")
        sys.exit(1)
