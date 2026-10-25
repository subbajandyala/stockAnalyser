"""
Daily Notion journal — writes the day's chart_bot activity to Notion automatically.

After market close it:
  1. runs signal_outcomes.process(date) (MFE/MAE, target-or-SL-first per signal)
  2. reads the chart_bot signal log + today's Kite fills (kite.trades())
  3. upserts one row per bot order into the "💹 Trades" database
     (entry/exit are FIFO-matched from real fills, so P&L is exact)
  4. upserts one row for the day into "📅 Daily Trading Summary", with the
     signal table and outcome table in the page body

Re-running the same day updates rows instead of duplicating them.

Several machines (one Kite account each) write to the same two databases: every
row carries an "Account" select, and the daily summary is one row per account
per day. The account name is NOTION_ACCOUNT, else KITE_NAME, else the Kite
profile's short name.

Needs in /root/stockAnalyser/.env (never committed):
  NOTION_TOKEN=secret_...         # Notion integration token
Optional overrides:
  NOTION_ACCOUNT=Subba            # name shown in the Account column
  NOTION_DAILY_DB=<database id>
  NOTION_TRADES_DB=<database id>

Cron (server clock is UTC; 15:50 IST = 10:20 UTC):
  20 10 * * 1-5 cd /root/stockAnalyser && /root/venv/bin/python scripts/notion_daily_log.py >> notion_log.log 2>&1

Usage:
  python scripts/notion_daily_log.py                 # today
  python scripts/notion_daily_log.py --date 2026-10-17
  python scripts/notion_daily_log.py --tag-untagged  # once: put this account's name on rows
                                                     # written before the Account column existed
"""

import argparse
import datetime
import json
import pathlib
import sys
import time
from collections import defaultdict

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import pandas as pd
import requests
from kiteconnect import KiteConnect

from scripts.chart_bot import _kite_creds
import scripts.signal_outcomes as outcomes

_ROOT = pathlib.Path(__file__).parent.parent
_IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


def _env_value(name: str) -> str:
    import os
    v = os.environ.get(name, "")
    if v:
        return v
    env_path = _ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, val = line.split("=", 1)
                if k.strip() == name:
                    return val.strip().strip('"').strip("'")
    return ""


_API = "https://api.notion.com/v1"
DAILY_DB  = _env_value("NOTION_DAILY_DB") or "7f3c5ed1148144e1bc954a9670fa9ec1"
TRADES_DB = _env_value("NOTION_TRADES_DB") or "48fc8a2c491c4204a12d8a7172fa0116"
MAX_TABLE_ROWS = 90


# ── Notion API helpers ────────────────────────────────────────────────────────

def _headers() -> dict:
    token = _env_value("NOTION_TOKEN")
    if not token:
        sys.exit("NOTION_TOKEN missing in .env — see the setup steps in this script's docstring.")
    return {"Authorization": f"Bearer {token}", "Notion-Version": "2022-06-28",
            "Content-Type": "application/json"}


def _call(method: str, path: str, body: dict | None = None) -> dict:
    for attempt in range(3):
        r = requests.request(method, f"{_API}{path}", headers=_headers(), json=body, timeout=30)
        if r.status_code == 429:
            time.sleep(float(r.headers.get("Retry-After", 1)))
            continue
        if not r.ok:
            raise RuntimeError(f"Notion {method} {path} → {r.status_code}: {r.text[:300]}")
        time.sleep(0.35)  # stay under 3 requests/second
        return r.json()
    raise RuntimeError(f"Notion {method} {path} rate-limited")


def _find(db: str, prop: str, kind: str, value: str, account: str | None = None) -> str | None:
    flt = {"property": prop, kind: {"equals": value}}
    if account:
        flt = {"and": [flt, {"property": "Account", "select": {"equals": account}}]}
    res = _call("POST", f"/databases/{db}/query", {"filter": flt, "page_size": 1})
    return res["results"][0]["id"] if res.get("results") else None


