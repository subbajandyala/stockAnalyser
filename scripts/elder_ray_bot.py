"""
Elder Ray Signal Bot
====================
Two modes:

  LOCAL (default) — loop every N minutes while market is open:
      python scripts/elder_ray_bot.py
      python scripts/elder_ray_bot.py NIFTY BANKNIFTY --interval 3
      python scripts/elder_ray_bot.py --strong-only

  CLOUD / GitHub Actions (--once) — single scan then exit:
      python scripts/elder_ray_bot.py --once --state-file /tmp/er_state.json
      State file carries last-alerted signal per symbol across runs.

Telegram setup (any one of):
    - GitHub Secrets: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID  (cloud mode)
    - Environment vars: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    - ~/.kite_creds.json keys: "telegram_bot_token", "telegram_chat_id"

Set KITE_AUTO_TRADE=1 to enable auto order execution (NIFTY and SENSEX only,
STRONG signals only).
"""

import argparse
import datetime
import json
import os
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import requests

from kiteconnect import KiteConnect as _KC

from screener.elder_ray import run_elder_ray_signal, INDEX_CONFIG
from screener.kite_order import (
    place_order_for_signal  as _place_order,
    has_open_position       as _has_pos,
    position_pnl_pct        as _pnl_pct,
    exit_position           as _exit_pos,
    resolve_and_fetch_ltp   as _resolve_ltp,
    fetch_option_ltp        as _option_ltp,
)
from screener.cpr import load_cpr_for_symbol, cpr_confirms as _cpr_confirms

_AUTO_TRADE   = os.environ.get("KITE_AUTO_TRADE", "0") == "1"
_TRADE_SYMBOLS = {"NIFTY", "SENSEX"}   # BANKNIFTY excluded

_IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

DEFAULT_SYMBOLS  = ["NIFTY", "BANKNIFTY", "SENSEX"]
ALERT_SIGNALS    = {"STRONG BUY CE", "BUY CE", "STRONG BUY PE", "BUY PE"}
MARKET_OPEN      = datetime.time(9, 15)
MARKET_CLOSE     = datetime.time(15, 30)
NO_ENTRY_AFTER   = datetime.time(14, 30)   # no new orders after this time

# Reversal / profit-booking thresholds
_CONTRA_MIN_MINUTES = 30    # ignore contra signal within 30 min of entry
_PROFIT_BOOK_PCT    =  0.15  # book profit and go flat when position is up >= 15% on contra signal
_REVERSAL_LOSS_PCT  = -0.20  # only reverse (exit + re-enter) when position is losing > 20%

# Scalp notification targets (premium points)
_SCALP_TARGETS   = {"NIFTY": 10, "SENSEX": 20}
_SCALP_STOPS     = {"NIFTY": 7,  "SENSEX": 14}
_SCALP_EOD_CLOSE = datetime.time(14, 0)
_SCALP_SYMBOLS   = {"NIFTY", "SENSEX"}


# ── Credentials ───────────────────────────────────────────────────────────────

def _load_local_creds() -> dict:
    path = pathlib.Path.home() / ".kite_creds.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return {}


def _telegram_creds() -> tuple[str, str]:
    local = _load_local_creds()
    tok = (os.getenv("TELEGRAM_BOT_TOKEN") or
           local.get("telegram_bot_token", ""))
    cid = (os.getenv("TELEGRAM_CHAT_ID") or
           local.get("telegram_chat_id", ""))
    return tok, cid


def _ntfy_topic() -> str:
    local = _load_local_creds()
    return (os.getenv("NTFY_TOPIC") or local.get("ntfy_topic", ""))


# ── Notification senders ───────────────────────────────────────────────────────────────

def send_telegram(message: str, token: str, chat_id: str) -> bool:
    if not (token and chat_id):
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
            timeout=10,
        )
        return r.ok
    except Exception:
        return False


def send_ntfy(title: str, body: str, topic: str, priority: str = "high") -> bool:
    """Send push notification via ntfy.sh — free, no account needed."""
    if not topic:
        return False
    try:
        r = requests.post(
            f"https://ntfy.sh/{topic}",
            data=body.encode("utf-8"),
            headers={
                "Title":        title.encode("utf-8"),
                "Priority":     priority,
                "Tags":         "chart_increasing",
                "Content-Type": "text/plain; charset=utf-8",
            },
            timeout=10,
        )
        if not r.ok:
            print(f"  ntfy.sh HTTP {r.status_code}: {r.text[:200]}")
        return r.ok
    except Exception as e:
        print(f"  ntfy.sh exception: {e}")
        return False


def notify(title: str, message: str, tg_token: str, tg_chat: str, ntfy_topic: str) -> None:
    sent = False
    if tg_token and tg_chat:
        ok = send_telegram(message, tg_token, tg_chat)
        print(f"  Telegram : {'✅ sent' if ok else '❌ failed'}")
        sent = sent or ok
    if ntfy_topic:
        ok = send_ntfy(title, message, ntfy_topic)
        print(f"  ntfy.sh  : {'✅ sent' if ok else '❌ failed'}")
        sent = sent or ok
    if not sent:
        print("  No notification channel configured (Telegram or ntfy.sh)")


# ── Alert formatter ───────────────────────────────────────────────────────────────

