"""
Elder Ray Signal Bot
====================
Monitors Elder Ray signals for configured symbols every N minutes during
market hours and sends a Telegram alert when a BUY signal fires.

Usage:
    python scripts/elder_ray_bot.py                  # default symbols, 5-min poll
    python scripts/elder_ray_bot.py NIFTY BANKNIFTY  # specific symbols
    python scripts/elder_ray_bot.py --interval 3     # poll every 3 minutes

Telegram setup:
    Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in your environment or in
    ~/.kite_creds.json (add "telegram_bot_token" and "telegram_chat_id" keys).

Kite credentials (optional, enriches alert with live premium):
    Set api_key + access_token in ~/.kite_creds.json
"""

import argparse
import datetime
import json
import os
import pathlib
import sys
import time

# allow running from anywhere
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import requests

from screener.elder_ray import run_elder_ray_signal, fetch_atm_option, INDEX_CONFIG

_IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

DEFAULT_SYMBOLS = ["NIFTY", "BANKNIFTY", "SENSEX", "FINNIFTY"]
ALERT_SIGNALS   = {"STRONG BUY CE", "BUY CE", "STRONG BUY PE", "BUY PE"}

MARKET_OPEN  = datetime.time(9, 15)
MARKET_CLOSE = datetime.time(15, 30)


# ── Credentials ───────────────────────────────────────────────────────────────

def _load_creds() -> dict:
    path = pathlib.Path.home() / ".kite_creds.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return {}


def _telegram_token_chat(creds: dict) -> tuple[str, str]:
    tok = (
        creds.get("telegram_bot_token") or
        os.getenv("TELEGRAM_BOT_TOKEN", "")
    )
    cid = (
        creds.get("telegram_chat_id") or
        os.getenv("TELEGRAM_CHAT_ID", "")
    )
    return tok, cid


# ── Telegram sender ───────────────────────────────────────────────────────────

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


# ── Alert formatter ───────────────────────────────────────────────────────────

def _fmt_alert(result: dict, atm_opt: dict | None) -> str:
    signal    = result["signal"]
    symbol    = result["symbol"]
    price     = result["price"]
    score_ce  = result["score_ce"]
    score_pe  = result["score_pe"]
    atm       = result.get("atm", round(price / INDEX_CONFIG.get(symbol, {}).get("strike_step", 50)) * INDEX_CONFIG.get(symbol, {}).get("strike_step", 50))
    ist_now   = result.get("ist_now", datetime.datetime.now(_IST))
    direction = result.get("direction", "")

    emoji = "🚀" if "STRONG" in signal else "⚡"
    dir_emoji = "🟢 CALL" if direction == "CE" else "🔴 PUT"

    lines = [
        f"{emoji} *ELDER RAY SIGNAL — {symbol}*",
        f"🕐 {ist_now.strftime('%H:%M:%S IST')}",
        f"",
        f"Signal : *{signal}*",
        f"Type   : {dir_emoji}",
        f"Spot   : ₹{price:,.0f}",
        f"ATM    : {atm}",
        f"",
        f"Score CE: {score_ce} | Score PE: {score_pe}",
    ]

    if atm_opt:
        exp = atm_opt.get("expiry_str", "")
        if direction == "CE" and atm_opt.get("ce"):
            ce = atm_opt["ce"]
            lines += [
                f"",
                f"📌 *Option to buy:* {symbol} {atm} CE ({exp})",
                f"   LTP : ₹{ce['ltp']:.1f}",
                f"   OI  : {ce['oi']:,}",
                f"   IV  : {ce['iv']:.1f}%",
            ]
        elif direction == "PE" and atm_opt.get("pe"):
            pe = atm_opt["pe"]
            lines += [
                f"",
                f"📌 *Option to buy:* {symbol} {atm} PE ({exp})",
                f"   LTP : ₹{pe['ltp']:.1f}",
                f"   OI  : {pe['oi']:,}",
                f"   IV  : {pe['iv']:.1f}%",
            ]
        if atm_opt.get("pcr"):
            lines.append(f"   PCR : {atm_opt['pcr']}")

    # top 3 factors
    factors = result.get("factors", [])
    relevant = [f for f in factors if (direction == "CE" and f[2] == "BULL") or (direction == "PE" and f[2] == "BEAR")][:3]
    if relevant:
        lines.append(f"")
        lines.append(f"*Reasons:*")
        for name, desc, _, _ in relevant:
            lines.append(f"  • {name}: {desc}")

    lines += [
        f"",
        f"⚠️ Always verify before executing. Not financial advice.",
    ]
    return "\n".join(lines)


# ── Market hours check ────────────────────────────────────────────────────────

def _is_market_open() -> bool:
    now = datetime.datetime.now(_IST).time()
    return MARKET_OPEN <= now <= MARKET_CLOSE


