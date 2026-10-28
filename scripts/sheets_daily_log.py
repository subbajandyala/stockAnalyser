"""
Daily Google Sheets journal — the free replacement for the Notion journal.

After market close it writes the day's chart_bot activity for this machine's
account to one shared Google Sheet with two tabs, like the two Notion pages:
  Trades  one row per bot order: account, date, time, contract, entry / exit
          from the real Kite fills (FIFO-matched), P&L, result
  Daily   one row per account per day: signals, trades, wins, losses, day P&L,
          how often the target / stop came first, notes

The sheet is written through a small Google Apps Script web app (no Google key
file on the machine); scripts/sheets_apps_script.gs has the code and the setup
steps. Re-running the same day updates rows instead of duplicating them.

Needs in /root/stockAnalyser/.env (never committed):
  SHEETS_WEBHOOK_URL=https://script.google.com/macros/s/.../exec
  SHEETS_SECRET=<the same secret word as in the Apps Script>
The account name comes from NOTION_ACCOUNT, else KITE_NAME, else Kite's short name.

Cron (server clock is UTC; 15:52 IST = 10:22 UTC; give each machine its own minute):
  22 10 * * 1-5 cd /root/stockAnalyser && /root/venv/bin/python scripts/sheets_daily_log.py >> sheets_log.log 2>&1

Usage:
  python scripts/sheets_daily_log.py                   # today
  python scripts/sheets_daily_log.py --date 2026-10-27 # an earlier day (no Kite fills: entry/exit blank)
  python scripts/sheets_daily_log.py --dry-run         # print the rows, don't send
"""

import argparse
import datetime
import json
import pathlib
import sys
from collections import defaultdict

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import pandas as pd
import requests
from kiteconnect import KiteConnect

from scripts.chart_bot import _kite_creds
import scripts.notion_daily_log as nd

_IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


def _num(v):
    try:
        f = float(v)
        return None if f != f else round(f, 2)
    except (TypeError, ValueError):
        return None


def _diff(a, b) -> float | None:
    """a − b in premium points (SL Diff = entry − stop, Target Diff = target − entry)."""
    a, b = _num(a), _num(b)
    return None if a is None or b is None else round(a - b, 2)


def _held(t1: str | None, t2: str | None) -> float | None:
    """Minutes between two HH:MM:SS fill times."""
    try:
        a, b = (datetime.datetime.strptime(t, "%H:%M:%S") for t in (t1, t2))
        return round((b - a).total_seconds() / 60, 1)
    except (TypeError, ValueError):
        return None


