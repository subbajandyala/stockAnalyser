"""
EOD Trade Report
================
Runs at 4:00 PM IST. Reads today's trade log, fetches final premiums
from Kite, builds an Excel workbook, and emails it to the configured address.

Cron (4:00 PM IST = 10:30 UTC):
    30 10 * * 1-5 /root/venv/bin/python /root/stockAnalyser/scripts/eod_report.py >> /root/eod_report.log 2>&1

Requires in ~/.kite_creds.json:
    "gmail_user"         : "yourname@gmail.com"
    "gmail_app_password" : "xxxx xxxx xxxx xxxx"   ← Google App Password (not your login password)
    "report_to"          : "subba.jandyala@gmail.com"
"""

import datetime
import io
import json
import os
import pathlib
import smtplib
import sys
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

from kiteconnect import KiteConnect

_IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

_EXCHANGE = {"NIFTY": "NFO", "SENSEX": "BFO"}
_LOT_SIZE = {"NIFTY": 65, "SENSEX": 20}


# ── Credentials ───────────────────────────────────────────────────────────────

def _creds() -> dict:
    p = pathlib.Path.home() / ".kite_creds.json"
    try:
        c = json.loads(p.read_text()) if p.exists() else {}
    except Exception:
        c = {}
    c.setdefault("api_key",          os.getenv("KITE_API_KEY", ""))
    c.setdefault("access_token",     os.getenv("KITE_ACCESS_TOKEN", ""))
    c.setdefault("gmail_user",       os.getenv("GMAIL_USER", ""))
    c.setdefault("gmail_app_password", os.getenv("GMAIL_APP_PASSWORD", ""))
    c.setdefault("report_to",        "subba.jandyala@gmail.com")
    return c


# ── Trade log ─────────────────────────────────────────────────────────────────

def _load_log(date_str: str) -> dict:
    p = pathlib.Path(f"/tmp/er_trade_log_{date_str}.json")
    if not p.exists():
        return {"date": date_str, "signals": [], "trades": []}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {"date": date_str, "signals": [], "trades": []}


def _load_rf_log(date_str: str) -> dict:
    p = pathlib.Path(f"/tmp/rf_signal_log_{date_str}.json")
    if not p.exists():
        return {"date": date_str, "signals": []}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {"date": date_str, "signals": []}


# ── Fetch final premiums for open trades ──────────────────────────────────────

def _fill_final_premiums(trades: list, api_key: str, access_token: str) -> None:
    """For trades with no exit yet, fetch LTP as the final premium."""
    open_ts = {
        t["tradingsymbol"]: t
        for t in trades
        if t.get("exit_time") is None and t.get("tradingsymbol")
    }
    if not open_ts:
        return

    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)

    for sym, trade in open_ts.items():
        instrument_sym = trade.get("symbol", "NIFTY")
        exchange       = _EXCHANGE.get(instrument_sym, "NFO")
        lot            = _LOT_SIZE.get(instrument_sym, 65)
        key            = f"{exchange}:{sym}"
        try:
            q   = kite.ltp([key])
            ltp = float(q.get(key, {}).get("last_price", 0))
            if ltp > 0:
                trade["exit_premium"] = round(ltp, 1)
                trade["exit_reason"]  = "eod_ltp"
                pnl_pts               = round(ltp - trade["entry_premium"], 1)
                trade["pnl_pts"]      = pnl_pts
                trade["pnl_inr"]      = round(pnl_pts * trade["qty"], 0)
        except Exception as e:
            print(f"  LTP fetch failed for {sym}: {e}")


# ── Excel builder ─────────────────────────────────────────────────────────────

_GREEN  = "FF00AA6C"
_RED    = "FFD32F2F"
_TEAL   = "FF00D4AA"
_DARK   = "FF161B2E"
_HEADER = "FF1E2A45"
_WHITE  = "FFFFFFFF"
_AMBER  = "FFFFC107"


def _hdr_fill(hex_color: str) -> PatternFill:
    return PatternFill("solid", fgColor=hex_color)