def _fmt_alert(result: dict) -> str:
    signal    = result["signal"]
    symbol    = result["symbol"]
    price     = result["price"]
    score_ce  = result["score_ce"]
    score_pe  = result["score_pe"]
    direction = result.get("direction", "")
    step      = INDEX_CONFIG.get(symbol, {}).get("strike_step", 50)
    atm       = result.get("atm", round(price / step) * step)
    ist_now   = result.get("ist_now", datetime.datetime.now(_IST))

    emoji     = "🚀" if "STRONG" in signal else "⚡"
    dir_line  = "🟢 BUY CALL (CE)" if direction == "CE" else "🔴 BUY PUT (PE)"
    opt_label = f"{symbol} {int(atm)} {direction}"

    lines = [
        f"{emoji} *ELDER RAY — {symbol}*",
        f"🕐 {ist_now.strftime('%H:%M IST')}",
        f"",
        f"Signal : *{signal}*",
        f"Action : {dir_line}",
        f"Spot   : ₹{price:,.0f}   ATM: {int(atm)}",
        f"Score  : CE {score_ce} | PE {score_pe}",
        f"",
        f"📌 *Option to buy:* `{opt_label}` (nearest expiry)",
    ]

    # top 3 supporting factors
    factors   = result.get("factors", [])
    side_tag  = "BULL" if direction == "CE" else "BEAR"
    relevant  = [f for f in factors if f[2] == side_tag][:3]
    if relevant:
        lines.append("")
        lines.append("*Why:*")
        for name, desc, _, _ in relevant:
            lines.append(f"  • {name}: {desc}")

    lines += [
        "",
        "⚠️ Verify before executing. Not financial advice.",
    ]
    return "\n".join(lines)


# ── State management (for --once / GitHub Actions mode) ──────────────────────────────────

def _load_state(state_file: str) -> dict:
    try:
        return json.loads(pathlib.Path(state_file).read_text())
    except Exception:
        return {}


def _save_state(state_file: str, state: dict) -> None:
    pathlib.Path(state_file).write_text(json.dumps(state, indent=2))


# ── Daily trade log ───────────────────────────────────────────────────────────

def _trade_log_path(date_str: str) -> pathlib.Path:
    return pathlib.Path(f"/tmp/er_trade_log_{date_str}.json")


def _load_trade_log(date_str: str) -> dict:
    p = _trade_log_path(date_str)
    try:
        return json.loads(p.read_text()) if p.exists() else {"date": date_str, "signals": [], "kite_signals": [], "trades": []}
    except Exception:
        return {"date": date_str, "signals": [], "kite_signals": [], "trades": []}


def _save_trade_log(date_str: str, log: dict) -> None:
    _trade_log_path(date_str).write_text(json.dumps(log, indent=2))


def _log_signal(date_str: str, symbol: str, signal: str, spot: float, atm: int) -> None:
    log = _load_trade_log(date_str)
    log["signals"].append({
        "time":   datetime.datetime.now(_IST).strftime("%H:%M"),
        "symbol": symbol, "signal": signal,
        "spot":   round(spot, 0), "atm": atm,
    })
    _save_trade_log(date_str, log)


def _log_entry(date_str: str, symbol: str, direction: str, trade: dict) -> None:
    log = _load_trade_log(date_str)
    log["trades"].append({
        "id":              len(log["trades"]) + 1,
        "entry_time":      datetime.datetime.now(_IST).strftime("%H:%M"),
        "symbol":          symbol,
        "direction":       direction,
        "trade_mode":      trade.get("trade_mode", "scalp"),
        "tradingsymbol":   trade.get("tradingsymbol", ""),
        "strike":          trade.get("strike", 0),
        "qty":             trade.get("qty", 0),
        "entry_premium":   trade.get("entry_price", 0),
        "target_premium":  trade.get("target", 0),
        "stop_premium":    trade.get("sl", 0),
        "exit_time":       None,
        "exit_premium":    None,
        "exit_reason":     None,
        "pnl_pts":         None,
        "pnl_inr":         None,
    })
    _save_trade_log(date_str, log)


def _log_kite_attempt(
    date_str: str, symbol: str, direction: str, signal: str,
    spot: float, trade: dict,
) -> None:
    log = _load_trade_log(date_str)
    log.setdefault("kite_signals", [])
    entry: dict = {
        "id":        len(log["kite_signals"]) + 1,
        "time":      datetime.datetime.now(_IST).strftime("%H:%M"),
        "symbol":    symbol,
        "direction": direction,
        "signal":    signal,
        "spot":      round(spot, 0),
    }
    if "error" in trade:
        entry["status"] = "failed"
        entry["error"]  = trade["error"]
    else:
        entry["status"]        = "placed"
        entry["trade_mode"]    = trade.get("trade_mode", "scalp")
        entry["tradingsymbol"] = trade.get("tradingsymbol", "")
        entry["strike"]        = trade.get("strike", 0)
        entry["qty"]           = trade.get("qty", 0)
        entry["entry_premium"] = trade.get("entry_price", 0)
        entry["sl"]            = trade.get("sl", 0)
        entry["target"]        = trade.get("target", 0)
        entry["order_id"]      = trade.get("order_id", "")
        entry["gtt_id"]        = trade.get("gtt_id", "")
    log["kite_signals"].append(entry)
    _save_trade_log(date_str, log)


def _log_exit(date_str: str, tradingsymbol: str, exit_premium: float,
               exit_reason: str, lot_size: int) -> None:
    log = _load_trade_log(date_str)
    for t in reversed(log["trades"]):
        if t.get("tradingsymbol") == tradingsymbol and t.get("exit_time") is None:
            t["exit_time"]    = datetime.datetime.now(_IST).strftime("%H:%M")
            t["exit_premium"] = round(exit_premium, 1)
            t["exit_reason"]  = exit_reason
            pnl_pts           = round(exit_premium - t["entry_premium"], 1)
            t["pnl_pts"]      = pnl_pts
            t["pnl_inr"]      = round(pnl_pts * t["qty"], 0)
            break
    _save_trade_log(date_str, log)


def _trade_mode(state: dict, sym: str, direction: str, spot: float) -> str:
    """Return 'ride' if CPR confirms the signal, else 'scalp'."""
    cpr = state.get(f"{sym}:cpr")
    if not cpr:
        return "scalp"
    return "ride" if _cpr_confirms(spot, cpr, direction) else "scalp"


