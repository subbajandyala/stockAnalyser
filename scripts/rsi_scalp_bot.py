"""
RSI Scalp Bot  —  5-minute continuous loop
==========================================
Monitors ATM CE + PE options for NIFTY and SENSEX.
Fires ntfy.sh alert when RSI(14) on 5-min candles crosses UP through 70.

Dedup rules (per instrument, per day):
  • Alert when RSI crosses from below 70 to ≥ 70 (first touch)
  • Silence while RSI stays above 70
  • Re-alert only after RSI drops back below 70 and crosses 70 again
  • State resets at start of each trading day

Start (via start_droplet.sh or manually):
  nohup /root/venv/bin/python -u scripts/rsi_scalp_bot.py >> rsi_scalp.log 2>&1 &

Single scan (test):
  /root/venv/bin/python -u scripts/rsi_scalp_bot.py --once

Test notification:
  /root/venv/bin/python -u scripts/rsi_scalp_bot.py --test-notify
"""

import argparse
import datetime
import json
import logging
import os
import pathlib
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import requests
from screener.rsi_scalp import analyze_rsi_scalp
from screener.kite_order import place_rsi_scalp_order, exit_orphan_mis_positions

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

_IST          = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
MARKET_OPEN   = datetime.time(9, 20)
MARKET_CLOSE  = datetime.time(15, 15)
SCAN_INTERVAL = 300   # 5 minutes

RSI_THRESHOLD = 70
INDEXES       = ["NIFTY", "SENSEX"]
_DEFAULT_NTFY_TOPIC = "nty-revanth-subba-rsi"
_STATE_FILE   = "/tmp/rsi_scalp_state.json"


# ── Credentials ───────────────────────────────────────────────────────────────

def _kite_creds() -> tuple[str, str]:
    """Return (api_key, access_token). Priority: env vars → .env → ~/.kite_creds.json."""
    api_key      = os.environ.get("KITE_API_KEY", "")
    access_token = os.environ.get("KITE_ACCESS_TOKEN", "")
    # .env first — auto_token.py always writes the fresh token here
    if not api_key or not access_token:
        env_path = pathlib.Path(__file__).parent.parent / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if "=" in line and not line.startswith("#"):
                    k, _, v = line.partition("=")
                    k = k.strip(); v = v.strip().strip('"').strip("'")
                    if k == "KITE_API_KEY":
                        api_key = api_key or v
                    elif k == "KITE_ACCESS_TOKEN":
                        access_token = access_token or v
    # ~/.kite_creds.json as fallback
    if not api_key or not access_token:
        creds_file = pathlib.Path.home() / ".kite_creds.json"
        if creds_file.exists():
            try:
                data         = json.loads(creds_file.read_text())
                api_key      = api_key      or data.get("api_key", "")
                access_token = access_token or data.get("access_token", "")
            except Exception:
                pass
    return api_key, access_token


def _ntfy_topic() -> str:
    t = os.environ.get("RSI_SCALP_NTFY_TOPIC", os.environ.get("NTFY_TOPIC", ""))
    if not t:
        for src in [
            pathlib.Path.home() / ".kite_creds.json",
            pathlib.Path(__file__).parent.parent / ".env",
        ]:
            if src.exists():
                try:
                    if src.suffix == ".json":
                        t = json.loads(src.read_text()).get("rsi_scalp_ntfy_topic", "")
                    else:
                        for line in src.read_text().splitlines():
                            if "RSI_SCALP_NTFY_TOPIC" in line and "=" in line:
                                t = line.split("=", 1)[1].strip().strip('"').strip("'")
                except Exception:
                    pass
            if t:
                break
    return t or _DEFAULT_NTFY_TOPIC


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
                "Tags":         "chart_with_upwards_trend,fire",
                "Content-Type": "text/plain; charset=utf-8",
            },
            timeout=10,
        )
        return r.ok
    except Exception as e:
        print(f"  ntfy error: {e}")
        return False


# ── Alert formatter ───────────────────────────────────────────────────────────

def _fmt_alert(index: str, opt: str, result: dict) -> tuple[str, str]:
    key      = opt.lower()
    rsi      = result[f"{key}_rsi"]
    prev_rsi = result[f"{key}_prev_rsi"]
    price    = result[f"{key}_price"]
    strike   = result["strike"]
    spot     = result["spot"]
    expiry   = result["expiry"]
    now_ist  = datetime.datetime.now(_IST)

    instrument = f"{index} {strike} {opt}"
    title = f"RSI Scalp 🔥 {instrument} | RSI {rsi:.1f} crossed 70"

    body = "\n".join([
        f"🔥 RSI Scalp Signal — {instrument}",
        f"Time     : {now_ist.strftime('%H:%M IST')}",
        f"",
        f"RSI(14)  : {prev_rsi:.1f} → {rsi:.1f}  (crossed 70 ↑)",
        f"",
        f"Option   : {instrument}",
        f"LTP      : ₹{price:,.2f}",
        f"Spot     : ₹{spot:,.2f}",
        f"Expiry   : {expiry}",
        f"",
        f"Action   : {'BUY CALL (bullish momentum)' if opt == 'CE' else 'BUY PUT (bearish momentum)'}",
        f"",
        f"RSI > 70 = strong momentum. Exit when RSI drops below 70.",
        f"Scalping MIS · Not financial advice.",
    ])
    return title, body


