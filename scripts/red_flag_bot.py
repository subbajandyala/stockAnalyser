"""
Red Flag Signal Bot
===================
Runs run_red_flag_scan() across all F&O stocks and sends a push
notification via ntfy.sh listing stocks with STRONG CE or STRONG PE signals.

Cloud / GitHub Actions (--once):
    python scripts/red_flag_bot.py --once

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

from screener.red_flag import run_red_flag_scan

_IST         = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
MARKET_OPEN  = datetime.time(9, 15)
MARKET_CLOSE = datetime.time(15, 30)
ALERT_TRADES = {"STRONG CE", "STRONG PE"}


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
    return os.environ.get("NTFY_TOPIC", "")


# ── Notification ──────────────────────────────────────────────────────────────

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
                "Tags":         "triangular_flag_on_post",
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


# ── Single scan ───────────────────────────────────────────────────────────────

def scan_once(state_file: str) -> None:
    api_key, access_token = _kite_creds()
    ntfy_topic = _ntfy_topic()

    if not (api_key and access_token):
        print("ERROR: KITE_API_KEY and KITE_ACCESS_TOKEN are required.")
        sys.exit(1)

    now_ist = datetime.datetime.now(_IST)
    print(f"[{now_ist.strftime('%H:%M:%S IST')}] Red Flag scan — all F&O stocks")

    if not (MARKET_OPEN <= now_ist.time() <= MARKET_CLOSE):
        print("Market closed — no scan needed.")
        return

    print("  Running scan (this takes 2-3 minutes)...")
    try:
        df = run_red_flag_scan(api_key=api_key, access_token=access_token)
    except Exception as e:
        print(f"  ERROR: {e}")
        return

    if df.empty:
        print("  No results returned.")
        return

    # Filter to STRONG CE and STRONG PE only
    strong = df[df["trade"].isin(ALERT_TRADES)].copy()
    print(f"  Total flagged: {len(df)} | STRONG signals: {len(strong)}")

    if strong.empty:
        print("  No STRONG CE / STRONG PE signals this scan.")
        return

    # Load previous state to avoid duplicate alerts
    try:
        prev_state = json.loads(pathlib.Path(state_file).read_text())
    except Exception:
        prev_state = {}

    # Build current signal map: {symbol: trade}
    current = {row["symbol"]: row["trade"] for _, row in strong.iterrows()}

    # Only alert on new or changed signals
    new_signals = {
        sym: trade for sym, trade in current.items()
        if prev_state.get(sym) != trade
    }

    if not new_signals:
        print("  No new signals since last scan.")
        # Still save state
        pathlib.Path(state_file).write_text(json.dumps(current, indent=2))
        return

    # Group by signal type
    strong_ce = [s for s, t in new_signals.items() if t == "STRONG CE"]
    strong_pe = [s for s, t in new_signals.items() if t == "STRONG PE"]

    lines = [f"Red Flag Scan  {now_ist.strftime('%H:%M IST')}"]
    lines.append(f"New signals: {len(new_signals)}")
    lines.append("")

    if strong_ce:
        lines.append(f"STRONG CE ({len(strong_ce)} stocks):")
        for sym in strong_ce:
            row = df[df["symbol"] == sym].iloc[0]
            chg  = row.get("chg_pct", 0)
            flags = ", ".join(row.get("flags", []))
            lines.append(f"  {sym:15s}  {chg:+.1f}%  [{flags}]")

    if strong_pe:
        if strong_ce:
            lines.append("")
        lines.append(f"STRONG PE ({len(strong_pe)} stocks):")
        for sym in strong_pe:
            row = df[df["symbol"] == sym].iloc[0]
            chg  = row.get("chg_pct", 0)
            flags = ", ".join(row.get("flags", []))
            lines.append(f"  {sym:15s}  {chg:+.1f}%  [{flags}]")

    lines.append("")
    lines.append("Verify before executing. Not financial advice.")

    message = "\n".join(lines)
    title   = f"Red Flag: {len(strong_ce)} STRONG CE | {len(strong_pe)} STRONG PE"

    print(f"\n{'='*55}\n{message}\n{'='*55}\n")

    ok = send_ntfy(title, message, ntfy_topic)
    print(f"  ntfy.sh: {'OK' if ok else 'FAILED'}")

    # Save updated state (full current map, not just new)
    pathlib.Path(state_file).write_text(json.dumps(current, indent=2))
    print("State saved.")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Red Flag signal monitor")
    ap.add_argument("--once", action="store_true",
                    help="Single scan then exit (GitHub Actions mode)")
    ap.add_argument("--state-file", default="/tmp/rf_state.json",
                    help="State file for dedup (default: /tmp/rf_state.json)")
    ap.add_argument("--test-notify", action="store_true",
                    help="Send a test notification and exit")
    args = ap.parse_args()

    if args.test_notify:
        ntfy_topic = _ntfy_topic()
        now  = datetime.datetime.now(_IST).strftime("%H:%M IST")
        title = "Red Flag Bot - Test Notification"
        body  = (
            f"Red Flag Bot is working!\n"
            f"Time: {now}\n\n"
            f"You will receive alerts listing STRONG CE and STRONG PE stocks "
            f"from the F&O universe every 10 minutes during market hours (09:15-15:30 IST)."
        )
        print(body)
        ok = send_ntfy(title, body, _ntfy_topic())
        print(f"ntfy.sh: {'OK' if ok else 'FAILED'}")
    elif args.once:
        scan_once(args.state_file)
    else:
        print("Use --once for GitHub Actions mode.")
        sys.exit(1)