def _thin_border() -> Border:
    s = Side(style="thin", color="FF333333")
    return Border(left=s, right=s, top=s, bottom=s)


def _write_signals_sheet(ws, signals: list, date_str: str) -> None:
    ws.title = "All Signals"
    ws.sheet_properties.tabColor = "00D4AA"

    headers = ["Time", "Symbol", "Signal", "Spot Price", "ATM Strike"]
    ws.append(headers)
    for col, h in enumerate(headers, 1):
        cell = ws.cell(1, col)
        cell.font      = Font(bold=True, color=_WHITE, name="Calibri", size=11)
        cell.fill      = _hdr_fill(_HEADER)
        cell.alignment = Alignment(horizontal="center")
        cell.border    = _thin_border()

    for row in signals:
        ws.append([
            row.get("time", ""),
            row.get("symbol", ""),
            row.get("signal", ""),
            row.get("spot", ""),
            row.get("atm", ""),
        ])

    for col_cells in ws.columns:
        length = max(len(str(c.value or "")) for c in col_cells) + 4
        ws.column_dimensions[get_column_letter(col_cells[0].column)].width = length

    ws.freeze_panes = "A2"


def _write_trades_sheet(ws, trades: list) -> None:
    ws.title = "Trades"
    ws.sheet_properties.tabColor = "387ED1"

    headers = [
        "#", "Entry Time", "Symbol", "Dir", "Tradingsymbol",
        "Strike", "Qty", "Entry ₹", "Target ₹", "Stop ₹",
        "Exit Time", "Exit ₹", "Reason", "P&L pts", "P&L ₹",
    ]
    ws.append(headers)
    for col, h in enumerate(headers, 1):
        cell = ws.cell(1, col)
        cell.font      = Font(bold=True, color=_WHITE, name="Calibri", size=11)
        cell.fill      = _hdr_fill(_HEADER)
        cell.alignment = Alignment(horizontal="center")
        cell.border    = _thin_border()

    total_pnl = 0.0
    for t in trades:
        pnl_pts = t.get("pnl_pts")
        pnl_inr = t.get("pnl_inr", 0) or 0
        total_pnl += pnl_inr

        row_data = [
            t.get("id", ""),
            t.get("entry_time", ""),
            t.get("symbol", ""),
            t.get("direction", ""),
            t.get("tradingsymbol", ""),
            t.get("strike", ""),
            t.get("qty", ""),
            t.get("entry_premium", ""),
            t.get("target_premium", ""),
            t.get("stop_premium", ""),
            t.get("exit_time", "—"),
            t.get("exit_premium", "—"),
            t.get("exit_reason", "open"),
            pnl_pts if pnl_pts is not None else "—",
            pnl_inr if pnl_pts is not None else "—",
        ]
        ws.append(row_data)

        row_num = ws.max_row
        pnl_cell = ws.cell(row_num, 15)
        if pnl_inr > 0:
            pnl_cell.fill = _hdr_fill("FF1A3A2A")
            pnl_cell.font = Font(color=_TEAL, bold=True, name="Calibri")
        elif pnl_inr < 0:
            pnl_cell.fill = _hdr_fill("FF3A1A1A")
            pnl_cell.font = Font(color="FFFF6B6B", bold=True, name="Calibri")

        for col in range(1, 16):
            ws.cell(row_num, col).border = _thin_border()
            ws.cell(row_num, col).alignment = Alignment(horizontal="center")

    # Total row
    total_row = ws.max_row + 1
    ws.cell(total_row, 14).value = "TOTAL"
    ws.cell(total_row, 14).font  = Font(bold=True, color=_WHITE, name="Calibri")
    ws.cell(total_row, 15).value = round(total_pnl, 0)
    total_fill = _hdr_fill(_GREEN if total_pnl >= 0 else _RED)
    for col in [14, 15]:
        ws.cell(total_row, col).fill      = total_fill
        ws.cell(total_row, col).font      = Font(bold=True, color=_WHITE, name="Calibri")
        ws.cell(total_row, col).alignment = Alignment(horizontal="center")
        ws.cell(total_row, col).border    = _thin_border()

    for col_cells in ws.columns:
        length = max(len(str(c.value or "")) for c in col_cells) + 4
        ws.column_dimensions[get_column_letter(col_cells[0].column)].width = length

    ws.freeze_panes = "A2"
    return total_pnl