def _next_poll_sleep(interval_seconds: int) -> None:
    """Sleep until next poll, show countdown."""
    for remaining in range(interval_seconds, 0, -30):
        now = datetime.datetime.now(_IST)
        print(f"  next poll in {remaining}s  [{now.strftime('%H:%M:%S IST')}]", end="\r")
        time.sleep(min(30, remaining))
    print()


# ── Main loop ─────────────────────────────────────────────────────────────────

def run_bot(symbols: list[str], interval_minutes: int, strong_only: bool) -> None:
    creds     = _load_creds()
    api_key   = creds.get("api_key", "")
    api_token = creds.get("access_token", "")
    tg_token, tg_chat = _telegram_token_chat(creds)

    if not (tg_token and tg_chat):
        print("⚠️  No Telegram credentials found.")
        print("   Add to ~/.kite_creds.json:")
        print('   "telegram_bot_token": "your-bot-token"')
        print('   "telegram_chat_id":   "your-chat-id"')
        print()

    interval_sec   = interval_minutes * 60
    last_signal    = {s: None for s in symbols}   # track to avoid re-alerting same signal
    last_alert_ts  = {s: None for s in symbols}   # cooldown: don't alert same signal within 30 min

    watch_signals = {"STRONG BUY CE", "STRONG BUY PE"} if strong_only else ALERT_SIGNALS

    print(f"🤖 Elder Ray Bot started")
    print(f"   Symbols  : {', '.join(symbols)}")
    print(f"   Interval : {interval_minutes} min")
    print(f"   Watching : {', '.join(sorted(watch_signals))}")
    print(f"   Telegram : {'✅ configured' if (tg_token and tg_chat) else '❌ not configured'}")
    print(f"   Kite     : {'✅ connected' if (api_key and api_token) else '⚠️  no credentials (premium data disabled)'}")
    print()

    while True:
        now_ist = datetime.datetime.now(_IST)

        if not _is_market_open():
            next_open = now_ist.replace(hour=9, minute=15, second=0, microsecond=0)
            if now_ist.time() > MARKET_CLOSE:
                next_open += datetime.timedelta(days=1)
            wait_sec = int((next_open - now_ist).total_seconds())
            print(f"[{now_ist.strftime('%H:%M IST')}] Market closed. Next open at 09:15 IST (~{wait_sec//3600}h {(wait_sec%3600)//60}m)")
            # sleep in 5-min chunks so Ctrl-C works
            time.sleep(min(300, max(60, wait_sec)))
            continue

        print(f"\n[{now_ist.strftime('%H:%M:%S IST')}] Scanning {len(symbols)} symbols...")

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

            if signal not in watch_signals:
                # signal dropped — reset so next trigger fires fresh
                if last_signal[symbol] in watch_signals:
                    last_signal[symbol] = signal
                continue

            # cooldown: same signal, same direction → don't re-alert within 30 min
            ts_now = now_ist
            cooldown_ok = (
                last_signal[symbol] != signal or
                last_alert_ts[symbol] is None or
                (ts_now - last_alert_ts[symbol]).total_seconds() > 1800
            )

            if not cooldown_ok:
                print(f"       (cooldown — alerted {int((ts_now - last_alert_ts[symbol]).total_seconds() / 60)}m ago)")
                continue

            # fetch live Kite premium if credentials available
            atm_opt = None
            if api_key and api_token:
                try:
                    atm_opt = fetch_atm_option(api_key, api_token, symbol, price)
                except Exception:
                    pass

            message = _fmt_alert(result, atm_opt)
            print(f"\n{'='*60}")
            print(message)
            print(f"{'='*60}\n")

            if tg_token and tg_chat:
                ok = send_telegram(message, tg_token, tg_chat)
                print(f"  Telegram: {'✅ sent' if ok else '❌ failed'}")
            else:
                print("  Telegram: not configured (alert printed above)")

            last_signal[symbol]   = signal
            last_alert_ts[symbol] = ts_now

        _next_poll_sleep(interval_sec)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Elder Ray signal monitor with Telegram alerts")
    ap.add_argument("symbols", nargs="*", default=DEFAULT_SYMBOLS,
                    help=f"Symbols to watch (default: {' '.join(DEFAULT_SYMBOLS)})")
    ap.add_argument("--interval", "-i", type=int, default=5,
                    help="Poll interval in minutes (default: 5)")
    ap.add_argument("--strong-only", action="store_true",
                    help="Alert only on STRONG BUY CE / STRONG BUY PE (ignore regular BUY)")
    args = ap.parse_args()

    symbols = [s.upper() for s in args.symbols]
    for s in symbols:
        if s not in INDEX_CONFIG:
            print(f"Unknown symbol: {s}. Valid: {list(INDEX_CONFIG.keys())}")
            sys.exit(1)

    try:
        run_bot(symbols, args.interval, args.strong_only)
    except KeyboardInterrupt:
        print("\n\nBot stopped.")