def _untagged(db: str) -> list[str]:
    """Page ids of rows with no Account yet."""
    ids, cursor = [], None
    while True:
        body = {"filter": {"property": "Account", "select": {"is_empty": True}}, "page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        res = _call("POST", f"/databases/{db}/query", body)
        ids += [r["id"] for r in res.get("results", [])]
        if not res.get("has_more"):
            return ids
        cursor = res.get("next_cursor")


def account_name() -> str:
    name = _env_value("NOTION_ACCOUNT") or _env_value("KITE_NAME")
    if name:
        return name.strip()
    try:
        api_key, access_token = _kite_creds()
        k = KiteConnect(api_key=api_key)
        k.set_access_token(access_token)
        prof = k.profile()
        return prof.get("user_shortname") or prof.get("user_id") or "Main"
    except Exception:
        return "Main"


def _upsert(db: str, page_id: str | None, props: dict) -> str:
    if page_id:
        _call("PATCH", f"/pages/{page_id}", {"properties": props})
        return page_id
    return _call("POST", "/pages", {"parent": {"database_id": db}, "properties": props})["id"]


def _replace_body(page_id: str, blocks: list) -> None:
    cursor = None
    while True:
        q = f"?start_cursor={cursor}" if cursor else ""
        res = _call("GET", f"/blocks/{page_id}/children{q}")
        for b in res.get("results", []):
            _call("DELETE", f"/blocks/{b['id']}")
        if not res.get("has_more"):
            break
        cursor = res.get("next_cursor")
    for i in range(0, len(blocks), 50):
        _call("PATCH", f"/blocks/{page_id}/children", {"children": blocks[i:i + 50]})


def _txt(s) -> list:
    return [{"type": "text", "text": {"content": str(s)[:1900]}}]


def _num(v):
    try:
        f = float(v)
        return None if f != f else round(f, 2)
    except (TypeError, ValueError):
        return None


def _heading(s: str) -> dict:
    return {"type": "heading_2", "heading_2": {"rich_text": _txt(s)}}


def _para(s: str) -> dict:
    return {"type": "paragraph", "paragraph": {"rich_text": _txt(s)}}


def _table(header: list, rows: list) -> dict:
    rows = rows[:MAX_TABLE_ROWS]
    cells = lambda r: {"type": "table_row", "table_row": {"cells": [_txt("" if c is None else c) for c in r]}}
    return {"type": "table", "table": {"table_width": len(header), "has_column_header": True,
                                       "has_row_header": False, "children": [cells(header)] + [cells(r) for r in rows]}}


# ── Data ──────────────────────────────────────────────────────────────────────

def _load_log(date_str: str) -> list[dict]:
    p = pathlib.Path(f"/tmp/chart_bot_log_{date_str}.json")
    try:
        return json.loads(p.read_text()).get("signals", []) if p.exists() else []
    except Exception:
        return []


def match_fills(bot_trades: list[dict], fills: list[dict]) -> dict:
    """FIFO-match each bot BUY order to later SELL fills of the same contract.

    Returns {order_id: {"entry", "exit", "qty", "closed_qty", "pnl"}}.
    """
    by_ts = defaultdict(list)
    for f in fills:
        by_ts[f["tradingsymbol"]].append(f)
    out = {}
    for ts, fl in by_ts.items():
        fl.sort(key=lambda f: str(f.get("fill_timestamp") or f.get("exchange_timestamp") or ""))
        buys = defaultdict(lambda: [0, 0.0])        # order_id → [qty, cost]
        for f in fl:
            if f["transaction_type"] == "BUY":
                b = buys[str(f["order_id"])]
                b[0] += int(f["quantity"]); b[1] += int(f["quantity"]) * float(f["average_price"])
        queue = [str(t["order_id"]) for t in bot_trades if t.get("tradingsymbol") == ts and str(t["order_id"]) in buys]
        state = {oid: {"qty": buys[oid][0], "entry": buys[oid][1] / buys[oid][0] if buys[oid][0] else 0,
                       "closed_qty": 0, "proceeds": 0.0} for oid in queue}
        qi = 0
        for f in fl:
            if f["transaction_type"] != "SELL":
                continue
            left = int(f["quantity"])
            while left > 0 and qi < len(queue):
                s = state[queue[qi]]
                take = min(left, s["qty"] - s["closed_qty"])
                s["closed_qty"] += take
                s["proceeds"] += take * float(f["average_price"])
                left -= take
                if s["closed_qty"] >= s["qty"]:
                    qi += 1
        for oid, s in state.items():
            exit_px = s["proceeds"] / s["closed_qty"] if s["closed_qty"] else None
            out[oid] = {"entry": s["entry"], "exit": exit_px, "qty": s["qty"], "closed_qty": s["closed_qty"],
                        "pnl": s["proceeds"] - s["closed_qty"] * s["entry"]}
    return out


# ── Main ──────────────────────────────────────────────────────────────────────

def tag_untagged(account: str) -> None:
    for db, label in ((TRADES_DB, "trades"), (DAILY_DB, "daily rows")):
        ids = _untagged(db)
        for pid in ids:
            _call("PATCH", f"/pages/{pid}", {"properties": {"Account": {"select": {"name": account}}}})
        print(f"Tagged {len(ids)} {label} as {account}")


def run(date_str: str) -> None:
    account = account_name()
    try:
        outcomes.process(date_str)
    except Exception as e:
        print(f"  signal outcomes failed (continuing): {e}")

    log = _load_log(date_str)
    signals = [s for s in log if s.get("direction") in ("CE", "PE") and s.get("auto_trade") != "no signal"]
    bot_trades = [s for s in log if s.get("auto_trade") == "placed" and s.get("order_id")]

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
    matched = match_fills(bot_trades, fills)

    # Trades database
    wins = losses = 0
    day_pnl = 0.0
    trade_rows = []
    for t in bot_trades:
        oid = str(t["order_id"])
        m = matched.get(oid, {})
        pnl = m.get("pnl") if m.get("closed_qty") else None
        open_qty = (m.get("qty", 0) - m.get("closed_qty", 0)) if m else 0
        result = "Open" if (not m or open_qty > 0) else ("Win" if (pnl or 0) > 0 else "Loss")
        if result == "Win":
            wins += 1
        elif result == "Loss":
            losses += 1
        day_pnl += pnl or 0
        hhmm = str(t.get("time", "")).split()[0] or "00:00"
        props = {
            "Contract":  {"title": _txt(t.get("tradingsymbol", ""))},
            "Date":      {"date": {"start": f"{date_str}T{hhmm}:00+05:30"}},
            "Symbol":    {"select": {"name": t.get("symbol", "")}},
            "Direction": {"select": {"name": t.get("direction", "")}},
            "Score":     {"number": _num(t.get("score"))},
            "Qty":       {"number": _num(m.get("qty") or t.get("qty"))},
            "Entry":     {"number": _num(m.get("entry") or t.get("entry_premium"))},
            "Exit":      {"number": _num(m.get("exit"))},
            "P&L":       {"number": _num(pnl)},
            "Result":    {"select": {"name": result}},
            "Order ID":  {"rich_text": _txt(oid)},
            "Account":   {"select": {"name": account}},
        }
        _upsert(TRADES_DB, _find(TRADES_DB, "Order ID", "rich_text", oid), props)
        trade_rows.append([hhmm, t.get("tradingsymbol", ""), t.get("direction", ""), m.get("qty") or t.get("qty", ""),
                           _num(m.get("entry") or t.get("entry_premium")), _num(m.get("exit")) or "", _num(pnl) or "", result])

    # Outcome stats for the day (live signals only)
    oc = pd.DataFrame()
    if outcomes._CSV.exists():
        oc = pd.read_csv(outcomes._CSV, dtype=str)
        oc = oc[(oc["date"] == date_str) & (oc["source"] == "live")]
    decided = oc[oc["first_hit"].isin(["target", "sl"])] if not oc.empty else oc
    tgt_pct = (decided["first_hit"] == "target").mean() if len(decided) else None
    sl_pct  = (decided["first_hit"] == "sl").mean() if len(decided) else None

    skipped = defaultdict(int)
    for s in signals:
        at = str(s.get("auto_trade", ""))
        if at.startswith(("skipped:", "blocked:")):
            skipped[at.split(":", 1)[1].split("—")[0].strip()[:60]] += 1
    notes = f"{account}: {len(signals)} signals, {len(bot_trades)} orders. P&L is chart_bot trades only."
    if skipped:
        notes += " Top skip reasons: " + "; ".join(f"{k} ×{v}" for k, v in sorted(skipped.items(), key=lambda x: -x[1])[:3])

    props = {
        "Day":              {"title": _txt(f"{date_str} · {account}")},
        "Account":          {"select": {"name": account}},
        "Date":             {"date": {"start": date_str}},
        "Signals":          {"number": len(signals)},
        "Trades":           {"number": len(bot_trades)},
        "Wins":             {"number": wins},
        "Losses":           {"number": losses},
        "Day P&L":          {"number": round(day_pnl, 2)},
        "Target hit first": {"number": None if tgt_pct is None else round(float(tgt_pct), 3)},
        "SL hit first":     {"number": None if sl_pct is None else round(float(sl_pct), 3)},
        "Notes":            {"rich_text": _txt(notes)},
    }
    page = _upsert(DAILY_DB, _find(DAILY_DB, "Date", "date", date_str, account), props)

    blocks = [_heading("Trades")]
    blocks.append(_table(["Time", "Contract", "Dir", "Qty", "Entry", "Exit", "P&L", "Result"], trade_rows)
                  if trade_rows else _para("No orders placed."))
    blocks.append(_heading("Signals"))
    sig_rows = [[s.get("time", ""), s.get("symbol", ""), s.get("verdict", ""), s.get("score", ""),
                 s.get("tf_5m", ""), s.get("noise", ""), str(s.get("auto_trade", ""))[:80]] for s in signals]
    blocks.append(_table(["Time", "Symbol", "Verdict", "Score", "5m", "Noise", "Action"], sig_rows)
                  if sig_rows else _para("No signals."))
    blocks.append(_heading("What the market did after each signal"))
    if not oc.empty:
        oc_rows = [[r["time"], r["symbol"], r["direction"], r["score"], r["mfe30"], r["mae30"],
                    r["eod_move"], r["first_hit"]] for _, r in oc.iterrows()]
        blocks.append(_table(["Time", "Symbol", "Dir", "Score", "Best 30m (pts)", "Worst 30m (pts)",
                              "EOD move (pts)", "Hit first"], oc_rows))
    else:
        blocks.append(_para("No outcome data."))
    _replace_body(page, blocks)
    print(f"Notion updated for {date_str} ({account}): {len(signals)} signals, {len(bot_trades)} trades, P&L ₹{day_pnl:,.0f}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Write the day's chart_bot activity to Notion")
    ap.add_argument("--date", default=datetime.datetime.now(_IST).strftime("%Y-%m-%d"))
    ap.add_argument("--tag-untagged", action="store_true",
                    help="set this machine's account name on rows that have no Account yet, then exit")
    args = ap.parse_args()
    if args.tag_untagged:
        tag_untagged(account_name())
    else:
        run(args.date)
