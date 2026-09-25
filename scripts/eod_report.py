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
    ws.title = "Signals"
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


def _build_excel(log: dict) -> bytes:
    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    ws_trades  = wb.create_sheet("Trades")
    ws_signals = wb.create_sheet("Signals")

    total_pnl = _write_trades_sheet(ws_trades, log.get("trades", []))
    _write_signals_sheet(ws_signals, log.get("signals", []), log["date"])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue(), total_pnl


# ── Email ─────────────────────────────────────────────────────────────────────

def _send_email(
    gmail_user: str,
    app_password: str,
    to_addr: str,
    date_str: str,
    excel_bytes: bytes,
    total_pnl: float,
    n_signals: int,
    n_trades: int,
) -> bool:
    sign = "+" if total_pnl >= 0 else ""
    subject = (
        f"Elder Ray EOD Report {date_str} | "
        f"P&L {sign}₹{total_pnl:,.0f} | "
        f"{n_trades} trades | {n_signals} signals"
    )

    body = (
        f"Elder Ray EOD Report — {date_str}\n\n"
        f"Signals fired : {n_signals}\n"
        f"Trades        : {n_trades}\n"
        f"Total P&L     : {sign}₹{total_pnl:,.0f}\n\n"
        f"See the attached Excel for full details.\n\n"
        f"—\nElder Ray Bot | Not financial advice."
    )

    msg = MIMEMultipart()
    msg["From"]    = gmail_user
    msg["To"]      = to_addr
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    part = MIMEBase("application", "octet-stream")
    part.set_payload(excel_bytes)
    encoders.encode_base64(part)
    part.add_header(
        "Content-Disposition",
        f'attachment; filename="elder_ray_{date_str}.xlsx"',
    )
    msg.attach(part)

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as s:
            s.ehlo()
            s.starttls()
            s.login(gmail_user, app_password)
            s.sendmail(gmail_user, to_addr, msg.as_string())
        return True
    except Exception as e:
        print(f"  Email failed: {e}")
        return False


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    now_ist   = datetime.datetime.now(_IST)
    date_str  = now_ist.strftime("%Y-%m-%d")
    print(f"[{now_ist.strftime('%H:%M IST')}] EOD report for {date_str}")

    creds = _creds()
    api_key      = creds["api_key"]
    access_token = creds["access_token"]
    gmail_user   = creds["gmail_user"]
    app_password = creds["gmail_app_password"]
    report_to    = creds["report_to"]

    if not gmail_user or not app_password:
        print("ERROR: gmail_user and gmail_app_password required in ~/.kite_creds.json")
        sys.exit(1)

    log = _load_log(date_str)
    print(f"  Signals : {len(log['signals'])}")
    print(f"  Trades  : {len(log['trades'])}")

    if api_key and access_token:
        print("  Fetching final premiums for open trades...")
        _fill_final_premiums(log["trades"], api_key, access_token)
    else:
        print("  No Kite creds — open trades will show no exit premium")

    print("  Building Excel...")
    excel_bytes, total_pnl = _build_excel(log)

    sign = "+" if total_pnl >= 0 else ""
    print(f"  Total P&L: {sign}₹{total_pnl:,.0f}")
    print(f"  Emailing to {report_to}...")

    ok = _send_email(
        gmail_user, app_password, report_to,
        date_str, excel_bytes, total_pnl,
        len(log["signals"]), len(log["trades"]),
    )
    print(f"  Email: {'✅ sent' if ok else '❌ failed'}")
