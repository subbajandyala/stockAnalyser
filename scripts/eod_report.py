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

_INDEX_TOKENS = {
    "NIFTY": 256265, "BANKNIFTY": 260105, "SENSEX": 265,
    "FINNIFTY": 257801, "MIDCPNIFTY": 288009, "INDIAVIX": 264969,
}

_candle_cache: dict = {}  # "SYMBOL:YYYY-MM-DD" -> list of kite candle dicts


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


def _load_chart_log(date_str: str) -> dict:
    p = pathlib.Path(f"/tmp/chart_bot_log_{date_str}.json")
    if not p.exists():
        return {"date": date_str, "signals": []}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {"date": date_str, "signals": []}


def _load_cpr_adx_log(date_str: str) -> dict:
    p = pathlib.Path(f"/tmp/cpr_adx_log_{date_str}.json")
    if not p.exists():
        return {"date": date_str, "signals": []}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {"date": date_str, "signals": []}


def _load_rsi_scalp_log(date_str: str) -> dict:
    p = pathlib.Path(f"/tmp/rsi_scalp_log_{date_str}.json")
    if not p.exists():
        return {"date": date_str, "signals": []}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {"date": date_str, "signals": []}


def _load_agent_flow_log(date_str: str) -> dict:
    p = pathlib.Path(f"/tmp/agent_flow_log_{date_str}.json")
    if not p.exists():
        return {"date": date_str, "signals": []}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {"date": date_str, "signals": []}


def _load_rf_log(date_str: str) -> dict:
    p = pathlib.Path(f"/tmp/rf_signal_log_{date_str}.json")
    if not p.exists():
        return {"date": date_str, "signals": []}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {"date": date_str, "signals": []}


# ── Performance helpers ───────────────────────────────────────────────────────

def _get_instrument_token(symbol: str) -> int | None:
    """Return Kite instrument token for a symbol (index or NSE stock)."""
    if symbol in _INDEX_TOKENS:
        return _INDEX_TOKENS[symbol]
    # Try kite_data module first — it manages its own cache and can refresh
    try:
        from screener.kite_data import get_token as _get_token
        tok = _get_token(symbol)
        if tok:
            return tok
    except Exception:
        pass
    # Fallback: read the cached instruments file directly
    for cache_path in ["/tmp/kite_instr_nse.json", "/tmp/kite_instr_bfo.json"]:
        p = pathlib.Path(cache_path)
        if p.exists():
            try:
                for instr in json.loads(p.read_text()):
                    if (instr.get("tradingsymbol") == symbol
                            and instr.get("segment") in ("NSE", "NSE-EQ", "BFO-FO")):
                        return instr["instrument_token"]
            except Exception:
                pass
    return None


def _fetch_candles(kite: KiteConnect, symbol: str, date_str: str) -> list:
    """Fetch 1-minute candles for symbol on date_str. Cached per symbol."""
    key = f"{symbol}:{date_str}"
    if key in _candle_cache:
        return _candle_cache[key]
    token = _get_instrument_token(symbol)
    if not token:
        _candle_cache[key] = []
        return []
    try:
        d = datetime.datetime.strptime(date_str, "%Y-%m-%d")
        candles = kite.historical_data(
            token,
            d.replace(hour=9, minute=0, second=0),
            d.replace(hour=15, minute=35, second=0),
            "minute",
        )
        _candle_cache[key] = candles
    except Exception as e:
        print(f"  [perf] candles failed {symbol}: {e}")
        _candle_cache[key] = []
    return _candle_cache[key]


def _close_at(candles: list, target_dt: datetime.datetime) -> float | None:
    """Return the close of the first candle at or after target_dt (IST-naive)."""
    for c in candles:
        cdt = c["date"]
        if hasattr(cdt, "tzinfo") and cdt.tzinfo is not None:
            cdt = cdt.replace(tzinfo=None)
        if cdt >= target_dt:
            return float(c["close"])
    return None


def _eod_close(candles: list) -> float | None:
    """Return the last candle close (end-of-day price)."""
    if not candles:
        return None
    return float(candles[-1]["close"])