# ── State persistence ─────────────────────────────────────────────────────────

def _load_state(path: str) -> dict:
    try:
        return json.loads(pathlib.Path(path).read_text())
    except Exception:
        return {}


def _save_state(path: str, state: dict) -> None:
    pathlib.Path(path).write_text(json.dumps(state, indent=2))


def _bot_tradingsymbols(state: dict) -> set:
    """Return the set of tradingsymbols the bot placed today (for orphan-exit scoping)."""
    today = datetime.datetime.now(_IST).date().isoformat()
    return {
        v["tradingsymbol"]
        for v in state.values()
        if isinstance(v, dict) and v.get("tradingsymbol") and v.get("date") == today
    }


# ── Dedup logic ───────────────────────────────────────────────────────────────
# State key: "NIFTY_22500_CE"
# State value: {"above_70": bool, "date": "YYYY-MM-DD", "tradingsymbol": ..., "order_id": ...}

def _instrument_key(index: str, strike: int, opt: str) -> str:
    return f"{index}_{strike}_{opt}"


def _should_alert(key: str, current_rsi: float, state: dict) -> bool:
    today     = datetime.datetime.now(_IST).date().isoformat()
    prev      = state.get(key, {})
    now_above = current_rsi >= RSI_THRESHOLD

    # New day or first time seeing this key — update state, fire only on fresh crossover
    if prev.get("date") != today:
        state[key] = {**state.get(key, {}), "above_70": now_above, "date": today}
        # Only fire if RSI is crossing up right now, not if it was already above
        # (prevents re-firing when bot restarts mid-day with RSI already above 70)
        return now_above and not prev.get("above_70", False)

    was_above = prev.get("above_70", False)
    state[key] = {**prev, "above_70": now_above}

    # Fire only on the transition from below → above
    return not was_above and now_above


# ── Single scan pass ──────────────────────────────────────────────────────────

def _is_auto_trade() -> bool:
    """Check KITE_AUTO_TRADE in env vars, then .env file."""
    if os.environ.get("KITE_AUTO_TRADE") == "1":
        return True
    env_path = pathlib.Path(__file__).parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                if k.strip() == "KITE_AUTO_TRADE" and v.strip().strip('"').strip("'") == "1":
                    return True
    return False


def _scan(ntfy_topic: str, state: dict) -> None:
    now_ist      = datetime.datetime.now(_IST)
    alerts_fired = 0
    auto_trade   = _is_auto_trade()
    if auto_trade:
        print("  [AUTO-TRADE ON]", flush=True)

    # At most one order per index per scan — prevents CE + PE firing simultaneously
    _ordered_this_scan: set = set()

    for index in INDEXES:
        result = analyze_rsi_scalp(index)

        if result.get("error"):
            print(f"  {index:8s}: ERROR — {result['error']}")
            continue

        strike = result["strike"]
        spot   = result["spot"]

        for opt in ("CE", "PE"):
            key_lo = opt.lower()
            rsi    = result.get(f"{key_lo}_rsi")
            err    = result.get(f"{key_lo}_error")

            if err or rsi is None:
                print(f"  {index} {strike} {opt}: {err or 'no RSI'}")
                continue

            bar = "▓" * min(int(rsi / 10), 10)
            flag = "🔥" if rsi >= RSI_THRESHOLD else "  "
            print(f"  {flag} {index} {strike} {opt} | RSI {rsi:5.1f} {bar} | ₹{result[f'{key_lo}_price']:,.1f}")

            inst_key = _instrument_key(index, strike, opt)
            if _should_alert(inst_key, rsi, state):
                title, body = _fmt_alert(index, opt, result)
                print(f"\n{'='*55}\n{body}\n{'='*55}\n")
                ok = send_ntfy(title, body, ntfy_topic, priority="urgent")
                print(f"  ntfy.sh [{ntfy_topic}]: {'OK' if ok else 'FAILED'}")
                alerts_fired += 1

                if auto_trade:
                    if index in _ordered_this_scan:
                        print(f"  Already placed {index} order this scan — skipping {opt}")
                        continue
                    api_key, access_token = _kite_creds()
                    if api_key and access_token:
                        print(f"  KITE_AUTO_TRADE=1 — placing RSI scalp order: {index} {opt}")
                        order_res = place_rsi_scalp_order(index, opt, spot, api_key, access_token, ntfy_topic)
                        if order_res.get("error"):
                            print(f"  Order FAILED: {order_res['error']}")
                        else:
                            print(
                                f"  Order OK: {order_res['tradingsymbol']} "
                                f"qty={order_res['qty']} ltp={order_res['entry_ltp']} "
                                f"limit={order_res['limit_price']} "
                                f"exit_in=60s | id={order_res['order_id']}"
                            )
                            # Record bot's exact contract — used to scope orphan exit
                            state[inst_key]["tradingsymbol"] = order_res["tradingsymbol"]
                            state[inst_key]["order_id"]      = order_res["order_id"]
                            _ordered_this_scan.add(index)
                    else:
                        print("  KITE_AUTO_TRADE=1 but no Kite creds found — skipping order")

    print(f"  [{now_ist.strftime('%H:%M:%S')}] Scan done — {alerts_fired} alert(s) fired")


