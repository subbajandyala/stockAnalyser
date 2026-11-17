"""
Straddle Tracker Bot
====================
On expiry day (after 15:30) rebuilds the T-1 15:20 ATM straddle → expiry
record from Kite candles, saves it to data/straddle_tracker.json and
pushes a one-message summary to ntfy.

Does nothing on non-expiry days, so it is safe to cron every weekday:

    # 15:45 IST Mon–Fri (droplet clock is UTC → 10:15 UTC)
    15 10 * * 1-5 cd /root/stockAnalyser && /root/venv/bin/python scripts/straddle_tracker_bot.py --once >> straddle_bot.log 2>&1

Usage:
    python scripts/straddle_tracker_bot.py --once                    # NIFTY, today if expiry
    python scripts/straddle_tracker_bot.py --once --symbol SENSEX
    python scripts/straddle_tracker_bot.py --once --force            # record even before 15:30 (partial)
    python scripts/straddle_tracker_bot.py --once --no-notify

Requires KITE_API_KEY and KITE_ACCESS_TOKEN env vars (or ~/.kite_creds.json).
"""

import argparse
import datetime
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import requests

from screener import straddle_tracker as stt

_IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


def _kite_creds() -> tuple[str, str]:
    api_key = os.environ.get("KITE_API_KEY", "")
    token = os.environ.get("KITE_ACCESS_TOKEN", "")
    if not (api_key and token):
        p = pathlib.Path.home() / ".kite_creds.json"
        if p.exists():
            try:
                c = json.loads(p.read_text())
                api_key = api_key or c.get("api_key", "")
                token = token or c.get("access_token", "")
            except Exception:
                pass
    return api_key, token


def _ntfy_topic() -> str:
    return os.environ.get("NTFY_STRADDLE_TOPIC", "") or os.environ.get("NTFY_TOPIC", "")


def send_ntfy(title: str, body: str, topic: str) -> bool:
    if not topic:
        return False
    try:
        r = requests.post(
            f"https://ntfy.sh/{topic}",
            data=body.encode("utf-8"),
            headers={"Title": title.encode("utf-8"), "Tags": "bar_chart",
                     "Content-Type": "text/plain; charset=utf-8"},
            timeout=10,
        )
        return r.ok
    except Exception as e:
        print(f"  ntfy.sh exception: {e}")
        return False


def run_once(symbol: str, force: bool, notify: bool) -> int:
    api_key, token = _kite_creds()
    if not (api_key and token):
        print("Kite credentials missing.")
        return 1

    now = datetime.datetime.now(_IST).replace(tzinfo=None)
    kite = stt._Kite(api_key, token)
    opts = stt._option_rows(kite.instruments(stt.INSTRUMENTS[symbol]["exchange"]), symbol)
    expiry = stt.nearest_expiry(opts, now.date())
    if expiry != now.date():
        print(f"[{now:%Y-%m-%d %H:%M}] {symbol}: not an expiry day (next {expiry}). Nothing to do.")
        return 0
    if now.time() < datetime.time(15, 30) and not force:
        print(f"[{now:%H:%M}] {symbol}: expiry day but market still open — rerun after 15:30 or use --force.")
        return 0

    rec = stt.build_expiry_record(api_key, token, symbol, expiry, now)
    stt.upsert_record(rec)
    text = stt.format_summary_text(rec)
    print(f"[{now:%Y-%m-%d %H:%M}] saved ({rec['status']}) → {stt.data_path()}\n{text}")

    if notify and rec["status"] == "complete":
        df = stt.records_df(symbol)
        s = stt.summarize(df)
        if s.get("n", 0) >= 2:
            text += (f"\n\nLast {s['n']} expiries: ratio {s['ratio_of_avgs']:.2f} · "
                     f"inside {s['pct_closed_inside']:.0f}% · seller win {s['seller_win_rate']:.0f}%")
        ok = send_ntfy(f"📊 {symbol} expected vs actual move", text, _ntfy_topic())
        print(f"  ntfy.sh: {'OK' if ok else 'skipped/failed'}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="ATM straddle expected-move tracker")
    ap.add_argument("--once", action="store_true", help="Single run then exit")
    ap.add_argument("--symbol", default="NIFTY", choices=list(stt.INSTRUMENTS))
    ap.add_argument("--force", action="store_true", help="Record even before 15:30 (partial)")
    ap.add_argument("--no-notify", action="store_true")
    args = ap.parse_args()
    if not args.once:
        print("Use --once.")
        sys.exit(1)
    try:
        sys.exit(run_once(args.symbol, args.force, not args.no_notify))
    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)