# ── Single scan (GitHub Actions / --once mode) ────────────────────────────────────────────

def scan_once(symbols: list[str], strong_only: bool, state_file: str) -> None:
    tg_token, tg_chat = _telegram_creds()
    ntfy_topic        = _ntfy_topic()
    watch = {"STRONG BUY CE", "STRONG BUY PE"} if strong_only else ALERT_SIGNALS

    state = _load_state(state_file)

    now_ist   = datetime.datetime.now(_IST)
    today_str = now_ist.strftime("%Y-%m-%d")

    # Kite creds (used by both scalp monitoring and auto-trade)
    _local        = _load_local_creds()
    _api_key      = os.getenv("KITE_API_KEY")      or _local.get("api_key", "plz6ik09bgb62mey")
    _access_token = os.getenv("KITE_ACCESS_TOKEN") or _local.get("access_token", "")

    # Reset all state at the start of each new trading day
    if state.get("date") != today_str:
        state = {"date": today_str}
        print(f"[{now_ist.strftime('%H:%M:%S IST')}] New trading day — state reset")

    # ── CPR: compute once per day per trade symbol ────────────────────────────
    if _AUTO_TRADE and _api_key and _access_token:
        from kiteconnect import KiteConnect as _KC2
        _kite_cpr = _KC2(api_key=_api_key)
        _kite_cpr.set_access_token(_access_token)
        for _sym in _TRADE_SYMBOLS:
            _cpr_key = f"{_sym}:cpr"
            if _cpr_key not in state:
                _cpr = load_cpr_for_symbol(_kite_cpr, _sym)
                if _cpr:
                    state[_cpr_key] = _cpr
                    _narrow = "NARROW" if _cpr["is_narrow"] else "WIDE"
                    print(
                        f"  CPR {_sym}: TC={_cpr['tc']} BC={_cpr['bc']}"
                        f"  width={_cpr['width_pct']}%  [{_narrow}]"
                    )
                else:
                    print(f"  CPR {_sym}: fetch failed — will trade as scalp")

    print(f"[{now_ist.strftime('%H:%M:%S IST')}] Elder Ray scan — {', '.join(symbols)}")

    if not (MARKET_OPEN <= now_ist.time() <= MARKET_CLOSE):
        print("Market closed — no scan needed.")
        return

    # ── Pass 1: collect all signals ───────────────────────────────────────────
    scan_results = {}
    for symbol in symbols:
        try:
            result = run_elder_ray_signal(symbol)
        except Exception as e:
            print(f"  {symbol}: ERROR — {e}")
            continue
        if result.get("error"):
            print(f"  {symbol}: {result['error']}")
            continue
        scan_results[symbol] = result
        signal = result.get("signal", "WAIT")
        sce    = result.get("score_ce", 0)
        spe    = result.get("score_pe", 0)
        price  = result.get("price", 0)
        marker = "🔥" if "STRONG" in signal else ("⚡" if signal in ALERT_SIGNALS else "  ")
        print(f"  {marker} {symbol:12s} | {signal:16s} | CE:{sce} PE:{spe} | ₹{price:,.0f}")

    # ── Pass 2: notifications ─────────────────────────────────────────────────
    new_scalp_alerts: dict = {}   # sym → result for symbols where a NEW alert just fired
    for symbol, result in scan_results.items():
        signal      = result.get("signal", "WAIT")
        prev_signal = state.get(symbol)
        if signal not in watch:
            if prev_signal in watch:
                state[symbol] = signal
            continue
        if signal == prev_signal:
            print(f"       {symbol}: already alerted — no change")
            continue
        message = _fmt_alert(result)
        print(f"\n{'='*55}\n{message}\n{'='*55}\n")
        notify(f"Elder Ray: {signal} — {symbol}", message, tg_token, tg_chat, ntfy_topic)
        state[symbol] = signal
        step = INDEX_CONFIG.get(symbol, {}).get("strike_step", 50)
        atm  = int(result.get("atm", round(result["price"] / step) * step))
        _log_signal(today_str, symbol, signal, result["price"], atm)
        if symbol in _SCALP_SYMBOLS:
            new_scalp_alerts[symbol] = result

    # ── Pass 3: scalp monitoring — auto-exit at 10 pts NIFTY / 20 pts SENSEX ──
    if _access_token:
        try:
            _kite_s = _KC(api_key=_api_key)
            _kite_s.set_access_token(_access_token)
        except Exception as _e:
            _kite_s = None
            print(f"  [SCALP] Kite client error: {_e}")

        if _kite_s:
            # 3a — lock entry premium for any newly fired strong signals
            for sym, res in new_scalp_alerts.items():
                direction = "CE" if res["signal"].endswith("CE") else "PE"
                step      = INDEX_CONFIG.get(sym, {}).get("strike_step", 50)
                atm       = int(res.get("atm", round(res["price"] / step) * step))
                ts, ltp   = _resolve_ltp(_kite_s, sym, atm, direction)
                if ts and ltp > 0:
                    state[f"{sym}:scalp_ts"]    = ts
                    state[f"{sym}:scalp_entry"] = ltp
                    state[f"{sym}:scalp_dir"]   = direction
                    state[f"{sym}:scalp_state"] = "watching"
                    tgt = _SCALP_TARGETS.get(sym, 10)
                    stp = _SCALP_STOPS.get(sym, 7)
                    print(f"  [SCALP] {sym} {direction} locked @ ₹{ltp:.1f}  target +{tgt}  stop -{stp}")

            # 3b — monitor open scalp positions → auto-exit when target / stop / EOD hit
            def _scalp_exit(sym: str, scalp_dir: str, scalp_entry: float,
                            cur: float, gain: float, reason: str) -> bool:
                """Auto-exit the position. Returns True on success."""
                gtt_id   = state.get(f"{sym}:gtt_id")
                exit_res = _exit_pos(sym, _api_key, _access_token, gtt_id)
                if "error" in exit_res:
                    print(f"  [SCALP] EXIT FAILED ({reason}): {exit_res['error']}")
                    if ntfy_topic:
                        send_ntfy(
                            f"⚠️ EXIT FAILED: {sym} {scalp_dir}",
                            f"Could not auto-exit ({reason}):\n{exit_res['error']}\n\nPlease exit manually!",
                            ntfy_topic, priority="urgent",
                        )
                    return False

                exits     = exit_res.get("exits", [])
                ts_label  = exits[0]["tradingsymbol"] if exits else sym
                print(f"  [SCALP] AUTO-EXITED {ts_label} | {reason} | P&L {gain:+.1f} pts")

                if reason == "target":
                    title = f"✅ BOOKED: {sym} {scalp_dir} +{gain:.0f} pts"
                    body  = (
                        f"Scalp target hit — position closed!\n\n"
                        f"{ts_label}\n"
                        f"Entry  : ₹{scalp_entry:.1f}\n"
                        f"Exit   : ₹{cur:.1f}\n"
                        f"Profit : +{gain:.0f} pts\n\n"
                        f"Watching for next signal to re-enter..."
                    )
                elif reason == "stop":
                    title = f"🛑 STOPPED: {sym} {scalp_dir} {gain:.0f} pts"
                    body  = (
                        f"Stop loss hit — position closed!\n\n"
                        f"{ts_label}\n"
                        f"Entry  : ₹{scalp_entry:.1f}\n"
                        f"Exit   : ₹{cur:.1f}\n"
                        f"Loss   : {gain:.0f} pts"
                    )
                else:
                    title = f"⏰ EOD CLOSED: {sym} {scalp_dir} {gain:+.0f} pts"
                    body  = (
                        f"2:00 PM EOD exit — position closed!\n\n"
                        f"{ts_label}\n"
                        f"Entry  : ₹{scalp_entry:.1f}\n"
                        f"Exit   : ₹{cur:.1f}\n"
                        f"P&L    : {gain:+.0f} pts"
                    )
                if ntfy_topic:
                    send_ntfy(title, body, ntfy_topic,
                              priority="urgent" if reason == "target" else "high")

                # Log the exit
                from screener.kite_order import LOT_SIZE as _LOT
                _log_exit(today_str, scalp_ts, cur, reason, _LOT.get(sym, 65))

                # Clear full position state so Pass 4 can re-enter fresh
                for _k in (f"{sym}:gtt_id", f"{sym}:trade", f"{sym}:entry_time",
                           f"{sym}:entry_direction", f"{sym}:entry_price",
                           f"{sym}:last_trade_signal", f"{sym}:exit_time",
                           f"{sym}:scalp_ts", f"{sym}:scalp_entry",
                           f"{sym}:scalp_dir"):
                    state.pop(_k, None)
                state[f"{sym}:scalp_state"] = "booked"
                return True

            for sym in _SCALP_SYMBOLS:
                scalp_state  = state.get(f"{sym}:scalp_state")
                scalp_ts     = state.get(f"{sym}:scalp_ts", "")
                scalp_entry  = float(state.get(f"{sym}:scalp_entry", 0))
                scalp_dir    = state.get(f"{sym}:scalp_dir", "")
                tgt          = _SCALP_TARGETS.get(sym, 10)
                stp          = _SCALP_STOPS.get(sym, 7)

                if scalp_state == "watching" and scalp_ts and scalp_entry > 0:
                    cur = _option_ltp(_kite_s, sym, scalp_ts)
                    if cur > 0:
                        gain = round(cur - scalp_entry, 1)
                        print(f"  [SCALP] {sym} {scalp_dir}: ₹{scalp_entry} → ₹{cur} ({gain:+.1f} pts)")

                        # Only auto-exit if BOT placed the position; never touch manual trades
                        bot_placed = bool(state.get(f"{sym}:trade"))
                        has_pos = _has_pos(_kite_s, sym) if bot_placed else False

                        if gain >= tgt:
                            if has_pos:
                                _scalp_exit(sym, scalp_dir, scalp_entry, cur, gain, "target")
                            else:
                                # No auto-trade position — notify only
                                if ntfy_topic:
                                    send_ntfy(
                                        f"🎯 BOOK NOW: {sym} {scalp_dir} +{gain:.0f} pts",
                                        f"Target hit!\n\n{sym} {scalp_dir}\nEntry  : ₹{scalp_entry:.1f}\nNow    : ₹{cur:.1f}\nGain   : +{gain:.0f} pts\n\nBook your position now!",
                                        ntfy_topic, priority="urgent",
                                    )
                                state[f"{sym}:scalp_state"] = "booked"

                        elif gain <= -stp:
                            if has_pos:
                                _scalp_exit(sym, scalp_dir, scalp_entry, cur, gain, "stop")
                            else:
                                if ntfy_topic:
                                    send_ntfy(
                                        f"🛑 STOP HIT: {sym} {scalp_dir} {gain:.0f} pts",
                                        f"Stop hit!\n\n{sym} {scalp_dir}\nEntry  : ₹{scalp_entry:.1f}\nNow    : ₹{cur:.1f}\nLoss   : {gain:.0f} pts\n\nExit now!",
                                        ntfy_topic, priority="urgent",
                                    )
                                for _k in (f"{sym}:scalp_ts", f"{sym}:scalp_entry",
                                           f"{sym}:scalp_dir", f"{sym}:scalp_state"):
                                    state.pop(_k, None)

                        elif now_ist.time() >= _SCALP_EOD_CLOSE:
                            if has_pos:
                                _scalp_exit(sym, scalp_dir, scalp_entry, cur, gain, "eod")
                            else:
                                if ntfy_topic:
                                    send_ntfy(
                                        f"⏰ EOD EXIT: {sym} {scalp_dir} {gain:+.0f} pts",
                                        f"2:00 PM — close your position!\n\n{sym} {scalp_dir}\nEntry  : ₹{scalp_entry:.1f}\nNow    : ₹{cur:.1f}\nP&L    : {gain:+.0f} pts",
                                        ntfy_topic, priority="high",
                                    )
                                for _k in (f"{sym}:scalp_ts", f"{sym}:scalp_entry",
                                           f"{sym}:scalp_dir", f"{sym}:scalp_state"):
                                    state.pop(_k, None)

                elif scalp_state == "booked":
                    # Pass 4 (auto-trade) handles re-entry via fresh signal detection.
                    # Here we just reset scalp_state when a new strong signal confirms
                    # so the next entry locks in a fresh premium.
                    res = scan_results.get(sym, {})
                    if "STRONG" in res.get("signal", ""):
                        state.pop(f"{sym}:scalp_state", None)

    # ── Pass 4: auto-trade ────────────────────────────────────────────────────
    if _AUTO_TRADE:
        if now_ist.time() >= NO_ENTRY_AFTER:
            print(f"  [AUTO-TRADE] Past {NO_ENTRY_AFTER.strftime('%H:%M')} IST cutoff — no new orders")
        else:
            _kite = _KC(api_key=_api_key)
            _kite.set_access_token(_access_token)  # creds already loaded above

            # ── Detect closed positions and unlock re-entry after 30 min ─────
            for sym in list(_TRADE_SYMBOLS):
                if not state.get(f"{sym}:entry_direction"):
                    continue  # no trade recorded today for this symbol
                if _has_pos(_kite, sym):
                    continue  # position still open, nothing to do

                # Position is gone (manual exit or GTT triggered)
                exit_time_str = state.get(f"{sym}:exit_time")
                if not exit_time_str:
                    # First scan after position closed — start cooldown
                    state[f"{sym}:exit_time"] = now_ist.isoformat()
                    print(f"  [AUTO-TRADE] {sym}: position closed — 30min cooldown before re-entry")
                    if ntfy_topic:
                        send_ntfy(f"Position closed: {sym}", "30min cooldown started before next re-entry.", ntfy_topic, priority="default")
                    continue

                # Check if cooldown has passed
                try:
                    exit_time = datetime.datetime.fromisoformat(exit_time_str)
                    minutes_since_exit = (now_ist - exit_time).total_seconds() / 60
                except Exception:
                    minutes_since_exit = 9999

                if minutes_since_exit < 30:
                    remaining = int(30 - minutes_since_exit)
                    print(f"  [AUTO-TRADE] {sym}: cooldown after exit — {remaining}min remaining")
                    continue

                # 30 min passed — clear all blocks, allow fresh re-entry
                print(f"  [AUTO-TRADE] {sym}: cooldown complete — re-entry unlocked")
                for k in (f"{sym}:last_trade_signal", f"{sym}:entry_direction",
                          f"{sym}:entry_price", f"{sym}:gtt_id", f"{sym}:trade", f"{sym}:exit_time"):
                    state.pop(k, None)

            tradeable       = {}  # sym → res  (no open position, fresh entry)
            profit_bookings = {}  # sym → res  (in profit >= 15%, contra signal → book and go flat)
            reversals       = {}  # sym → res  (losing > 20%, contra signal → exit + re-enter)

            for sym, res in scan_results.items():
                if sym not in _TRADE_SYMBOLS:
                    continue
                if "STRONG" not in res.get("signal", ""):
                    continue

                new_dir = "CE" if res["signal"].endswith("CE") else "PE"

                if not _has_pos(_kite, sym):
                    # No open position — skip if we already attempted this signal today
                    if res["signal"] == state.get(f"{sym}:last_trade_signal"):
                        print(f"       {sym}: already attempted today — skipping")
                        continue
                    tradeable[sym] = res
                else:
                    # Open position exists — check direction
                    existing_dir = state.get(f"{sym}:entry_direction", "")
                    if not existing_dir or new_dir == existing_dir:
                        print(f"  [AUTO-TRADE] {sym}: already in {existing_dir or '?'} — holding")
                        continue

                    # Contra signal — check reversal eligibility
                    entry_time_str    = state.get(f"{sym}:entry_time", "")
                    minutes_since_entry = 9999.0
                    if entry_time_str:
                        try:
                            et = datetime.datetime.fromisoformat(entry_time_str)
                            minutes_since_entry = (now_ist - et).total_seconds() / 60
                        except Exception:
                            pass

                    if minutes_since_entry < _CONTRA_MIN_MINUTES:
                        print(
                            f"  [AUTO-TRADE] {sym}: contra {new_dir} signal only "
                            f"{minutes_since_entry:.0f}m after entry — noise filter, ignoring"
                        )
                        continue

                    # Check current P&L of open position
                    pnl = _pnl_pct(_kite, sym)
                    if pnl is None:
                        # Position gone — GTT must have triggered; treat as fresh
                        print(f"  [AUTO-TRADE] {sym}: position gone (GTT triggered?) — treating as fresh entry")
                        tradeable[sym] = res
                        continue

                    # In profit >= 15% with a contra signal → book profit, go flat
                    if pnl >= _PROFIT_BOOK_PCT:
                        print(
                            f"  [AUTO-TRADE] {sym}: contra {new_dir} signal — P&L {pnl:+.0%} "
                            f"— BOOKING PROFIT (signal reversed, locking in gains)"
                        )
                        profit_bookings[sym] = res
                        continue

                    if pnl > _REVERSAL_LOSS_PCT:
                        print(
                            f"  [AUTO-TRADE] {sym}: contra {new_dir} signal — P&L {pnl:+.0%} "
                            f"— holding (need <{_REVERSAL_LOSS_PCT:.0%} to reverse)"
                        )
                        continue

                    # Losing > 20% after 30+ min with contra signal — eligible for reversal
                    print(
                        f"  [AUTO-TRADE] {sym}: REVERSAL — {existing_dir}→{new_dir} "
                        f"| P&L {pnl:+.0%} | {minutes_since_entry:.0f}m since entry"
                    )
                    reversals[sym] = res

            # ── Book profits (exit flat, no re-entry) ────────────────────────
            for sym, res in profit_bookings.items():
                old_dir = state.get(f"{sym}:entry_direction", "?")
                gtt_id  = state.get(f"{sym}:gtt_id")

                exit_res = _exit_pos(sym, _api_key, _access_token, gtt_id)
                if "error" in exit_res:
                    print(f"  [AUTO-TRADE] {sym}: PROFIT-BOOK EXIT FAILED — {exit_res['error']}")
                    if ntfy_topic:
                        send_ntfy(f"PROFIT BOOK FAILED: {sym}", f"Could not exit: {exit_res['error']}", ntfy_topic, priority="high")
                    continue

                pnl = _pnl_pct(_kite, sym) or 0.0  # best-effort final P&L for the alert
                print(f"  [AUTO-TRADE] {sym}: PROFIT BOOKED — exited {old_dir} at ~{pnl:+.0%} | going flat")
                if ntfy_topic:
                    exits = exit_res.get("exits", [])
                    ts_label = exits[0]["tradingsymbol"] if exits else sym
                    send_ntfy(
                        f"PROFIT BOOKED: {ts_label}",
                        (
                            f"Exited {old_dir} position\n"
                            f"Reason : contra signal fired, locking gains\n"
                            f"P&L    : ~{pnl:+.0%} on premium\n"
                            f"Now    : FLAT — no new entry"
                        ),
                        ntfy_topic,
                    )

                # Clear position state; last_trade_signal marks signal as done for today
                new_dir = "CE" if res["signal"].endswith("CE") else "PE"
                for k in (f"{sym}:entry_time", f"{sym}:entry_direction", f"{sym}:entry_price",
                          f"{sym}:gtt_id", f"{sym}:trade"):
                    state.pop(k, None)
                state[f"{sym}:last_trade_signal"] = res["signal"]

            # ── Execute reversals (exit + re-enter, independent of conflict logic) ──
            for sym, res in reversals.items():
                new_dir  = "CE" if res["signal"].endswith("CE") else "PE"
                old_dir  = state.get(f"{sym}:entry_direction", "?")
                gtt_id   = state.get(f"{sym}:gtt_id")

                exit_res = _exit_pos(sym, _api_key, _access_token, gtt_id)
                if "error" in exit_res:
                    print(f"  [AUTO-TRADE] {sym}: EXIT FAILED — {exit_res['error']} — not flipping")
                    if ntfy_topic:
                        send_ntfy(f"EXIT FAILED: {sym}", f"Reversal exit failed:\n{exit_res['error']}", ntfy_topic, priority="high")
                    continue

                print(f"  [AUTO-TRADE] {sym}: exited {old_dir} — now entering {new_dir}")
                if ntfy_topic:
                    send_ntfy(f"REVERSAL: {sym} {old_dir}→{new_dir}", f"Exited {old_dir} (loss >20%)\nNow entering {new_dir}...", ntfy_topic)

                # Clear old position state
                for k in (f"{sym}:entry_time", f"{sym}:entry_direction", f"{sym}:entry_price",
                          f"{sym}:gtt_id", f"{sym}:trade", f"{sym}:last_trade_signal"):
                    state.pop(k, None)

                spot  = res.get("price", 0)
                mode  = _trade_mode(state, sym, new_dir, spot)
                trade = _place_order(sym, new_dir, spot, _api_key, _access_token, 1.0, mode)
                _log_kite_attempt(today_str, sym, new_dir, res["signal"], spot, trade)
                print(f"  [AUTO-TRADE] {sym}: trade_mode={mode}")
                if "error" in trade:
                    print(f"  [AUTO-TRADE] {sym}: reversal re-entry FAILED — {trade['error']}")
                    if ntfy_topic:
                        send_ntfy(f"REVERSAL RE-ENTRY FAILED: {sym}", trade["error"], ntfy_topic, priority="high")
                    state[f"{sym}:last_trade_signal"] = res["signal"]
                else:
                    print(
                        f"  [AUTO-TRADE] REVERSAL ORDER — {trade['tradingsymbol']}"
                        f" | Entry ₹{trade['entry_price']} | SL ₹{trade['sl']} | Target ₹{trade['target']}"
                    )
                    if ntfy_topic:
                        send_ntfy(
                            f"REVERSAL ORDER: {trade['tradingsymbol']}",
                            (
                                f"Reversed: {old_dir} → {new_dir}\n"
                                f"Strike  : {trade['strike']}\n"
                                f"Qty     : {trade['qty']}\n"
                                f"Entry   : Rs.{trade['entry_price']}\n"
                                f"SL      : Rs.{trade['sl']} (scalp stop)\n"
                                f"Target  : Rs.{trade['target']} (scalp target)\n"
                                f"GTT ID  : {trade.get('gtt_id', 'N/A')}"
                            ),
                            ntfy_topic,
                        )
                    state[f"{sym}:trade"]             = trade["order_id"]
                    state[f"{sym}:last_trade_signal"] = res["signal"]
                    state[f"{sym}:gtt_id"]            = trade.get("gtt_id")
                    state[f"{sym}:entry_time"]        = now_ist.isoformat()
                    state[f"{sym}:entry_direction"]   = new_dir
                    state[f"{sym}:entry_price"]       = trade.get("entry_price", 0)
                    _log_entry(today_str, sym, new_dir, trade)

            # ── Fresh entries with conflict detection + capital split ─────────
            if tradeable:
                directions  = {sym: ("CE" if res["signal"].endswith("CE") else "PE")
                               for sym, res in tradeable.items()}
                unique_dirs = set(directions.values())

                if len(unique_dirs) > 1:
                    conflict_msg = " vs ".join(f"{s}:{d}" for s, d in directions.items())
                    print(f"  [AUTO-TRADE] CONFLICT — {conflict_msg} — skipping all")
                    if ntfy_topic:
                        send_ntfy("Auto-Trade CONFLICT", f"Conflicting signals: {conflict_msg}\nNo orders placed.", ntfy_topic, priority="default")
                else:
                    n        = len(tradeable)
                    fraction = 1.0 / n
                    print(f"  [AUTO-TRADE] {n} fresh signal(s) — capital split {fraction:.0%} each")

                    for sym, res in tradeable.items():
                        direction = directions[sym]
                        spot      = res.get("price", 0)
                        mode      = _trade_mode(state, sym, direction, spot)
                        print(f"  [AUTO-TRADE] {sym} {direction} @ spot {spot:.0f} ({fraction:.0%} capital) [{mode}]")
                        trade = _place_order(sym, direction, spot, _api_key, _access_token, fraction, mode)
                        _log_kite_attempt(today_str, sym, direction, res["signal"], spot, trade)
                        if "error" in trade:
                            error_msg = trade["error"]
                            print(f"  [AUTO-TRADE] SKIPPED — {error_msg}")
                            if ntfy_topic:
                                send_ntfy(f"Auto-Trade SKIPPED: {sym}", f"Signal: {res['signal']}\nReason: {error_msg}", ntfy_topic, priority="default")
                            # Re-fire fix: mark attempted unless it was blocked by an existing position
                            if "already exists" not in error_msg:
                                state[f"{sym}:last_trade_signal"] = res["signal"]
                        else:
                            print(
                                f"  [AUTO-TRADE] ORDER PLACED — {trade['tradingsymbol']}"
                                f" | Qty {trade['qty']} | Entry ₹{trade['entry_price']}"
                                f" | SL ₹{trade['sl']} | Target ₹{trade['target']}"
                                f" | GTT {trade.get('gtt_id', 'N/A')}"
                            )
                            if ntfy_topic:
                                send_ntfy(
                                    f"ORDER PLACED: {trade['tradingsymbol']}",
                                    (
                                        f"Symbol  : {sym} {trade['opt_type']}\n"
                                        f"Strike  : {trade['strike']}\n"
                                        f"Qty     : {trade['qty']} ({fraction:.0%} capital)\n"
                                        f"Entry   : Rs.{trade['entry_price']}\n"
                                        f"SL      : Rs.{trade['sl']} (scalp stop)\n"
                                        f"Target  : Rs.{trade['target']} (scalp target)\n"
                                        f"GTT ID  : {trade.get('gtt_id', 'N/A')}\n"
                                        f"Order ID: {trade['order_id']}"
                                    ),
                                    ntfy_topic,
                                )
                            state[f"{sym}:trade"]             = trade["order_id"]
                            state[f"{sym}:last_trade_signal"] = res["signal"]
                            state[f"{sym}:gtt_id"]            = trade.get("gtt_id")
                            state[f"{sym}:entry_time"]        = now_ist.isoformat()
                            state[f"{sym}:entry_direction"]   = direction
                            state[f"{sym}:entry_price"]       = trade.get("entry_price", 0)
                            _log_entry(today_str, sym, direction, trade)

    _save_state(state_file, state)
    print("State saved.")