def _write_kite_signals_sheet(ws, kite_signals: list) -> None:
    ws.title = "Kite Signals"
    ws.sheet_properties.tabColor = "FFC107"

    headers = [
        "#", "Time", "Symbol", "Direction", "Signal", "Spot",
        "Status", "Tradingsymbol", "Strike", "Qty",
        "Entry ₹", "SL ₹", "Target ₹", "Order ID", "GTT ID", "Error",
    ]
    ws.append(headers)
    for col, h in enumerate(headers, 1):
        cell = ws.cell(1, col)
        cell.font      = Font(bold=True, color=_WHITE, name="Calibri", size=11)
        cell.fill      = _hdr_fill(_HEADER)
        cell.alignment = Alignment(horizontal="center")
        cell.border    = _thin_border()

    for k in kite_signals:
        placed = k.get("status") == "placed"
        row_data = [
            k.get("id", ""),
            k.get("time", ""),
            k.get("symbol", ""),
            k.get("direction", ""),
            k.get("signal", ""),
            k.get("spot", ""),
            k.get("status", ""),
            k.get("tradingsymbol", "—") if placed else "—",
            k.get("strike", "—")        if placed else "—",
            k.get("qty", "—")           if placed else "—",
            k.get("entry_premium", "—") if placed else "—",
            k.get("sl", "—")            if placed else "—",
            k.get("target", "—")        if placed else "—",
            k.get("order_id", "—")      if placed else "—",
            k.get("gtt_id", "—")        if placed else "—",
            k.get("error", "")          if not placed else "",
        ]
        ws.append(row_data)
        row_num = ws.max_row
        status_cell = ws.cell(row_num, 7)
        if placed:
            status_cell.fill = _hdr_fill("FF1A3A2A")
            status_cell.font = Font(color=_TEAL, bold=True, name="Calibri")
        else:
            status_cell.fill = _hdr_fill("FF3A1A1A")
            status_cell.font = Font(color="FFFF6B6B", bold=True, name="Calibri")
        for col in range(1, 17):
            ws.cell(row_num, col).border    = _thin_border()
            ws.cell(row_num, col).alignment = Alignment(horizontal="center")

    for col_cells in ws.columns:
        length = max(len(str(c.value or "")) for c in col_cells) + 4
        ws.column_dimensions[get_column_letter(col_cells[0].column)].width = length

    ws.freeze_panes = "A2"


def _write_rf_signals_sheet(ws, signals: list) -> None:
    ws.title = "Red Flag Signals"
    ws.sheet_properties.tabColor = "D32F2F"

    headers = ["Time", "Symbol", "Signal", "Chg %", "Flags"]
    ws.append(headers)
    for col, h in enumerate(headers, 1):
        cell = ws.cell(1, col)
        cell.font      = Font(bold=True, color=_WHITE, name="Calibri", size=11)
        cell.fill      = _hdr_fill(_HEADER)
        cell.alignment = Alignment(horizontal="center")
        cell.border    = _thin_border()

    for s in signals:
        chg = s.get("chg_pct", 0)
        ws.append([
            s.get("time", ""),
            s.get("symbol", ""),
            s.get("signal", ""),
            chg,
            s.get("flags", ""),
        ])
        row_num   = ws.max_row
        chg_cell  = ws.cell(row_num, 4)
        sig_cell  = ws.cell(row_num, 3)
        is_ce     = "CE" in str(s.get("signal", ""))
        sig_cell.font = Font(
            color=_TEAL if is_ce else "FFFF6B6B",
            bold=True, name="Calibri",
        )
        chg_cell.font = Font(
            color=("FF00AA6C" if chg >= 0 else "FFD32F2F"),
            bold=True, name="Calibri",
        )
        for col in range(1, 6):
            ws.cell(row_num, col).border    = _thin_border()
            ws.cell(row_num, col).alignment = Alignment(horizontal="center")

    for col_cells in ws.columns:
        length = max(len(str(c.value or "")) for c in col_cells) + 4
        ws.column_dimensions[get_column_letter(col_cells[0].column)].width = length

    ws.freeze_panes = "A2"


