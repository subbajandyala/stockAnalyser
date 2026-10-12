"""
CPR + ADX Intraday Signal Bot  —  1-minute continuous loop
===========================================================
Runs as a long-lived background process (supervisor + worker pattern).
Scans all 25 Nifty 50 F&O stocks every 60 seconds during market hours.
Sends ntfy.sh push when a signal fires or upgrades.

Dedup rules (per stock, per day):
  • Alert when score crosses +3/-3 for the first time  (regular signal)
  • Re-alert when score jumps to ±5                    (upgraded to STRONG)
  • Re-alert when direction flips  (BUY → SHORT or vice versa)
  • State resets at start of each trading day

Start (via start_droplet.sh or manually):
  nohup python scripts/cpr_adx_bot.py >> cpr_adx.log 2>&1 &

Single scan (test):
  python scripts/cpr_adx_bot.py --once

Test notification:
  python scripts/cpr_adx_bot.py --test-notify
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
import concurrent.futures

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import requests

from screener.cpr_adx import analyze_cpr_adx, NIFTY50_FNO

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

_IST         = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
MARKET_OPEN  = datetime.time(9, 20)
MARKET_CLOSE = datetime.time(14, 30)
SCAN_INTERVAL_SEC = 60        # scan every 60 seconds

MIN_SIGNAL_SCORE  = 3         # regular signal threshold
STRONG_SCORE      = 5         # strong / upgrade threshold

_DEFAULT_NTFY_TOPIC = "marketPulse-cpr-adx-bot"
_STATE_FILE = "/tmp/cpr_adx_bot_state.json"


# ── State persistence ─────────────────────────────────────────────────────────

def _load_state(path: str) -> dict:
    try:
        return json.loads(pathlib.Path(path).read_text())
    except Exception:
        return {}


def _save_state(path: str, state: dict) -> None:
    try:
        pathlib.Path(path).write_text(json.dumps(state, indent=2))
    except Exception as e:
        print(f"  Warning: could not save state: {e}")


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
    t = os.environ.get("CPR_ADX_NTFY_TOPIC", os.environ.get("NTFY_TOPIC", ""))
    if not t:
        for src in [
            pathlib.Path.home() / ".kite_creds.json",
            pathlib.Path(__file__).parent.parent / ".env",
        ]:
            if src.exists():
                try:
                    if src.suffix == ".json":
                        c = json.loads(src.read_text())
                        t = c.get("cpr_adx_ntfy_topic", "") or c.get("ntfy_agent_topic", "")
                    else:
                        for line in src.read_text().splitlines():
                            line = line.strip()
                            if not line or line.startswith("#") or "=" not in line:
                                continue
                            k, v = line.split("=", 1)
                            k = k.strip(); v = v.strip().strip('"').strip("'")
                            if k == "CPR_ADX_NTFY_TOPIC":
                                t = v
                                break
                            if k == "NTFY_TOPIC" and not t:
                                t = v  # shared topic as fallback
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
                "Tags":         "chart,stocks",
                "Content-Type": "text/plain; charset=utf-8",
            },
            timeout=10,
        )
        return r.ok
    except Exception as e:
        print(f"  ntfy error: {e}")
        return False


# ── Alert formatter ───────────────────────────────────────────────────────────

def _fmt_alert(r: dict, upgraded: bool = False) -> tuple[str, str]:
    sym       = r["symbol"]
    price     = r["price"]
    score     = r["score"]
    direction = r["direction"]
    signal    = r["signal"]
    stars     = r["stars"]
    cpr       = r["cpr"]
    adx       = r["adx"]
    di_p      = r["di_p"]
    di_m      = r["di_m"]
    sl        = r["sl"]
    target    = r["target"]
    rr        = r["rr"]
    reasons   = r["reasons"]
    now_ist   = datetime.datetime.now(_IST)

    emoji      = "🚀" if abs(score) >= STRONG_SCORE else "⚡"
    action     = "BUY (Long)" if direction == "BUY" else "SHORT (Sell)"
    dir_arrow  = "↑" if direction == "BUY" else "↓"
    upgrade_tag = " 🔥 UPGRADED" if upgraded else ""

    title = f"CPR+ADX {signal}: {sym} {dir_arrow} | {stars} | Score {score:+d}{upgrade_tag}"

    lines = [
        f"{emoji} CPR+ADX{' 🔥 UPGRADED' if upgraded else ''} — {sym}",
        f"Time   : {now_ist.strftime('%H:%M IST')}",
        f"",
        f"Signal : {signal}  {stars}",
        f"Action : {action}",
        f"Score  : {score:+d} / 5",
        f"",
        f"Price  : ₹{price:,.1f}",
        f"CPR    : ₹{cpr['bot']:,.1f} – ₹{cpr['top']:,.1f}  (width {cpr['width_pct']:.2f}%)",
        f"ADX    : {adx}   DI+ {di_p}   DI− {di_m}",
        f"",
    ]

    if sl and target:
        lines += [
            f"Entry  : ₹{price:,.1f}",
            f"SL     : ₹{sl:,.1f}",
            f"Target : ₹{target:,.1f}",
            f"R:R    : {rr}x",
            f"",
        ]

    if reasons:
        lines.append("Why:")
        for reason in reasons[:4]:
            lines.append(f"  · {reason}")

    lines += ["", "Intraday MIS · Verify before trading · Not financial advice."]
    return title, "\n".join(lines)


# ── Dedup logic ───────────────────────────────────────────────────────────────
# State per symbol:  {"direction": "BUY"|"SHORT"|"WAIT", "strength": "regular"|"strong", "date": "YYYY-MM-DD"}

def _should_alert(sym: str, result: dict, state: dict) -> tuple[bool, bool]:
    """
    Returns (should_alert, is_upgrade).
    Checks direction, strength, and date reset.
    """
    direction = result["direction"]
    score     = result["score"]

    if direction == "WAIT":
        return False, False

    strength  = "strong" if abs(score) >= STRONG_SCORE else "regular"
    today     = datetime.datetime.now(_IST).date().isoformat()
    prev      = state.get(sym, {})
    prev_date = prev.get("date", "")

    # New trading day — only alert if direction actually changed from yesterday.
    # Carrying the same BUY/SHORT signal overnight is not worth re-alerting.
    if prev_date != today:
        prev_dir = prev.get("direction", "WAIT")
        if prev_dir == direction:
            # Same signal continues from yesterday — update date silently, no alert
            state[sym] = {"direction": direction, "strength": strength, "date": today}
            return False, False
        # Direction changed overnight (or no prior signal) — fire
        return True, False

    prev_dir      = prev.get("direction", "WAIT")
    prev_strength = prev.get("strength", "")

    # Direction changed
    if prev_dir != direction:
        return True, False

    # Score upgraded from regular to strong
    if prev_strength == "regular" and strength == "strong":
        return True, True

    return False, False


def _update_state(sym: str, result: dict, state: dict) -> None:
    direction = result["direction"]
    score     = result["score"]
    strength  = "strong" if abs(score) >= STRONG_SCORE else "regular"
    today     = datetime.datetime.now(_IST).date().isoformat()
    state[sym] = {"direction": direction, "strength": strength, "date": today}


# ── Single scan pass ──────────────────────────────────────────────────────────

def _scan(symbols: list[str], api_key: str, access_token: str,
          ntfy_topic: str, state: dict) -> None:
    now_ist = datetime.datetime.now(_IST)

    def _analyse(sym):
        return analyze_cpr_adx(sym, api_key, access_token)

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(_analyse, symbols))

    alerts_fired = 0
    for result in results:
        sym = result["symbol"]

        if result.get("error"):
            print(f"  {sym:14s}: ERROR — {result['error']}")
            continue

        score     = result["score"]
        direction = result["direction"]
        adx       = result["adx"]
        stars     = result["stars"]

        marker = "🚀" if abs(score) >= STRONG_SCORE else ("⚡" if abs(score) >= MIN_SIGNAL_SCORE else "  ")
        print(f"  {marker} {sym:14s} | {direction:5s} | score {score:+d} | ADX {adx:4.0f} | {stars}")

        should_alert, is_upgrade = _should_alert(sym, result, state)
        if not should_alert:
            continue

        title, message = _fmt_alert(result, upgraded=is_upgrade)
        print(f"\n{'='*55}\n{message}\n{'='*55}\n")

        priority = "urgent" if abs(score) >= STRONG_SCORE else "high"
        ok = send_ntfy(title, message, ntfy_topic, priority=priority)
        print(f"  ntfy.sh [{ntfy_topic}]: {'OK' if ok else 'FAILED'}")
        alerts_fired += 1

        _update_state(sym, result, state)
        _save_state(_STATE_FILE, state)

    print(f"  [{now_ist.strftime('%H:%M:%S')}] Scan done — {alerts_fired} alert(s) fired")


# ── Worker (continuous loop) ──────────────────────────────────────────────────

def run_worker(symbols: list[str]) -> None:
    api_key, access_token = _kite_creds()
    ntfy_topic = _ntfy_topic()

    if not (api_key and access_token):
        print("ERROR: KITE_API_KEY and KITE_ACCESS_TOKEN required.")
        sys.exit(1)

    print(f"CPR+ADX worker started — {len(symbols)} stocks — topic: {ntfy_topic}")
    state: dict = _load_state(_STATE_FILE)
    print(f"  Loaded {len(state)} dedup entries from state file")

    while True:
        now_ist = datetime.datetime.now(_IST)
        t       = now_ist.time()

        if t < MARKET_OPEN:
            wait_sec = (
                datetime.datetime.combine(now_ist.date(), MARKET_OPEN, _IST) - now_ist
            ).seconds
            print(f"[{now_ist.strftime('%H:%M IST')}] Market not open yet — waiting {wait_sec//60}m")
            time.sleep(min(wait_sec, 60))
            continue

        if t > MARKET_CLOSE:
            print(f"[{now_ist.strftime('%H:%M IST')}] Market closed — shutting down worker.")
            break

        print(f"\n[{now_ist.strftime('%H:%M:%S IST')}] Scanning {len(symbols)} stocks…")
        try:
            _scan(symbols, api_key, access_token, ntfy_topic, state)
        except Exception as e:
            print(f"  Scan error: {e}")

        time.sleep(SCAN_INTERVAL_SEC)


# ── Supervisor (auto-restart on crash) ────────────────────────────────────────

def run_supervisor(symbols: list[str]) -> None:
    """
    Spawns the worker as a subprocess. If it crashes, restarts after 5s.
    Exits cleanly when the worker exits normally (market closed).
    """
    script = str(pathlib.Path(__file__).resolve())
    sym_args = symbols

    print(f"CPR+ADX supervisor started — managing worker")
    while True:
        cmd = [sys.executable, script, "--worker"] + sym_args
        proc = subprocess.Popen(cmd)
        proc.wait()
        code = proc.returncode

        if code == 0:
            print("Worker exited cleanly (market closed). Supervisor done.")
            break

        now_ist = datetime.datetime.now(_IST)
        if now_ist.time() > MARKET_CLOSE:
            print("Worker exited after market close. Supervisor done.")
            break

        print(f"Worker crashed (exit {code}) — restarting in 5s…")
        time.sleep(5)


# ── Single scan mode (for testing) ───────────────────────────────────────────

def scan_once(symbols: list[str]) -> None:
    api_key, access_token = _kite_creds()
    ntfy_topic = _ntfy_topic()

    if not (api_key and access_token):
        print("ERROR: KITE_API_KEY and KITE_ACCESS_TOKEN required.")
        sys.exit(1)

    now_ist = datetime.datetime.now(_IST)
    print(f"[{now_ist.strftime('%H:%M:%S IST')}] CPR+ADX one-shot scan — {len(symbols)} stocks")

    if not (MARKET_OPEN <= now_ist.time() <= MARKET_CLOSE):
        print("Outside scan window (09:20–14:30) — skipping.")
        return

    state: dict = {}
    _scan(symbols, api_key, access_token, ntfy_topic, state)
    print("Done.")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="CPR + ADX intraday stock signal bot")
    ap.add_argument("symbols", nargs="*", default=NIFTY50_FNO)
    ap.add_argument("--once",        action="store_true", help="Single scan then exit")
    ap.add_argument("--worker",      action="store_true", help="Run as worker (called by supervisor)")
    ap.add_argument("--test-notify", action="store_true", help="Send test notification and exit")
    args = ap.parse_args()

    symbols = [s.upper() for s in args.symbols]

    if args.test_notify:
        ntfy_topic = _ntfy_topic()
        now = datetime.datetime.now(_IST).strftime("%H:%M IST")
        title = "CPR+ADX Bot — Test"
        body  = (
            f"CPR + ADX Intraday Bot is running!\n"
            f"Time     : {now}\n"
            f"Watching : {len(symbols)} Nifty 50 F&O stocks\n"
            f"Interval : Every 60 seconds\n"
            f"Strategy : CPR breakout/bounce + ADX trend strength\n"
            f"Signals  : Score ≥ 3 = alert  |  Score ≥ 5 = urgent\n"
            f"Dedup    : Re-alerts on direction flip or score upgrade only\n\n"
            f"Stocks: {', '.join(symbols[:10])}{'...' if len(symbols) > 10 else ''}\n\n"
            f"Alerts fire 09:20–14:30 IST, Mon–Fri."
        )
        print(body)
        ok = send_ntfy(title, body, ntfy_topic)
        print(f"ntfy.sh: {'OK' if ok else 'FAILED'}")

    elif args.once:
        scan_once(symbols)

    elif args.worker:
        run_worker(symbols)

    else:
        # Default: run supervisor (what start_droplet.sh calls)
        run_supervisor(symbols)