def _build_perf_rows(
    chart_log: dict,
    cpr_adx_log: dict,
    agent_flow_log: dict,
    kite: KiteConnect | None,
    date_str: str,
) -> list:
    if kite is None:
        return []
    rows = []

    def _parse_hm(t: str):
        t = t.replace(" IST", "").strip()
        try:
            h, m = map(int, t.split(":"))
            return h, m
        except Exception:
            return None, None

    def _process(signals, source, sym_key, spot_key, dir_key):
        base = datetime.datetime.strptime(date_str, "%Y-%m-%d")
        for s in signals:
            symbol = s.get(sym_key, "")
            time_str = s.get("time", "")
            raw_spot = s.get(spot_key)
            direction = s.get(dir_key, "")
            if not symbol or not raw_spot:
                continue
            try:
                spot = float(raw_spot)
            except Exception:
                continue
            h, m = _parse_hm(time_str)
            if h is None:
                continue
            target_dt = base.replace(hour=h, minute=m) + datetime.timedelta(minutes=15)
            candles   = _fetch_candles(kite, symbol, date_str)
            p15       = _close_at(candles, target_dt)
            peod      = _eod_close(candles)
            dir_up    = direction in ("CE", "CALL", "BUY")

            def _outcome(ref_price):
                if ref_price is None:
                    return "", "", "—"
                chg  = round(ref_price - spot, 2)
                pct  = round(chg / spot * 100, 2) if spot else 0
                if chg == 0:
                    ok = "→"
                elif (dir_up and chg > 0) or (not dir_up and chg < 0):
                    ok = "✓"
                else:
                    ok = "✗"
                return chg, pct, ok

            chg_pts, chg_pct, correct      = _outcome(p15)
            eod_pts, eod_pct, eod_correct  = _outcome(peod)

            rows.append({
                "source":      source,
                "time":        time_str,
                "symbol":      symbol,
                "direction":   direction,
                "spot":        spot,
                "price_15m":   round(p15,  2) if p15  is not None else "—",
                "chg_pts":     chg_pts,
                "chg_pct":     chg_pct,
                "correct":     correct,
                "price_eod":   round(peod, 2) if peod is not None else "—",
                "eod_pts":     eod_pts,
                "eod_pct":     eod_pct,
                "eod_correct": eod_correct,
            })

    _process(chart_log.get("signals", []),      "Chart Bot",  "symbol", "spot",  "direction")
    _process(cpr_adx_log.get("signals", []),    "CPR+ADX",    "symbol", "price", "direction")
    _process(agent_flow_log.get("signals", []), "Agent Flow", "symbol", "spot",  "direction")
    return rows


# ── Extract Kite Signals from chart_bot_log ───────────────────────────────────

def _extract_kite_signals(chart_log: dict) -> list:
    """
    Build a Kite Signals list from chart_bot_log entries.
    Includes any signal where auto_trade was attempted (placed, skipped, or failed).
    Also includes signals where auto_trade == "off" so the EOD report shows all
    chart signals and their trade status in one sheet.
    """
    result = []
    for i, s in enumerate(chart_log.get("signals", []), 1):
        auto_trade = s.get("auto_trade", "")
        placed = auto_trade == "placed"
        entry = {
            "id":            i,
            "time":          s.get("time", ""),
            "symbol":        s.get("symbol", ""),
            "direction":     s.get("direction", ""),
            "signal":        s.get("verdict", ""),
            "spot":          s.get("spot", ""),
            "status":        "placed" if placed else ("skipped" if auto_trade.startswith("skipped") else auto_trade),
            "tradingsymbol": s.get("tradingsymbol", "—") if placed else "—",
            "strike":        s.get("strike", "—") if placed else "—",
            "qty":           s.get("qty", "—") if placed else "—",
            "entry_premium": s.get("entry_premium", "—") if placed else "—",
            "sl":            s.get("sl_premium", "—") if placed else "—",
            "target":        s.get("target_premium", "—") if placed else "—",
            "order_id":      s.get("order_id", "—") if placed else "—",
            "gtt_id":        s.get("gtt_id", "—") if placed else "—",
            "error":         auto_trade if not placed and auto_trade else "",
        }
        result.append(entry)
    return result


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