def collect(date_str: str, account: str) -> dict:
    """The day's rows for this account: {"trades": [...], "daily": {...}}."""
    try:
        nd.outcomes.process(date_str)
    except Exception as e:
        print(f"  signal outcomes failed (continuing): {e}")

    log = nd._load_log(date_str)
    signals = [s for s in log if s.get("direction") in ("CE", "PE") and s.get("auto_trade") != "no signal"]
    # "placed" and "placed (ride)" are both real orders
    bot_trades = [s for s in log if str(s.get("auto_trade", "")).startswith("placed") and s.get("order_id")]

    fills = []
    today = datetime.datetime.now(_IST).strftime("%Y-%m-%d")
    if bot_trades and date_str == today:
        api_key, access_token = _kite_creds()
        k = KiteConnect(api_key=api_key)
        k.set_access_token(access_token)
        try:
            fills = k.trades()
        except Exception as e:
            print(f"  Kite trades() failed: {e}")
    elif bot_trades:
        print("  Kite only returns today's fills — exit/P&L left blank for past dates")
    matched = nd.match_fills(bot_trades, fills)

    trades, wins, losses, day_pnl = [], 0, 0, 0.0
    for t in bot_trades:
        oid = str(t["order_id"])
        m = matched.get(oid, {})
        pnl = m.get("pnl") if m.get("closed_qty") else None
        left = (m.get("qty", 0) - m.get("closed_qty", 0)) if m else 0
        result = "Open" if (not m or left > 0) else ("Win" if (pnl or 0) > 0 else "Loss")
        wins += result == "Win"
        losses += result == "Loss"
        day_pnl += pnl or 0
        trades.append({
            "Order ID": oid, "Account": account, "Date": date_str,
            "Time": str(t.get("time", "")).split()[0],
            "Contract": t.get("tradingsymbol", ""), "Symbol": t.get("symbol", ""),
            "Direction": t.get("direction", ""), "Candle": t.get("verdict", ""),
            "Ride": "yes" if "ride" in str(t.get("auto_trade", "")) else "",
            "Qty": _num(m.get("qty") or t.get("qty")),
            "Entry": _num(m.get("entry") or t.get("entry_premium")),
            "SL": _num(t.get("sl_premium")), "Target": _num(t.get("target_premium")),
            "Exit": _num(m.get("exit")), "P&L": _num(pnl), "Result": result,
            "SL Diff": _diff(m.get("entry") or t.get("entry_premium"), t.get("sl_premium")),
            "Target Diff": _diff(t.get("target_premium"), m.get("entry") or t.get("entry_premium")),
            "Entry time": m.get("entry_time", ""), "Exit time": m.get("exit_time", ""),
            "Held (min)": _held(m.get("entry_time"), m.get("exit_time")),
        })

    oc = pd.DataFrame()
    if nd.outcomes._CSV.exists():
        oc = pd.read_csv(nd.outcomes._CSV, dtype=str)
        oc = oc[(oc["date"] == date_str) & (oc["source"] == "live")]
    decided = oc[oc["first_hit"].isin(["target", "sl"])] if not oc.empty else oc
    tgt_pct = (decided["first_hit"] == "target").mean() if len(decided) else None
    sl_pct = (decided["first_hit"] == "sl").mean() if len(decided) else None

    skipped = defaultdict(int)
    for s in signals:
        at = str(s.get("auto_trade", ""))
        if at.startswith(("skipped:", "blocked:")):
            skipped[at.split(":", 1)[1].split("—")[0].strip()[:60]] += 1
    notes = f"{len(signals)} signals, {len(bot_trades)} orders. P&L is chart_bot trades only."
    if skipped:
        notes += " Top skip reasons: " + "; ".join(f"{k} ×{v}" for k, v in sorted(skipped.items(), key=lambda x: -x[1])[:3])

    daily = {
        "Day": f"{date_str} · {account}", "Account": account, "Date": date_str,
        "Signals": len(signals), "Trades": len(bot_trades), "Wins": wins, "Losses": losses,
        "Open": sum(1 for t in trades if t["Result"] == "Open"), "Day P&L": round(day_pnl, 2),
        "Target hit first": None if tgt_pct is None else round(float(tgt_pct), 3),
        "SL hit first": None if sl_pct is None else round(float(sl_pct), 3),
        "Notes": notes,
    }
    return {"trades": trades, "daily": daily}


def send(url: str, secret: str, payload: dict) -> dict:
    r = requests.post(url, data=json.dumps({"secret": secret, **payload}),
                      headers={"Content-Type": "application/json"}, timeout=60)
    r.raise_for_status()
    try:
        out = r.json()
    except ValueError:
        raise RuntimeError(f"unexpected reply from the sheet: {r.text[:200]}")
    if not out.get("ok"):
        raise RuntimeError(f"sheet refused the rows: {out.get('error', out)}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Write the day's chart_bot activity to the Google Sheet")
    ap.add_argument("--date", default=datetime.datetime.now(_IST).strftime("%Y-%m-%d"))
    ap.add_argument("--dry-run", action="store_true", help="print the rows, don't send")
    args = ap.parse_args()

    url, secret = nd._env_value("SHEETS_WEBHOOK_URL"), nd._env_value("SHEETS_SECRET")
    if not args.dry_run and not (url and secret):
        sys.exit("SHEETS_WEBHOOK_URL / SHEETS_SECRET missing in .env — see scripts/sheets_apps_script.gs")
    account = nd.account_name()
    payload = collect(args.date, account)
    d = payload["daily"]
    if args.dry_run:
        print(json.dumps(payload, indent=1, default=str))
        return
    out = send(url, secret, payload)
    print(f"Sheet updated for {args.date} ({account}): {d['Trades']} trades, "
          f"P&L ₹{d['Day P&L']:,.0f} — {out.get('trades_added', 0)} added, {out.get('trades_updated', 0)} updated")


if __name__ == "__main__":
    main()
