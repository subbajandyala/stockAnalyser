"""
Chart Analysis Signal Bot
=========================
Runs analyze_chart() for each symbol/timeframe and sends a push notification
via ntfy.sh when a tradeable signal fires (STRONG BUY / BUY / STRONG SELL / SELL).

Signal → Option direction:
  STRONG BUY / BUY  → BUY CE
  STRONG SELL / SELL → BUY PE

Multi-timeframe: scans 15m first, confirms with 5m entry timing.
A signal only fires when BOTH timeframes agree in direction.

Set KITE_AUTO_TRADE=1 to enable auto order execution.

Cron (every 5 min, market hours):
  */5 9-15 * * 1-5 cd /root/stockAnalyser && /root/venv/bin/python -u scripts/chart_bot.py --once >> chart_bot.log 2>&1

Single test run:
  /root/venv/bin/python -u scripts/chart_bot.py --once
  /root/venv/bin/python -u scripts/chart_bot.py --test-notify
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
    has_open_position,
    place_order_for_signal as _place_order,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

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
# Minimum score to auto-execute an order
MIN_TRADE_SCORE  = 5

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
print(f"[chart_bot] AUTO_TRADE={'ON' if _AUTO_TRADE else 'OFF (set KITE_AUTO_TRADE=1 in .env to enable)'}", flush=True)


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
    lines += [
        f"",
        f"15m : {verdict} ({score:+d})",
        f"5m  : {c_verdict} ({c_score:+d})",
        f"",
    ]

    ok_reasons  = [r["text"] for r in reasons if r.get("ok") is True][:4]
    bad_reasons = [r["text"] for r in reasons if r.get("ok") is False][:2]
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
    today = now_ist.strftime("%Y%m%d")

    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)

    for symbol in symbols:
        trade_key = f"{today}:{symbol}:trade"
        order_id  = state.get(trade_key)
        if not order_id:
            continue  # no recorded trade for this symbol today

        # Look up original direction from the alerted state key
        # state[symbol] = "YYYYMMDD:symbol:direction:verdict"
        state_val = state.get(symbol, "")
        parts = state_val.split(":")
        recorded_direction = parts[2] if len(parts) >= 4 else None

        position_open = has_open_position(kite, symbol)

        if not position_open:
            print(f"  [MONITOR] {symbol}: position closed (GTT hit) — re-entry unlocked")
            del state[trade_key]
            if ntfy_topic:
                send_ntfy(
                    f"Chart Bot: {symbol} position closed",
                    (
                        f"The GTT was triggered (SL or Target hit).\n"
                        f"Re-entry unlocked — bot will enter again on the next strong signal."
                    ),
                    ntfy_topic,
                    priority="default",
                )
            continue

        # Position still open — check if 15m signal has reversed
        if not recorded_direction:
            continue
        try:
            p_res = analyze_chart(symbol, PRIMARY_TF, api_key, access_token)
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

        if current_direction != recorded_direction:
            print(
                f"  [MONITOR] {symbol}: SIGNAL REVERSED — was {recorded_direction}, "
                f"now {current_verdict} ({current_score:+d}) — alerting"
            )
            if ntfy_topic:
                send_ntfy(
                    f"⚠️ Chart Bot: {symbol} signal REVERSED",
                    (
                        f"You have an open {recorded_direction} position.\n"
                        f"15m chart now shows: {current_verdict} ({current_score:+d})\n\n"
                        f"Consider reviewing / exiting your position."
                    ),
                    ntfy_topic,
                    priority="urgent",
                )


# ── Single scan ───────────────────────────────────────────────────────────────

def scan_once(symbols: list[str], state_file: str) -> None:
    api_key, access_token = _kite_creds()
    ntfy_topic = _ntfy_topic()

    if not (api_key and access_token):
        print("ERROR: KITE_API_KEY and KITE_ACCESS_TOKEN required.")
        sys.exit(1)

    now_ist = datetime.datetime.now(_IST)
    print(f"[{now_ist.strftime('%H:%M:%S IST')}] Chart bot scan — {', '.join(symbols)}")

    if not (MARKET_OPEN <= now_ist.time() <= MARKET_CLOSE):
        print("Market closed — skipping.")
        return

    state = _load_state(state_file)

    # Monitor existing open positions before scanning for new signals
    _monitor_positions(symbols, state, api_key, access_token, ntfy_topic, now_ist)
    _save_state(state_file, state)  # persist any GTT-hit clearances immediately

    for symbol in symbols:
        try:
            p_res = analyze_chart(symbol, PRIMARY_TF, api_key, access_token)
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

        if direction == "WAIT" or abs(score) < MIN_NOTIFY_SCORE:
            continue

        # Confirm on 5m
        try:
            c_res = analyze_chart(symbol, CONFIRM_TF, api_key, access_token)
        except Exception as e:
            print(f"       5m confirm failed: {e}")
            c_res = {}

        c_verdict   = c_res.get("verdict", {}).get("verdict", "WAIT") if c_res else "WAIT"
        c_direction = _verdict_direction(c_verdict)
        c_score     = c_res.get("verdict", {}).get("score", 0) if c_res else 0

        print(f"         {CONFIRM_TF} confirm: {c_verdict} ({c_score:+d})")

        # 5m must NOT actively oppose the primary direction.
        # WAIT on 5m is acceptable (consolidation before continuation).
        # Only block when 5m gives a confirmed opposite signal (CE vs PE).
        if c_direction != "WAIT" and c_direction != direction:
            print(f"         Confirmed conflict ({direction} vs {c_direction}) — skipping")
            continue

        # Dedup: skip if same date+direction+verdict already alerted AND order was placed.
        # Prefix with today's date so stale state from yesterday never blocks today.
        today     = now_ist.strftime("%Y%m%d")
        state_key = f"{today}:{symbol}:{direction}:{verdict}"
        trade_key = f"{today}:{symbol}:trade"
        already_alerted = state.get(symbol) == state_key
        already_traded  = bool(state.get(trade_key))

        if already_alerted and already_traded:
            print(f"         Already alerted+traded today — no change")
            continue
        elif already_alerted:
            print(f"         Already alerted (no trade yet) — will retry order if conditions met")

        # Format and send notification
        title, message = _fmt_alert(symbol, p_res, c_res)
        print(f"\n{'='*55}\n{message}\n{'='*55}\n")

        if ntfy_topic:
            ok = send_ntfy(title, message, ntfy_topic)
            print(f"  ntfy.sh: {'OK' if ok else 'FAILED'}")
        else:
            print("  No ntfy topic configured.")

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
                chart_sl=chart_sl, chart_target=chart_tgt,
            )
            if "error" in trade:
                print(f"  [AUTO-TRADE] SKIPPED — {trade['error']}")
                if ntfy_topic:
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
                    f" | Target ₹{trade['target']}"
                )
                if ntfy_topic:
                    send_ntfy(
                        f"Chart ORDER PLACED: {trade['tradingsymbol']}",
                        (
                            f"Symbol  : {symbol} {trade['opt_type']}\n"
                            f"Strike  : {trade['strike']}\n"
                            f"Qty     : {trade['qty']}\n"
                            f"Entry   : ₹{trade['entry_price']}\n"
                            f"SL      : ₹{trade['sl']}\n"
                            f"Target  : ₹{trade['target']}\n"
                            f"Order ID: {trade['order_id']}"
                        ),
                        ntfy_topic,
                    )
                state[trade_key] = trade["order_id"]

        state[symbol] = state_key  # mark as alerted (date-prefixed)

    _save_state(state_file, state)
    print("Done.")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Chart Analysis signal bot")
    ap.add_argument("symbols", nargs="*", default=SYMBOLS)
    ap.add_argument("--once", action="store_true", help="Single scan then exit")
    ap.add_argument("--state-file", default="/tmp/chart_bot_state.json")
    ap.add_argument("--loop",        action="store_true", help="Run as daemon, scanning every --interval seconds")
    ap.add_argument("--interval",    type=int, default=300, help="Loop interval in seconds (default: 300)")
    ap.add_argument("--test-notify", action="store_true", help="Send test notification and exit")
    args = ap.parse_args()

    symbols = [s.upper() for s in args.symbols]

    if args.test_notify:
        ntfy_topic = _ntfy_topic()
        now = datetime.datetime.now(_IST).strftime("%H:%M IST")
        title = "Chart Bot — Test Notification"
        body  = (
            f"Chart Analysis Bot is running!\n"
            f"Time     : {now}\n"
            f"Watching : {', '.join(symbols)}\n"
            f"Strategy : 15m primary + 5m confirmation\n"
            f"Signals  : SMC structure + EMA + VWAP + RSI + ADX + OB + CPR\n\n"
            f"You will receive alerts when STRONG BUY or STRONG SELL fires on "
            f"both timeframes during market hours (09:20–15:25 IST)."
        )
        print(body)
        ok = send_ntfy(title, body, ntfy_topic)
        print(f"ntfy.sh: {'OK' if ok else 'FAILED — check NTFY_TOPIC in .env'}")
    elif args.once:
        scan_once(symbols, args.state_file)
    elif args.loop:
        print(f"Chart bot loop started — scanning every {args.interval}s")
        while True:
            try:
                scan_once(symbols, args.state_file)
            except Exception as e:
                print(f"[loop] Unhandled error: {e}")
            time.sleep(args.interval)
    else:
        print("Use --once for cron mode, --loop for daemon mode, or --test-notify.")
        sys.exit(1)