def _write_cpr_adx_sheet(ws, signals: list) -> None:
    ws.title = "CPR + ADX"
    ws.sheet_properties.tabColor = "FF9800"

    headers = [
        "#", "Time", "Symbol", "Direction", "Signal", "Stars", "Score",
        "Price", "ADX", "DI+", "DI−", "CPR Bot", "CPR Top", "CPR Width%",
        "SL", "Target", "R:R", "Upgraded", "Reasons",
    ]
    ws.append(headers)
    for col, h in enumerate(headers, 1):
        cell = ws.cell(1, col)
        cell.font      = Font(bold=True, color=_WHITE, name="Calibri", size=11)
        cell.fill      = _hdr_fill(_HEADER)
        cell.alignment = Alignment(horizontal="center")
        cell.border    = _thin_border()

    for i, s in enumerate(signals, 1):
        direction = s.get("direction", "")
        row_data = [
            i,
            s.get("time", ""),
            s.get("symbol", ""),
            direction,
            s.get("signal", ""),
            s.get("stars", ""),
            s.get("score", ""),
            s.get("price", ""),
            s.get("adx", ""),
            s.get("di_plus", ""),
            s.get("di_minus", ""),
            s.get("cpr_bot", ""),
            s.get("cpr_top", ""),
            s.get("cpr_width", ""),
            s.get("sl", ""),
            s.get("target", ""),
            s.get("rr", ""),
            s.get("upgraded", ""),
            s.get("reasons", ""),
        ]
        ws.append(row_data)
        row_num  = ws.max_row
        dir_cell = ws.cell(row_num, 4)
        if direction == "BUY":
            dir_cell.font = Font(color=_TEAL, bold=True, name="Calibri")
        elif direction == "SHORT":
            dir_cell.font = Font(color="FFFF6B6B", bold=True, name="Calibri")
        for col in range(1, len(headers) + 1):
            ws.cell(row_num, col).border    = _thin_border()
            ws.cell(row_num, col).alignment = Alignment(horizontal="center")

    for col_cells in ws.columns:
        length = max(len(str(c.value or "")) for c in col_cells) + 4
        ws.column_dimensions[get_column_letter(col_cells[0].column)].width = min(length, 40)
    ws.freeze_panes = "A2"


def _write_rsi_scalp_sheet(ws, signals: list) -> None:
    ws.title = "RSI Scalp"
    ws.sheet_properties.tabColor = "E91E63"

    headers = [
        "#", "Time", "Index", "Opt", "Strike", "Spot",
        "RSI", "Prev RSI", "LTP", "Expiry",
        "Auto-Trade", "Tradingsymbol", "Order ID",
    ]
    ws.append(headers)
    for col, h in enumerate(headers, 1):
        cell = ws.cell(1, col)
        cell.font      = Font(bold=True, color=_WHITE, name="Calibri", size=11)
        cell.fill      = _hdr_fill(_HEADER)
        cell.alignment = Alignment(horizontal="center")
        cell.border    = _thin_border()

    for i, s in enumerate(signals, 1):
        opt        = s.get("opt", "")
        auto_trade = s.get("auto_trade", "")
        row_data = [
            i,
            s.get("time", ""),
            s.get("index", ""),
            opt,
            s.get("strike", ""),
            s.get("spot", ""),
            s.get("rsi", ""),
            s.get("prev_rsi", ""),
            s.get("ltp", ""),
            s.get("expiry", ""),
            auto_trade,
            s.get("tradingsymbol", ""),
            s.get("order_id", ""),
        ]
        ws.append(row_data)
        row_num  = ws.max_row
        opt_cell = ws.cell(row_num, 4)
        if opt == "CE":
            opt_cell.font = Font(color=_TEAL, bold=True, name="Calibri")
        elif opt == "PE":
            opt_cell.font = Font(color="FFFF6B6B", bold=True, name="Calibri")
        at_cell = ws.cell(row_num, 11)
        if auto_trade == "placed":
            at_cell.fill = _hdr_fill("FF1A3A2A")
            at_cell.font = Font(color=_TEAL, bold=True, name="Calibri")
        elif auto_trade.startswith(("skipped", "failed")):
            at_cell.fill = _hdr_fill("FF3A1A1A")
            at_cell.font = Font(color="FFFF6B6B", bold=True, name="Calibri")
        for col in range(1, len(headers) + 1):
            ws.cell(row_num, col).border    = _thin_border()
            ws.cell(row_num, col).alignment = Alignment(horizontal="center")

    for col_cells in ws.columns:
        length = max(len(str(c.value or "")) for c in col_cells) + 4
        ws.column_dimensions[get_column_letter(col_cells[0].column)].width = min(length, 40)
    ws.freeze_panes = "A2"


