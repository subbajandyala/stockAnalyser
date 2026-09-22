"""
Agent Flow Signal Bot
=====================
Runs run_agent_analysis() for each symbol and sends a push notification
via ntfy.sh when a tradeable signal fires (no_trade=False, rating >= 3).

Cloud / GitHub Actions (--once):
    python scripts/agent_flow_bot.py NIFTY BANKNIFTY SENSEX --once

Requires KITE_API_KEY and KITE_ACCESS_TOKEN env vars (or ~/.kite_creds.json).
Set KITE_AUTO_TRADE=1 to enable auto order execution (NIFTY and SENSEX only).
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

from screener.agent_flow import run_agent_analysis, INSTRUMENTS
from screener.kite_order import place_order_for_signal as _place_order

_AUTO_TRADE = os.environ.get("KITE_AUTO_TRADE", "0") == "1"
_TRADE_SYMBOLS = {"NIFTY", "SENSEX"}   # BANKNIFTY excluded

_IST         = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
MARKET_OPEN  = datetime.time(9, 15)
MARKET_CLOSE = datetime.time(15, 30)
MIN_RATING   = 3   # alert only on 3-star and above


# ── Credentials ───────────────────────────────────────────────────────────────

def _kite_creds() -> tuple[str, str]:
    api_key = os.environ.get("KITE_API_KEY", "")
    token   = os.environ.get("KITE_ACCESS_TOKEN", "")
    if not (api_key and token):
        p = pathlib.Path.home() / ".kite_creds.json"
        if p.exists():
            try:
                c = json.loads(p.read_text())
                api_key = api_key or c.get("api_key", "plz6ik09bgb62mey")
                token   = token   or c.get("access_token", "")
            except Exception:
                pass
    return api_key, token


def _ntfy_topic() -> str:
    t = os.environ.get("NTFY_TOPIC", "")
    if not t:
        p = pathlib.Path.home() / ".kite_creds.json"
        if p.exists():
            try:
                t = json.loads(p.read_text()).get("ntfy_agent_topic", "")
            except Exception:
                pass
    return t


# ── Notification ───────────────────────────────────────────────────────────────

def send_ntfy(title: str, body: str, topic: str) -> bool:
    if not topic:
        return False
    try:
        r = requests.post(
            f"https://ntfy.sh/{topic}",
            data=body.encode("utf-8"),
            headers={
                "Title":        title.encode("utf-8"),
                "Priority":     "high",
                "Tags":         "robot",
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


# ── Alert formatter ───────────────────────────────────────────────────────────────

_STARS = {1: "★", 2: "★★", 3: "★★★", 4: "★★★★", 5: "★★★★★"}


def _fmt_alert(r: dict) -> tuple[str, str]:
    symbol    = r["symbol"]
    direction = r["direction"]        # CALL / PUT
    bias      = r["bias"]
    conf      = r["confidence"]
    strike    = int(r["strike"])
    ltp       = r["ltp"]
    rating    = r.get("rating", 3)
    prob      = r.get("probability", 0)
    tgt       = r.get("targets", {})
    signals   = r.get("signals", [])
    spot      = r.get("spot", 0)
    expiry    = r.get("expiry", "")
    now_ist   = datetime.datetime.now(_IST)

    emoji  = "🚀" if rating >= 4 else "⚡"
    dline  = "🟢 BUY CALL (CE)" if direction == "CALL" else "🔴 BUY PUT (PE)"
    opt    = f"{symbol} {strike} {'CE' if direction == 'CALL' else 'PE'}"
    stars  = _STARS.get(rating, "★★★")

    lines = [
        f"{emoji} AGENT FLOW — {symbol}",
        f"Time: {now_ist.strftime('%H:%M IST')}",
        f"",
        f"Signal : {direction} ({bias})",
        f"Action : {dline}",
        f"Spot   : Rs.{spot:,.0f}   Strike: {strike}",
        f"LTP    : Rs.{ltp:.1f}",
        f"Rating : {stars}   Confidence: {conf}%   Prob: {prob}%",
    ]

    if expiry:
        lines.append(f"Expiry : {expiry}")

    if tgt:
        lines += [
            f"",
            f"SL     : Rs.{tgt.get('sl', 0):.1f}",
            f"T1/T2/T3: Rs.{tgt.get('t1', 0):.1f} / Rs.{tgt.get('t2', 0):.1f} / Rs.{tgt.get('t3', 0):.1f}",
            f"R:R    : {tgt.get('rr', 0):.1f}x",
        ]

    if signals:
        lines.append(f"")
        lines.append(f"Why:")
        for s in signals[:4]:
            lines.append(f"  - {s}")

    lines += ["", "Verify before executing. Not financial advice."]

    title = f"Agent Flow: {direction} {symbol} {strike} | {stars}"
    return title, "\n".join(lines)


# ── State management ───────────────────────────────────────────────────────────────

def _load_state(path: str) -> dict:
    try:
        return json.loads(pathlib.Path(path).read_text())
    except Exception:
        return {}


def _save_state(path: str, state: dict) -> None:
    pathlib.Path(path).write_text(json.dumps(state, indent=2))


# ── Single scan ───────────────────────────────────────────────────────────────

def scan_once(symbols: list[str], state_file: str) -> None:
    api_key, access_token = _kite_creds()
    ntfy_topic = _ntfy_topic()

    if not (api_key and access_token):
        print("ERROR: KITE_API_KEY and KITE_ACCESS_TOKEN are required.")
        sys.exit(1)

    now_ist = datetime.datetime.now(_IST)
    print(f"[{now_ist.strftime('%H:%M:%S IST')}] Agent Flow scan — {', '.join(symbols)}")

    if not (MARKET_OPEN <= now_ist.time() <= MARKET_CLOSE):
        print("Market closed — no scan needed.")
        return

    state = _load_state(state_file)

    for symbol in symbols:
        try:
            result = run_agent_analysis(symbol, api_key, access_token)
        except Exception as e:
            print(f"  {symbol}: ERROR — {e}")
            continue

        if result.get("error"):
            print(f"  {symbol}: {result['error']}")
            continue

        no_trade = result.get("no_trade", True)
        bias     = result.get("bias", "Neutral")
        conf     = result.get("confidence", 0)
        rating   = result.get("rating", 1)
        direction = result.get("direction", "")

        marker = "🔥" if rating >= 4 else ("⚡" if not no_trade else "  ")
        print(f"  {marker} {symbol:12s} | {'NO TRADE' if no_trade else direction:8s} | {bias:20s} | {conf}% conf | {rating}★")

        if no_trade or rating < MIN_RATING:
            continue

        # deduplicate: skip if same direction+strike already alerted
        strike    = result.get("strike", 0)
        state_key = f"{symbol}:{direction}:{int(strike)}"
        if state.get(symbol) == state_key:
            print(f"       (already alerted — no change)")
            continue

        title, message = _fmt_alert(result)
        print(f"\n{'='*55}\n{message}\n{'='*55}\n")

        if ntfy_topic:
            ok = send_ntfy(title, message, ntfy_topic)
            print(f"  ntfy.sh : {'OK' if ok else 'FAILED'}")
        else:
            print("  No ntfy topic configured.")

        # ── Auto-execute order ─────────────────────────────────────────────────────────────
        if _AUTO_TRADE and symbol in _TRADE_SYMBOLS:
            direction = result.get("direction", "")   # "CALL" or "PUT"
            spot      = result.get("spot", 0)
            print(f"  [AUTO-TRADE] Placing {direction} order for {symbol} @ spot {spot:.0f}")
            trade = _place_order(symbol, direction, spot, api_key, access_token)
            if "error" in trade:
                print(f"  [AUTO-TRADE] SKIPPED — {trade['error']}")
                if ntfy_topic:
                    send_ntfy(
                        f"Auto-Trade SKIPPED: {symbol}",
                        f"Signal: {direction}\nReason: {trade['error']}",
                        ntfy_topic,
                    )
            else:
                print(
                    f"  [AUTO-TRADE] ORDER PLACED — {trade['tradingsymbol']}"
                    f" | Entry ₹{trade['entry_price']}"
                    f" | SL ₹{trade['sl']}"
                    f" | Target ₹{trade['target']}"
                    f" | GTT {trade.get('gtt_id', 'N/A')}"
                )
                if ntfy_topic:
                    send_ntfy(
                        f"ORDER PLACED: {trade['tradingsymbol']}",
                        (
                            f"Symbol  : {symbol} {trade['opt_type']}\n"
                            f"Strike  : {trade['strike']}\n"
                            f"Qty     : {trade['qty']}\n"
                            f"Entry   : Rs.{trade['entry_price']}\n"
                            f"SL      : Rs.{trade['sl']} (-30%)\n"
                            f"Target  : Rs.{trade['target']} (+60%)\n"
                            f"GTT ID  : {trade.get('gtt_id', 'N/A')}\n"
                            f"Order ID: {trade['order_id']}"
                        ),
                        ntfy_topic,
                    )
                state[f"{symbol}:trade"] = trade["order_id"]
        # ────────────────────────────────────────────────────────────────────────────────

        state[symbol] = state_key

    _save_state(state_file, state)
    print("State saved.")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Agent Flow signal monitor")
    ap.add_argument("symbols", nargs="*",
                    default=["NIFTY", "BANKNIFTY", "SENSEX"],
                    help="Symbols to scan")
    ap.add_argument("--once", action="store_true",
                    help="Single scan then exit (GitHub Actions mode)")
    ap.add_argument("--state-file", default="/tmp/af_state.json",
                    help="State file for dedup (default: /tmp/af_state.json)")
    ap.add_argument("--test-notify", action="store_true",
                    help="Send a test notification and exit")
    args = ap.parse_args()

    symbols = [s.upper() for s in args.symbols]
    for s in symbols:
        if s not in INSTRUMENTS:
            print(f"Unknown symbol: {s}. Valid: {INSTRUMENTS}")
            sys.exit(1)

    if args.test_notify:
        ntfy_topic = _ntfy_topic()
        now = datetime.datetime.now(_IST).strftime("%H:%M IST")
        title = "Agent Flow Bot - Test Notification"
        body  = (
            f"Agent Flow Bot is working!\n"
            f"Time: {now}\n"
            f"Watching: {', '.join(symbols)}\n\n"
            f"You will receive alerts here when a rated trade setup fires during market hours (09:15-15:30 IST)."
        )
        print(body)
        ok = send_ntfy(title, body, ntfy_topic)
        print(f"ntfy.sh: {'OK' if ok else 'FAILED'}")
    elif args.once:
        scan_once(symbols, args.state_file)
    else:
        print("Use --once for GitHub Actions mode.")
        sys.exit(1)