def _build_excel(log: dict, rf_log: dict) -> bytes:
    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    ws_trades      = wb.create_sheet("Trades")
    ws_kite        = wb.create_sheet("Kite Signals")
    ws_all_signals = wb.create_sheet("All Signals")
    ws_rf          = wb.create_sheet("Red Flag Signals")

    total_pnl = _write_trades_sheet(ws_trades, log.get("trades", []))
    _write_kite_signals_sheet(ws_kite, log.get("kite_signals", []))
    _write_signals_sheet(ws_all_signals, log.get("signals", []), log["date"])
    _write_rf_signals_sheet(ws_rf, rf_log.get("signals", []))

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue(), total_pnl


# ── Delivery: ntfy attachment ──────────────────────────────────────────────────

import requests as _requests


def _eod_topic() -> str:
    p = pathlib.Path.home() / ".kite_creds.json"
    try:
        c = json.loads(p.read_text()) if p.exists() else {}
    except Exception:
        c = {}
    # dedicated EOD topic; falls back to main ntfy_topic if not set
    return os.getenv("EOD_NTFY_TOPIC") or c.get("eod_ntfy_topic") or c.get("ntfy_topic", "")


def _send_ntfy_report(
    topic: str,
    date_str: str,
    excel_bytes: bytes,
    total_pnl: float,
    n_signals: int,
    n_trades: int,
) -> bool:
    if not topic:
        print("  No EOD ntfy topic configured")
        return False
    sign     = "+" if total_pnl >= 0 else ""
    title    = f"EOD Report {date_str} | P&L {sign}₹{total_pnl:,.0f} | {n_trades} trades"
    filename = f"elder_ray_{date_str}.xlsx"
    try:
        r = _requests.post(
            f"https://ntfy.sh/{topic}",
            data=excel_bytes,
            headers={
                "Title":        title.encode("utf-8"),
                "Filename":     filename,
                "Tags":         "bar_chart",
                "Priority":     "default",
                "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            },
            timeout=30,
        )
        if not r.ok:
            print(f"  ntfy HTTP {r.status_code}: {r.text[:200]}")
        return r.ok
    except Exception as e:
        print(f"  ntfy failed: {e}")
        return False


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    now_ist  = datetime.datetime.now(_IST)
    date_str = now_ist.strftime("%Y-%m-%d")
    print(f"[{now_ist.strftime('%H:%M IST')}] EOD report for {date_str}")

    creds        = _creds()
    api_key      = creds["api_key"]
    access_token = creds["access_token"]
    topic        = _eod_topic()

    log    = _load_log(date_str)
    rf_log = _load_rf_log(date_str)
    print(f"  Signals     : {len(log['signals'])}")
    print(f"  Trades      : {len(log['trades'])}")
    print(f"  RF Signals  : {len(rf_log['signals'])}")

    if api_key and access_token:
        print("  Fetching final premiums for open trades...")
        _fill_final_premiums(log["trades"], api_key, access_token)
    else:
        print("  No Kite creds — open trades will show no exit premium")

    print("  Building Excel...")
    excel_bytes, total_pnl = _build_excel(log, rf_log)

    sign = "+" if total_pnl >= 0 else ""
    print(f"  Total P&L: {sign}₹{total_pnl:,.0f}")
    print(f"  Sending via ntfy topic '{topic}'...")

    ok = _send_ntfy_report(topic, date_str, excel_bytes, total_pnl,
                           len(log["signals"]), len(log["trades"]))
    print(f"  ntfy: {'✅ sent' if ok else '❌ failed'}")