def _write_agent_flow_sheet(ws, signals: list) -> None:
    ws.title = "Agent Flow"
    ws.sheet_properties.tabColor = "2196F3"

    headers = [
        "#", "Time", "Symbol", "Direction", "Bias", "Conf%", "Rating★",
        "Strike", "Spot", "LTP", "Expiry",
        "SL", "T1", "T2", "T3", "R:R",
        "Futures", "GEX", "Signals",
    ]
    ws.append(headers)
    for col, h in enumerate(headers, 1):
        cell = ws.cell(1, col)
        cell.font      = Font(bold=True, color=_WHITE, name="Calibri", size=11)
        cell.fill      = _hdr_fill(_HEADER)
        cell.alignment = Alignment(horizontal="center")
        cell.border    = _thin_border()

    for i, s in enumerate(signals, 1):
        direction = s.get("direction", "")
        rating    = s.get("rating", 0)
        row_data = [
            i,
            s.get("time", ""),
            s.get("symbol", ""),
            direction,
            s.get("bias", ""),
            s.get("confidence", ""),
            "★" * int(rating) if rating else "",
            s.get("strike", ""),
            s.get("spot", ""),
            s.get("ltp", ""),
            s.get("expiry", ""),
            s.get("sl", ""),
            s.get("t1", ""),
            s.get("t2", ""),
            s.get("t3", ""),
            s.get("rr", ""),
            s.get("futures", ""),
            s.get("gex", ""),
            s.get("signals", ""),
        ]
        ws.append(row_data)
        row_num  = ws.max_row
        dir_cell = ws.cell(row_num, 4)
        if direction == "CALL":
            dir_cell.font = Font(color=_TEAL, bold=True, name="Calibri")
        elif direction == "PUT":
            dir_cell.font = Font(color="FFFF6B6B", bold=True, name="Calibri")
        for col in range(1, len(headers) + 1):
            ws.cell(row_num, col).border    = _thin_border()
            ws.cell(row_num, col).alignment = Alignment(horizontal="center")
        # Widen signals column
        ws.cell(row_num, 19).alignment = Alignment(horizontal="left", wrap_text=True)

    for col_cells in ws.columns:
        length = max(len(str(c.value or "")) for c in col_cells) + 4
        ws.column_dimensions[get_column_letter(col_cells[0].column)].width = min(length, 50)
    ws.freeze_panes = "A2"


def _write_chart_bot_sheet(ws, signals: list) -> None:
    ws.title = "Chart Bot"
    ws.sheet_properties.tabColor = "9C27B0"

    headers = [
        "#", "Time", "Symbol", "Direction", "Verdict", "Score", "Conf%",
        "Spot", "15m", "5m", "Entry Low", "Entry High", "Stop Loss",
        "Targets", "R:R", "Auto-Trade",
        "Tradingsymbol", "Strike", "Qty", "Entry ₹", "SL ₹", "Target ₹", "Order ID",
    ]
    ws.append(headers)
    for col, h in enumerate(headers, 1):
        cell = ws.cell(1, col)
        cell.font      = Font(bold=True, color=_WHITE, name="Calibri", size=11)
        cell.fill      = _hdr_fill(_HEADER)
        cell.alignment = Alignment(horizontal="center")
        cell.border    = _thin_border()

    for i, s in enumerate(signals, 1):
        direction  = s.get("direction", "")
        auto_trade = s.get("auto_trade", "")
        row_data = [
            i,
            s.get("time", ""),
            s.get("symbol", ""),
            direction,
            s.get("verdict", ""),
            s.get("score", ""),
            s.get("conf", ""),
            s.get("spot", ""),
            s.get("tf_15m", ""),
            s.get("tf_5m", ""),
            s.get("entry_low", ""),
            s.get("entry_high", ""),
            s.get("stop_loss", ""),
            s.get("targets", ""),
            s.get("rr", ""),
            auto_trade,
            s.get("tradingsymbol", ""),
            s.get("strike", ""),
            s.get("qty", ""),
            s.get("entry_premium", ""),
            s.get("sl_premium", ""),
            s.get("target_premium", ""),
            s.get("order_id", ""),
        ]
        ws.append(row_data)
        row_num = ws.max_row

        # Colour direction cell
        dir_cell = ws.cell(row_num, 4)
        if direction == "CE":
            dir_cell.font = Font(color=_TEAL, bold=True, name="Calibri")
        elif direction == "PE":
            dir_cell.font = Font(color="FFFF6B6B", bold=True, name="Calibri")

        # Colour auto-trade cell
        at_cell = ws.cell(row_num, 16)
        if auto_trade == "placed":
            at_cell.fill = _hdr_fill("FF1A3A2A")
            at_cell.font = Font(color=_TEAL, bold=True, name="Calibri")
        elif auto_trade.startswith("skipped"):
            at_cell.fill = _hdr_fill("FF3A1A1A")
            at_cell.font = Font(color="FFFF6B6B", bold=True, name="Calibri")

        for col in range(1, len(headers) + 1):
            ws.cell(row_num, col).border    = _thin_border()
            ws.cell(row_num, col).alignment = Alignment(horizontal="center")

    for col_cells in ws.columns:
        length = max(len(str(c.value or "")) for c in col_cells) + 4
        ws.column_dimensions[get_column_letter(col_cells[0].column)].width = length

    ws.freeze_panes = "A2"