# ── Continuous loop (local mode) ───────────────────────────────────────────────────────────────

def run_loop(symbols: list[str], interval_minutes: int, strong_only: bool) -> None:
    tg_token, tg_chat = _telegram_creds()
    ntfy_topic        = _ntfy_topic()
    watch      = {"STRONG BUY CE", "STRONG BUY PE"} if strong_only else ALERT_SIGNALS
    last_signal    = {s: None for s in symbols}
    last_alert_ts  = {s: None for s in symbols}
    interval_sec   = interval_minutes * 60

    print(f"🤖 Elder Ray Bot  |  {', '.join(symbols)}  |  every {interval_minutes}m")
    print(f"   Watching : {', '.join(sorted(watch))}")
    print(f"   Telegram : {'✅' if (tg_token and tg_chat) else '❌ not configured'}")
    print(f"   ntfy.sh  : {'✅ topic=' + ntfy_topic if ntfy_topic else '❌ not configured'}")
    print()

    while True:
        now_ist = datetime.datetime.now(_IST)

        if not (MARKET_OPEN <= now_ist.time() <= MARKET_CLOSE):
            next_open = now_ist.replace(hour=9, minute=15, second=0, microsecond=0)
            if now_ist.time() > MARKET_CLOSE:
                next_open += datetime.timedelta(days=1)
            wait = int((next_open - now_ist).total_seconds())
            print(f"[{now_ist.strftime('%H:%M IST')}] Market closed — next open in {wait//3600}h {(wait%3600)//60}m")
            time.sleep(min(300, max(60, wait)))
            continue

        print(f"\n[{now_ist.strftime('%H:%M:%S IST')}] Scanning...")

        for symbol in symbols:
            try:
                result = run_elder_ray_signal(symbol)
            except Exception as e:
                print(f"  {symbol}: ERROR — {e}")
                continue

            if result.get("error"):
                print(f"  {symbol}: {result['error']}")
                continue

            signal = result.get("signal", "WAIT")
            sce    = result.get("score_ce", 0)
            spe    = result.get("score_pe", 0)
            price  = result.get("price", 0)
            marker = "🔥" if "STRONG" in signal else ("⚡" if signal in ALERT_SIGNALS else "  ")
            print(f"  {marker} {symbol:12s} | {signal:16s} | CE:{sce} PE:{spe} | ₹{price:,.0f}")

            if signal not in watch:
                if last_signal[symbol] in watch:
                    last_signal[symbol] = signal
                continue

            cooldown_ok = (
                last_signal[symbol] != signal or
                last_alert_ts[symbol] is None or
                (now_ist - last_alert_ts[symbol]).total_seconds() > 1800
            )
            if not cooldown_ok:
                ago = int((now_ist - last_alert_ts[symbol]).total_seconds() / 60)
                print(f"       (cooldown — alerted {ago}m ago)")
                continue

            message = _fmt_alert(result)
            print(f"\n{'='*55}\n{message}\n{'='*55}\n")

            title = f"Elder Ray: {signal} — {symbol}"
            notify(title, message, tg_token, tg_chat, ntfy_topic)

            last_signal[symbol]   = signal
            last_alert_ts[symbol] = now_ist

        # countdown to next scan
        for r in range(interval_sec, 0, -30):
            print(f"  next poll in {r}s  [{datetime.datetime.now(_IST).strftime('%H:%M:%S IST')}]", end="\r")
            time.sleep(min(30, r))
        print()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Elder Ray signal monitor")
    ap.add_argument("symbols", nargs="*", default=DEFAULT_SYMBOLS,
                    help=f"Symbols (default: {' '.join(DEFAULT_SYMBOLS)})")
    ap.add_argument("--interval", "-i", type=int, default=5,
                    help="Poll interval minutes (local loop mode, default 5)")
    ap.add_argument("--strong-only", action="store_true",
                    help="Alert only STRONG BUY CE / STRONG BUY PE")
    ap.add_argument("--once", action="store_true",
                    help="Single scan then exit (for GitHub Actions / cron)")
    ap.add_argument("--state-file", default="/tmp/er_state.json",
                    help="State file path for --once mode (default: /tmp/er_state.json)")
    ap.add_argument("--test-notify", action="store_true",
                    help="Send a test notification and exit (to verify ntfy/Telegram is working)")
    ap.add_argument("--test-order", action="store_true",
                    help="Place a test NIFTY CE market order in Kite (bypasses signal/market-hours check)")
    args = ap.parse_args()

    symbols = [s.upper() for s in args.symbols]
    for s in symbols:
        if s not in INDEX_CONFIG:
            print(f"Unknown symbol: {s}. Valid: {list(INDEX_CONFIG.keys())}")
            sys.exit(1)

    if args.test_order:
        from kiteconnect import KiteConnect
        _local = _load_local_creds()
        api_key      = os.getenv("KITE_API_KEY")      or _local.get("api_key", "plz6ik09bgb62mey")
        access_token = os.getenv("KITE_ACCESS_TOKEN") or _local.get("access_token", "")
        ntfy_topic   = _ntfy_topic()
        try:
            outbound_ip = requests.get("https://api.ipify.org", timeout=5).text.strip()
            print(f"[TEST ORDER] Outbound IP: {outbound_ip}")
        except Exception:
            print("[TEST ORDER] Could not determine outbound IP")
        if not access_token:
            print("ERROR: KITE_ACCESS_TOKEN required")
            sys.exit(1)
        kite = KiteConnect(api_key=api_key)
        kite.set_access_token(access_token)
        spot = 25000.0
        try:
            q = kite.ltp(["NSE:NIFTY 50"])
            spot = float(q["NSE:NIFTY 50"]["last_price"])
        except Exception as e:
            print(f"LTP fetch failed ({e}) — using fallback spot {spot:.0f}")
        print(f"[TEST ORDER] NIFTY CE @ spot {spot:.0f}")
        trade = _place_order("NIFTY", "CE", spot, api_key, access_token)
        if "error" in trade:
            print(f"[TEST ORDER] Result: {trade['error']}")
        else:
            print(
                f"[TEST ORDER] ORDER PLACED — {trade['tradingsymbol']}"
                f" | Entry ₹{trade.get('entry_price', 0)}"
                f" | SL ₹{trade.get('sl', 0)}"
                f" | Target ₹{trade.get('target', 0)}"
                f" | GTT {trade.get('gtt_id', 'N/A')}"
                f" | Order ID: {trade['order_id']}"
            )
            if ntfy_topic:
                send_ntfy(
                    f"TEST ORDER: {trade['tradingsymbol']}",
                    (
                        f"[TEST] Symbol  : NIFTY CE\n"
                        f"Strike  : {trade['strike']}\n"
                        f"Qty     : {trade['qty']}\n"
                        f"Entry   : Rs.{trade.get('entry_price', 0)}\n"
                        f"SL      : Rs.{trade.get('sl', 0)} (scalp stop)\n"
                        f"Target  : Rs.{trade.get('target', 0)} (scalp target)\n"
                        f"GTT ID  : {trade.get('gtt_id', 'N/A')}\n"
                        f"Order ID: {trade['order_id']}"
                    ),
                    ntfy_topic,
                )
    elif args.test_notify:
        tg_token, tg_chat = _telegram_creds()
        ntfy_topic = _ntfy_topic()
        now = datetime.datetime.now(_IST).strftime("%H:%M IST")
        title = "Elder Ray Bot — Test Notification"
        body  = (
            f"✅ *Elder Ray Bot is working!*\n"
            f"Time: {now}\n"
            f"Watching: {', '.join(symbols)}\n\n"
            f"You will receive alerts here when STRONG BUY CE or BUY CE signals fire during market hours (09:15–15:30 IST)."
        )
        print(body)
        notify(title, body, tg_token, tg_chat, ntfy_topic)
    elif args.once:
        scan_once(symbols, args.strong_only, args.state_file)
    else:
        try:
            run_loop(symbols, args.interval, args.strong_only)
        except KeyboardInterrupt:
            print("\nBot stopped.")
