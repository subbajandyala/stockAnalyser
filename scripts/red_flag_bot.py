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
    # Prefer a dedicated red-flag topic; fall back to the shared one
    return os.environ.get("NTFY_RF_TOPIC", "") or os.environ.get("NTFY_TOPIC", "")


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

def _rf_log_path(date_str: str) -> pathlib.Path:
    return pathlib.Path(f"/tmp/rf_signal_log_{date_str}.json")


def _load_rf_log(date_str: str) -> dict:
    p = _rf_log_path(date_str)
    try:
        return json.loads(p.read_text()) if p.exists() else {"date": date_str, "signals": []}
    except Exception:
        return {"date": date_str, "signals": []}


def _log_rf_signals(date_str: str, signals: dict, df) -> None:
    log  = _load_rf_log(date_str)
    now  = datetime.datetime.now(_IST).strftime("%H:%M")
    # track already-logged (symbol, signal) pairs to avoid duplicates
    already = {(e["symbol"], e["signal"]) for e in log["signals"]}
    added = False
    for sym, trade in signals.items():
        if (sym, trade) in already:
            continue
        row = df[df["symbol"] == sym]
        if row.empty:
            continue
        row = row.iloc[0]
        log["signals"].append({
            "time":    now,
            "symbol":  sym,
            "signal":  trade,
            "chg_pct": round(float(row.get("chg_pct", 0)), 2),
            "flags":   ", ".join(row.get("flags", [])) if isinstance(row.get("flags"), list) else str(row.get("flags", "")),
        })
        added = True
    if added:
        _rf_log_path(date_str).write_text(json.dumps(log, indent=2))


def _load_state(state_file: str) -> dict:
    """Load state file; migrates old {sym: trade_str} format to new {sym: {...}} format."""
    try:
        raw = json.loads(pathlib.Path(state_file).read_text())
        migrated = {}
        for sym, val in raw.items():
            if isinstance(val, str):
                # old format — no timestamps available
                migrated[sym] = {"trade": val, "first_seen": "", "last_seen": ""}
            else:
                migrated[sym] = val
        return migrated
    except Exception:
        return {}


def scan_once(state_file: str) -> None:
    api_key, access_token = _kite_creds()
    ntfy_topic = _ntfy_topic()

    if not (api_key and access_token):
        print("ERROR: KITE_API_KEY and KITE_ACCESS_TOKEN are required.")
        sys.exit(1)

    now_ist  = datetime.datetime.now(_IST)
    now_hhmm = now_ist.strftime("%H:%M")
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

    # Load previous state
    prev_state = _load_state(state_file)

    # Build current signal map: {symbol: trade_str}
    current_trades = {row["symbol"]: row["trade"] for _, row in strong.iterrows()}

    # Merge timestamps: preserve first_seen; update last_seen
    new_state: dict = {}
    new_signals: dict = {}  # sym → trade for newly appeared / changed signals

    for sym, trade in current_trades.items():
        prev = prev_state.get(sym, {})
        prev_trade   = prev.get("trade", "")   if isinstance(prev, dict) else prev
        prev_first   = prev.get("first_seen", "") if isinstance(prev, dict) else ""

        new_state[sym] = {
            "trade":      trade,
            "first_seen": prev_first if prev_first else now_hhmm,
            "last_seen":  now_hhmm,
        }

        if prev_trade != trade:
            new_signals[sym] = trade

    today_str = now_ist.strftime("%Y-%m-%d")

    # Always update last_seen in state even when no new signals
    pathlib.Path(state_file).write_text(json.dumps(new_state, indent=2))

    if not new_signals:
        print("  No new signals since last scan.")
        _log_rf_signals(today_str, current_trades, df)
        return

    # log all current strong signals (dedup inside)
    _log_rf_signals(today_str, current_trades, df)

    # Group by signal type
    strong_ce = [s for s, t in new_signals.items() if t == "STRONG CE"]
    strong_pe = [s for s, t in new_signals.items() if t == "STRONG PE"]

    lines = [f"Red Flag Scan  {now_hhmm} IST"]
    lines.append(f"New signals: {len(new_signals)}")
    lines.append("")

    def _fmt_row(sym: str) -> str:
        row   = df[df["symbol"] == sym].iloc[0]
        chg   = row.get("chg_pct", 0)
        flags = ", ".join(row.get("flags", []))
        entry = new_state.get(sym, {})
        first = entry.get("first_seen", "")
        last  = entry.get("last_seen",  "")
        ts_part = f"  first:{first}" if first else ""
        if last and last != first:
            ts_part += f"  last:{last}"
        return f"  {sym:15s}  {chg:+.1f}%  [{flags}]{ts_part}"

    if strong_ce:
        lines.append(f"STRONG CE ({len(strong_ce)} stocks):")
        for sym in strong_ce:
            lines.append(_fmt_row(sym))

    if strong_pe:
        if strong_ce:
            lines.append("")
        lines.append(f"STRONG PE ({len(strong_pe)} stocks):")
        for sym in strong_pe:
            lines.append(_fmt_row(sym))

    lines.append("")
    lines.append("Verify before executing. Not financial advice.")

    message = "\n".join(lines)
    title   = f"Red Flag: {len(strong_ce)} STRONG CE | {len(strong_pe)} STRONG PE"

    print(f"\n{'='*55}\n{message}\n{'='*55}\n")

    ok = send_ntfy(title, message, ntfy_topic)
    print(f"  ntfy.sh: {'OK' if ok else 'FAILED'}")
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