def _write_performance_sheet(ws, rows: list) -> None:
    ws.title = "Performance"
    ws.sheet_properties.tabColor = "4CAF50"

    headers = [
        "#", "Source", "Time", "Symbol", "Direction", "Spot @ Signal",
        # 15-min block
        "Spot +15min", "+15m Pts", "+15m %", "+15m ✓?",
        # EOD block
        "Spot EOD", "EOD Pts", "EOD %", "EOD ✓?",
    ]
    ws.append(headers)
    # Colour the two performance-block header groups distinctly
    _15M_FILL  = "FF1A3A5A"   # dark blue tint for +15m columns
    _EOD_FILL  = "FF2A1A4A"   # dark purple tint for EOD columns
    for col, h in enumerate(headers, 1):
        cell = ws.cell(1, col)
        cell.font      = Font(bold=True, color=_WHITE, name="Calibri", size=11)
        cell.fill      = _hdr_fill(_15M_FILL if 7 <= col <= 10 else (_EOD_FILL if col >= 11 else _HEADER))
        cell.alignment = Alignment(horizontal="center")
        cell.border    = _thin_border()

    correct_15m = wrong_15m = correct_eod = wrong_eod = 0

    def _color_outcome(ws, row_num, pct_col, cor_col, pct_val, correct, c_counter, w_counter):
        if pct_val != "":
            try:
                ws.cell(row_num, pct_col).font = Font(
                    color=(_TEAL if float(pct_val) > 0 else "FFFF6B6B"),
                    bold=True, name="Calibri",
                )
            except Exception:
                pass
        cor_cell = ws.cell(row_num, cor_col)
        if correct == "✓":
            cor_cell.fill = _hdr_fill("FF1A3A2A")
            cor_cell.font = Font(color=_TEAL, bold=True, name="Calibri")
            c_counter += 1
        elif correct == "✗":
            cor_cell.fill = _hdr_fill("FF3A1A1A")
            cor_cell.font = Font(color="FFFF6B6B", bold=True, name="Calibri")
            w_counter += 1
        return c_counter, w_counter

    for i, r in enumerate(rows, 1):
        direction = r["direction"]
        ws.append([
            i, r["source"], r["time"], r["symbol"], direction, r["spot"],
            r["price_15m"], r["chg_pts"], r["chg_pct"], r["correct"],
            r["price_eod"], r["eod_pts"], r["eod_pct"], r["eod_correct"],
        ])
        row_num = ws.max_row

        dir_cell = ws.cell(row_num, 5)
        if direction in ("CE", "CALL", "BUY"):
            dir_cell.font = Font(color=_TEAL, bold=True, name="Calibri")
        elif direction in ("PE", "PUT", "SHORT"):
            dir_cell.font = Font(color="FFFF6B6B", bold=True, name="Calibri")

        correct_15m, wrong_15m = _color_outcome(
            ws, row_num, 9, 10, r["chg_pct"], r["correct"], correct_15m, wrong_15m
        )
        correct_eod, wrong_eod = _color_outcome(
            ws, row_num, 13, 14, r["eod_pct"], r["eod_correct"], correct_eod, wrong_eod
        )

        for col in range(1, len(headers) + 1):
            ws.cell(row_num, col).border    = _thin_border()
            ws.cell(row_num, col).alignment = Alignment(horizontal="center")

    # Summary row
    if rows:
        def _acc(c, w):
            t = c + w
            return f"{c}/{t} ({round(c/t*100) if t else 0}%)"
        sum_row = ws.max_row + 1
        ws.cell(sum_row, 9).value  = "Accuracy →"
        ws.cell(sum_row, 10).value = _acc(correct_15m, wrong_15m)
        ws.cell(sum_row, 13).value = "Accuracy →"
        ws.cell(sum_row, 14).value = _acc(correct_eod, wrong_eod)
        for col in [9, 10, 13, 14]:
            ws.cell(sum_row, col).font = Font(bold=True, color=_WHITE, name="Calibri")
            ws.cell(sum_row, col).fill = _hdr_fill(_HEADER)
            ws.cell(sum_row, col).alignment = Alignment(horizontal="center")

    for col_cells in ws.columns:
        length = max(len(str(c.value or "")) for c in col_cells) + 4
        ws.column_dimensions[get_column_letter(col_cells[0].column)].width = min(length, 25)
    ws.freeze_panes = "A2"


