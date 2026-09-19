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

from screener.elder_ray import run_elder_ray_signal, INDEX_CONFIG

_IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

DEFAULT_SYMBOLS  = ["NIFTY", "BANKNIFTY", "SENSEX", "FINNIFTY"]
ALERT_SIGNALS    = {"STRONG BUY CE", "BUY CE", "STRONG BUY PE", "BUY PE"}
MARKET_OPEN      = datetime.time(9, 15)
MARKET_CLOSE     = datetime.time(15, 30)


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


# ── Notification senders ──────────────────────────────────────────────────────

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
                "Title":    title,
                "Priority": priority,
                "Tags":     "chart_increasing",
            },
            timeout=10,
        )
        return r.ok
    except Exception:
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


# ── Alert formatter ───────────────────────────────────────────────────────────

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


# ── State management (for --once / GitHub Actions mode) ───────────────────────

def _load_state(state_file: str) -> dict:
    try:
        return json.loads(pathlib.Path(state_file).read_text())
    except Exception:
        return {}


def _save_state(state_file: str, state: dict) -> None:
    pathlib.Path(state_file).write_text(json.dumps(state, indent=2))


# ── Single scan (GitHub Actions / --once mode) ────────────────────────────────

def scan_once(symbols: list[str], strong_only: bool, state_file: str) -> None:
    tg_token, tg_chat = _telegram_creds()
    ntfy_topic        = _ntfy_topic()
    watch = {"STRONG BUY CE", "STRONG BUY PE"} if strong_only else ALERT_SIGNALS

    state = _load_state(state_file)   # {"NIFTY": "BUY CE", ...}

    now_ist = datetime.datetime.now(_IST)
    print(f"[{now_ist.strftime('%H:%M:%S IST')}] Elder Ray scan — {', '.join(symbols)}")

    if not (MARKET_OPEN <= now_ist.time() <= MARKET_CLOSE):
        print("Market closed — no scan needed.")
        return

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

        prev_signal = state.get(symbol)

        if signal not in watch:
            if prev_signal in watch:
                state[symbol] = signal
            continue

        if signal == prev_signal:
            print(f"       (already alerted — no change)")
            continue

        message = _fmt_alert(result)
        print(f"\n{'='*55}\n{message}\n{'='*55}\n")

        title = f"Elder Ray: {signal} — {symbol}"
        notify(title, message, tg_token, tg_chat, ntfy_topic)

        state[symbol] = signal

    _save_state(state_file, state)
    print("State saved.")


# ── Continuous loop (local mode) ──────────────────────────────────────────────

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
    args = ap.parse_args()

    symbols = [s.upper() for s in args.symbols]
    for s in symbols:
        if s not in INDEX_CONFIG:
            print(f"Unknown symbol: {s}. Valid: {list(INDEX_CONFIG.keys())}")
            sys.exit(1)

    if args.once:
        scan_once(symbols, args.strong_only, args.state_file)
    else:
        try:
            run_loop(symbols, args.interval, args.strong_only)
        except KeyboardInterrupt:
            print("\nBot stopped.")
