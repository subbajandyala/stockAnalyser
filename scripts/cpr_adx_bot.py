"""
CPR + ADX Intraday Signal Bot
==============================
Scans Nifty 50 F&O stocks every 5 minutes.
Sends ntfy.sh push notification when score >= 3 (BUY) or <= -3 (SHORT).

Cron (every 5 min, 9:20–14:30, Mon–Fri):
  */5 9-14 * * 1-5 cd /root/stockAnalyser && /root/venv/bin/python scripts/cpr_adx_bot.py --once >> cpr_adx.log 2>&1

Single test run:
  /root/venv/bin/python scripts/cpr_adx_bot.py --once
  /root/venv/bin/python scripts/cpr_adx_bot.py --test-notify
"""

import argparse
import datetime
import json
import os
import pathlib
import sys
import concurrent.futures

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import requests

from screener.cpr_adx import analyze_cpr_adx, NIFTY50_FNO

_IST         = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
MARKET_OPEN  = datetime.time(9, 20)
MARKET_CLOSE = datetime.time(14, 30)   # no new intraday entries after 2:30 PM

MIN_NOTIFY_SCORE = 3   # alert threshold (abs score)

# ── Credentials ───────────────────────────────────────────────────────────────

def _kite_creds() -> tuple[str, str]:
    api_key = os.environ.get("KITE_API_KEY", "")
    token   = os.environ.get("KITE_ACCESS_TOKEN", "")
    if not (api_key and token):
        p = pathlib.Path.home() / ".kite_creds.json"
        if p.exists():
            try:
                c = json.loads(p.read_text())
                api_key = api_key or c.get("api_key", "")
                token   = token   or c.get("access_token", "")
            except Exception:
                pass
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
                        t = json.loads(src.read_text()).get("cpr_adx_ntfy_topic", "")
                    else:
                        for line in src.read_text().splitlines():
                            if "CPR_ADX_NTFY_TOPIC" in line and "=" in line:
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

def _fmt_alert(r: dict) -> tuple[str, str]:
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

    emoji  = "🚀" if abs(score) >= 5 else "⚡"
    action = "BUY (Long)" if direction == "BUY" else "SHORT (Sell)"
    dir_arrow = "↑" if direction == "BUY" else "↓"

    title = f"CPR+ADX {signal}: {sym} {dir_arrow} | {stars} | Score {score:+d}"

    lines = [
        f"{emoji} CPR+ADX SIGNAL — {sym}",
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


# ── State management ──────────────────────────────────────────────────────────

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
        print("ERROR: KITE_API_KEY and KITE_ACCESS_TOKEN required.")
        sys.exit(1)

    now_ist = datetime.datetime.now(_IST)
    print(f"[{now_ist.strftime('%H:%M:%S IST')}] CPR+ADX scan — {len(symbols)} stocks")

    if not (MARKET_OPEN <= now_ist.time() <= MARKET_CLOSE):
        print("Outside scan window (09:20–14:30) — skipping.")
        return

    state = _load_state(state_file)

    def _analyse(sym):
        return analyze_cpr_adx(sym, api_key, access_token)

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(_analyse, symbols))

    for result in results:
        sym = result["symbol"]

        if result.get("error"):
            print(f"  {sym:14s}: ERROR — {result['error']}")
            continue

        score     = result["score"]
        direction = result["direction"]
        adx       = result["adx"]
        stars     = result["stars"]

        marker = "🚀" if abs(score) >= 5 else ("⚡" if abs(score) >= 3 else "  ")
        print(f"  {marker} {sym:14s} | {direction:5s} | score {score:+d} | ADX {adx:4.0f} | {stars}")

        if direction == "WAIT" or abs(score) < MIN_NOTIFY_SCORE:
            continue

        # Dedup: skip if same symbol + direction already alerted this session
        state_key = f"{sym}:{direction}:{score}"
        prev_key  = state.get(sym, "")
        # Allow re-alert only if direction changed
        if prev_key.startswith(f"{sym}:{direction}:"):
            print(f"         Already alerted {direction} — skipping")
            continue

        title, message = _fmt_alert(result)
        print(f"\n{'='*55}\n{message}\n{'='*55}\n")

        if ntfy_topic:
            ok = send_ntfy(title, message, ntfy_topic,
                           priority="urgent" if abs(score) >= 5 else "high")
            print(f"  ntfy.sh: {'OK' if ok else 'FAILED'}")
        else:
            print("  No ntfy topic configured. Set CPR_ADX_NTFY_TOPIC in .env")

        state[sym] = state_key

    _save_state(state_file, state)
    print("Done.")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="CPR + ADX intraday stock signal bot")
    ap.add_argument("symbols", nargs="*", default=NIFTY50_FNO,
                    help="Symbols to scan (default: all Nifty 50 F&O stocks)")
    ap.add_argument("--once", action="store_true", help="Single scan then exit")
    ap.add_argument("--state-file", default="/tmp/cpr_adx_state.json")
    ap.add_argument("--test-notify", action="store_true",
                    help="Send a test notification and exit")
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
            f"Strategy : CPR breakout/bounce + ADX trend strength\n"
            f"Signals  : Score ≥ 3 = alert  |  Score ≥ 5 = urgent\n\n"
            f"Stocks: {', '.join(symbols[:10])}{'...' if len(symbols) > 10 else ''}\n\n"
            f"Alerts fire 09:20–14:30 IST, Mon–Fri."
        )
        print(body)
        ok = send_ntfy(title, body, ntfy_topic)
        print(f"ntfy.sh: {'OK' if ok else 'FAILED — check CPR_ADX_NTFY_TOPIC in .env'}")
    elif args.once:
        scan_once(symbols, args.state_file)
    else:
        print("Use --once for cron mode, or --test-notify to verify notifications.")
        sys.exit(1)