def _build_excel(
    log: dict,
    rf_log: dict,
    chart_log: dict,
    cpr_adx_log: dict,
    rsi_scalp_log: dict,
    agent_flow_log: dict,
    kite=None,
) -> bytes:
    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    ws_perf        = wb.create_sheet("Performance")
    ws_agent       = wb.create_sheet("Agent Flow")
    ws_chart       = wb.create_sheet("Chart Bot")
    ws_cpr         = wb.create_sheet("CPR + ADX")
    ws_rsi         = wb.create_sheet("RSI Scalp")
    ws_trades      = wb.create_sheet("Trades")
    ws_kite        = wb.create_sheet("Kite Signals")
    ws_all_signals = wb.create_sheet("All Signals")
    ws_rf          = wb.create_sheet("Red Flag Signals")

    perf_rows    = _build_perf_rows(chart_log, cpr_adx_log, agent_flow_log, kite, log["date"])
    kite_signals = _extract_kite_signals(chart_log) or log.get("kite_signals", [])
    _write_performance_sheet(ws_perf, perf_rows)
    _write_agent_flow_sheet(ws_agent, agent_flow_log.get("signals", []))
    _write_chart_bot_sheet(ws_chart, chart_log.get("signals", []))
    _write_cpr_adx_sheet(ws_cpr, cpr_adx_log.get("signals", []))
    _write_rsi_scalp_sheet(ws_rsi, rsi_scalp_log.get("signals", []))
    total_pnl = _write_trades_sheet(ws_trades, log.get("trades", []))
    _write_kite_signals_sheet(ws_kite, kite_signals)
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

    log            = _load_log(date_str)
    rf_log         = _load_rf_log(date_str)
    chart_log      = _load_chart_log(date_str)
    cpr_adx_log    = _load_cpr_adx_log(date_str)
    rsi_scalp_log  = _load_rsi_scalp_log(date_str)
    agent_flow_log = _load_agent_flow_log(date_str)

    chart_signals  = chart_log.get("signals", [])
    kite_from_chart = [s for s in chart_signals if s.get("auto_trade", "off") != "off"]
    print(f"  Agent Flow   : {len(agent_flow_log['signals'])} signals")
    print(f"  Chart Bot    : {len(chart_signals)} signals  ({len(kite_from_chart)} with auto_trade)")
    print(f"  CPR+ADX      : {len(cpr_adx_log['signals'])} signals")
    print(f"  RSI Scalp    : {len(rsi_scalp_log['signals'])} signals")
    print(f"  Elder Ray    : {len(log['signals'])} signals, {len(log['trades'])} trades")
    print(f"  Red Flag     : {len(rf_log['signals'])} signals")

    kite = None
    if api_key and access_token:
        kite = KiteConnect(api_key=api_key)
        kite.set_access_token(access_token)
        print("  Fetching final premiums for open trades...")
        _fill_final_premiums(log["trades"], api_key, access_token)
    else:
        print("  No Kite creds — open trades will show no exit premium")

    print("  Building Excel...")
    excel_bytes, total_pnl = _build_excel(
        log, rf_log, chart_log, cpr_adx_log, rsi_scalp_log, agent_flow_log, kite=kite
    )

    sign = "+" if total_pnl >= 0 else ""
    print(f"  Total P&L: {sign}₹{total_pnl:,.0f}")
    print(f"  Sending via ntfy topic '{topic}'...")

    ok = _send_ntfy_report(topic, date_str, excel_bytes, total_pnl,
                           len(log["signals"]), len(log["trades"]))
    print(f"  ntfy: {'✅ sent' if ok else '❌ failed'}")
