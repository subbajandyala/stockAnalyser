"""
Gamma Blast Signal Bot
======================
Runs run_gamma_blast_scan() every minute during the expiry day window
(2:00 PM – 3:30 PM IST) and sends ntfy push on GAMMA BLAST ↑/↓ signals.

Tuesday  → NIFTY   expiry
Thursday → SENSEX  expiry

Cloud / GitHub Actions (--once):
    python scripts/gamma_blast_bot.py --symbol NIFTY --once
    python scripts/gamma_blast_bot.py --symbol SENSEX --once

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
import pandas as pd

from screener.gamma_blast import run_gamma_blast_scan
from screener.trending_oi import fetch_instruments

_IST         = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
BLAST_START  = datetime.time(14, 0)
BLAST_END    = datetime.time(15, 30)

ALERT_SIGNALS = {"GAMMA BLAST ↑", "GAMMA BLAST ↓"}
_EXCH = {"NIFTY": "NFO", "BANKNIFTY": "NFO", "FINNIFTY": "NFO", "SENSEX": "BFO", "BANKEX": "BFO"}


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
                "Priority":     "urgent",
                "Tags":         "rotating_light",
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


# ── Instruments + expiry ──────────────────────────────────────────────────────

def _load_instruments(api_key: str, access_token: str, symbol: str) -> pd.DataFrame:
    print(f"  Fetching instruments for {symbol}...")
    return fetch_instruments(api_key, access_token, symbol)


def _nearest_expiry(instr_df: pd.DataFrame, symbol: str) -> str:
    """Return the nearest expiry date string (YYYY-MM-DD) for today."""
    today = datetime.date.today()
    opts = instr_df[
        (instr_df["name"] == symbol) &
        (instr_df["instrument_type"].isin(["CE", "PE"]))
    ]["expiry"].dropna().unique()
    opts_sorted = sorted(opts)
    # Prefer today's expiry (expiry day), else nearest future
    for exp in opts_sorted:
        try:
            exp_date = datetime.date.fromisoformat(str(exp)[:10])
            if exp_date >= today:
                return str(exp)[:10]
        except Exception:
            continue
    return opts_sorted[0][:10] if opts_sorted else str(today)


# ── Alert formatter ───────────────────────────────────────────────────────────

def _fmt_alert(res: dict, symbol: str) -> tuple[str, str]:
    signal   = res.get("signal", "")
    spot     = res.get("spot", 0)
    atm      = res.get("atm", 0)
    score_ce = res.get("score_ce", 0)
    score_pe = res.get("score_pe", 0)
    strike   = res.get("recommended_strike")
    rtype    = res.get("recommended_type", "")
    ltp      = res.get("ltp_entry")
    sl       = res.get("ltp_sl")
    tgt      = res.get("ltp_target")
    phase    = res.get("phase_label", "")
    now_ist  = datetime.datetime.now(_IST)

    direction = "CE (CALL)" if "↑" in signal else "PE (PUT)"
    emoji     = "💥"

    lines = [
        f"{emoji} GAMMA BLAST — {symbol}",
        f"Time  : {now_ist.strftime('%H:%M IST')}",
        f"Signal: {signal}",
        f"Phase : {phase}",
        f"",
        f"Spot  : Rs.{spot:,.0f}   ATM: {int(atm)}",
        f"Buy   : {symbol} {int(strike) if strike else 'ATM'} {rtype or direction}",
    ]

    if ltp:
        lines.append(f"LTP   : Rs.{ltp:.1f}")
    if sl:
        lines.append(f"SL    : Rs.{sl:.1f}  (50%)")
    if tgt:
        lines.append(f"Target: Rs.{tgt:.1f}  (3x)")

    lines += [
        f"Score : CE {score_ce} | PE {score_pe}",
        f"",
        f"EXPIRY DAY — act fast. Not financial advice.",
    ]

    title = f"GAMMA BLAST {symbol} | {signal} | Strike {int(strike) if strike else 'ATM'}"
    return title, "\n".join(lines)


# ── State management ──────────────────────────────────────────────────────────

def _load_state(path: str) -> dict:
    try:
        return json.loads(pathlib.Path(path).read_text())
    except Exception:
        return {}


def _save_state(path: str, state: dict) -> None:
    try:
        pathlib.Path(path).write_text(json.dumps(state, default=str, indent=2))
    except Exception as e:
        print(f"  Warning: could not save state: {e}")


# ── Single scan ───────────────────────────────────────────────────────────────

def scan_once(symbol: str, state_file: str) -> None:
    api_key, access_token = _kite_creds()
    ntfy_topic = _ntfy_topic()

    if not (api_key and access_token):
        print("ERROR: KITE_API_KEY and KITE_ACCESS_TOKEN are required.")
        sys.exit(1)

    now_ist = datetime.datetime.now(_IST)
    print(f"[{now_ist.strftime('%H:%M:%S IST')}] Gamma Blast scan — {symbol}")

    if not (BLAST_START <= now_ist.time() <= BLAST_END):
        print(f"Outside blast window (2:00-3:30 PM IST) — skipping.")
        return

    state = _load_state(state_file)

    # Load instruments (cached in state to avoid repeated downloads)
    instr_df = None
    if "instr_json" in state:
        try:
            instr_df = pd.read_json(state["instr_json"])
            print(f"  Instruments loaded from state ({len(instr_df)} rows)")
        except Exception:
            instr_df = None

    if instr_df is None or instr_df.empty:
        instr_df = _load_instruments(api_key, access_token, symbol)
        if instr_df.empty:
            print("  ERROR: Could not fetch instruments.")
            return
        state["instr_json"] = instr_df.to_json()

    expiry = _nearest_expiry(instr_df, symbol)
    print(f"  Expiry: {expiry}")

    # Restore prev scan state
    prev_chain    = state.get("prev_chain")
    spot_history  = state.get("spot_history", [])
    last_signal   = state.get("last_signal", "")

    try:
        res = run_gamma_blast_scan(
            api_key, access_token,
            symbol, expiry, instr_df,
            spot_history=spot_history if spot_history else None,
            prev_chain=prev_chain,
        )
    except Exception as e:
        print(f"  ERROR: {e}")
        return

    if res.get("error"):
        print(f"  {res['error']}")
        return

    signal = res.get("signal", "WAIT")
    spot   = res.get("spot", 0)
    score_ce = res.get("score_ce", 0)
    score_pe = res.get("score_pe", 0)

    marker = "💥" if signal in ALERT_SIGNALS else ("⚡" if signal not in ("WAIT", "WATCH") else "  ")
    print(f"  {marker} {symbol:12s} | {signal:20s} | CE:{score_ce} PE:{score_pe} | Rs.{spot:,.0f}")

    # Update state
    spot_history.append(spot)
    state["spot_history"] = spot_history[-20:]
    state["prev_chain"]   = res.get("new_prev_chain")

    # Alert on GAMMA BLAST ↑ or ↓ — send every time (1-min window is urgent)
    if signal in ALERT_SIGNALS:
        title, message = _fmt_alert(res, symbol)
        print(f"\n{'='*55}\n{message}\n{'='*55}\n")

        if ntfy_topic:
            ok = send_ntfy(title, message, ntfy_topic)
            print(f"  ntfy.sh: {'OK' if ok else 'FAILED'}")
        else:
            print("  No ntfy topic configured.")

    state["last_signal"] = signal
    _save_state(state_file, state)
    print("State saved.")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Gamma Blast signal monitor")
    ap.add_argument("--symbol", default="NIFTY",
                    help="Index to scan: NIFTY (Tuesday) or SENSEX (Thursday)")
    ap.add_argument("--once", action="store_true",
                    help="Single scan then exit (GitHub Actions mode)")
    ap.add_argument("--state-file", default="/tmp/gb_state.json",
                    help="State file for prev_chain/spot_history")
    ap.add_argument("--test-notify", action="store_true",
                    help="Send a test notification and exit")
    args = ap.parse_args()

    symbol = args.symbol.upper()

    if args.test_notify:
        ntfy_topic = _ntfy_topic()
        now  = datetime.datetime.now(_IST).strftime("%H:%M IST")
        title = "Gamma Blast Bot - Test Notification"
        body  = (
            f"Gamma Blast Bot is working!\n"
            f"Time: {now}\n\n"
            f"Watching: {symbol}\n"
            f"Window  : 2:00 PM - 3:30 PM IST (expiry days only)\n\n"
            f"You will receive URGENT alerts on GAMMA BLAST signals."
        )
        print(body)
        ok = send_ntfy(title, body, ntfy_topic)
        print(f"ntfy.sh: {'OK' if ok else 'FAILED'}")
    elif args.once:
        scan_once(symbol, args.state_file)
    else:
        print("Use --once for GitHub Actions mode.")
        sys.exit(1)