# ── Worker (continuous loop) ──────────────────────────────────────────────────

def run_worker() -> None:
    ntfy_topic = _ntfy_topic()
    print(f"RSI Scalp worker started — {INDEXES} — topic: {ntfy_topic}")

    # Load persisted state so restart never re-fires already-seen crossovers
    state: dict = _load_state(_STATE_FILE)

    # Exit only MIS positions the bot placed — never touch manual positions
    if os.environ.get("KITE_AUTO_TRADE", "0") == "1":
        api_key, access_token = _kite_creds()
        if api_key and access_token:
            known = _bot_tradingsymbols(state)
            print(f"  Checking for orphaned MIS positions (bot-placed only: {known or 'none'})...")
            orphans = exit_orphan_mis_positions(api_key, access_token, known_ts=known)
            if orphans:
                for o in orphans:
                    print(f"  Orphan exit: SELL {o['qty']}×{o['tradingsymbol']} | {o['order_id']}")
            else:
                print("  No orphaned MIS positions found.")

    while True:
        now_ist = datetime.datetime.now(_IST)
        t       = now_ist.time()

        if t < MARKET_OPEN:
            wait_sec = (
                datetime.datetime.combine(now_ist.date(), MARKET_OPEN, _IST) - now_ist
            ).seconds
            print(f"[{now_ist.strftime('%H:%M IST')}] Market not open — waiting {wait_sec // 60}m")
            time.sleep(min(wait_sec, 60))
            continue

        if t > MARKET_CLOSE:
            print(f"[{now_ist.strftime('%H:%M IST')}] Market closed — shutting down.")
            break

        print(f"\n[{now_ist.strftime('%H:%M:%S IST')}] Scanning RSI…")
        try:
            _scan(ntfy_topic, state)
        except Exception as e:
            print(f"  Scan error: {e}")

        _save_state(_STATE_FILE, state)  # persist after every scan
        time.sleep(SCAN_INTERVAL)


# ── Supervisor ────────────────────────────────────────────────────────────────

def run_supervisor() -> None:
    script = str(pathlib.Path(__file__).resolve())
    print("RSI Scalp supervisor started")
    while True:
        proc = subprocess.Popen([sys.executable, script, "--worker"])
        proc.wait()
        code = proc.returncode
        if code == 0:
            print("Worker exited cleanly. Supervisor done.")
            break
        now_ist = datetime.datetime.now(_IST)
        if now_ist.time() > MARKET_CLOSE:
            print("Worker exited after market close. Supervisor done.")
            break
        print(f"Worker crashed (exit {code}) — restarting in 5s…")
        time.sleep(5)


# ── Single scan (test) ────────────────────────────────────────────────────────

def scan_once() -> None:
    ntfy_topic = _ntfy_topic()
    now_ist    = datetime.datetime.now(_IST)
    print(f"[{now_ist.strftime('%H:%M:%S IST')}] RSI Scalp one-shot scan")
    state: dict = {}
    _scan(ntfy_topic, state)
    print("Done.")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="RSI Scalp bot — NIFTY & SENSEX ATM options")
    ap.add_argument("--once",        action="store_true", help="Single scan then exit")
    ap.add_argument("--worker",      action="store_true", help="Run as worker (called by supervisor)")
    ap.add_argument("--test-notify", action="store_true", help="Send test notification and exit")
    args = ap.parse_args()

    if args.test_notify:
        topic = _ntfy_topic()
        now   = datetime.datetime.now(_IST).strftime("%H:%M IST")
        title = "RSI Scalp Bot — Test"
        body  = (
            f"RSI Scalp Bot is running!\n"
            f"Time      : {now}\n"
            f"Indexes   : NIFTY, SENSEX\n"
            f"Options   : ATM CE + ATM PE\n"
            f"Timeframe : 5-minute candles\n"
            f"RSI       : Period 14 | Trigger at 70 crossover\n"
            f"Strategy  : Fire when RSI crosses above 70 (first time)\n"
            f"           Re-fires after RSI drops below 70 and crosses again\n\n"
            f"Alerts fire 09:20–15:15 IST, Mon–Fri."
        )
        print(body)
        ok = send_ntfy(title, body, topic)
        print(f"ntfy.sh: {'OK' if ok else 'FAILED'}")

    elif args.once:
        scan_once()

    elif args.worker:
        run_worker()

    else:
        run_supervisor()
