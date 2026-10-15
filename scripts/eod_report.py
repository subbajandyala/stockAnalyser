"""
EOD Trade Report
================
Runs at 4:05 PM IST (or on demand). Reads today's bot logs, fetches
spot candles from Kite, builds a focused Excel workbook, sends via ntfy.

Sheets:
  1. Summary    — day overview, signal counts, accuracy stats
  2. Chart Bot  — every signal with +15m and EOD spot performance inline
  3. Agent Flow — every signal with +15m and EOD spot performance inline

Performance measures whether the underlying spot moved in the right
direction after the signal was issued — not option premium.

Cron (4:05 PM IST = 10:35 UTC):
    35 10 * * 1-5 /root/venv/bin/python /root/stockAnalyser/scripts/eod_report.py >> /root/eod_report.log 2>&1

On demand:
    /root/venv/bin/python scripts/eod_report.py
    /root/venv/bin/python scripts/eod_report.py --date 2026-10-14
"""

import argparse
import datetime
import io
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
import requests as _requests
from kiteconnect import KiteConnect

_IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

_INDEX_TOKENS = {
    "NIFTY": 256265, "BANKNIFTY": 260105, "SENSEX": 265,
    "FINNIFTY": 257801, "MIDCPNIFTY": 288009, "INDIAVIX": 264969,
}

_candle_cache: dict = {}   # symbol:date_str → list of kite candle dicts


# ── Credentials ───────────────────────────────────────────────────────────────

def _creds() -> dict:
    env_vals: dict[str, str] = {}
    env_path = pathlib.Path(__file__).parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env_vals[k.strip()] = v.strip().strip('"').strip("'")
    p = pathlib.Path.home() / ".kite_creds.json"
    try:
        c = json.loads(p.read_text()) if p.exists() else {}
    except Exception:
        c = {}
    c["api_key"]      = (os.getenv("KITE_API_KEY")      or env_vals.get("KITE_API_KEY")      or c.get("api_key",      ""))
    c["access_token"] = (os.getenv("KITE_ACCESS_TOKEN") or env_vals.get("KITE_ACCESS_TOKEN") or c.get("access_token", ""))
    return c


# ── Log loaders ───────────────────────────────────────────────────────────────

def _load_chart_log(date_str: str) -> dict:
    p = pathlib.Path(f"/tmp/chart_bot_log_{date_str}.json")
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


def _load_cpr_log(date_str: str) -> dict:
    p = pathlib.Path(f"/tmp/cpr_scan_log_{date_str}.json")
    if not p.exists():
        return {"date": date_str, "signals": []}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {"date": date_str, "signals": []}


# ── Candle / performance helpers ──────────────────────────────────────────────

def _get_instrument_token(symbol: str) -> int | None:
    if symbol in _INDEX_TOKENS:
        return _INDEX_TOKENS[symbol]
    try:
        from screener.kite_data import get_token as _get_token
        tok = _get_token(symbol)
        if tok:
            return tok
    except Exception:
        pass
    return None


def _fetch_candles(kite: KiteConnect, symbol: str, date_str: str) -> list:
    key = f"{symbol}:{date_str}"
    if key in _candle_cache:
        return _candle_cache[key]
    token = _get_instrument_token(symbol)
    if not token:
        print(f"  [perf] no token for {symbol}")
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
        print(f"  [perf] {symbol}: {len(candles)} candles")
    except Exception as e:
        print(f"  [perf] candles failed for {symbol}: {e}")
        _candle_cache[key] = []
    return _candle_cache[key]


def _close_at(candles: list, target_dt: datetime.datetime) -> float | None:
    for c in candles:
        cdt = c["date"]
        if hasattr(cdt, "tzinfo") and cdt.tzinfo is not None:
            cdt = cdt.replace(tzinfo=None)
        if cdt >= target_dt:
            return float(c["close"])
    return None


def _eod_close(candles: list) -> float | None:
    return float(candles[-1]["close"]) if candles else None


def _perf(
    spot: float, price: float | None, direction: str
) -> tuple[str | float, str | float, str | float, str]:
    """(price_str, pts, pct%, outcome) — outcome is ✓ / ✗ / → / —."""
    if price is None:
        return "—", "—", "—", "—"
    pts = round(price - spot, 2)
    pct = round(pts / spot * 100, 2) if spot else 0
    dir_up = direction.upper() in ("CE", "CALL", "BUY")
    if pts == 0:
        outcome = "→"
    elif (dir_up and pts > 0) or (not dir_up and pts < 0):
        outcome = "✓"
    else:
        outcome = "✗"
    return round(price, 2), pts, pct, outcome


def _sig_perf(
    s: dict, kite: KiteConnect | None, date_str: str,
    sym_key: str = "symbol", spot_key: str = "spot", dir_key: str = "direction",
) -> tuple:
    """Fetch +15m and EOD performance for a single signal dict.
    Returns (p15_price, p15_pts, p15_pct, p15_ok, eod_price, eod_pts, eod_pct, eod_ok).
    """
    blank = ("—", "—", "—", "—", "—", "—", "—", "—")
    if kite is None:
        return blank

    symbol    = s.get(sym_key, "")
    time_str  = s.get("time", "")
    direction = s.get(dir_key, "")
    spot_raw  = s.get(spot_key)

    if not symbol or not spot_raw:
        return blank
    try:
        spot = float(spot_raw)
    except Exception:
        return blank

    candles = _fetch_candles(kite, symbol, date_str)
    if not candles:
        return blank

    try:
        t_clean = time_str.replace(" IST", "").strip()
        h_val, m_val = map(int, t_clean.split(":"))
        base    = datetime.datetime.strptime(date_str, "%Y-%m-%d")
        sig_dt  = base.replace(hour=h_val, minute=m_val)
        dt_15m  = sig_dt + datetime.timedelta(minutes=15)

        p15_val = _close_at(candles, dt_15m)
        eod_val = _eod_close(candles)

        p15 = _perf(spot, p15_val, direction)
        eod = _perf(spot, eod_val, direction)
        return (*p15, *eod)
    except Exception:
        return blank


# ── Style helpers ─────────────────────────────────────────────────────────────

_GREEN  = "FF00AA6C"
_RED    = "FFD32F2F"
_TEAL   = "FF00D4AA"
_HEADER = "FF1E2A45"
_WHITE  = "FFFFFFFF"
_AMBER  = "FFFFC107"
_15M_H  = "FF1A3A5A"   # dark blue — +15m header group
_EOD_H  = "FF2A1A4A"   # dark purple — EOD header group
_HIT_G  = "FF1A3A2A"   # dark green bg — correct
_HIT_R  = "FF3A1A1A"   # dark red bg — wrong


def _fill(hex_color: str) -> PatternFill:
    return PatternFill("solid", fgColor=hex_color)


def _border() -> Border:
    s = Side(style="thin", color="FF444444")
    return Border(left=s, right=s, top=s, bottom=s)


def _hdr(ws, row: int, col: int, text: str, bg: str | None = None) -> None:
    cell = ws.cell(row, col, text)
    cell.font      = Font(bold=True, color=_WHITE, name="Calibri", size=10)
    cell.fill      = _fill(bg or _HEADER)
    cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    cell.border    = _border()


def _cell(ws, row: int, col: int, value,
          fg: str | None = None, bg: str | None = None,
          bold: bool = False, wrap: bool = False) -> None:
    cell = ws.cell(row, col, value)
    cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=wrap)
    cell.border    = _border()
    if fg or bold:
        cell.font = Font(color=fg or _WHITE, bold=bold, name="Calibri")
    if bg:
        cell.fill = _fill(bg)


def _autowidth(ws, max_w: int = 45) -> None:
    for col_cells in ws.columns:
        try:
            w = max(len(str(c.value or "")) for c in col_cells) + 4
            ws.column_dimensions[get_column_letter(col_cells[0].column)].width = min(w, max_w)
        except Exception:
            pass


def _colour_perf(ws, row: int, pts_col: int, ok_col: int, pts, ok) -> None:
    """Colour the pts cell green/red and the outcome cell with bg."""
    if pts not in ("—", "", None):
        try:
            fv = float(pts)
            ws.cell(row, pts_col).font = Font(
                color=(_GREEN if fv > 0 else ("FFFF6B6B" if fv < 0 else "FF888888")),
                bold=True, name="Calibri",
            )
        except Exception:
            pass
    ok_cell = ws.cell(row, ok_col)
    if ok == "✓":
        ok_cell.fill = _fill(_HIT_G)
        ok_cell.font = Font(color=_TEAL, bold=True, name="Calibri")
    elif ok == "✗":
        ok_cell.fill = _fill(_HIT_R)
        ok_cell.font = Font(color="FFFF6B6B", bold=True, name="Calibri")


# ── Sheet: CPR Scan ───────────────────────────────────────────────────────────

_CPR_ORANGE = "FFFF9800"

_CPR_HEADERS = [
    ("#",             _HEADER), ("Time",          _HEADER), ("Symbol",   _HEADER),
    ("Direction",     _HEADER), ("Spot @ Signal",  _HEADER),
    ("TC",            _HEADER), ("BC",             _HEADER), ("PP",      _HEADER),
    ("Narrow",        _HEADER), ("Width %",        _HEADER),
    # +15m
    ("+15m Spot", _15M_H), ("+15m Pts", _15M_H), ("+15m %", _15M_H), ("+15m ✓?", _15M_H),
    # EOD
    ("EOD Spot",  _EOD_H), ("EOD Pts",  _EOD_H), ("EOD %",  _EOD_H), ("EOD ✓?",  _EOD_H),
]


def _write_cpr_scan(ws, signals: list, kite, date_str: str) -> dict:
    ws.title = "CPR Scan"
    ws.sheet_properties.tabColor = "FF9800"
    ws.row_dimensions[1].height = 36

    for col, (h, bg) in enumerate(_CPR_HEADERS, 1):
        _hdr(ws, 1, col, h, bg=bg)

    c15 = w15 = ce = we = 0

    for i, s in enumerate(signals, 1):
        direction = s.get("direction", "")

        p15_price, p15_pts, p15_pct, p15_ok, \
        eod_price, eod_pts, eod_pct, eod_ok = _sig_perf(s, kite, date_str)

        if p15_ok == "✓": c15 += 1
        elif p15_ok == "✗": w15 += 1
        if eod_ok == "✓": ce += 1
        elif eod_ok == "✗": we += 1

        row = [
            i,
            s.get("time", ""),
            s.get("symbol", ""),
            direction,
            s.get("spot", ""),
            s.get("tc", ""),
            s.get("bc", ""),
            s.get("pp", ""),
            "✓" if s.get("narrow") else "",
            s.get("width", ""),
            # +15m
            p15_price, p15_pts, p15_pct, p15_ok,
            # EOD
            eod_price, eod_pts, eod_pct, eod_ok,
        ]

        rn = ws.max_row + 1
        for col, val in enumerate(row, 1):
            _cell(ws, rn, col, val)

        if direction == "CE":
            ws.cell(rn, 4).font = Font(color=_TEAL, bold=True, name="Calibri")
        elif direction == "PE":
            ws.cell(rn, 4).font = Font(color="FFFF6B6B", bold=True, name="Calibri")

        # pts at col 12, ok at col 14 for +15m
        _colour_perf(ws, rn, 12, 14, p15_pts, p15_ok)
        # pts at col 16, ok at col 18 for EOD
        _colour_perf(ws, rn, 16, 18, eod_pts, eod_ok)

    _autowidth(ws)
    ws.freeze_panes = "A2"
    return {"c15": c15, "w15": w15, "ce": ce, "we": we}


# ── Sheet: Summary ────────────────────────────────────────────────────────────

def _write_summary(ws, date_str: str, cb_stats: dict, af_stats: dict,
                   cpr_stats: dict, n_chart: int, n_af: int, n_cpr: int,
                   n_exec: int) -> None:
    ws.title = "Summary"
    ws.sheet_properties.tabColor = "00D4AA"
    now_ist = datetime.datetime.now(_IST)

    # Title banner
    ws.merge_cells("A1:I1")
    c = ws.cell(1, 1, f"EOD Report — {date_str}   |   Generated {now_ist.strftime('%H:%M IST')}")
    c.font      = Font(bold=True, color=_WHITE, name="Calibri", size=13)
    c.fill      = _fill(_HEADER)
    c.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 26

    # Stats table
    headers = ["Source", "Signals", "Executed Trades",
               "+15m Correct", "+15m Wrong", "+15m Accuracy",
               "EOD Correct",  "EOD Wrong",  "EOD Accuracy"]
    for col, h in enumerate(headers, 1):
        _hdr(ws, 3, col, h)

    rows = [
        ("Chart Bot",  n_chart, n_exec, cb_stats),
        ("Agent Flow", n_af,    "—",    af_stats),
        ("CPR Scan",   n_cpr,   "—",    cpr_stats),
    ]
    for r_idx, (label, n_sigs, n_ex, stats) in enumerate(rows, 4):
        c15 = stats.get("c15", 0)
        w15 = stats.get("w15", 0)
        ce  = stats.get("ce",  0)
        we  = stats.get("we",  0)
        t15 = c15 + w15
        te  = ce  + we
        acc15 = f"{round(c15 / t15 * 100)}%" if t15 else "—"
        acce  = f"{round(ce  / te  * 100)}%" if te  else "—"

        for col, val in enumerate([label, n_sigs, n_ex, c15, w15, acc15, ce, we, acce], 1):
            fg = _TEAL if col == 6 and t15 and c15 / t15 >= 0.6 else (
                 "FFFF6B6B" if col == 6 and t15 and c15 / t15 < 0.4 else None)
            _cell(ws, r_idx, col, val, fg=fg, bold=True)

    _autowidth(ws, max_w=22)
    ws.freeze_panes = "A4"


# ── Sheet: Chart Bot ──────────────────────────────────────────────────────────

_CB_HEADERS = [
    # Basic signal info
    ("#",           _HEADER), ("Time",          _HEADER), ("Symbol",   _HEADER),
    ("Direction",   _HEADER), ("Verdict",        _HEADER), ("Score",    _HEADER),
    ("Conf %",      _HEADER), ("Spot @ Signal",  _HEADER),
    # +15m performance
    ("+15m Spot", _15M_H), ("+15m Pts", _15M_H), ("+15m %", _15M_H), ("+15m ✓?", _15M_H),
    # EOD performance
    ("EOD Spot",  _EOD_H), ("EOD Pts",  _EOD_H), ("EOD %",  _EOD_H), ("EOD ✓?",  _EOD_H),
    # Auto-trade details
    ("Status",        _HEADER), ("Contract",  _HEADER), ("Strike",  _HEADER),
    ("Qty",           _HEADER), ("Entry ₹",   _HEADER), ("SL ₹",    _HEADER),
    ("Target ₹",      _HEADER), ("Order ID",  _HEADER),
]


def _write_chart_bot(ws, signals: list, kite: KiteConnect | None, date_str: str) -> dict:
    ws.title = "Chart Bot"
    ws.sheet_properties.tabColor = "9C27B0"
    ws.row_dimensions[1].height = 36

    for col, (h, bg) in enumerate(_CB_HEADERS, 1):
        _hdr(ws, 1, col, h, bg=bg)

    c15 = w15 = ce = we = 0

    for i, s in enumerate(signals, 1):
        direction  = s.get("direction", "")
        auto_trade = s.get("auto_trade", "")
        placed     = auto_trade == "placed"

        p15_price, p15_pts, p15_pct, p15_ok, \
        eod_price, eod_pts, eod_pct, eod_ok = _sig_perf(s, kite, date_str)

        if p15_ok == "✓": c15 += 1
        elif p15_ok == "✗": w15 += 1
        if eod_ok == "✓": ce += 1
        elif eod_ok == "✗": we += 1

        row = [
            i,
            s.get("time", ""),
            s.get("symbol", ""),
            direction,
            s.get("verdict", ""),
            s.get("score", ""),
            s.get("conf", ""),
            s.get("spot", ""),
            # +15m
            p15_price, p15_pts, p15_pct, p15_ok,
            # EOD
            eod_price, eod_pts, eod_pct, eod_ok,
            # trade
            auto_trade,
            s.get("tradingsymbol", "")  if placed else "",
            s.get("strike", "")         if placed else "",
            s.get("qty", "")            if placed else "",
            s.get("entry_premium", "")  if placed else "",
            s.get("sl_premium", "")     if placed else "",
            s.get("target_premium", "") if placed else "",
            s.get("order_id", "")       if placed else "",
        ]

        rn = ws.max_row + 1
        for col, val in enumerate(row, 1):
            _cell(ws, rn, col, val)

        # Direction colour
        if direction == "CE":
            ws.cell(rn, 4).font = Font(color=_TEAL, bold=True, name="Calibri")
        elif direction == "PE":
            ws.cell(rn, 4).font = Font(color="FFFF6B6B", bold=True, name="Calibri")

        # Performance colours
        _colour_perf(ws, rn, 10, 12, p15_pts, p15_ok)
        _colour_perf(ws, rn, 14, 16, eod_pts, eod_ok)

        # Status colour
        at_cell = ws.cell(rn, 17)
        if placed:
            at_cell.fill = _fill(_HIT_G)
            at_cell.font = Font(color=_TEAL, bold=True, name="Calibri")
        elif auto_trade and auto_trade not in ("off", "no signal") and "no auto-trade" not in auto_trade:
            at_cell.fill = _fill("FF3A2A1A")
            at_cell.font = Font(color=_AMBER, name="Calibri")

    _autowidth(ws)
    ws.freeze_panes = "A2"
    return {"c15": c15, "w15": w15, "ce": ce, "we": we}


# ── Sheet: Agent Flow ─────────────────────────────────────────────────────────

_AF_HEADERS = [
    ("#",          _HEADER), ("Time",         _HEADER), ("Symbol",  _HEADER),
    ("Direction",  _HEADER), ("Bias",         _HEADER), ("Conf %",  _HEADER),
    ("Rating ★",   _HEADER), ("Spot @ Signal", _HEADER),
    # +15m
    ("+15m Spot", _15M_H), ("+15m Pts", _15M_H), ("+15m %", _15M_H), ("+15m ✓?", _15M_H),
    # EOD
    ("EOD Spot",  _EOD_H), ("EOD Pts",  _EOD_H), ("EOD %",  _EOD_H), ("EOD ✓?",  _EOD_H),
    # signal details
    ("Strike", _HEADER), ("SL",  _HEADER), ("T1",      _HEADER),
    ("T2",     _HEADER), ("R:R", _HEADER), ("Signals", _HEADER),
]


def _write_agent_flow(ws, signals: list, kite: KiteConnect | None, date_str: str) -> dict:
    ws.title = "Agent Flow"
    ws.sheet_properties.tabColor = "2196F3"
    ws.row_dimensions[1].height = 36

    for col, (h, bg) in enumerate(_AF_HEADERS, 1):
        _hdr(ws, 1, col, h, bg=bg)

    c15 = w15 = ce = we = 0

    for i, s in enumerate(signals, 1):
        direction = s.get("direction", "")
        rating    = s.get("rating", 0)

        p15_price, p15_pts, p15_pct, p15_ok, \
        eod_price, eod_pts, eod_pct, eod_ok = _sig_perf(s, kite, date_str)

        if p15_ok == "✓": c15 += 1
        elif p15_ok == "✗": w15 += 1
        if eod_ok == "✓": ce += 1
        elif eod_ok == "✗": we += 1

        row = [
            i,
            s.get("time", ""),
            s.get("symbol", ""),
            direction,
            s.get("bias", ""),
            s.get("confidence", ""),
            "★" * int(rating) if (rating and str(rating).isdigit()) else str(rating or ""),
            s.get("spot", ""),
            # +15m
            p15_price, p15_pts, p15_pct, p15_ok,
            # EOD
            eod_price, eod_pts, eod_pct, eod_ok,
            # details
            s.get("strike", ""),
            s.get("sl", ""),
            s.get("t1", ""),
            s.get("t2", ""),
            s.get("rr", ""),
            s.get("signals", ""),
        ]

        rn = ws.max_row + 1
        for col, val in enumerate(row, 1):
            _cell(ws, rn, col, val)

        # Direction colour
        if direction.upper() in ("CE", "CALL"):
            ws.cell(rn, 4).font = Font(color=_TEAL, bold=True, name="Calibri")
        elif direction.upper() in ("PE", "PUT"):
            ws.cell(rn, 4).font = Font(color="FFFF6B6B", bold=True, name="Calibri")

        _colour_perf(ws, rn, 10, 12, p15_pts, p15_ok)
        _colour_perf(ws, rn, 14, 16, eod_pts, eod_ok)

        # Wider signals column
        ws.cell(rn, 22).alignment = Alignment(horizontal="left", wrap_text=True)

    _autowidth(ws)
    ws.freeze_panes = "A2"
    return {"c15": c15, "w15": w15, "ce": ce, "we": we}


# ── Workbook builder ──────────────────────────────────────────────────────────

def _build_excel(
    chart_log: dict,
    agent_flow_log: dict,
    cpr_log: dict,
    kite,
    date_str: str,
) -> tuple[bytes, dict]:
    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    chart_sigs = chart_log.get("signals", [])
    af_sigs    = agent_flow_log.get("signals", [])
    cpr_sigs   = cpr_log.get("signals", [])
    n_exec     = len([s for s in chart_sigs if s.get("auto_trade") == "placed"])

    # Create sheets in display order
    ws_sum   = wb.create_sheet("Summary")
    ws_chart = wb.create_sheet("Chart Bot")
    ws_af    = wb.create_sheet("Agent Flow")
    ws_cpr   = wb.create_sheet("CPR Scan")

    # Write data sheets first so stats are available for Summary
    cb_stats  = _write_chart_bot(ws_chart, chart_sigs, kite, date_str)
    af_stats  = _write_agent_flow(ws_af,   af_sigs,    kite, date_str)
    cpr_stats = _write_cpr_scan(ws_cpr,    cpr_sigs,   kite, date_str)
    _write_summary(ws_sum, date_str, cb_stats, af_stats, cpr_stats,
                   len(chart_sigs), len(af_sigs), len(cpr_sigs), n_exec)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue(), {"cb": cb_stats, "af": af_stats, "cpr": cpr_stats}


# ── ntfy delivery ──────────────────────────────────────────────────────────────

def _ntfy_topic() -> str:
    p = pathlib.Path.home() / ".kite_creds.json"
    try:
        c = json.loads(p.read_text()) if p.exists() else {}
    except Exception:
        c = {}
    return os.getenv("EOD_NTFY_TOPIC") or c.get("eod_ntfy_topic") or c.get("ntfy_topic", "")


def _send_ntfy(topic: str, date_str: str, excel_bytes: bytes, stats: dict) -> bool:
    if not topic:
        print("  No ntfy topic configured — skipping send")
        return False

    cb  = stats.get("cb", {})
    af  = stats.get("af", {})
    t15 = cb.get("c15", 0) + cb.get("w15", 0)
    te  = cb.get("ce",  0) + cb.get("we",  0)
    acc15 = f"{round(cb['c15'] / t15 * 100)}%" if t15 else "—"
    acceod = f"{round(cb['ce']  / te  * 100)}%" if te  else "—"

    title    = f"EOD Report {date_str} | Chart Bot: {acc15} 15m / {acceod} EOD"
    filename = f"eod_report_{date_str}.xlsx"
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
    ap = argparse.ArgumentParser(description="EOD trade report builder")
    ap.add_argument("--date", default="", help="Override date YYYY-MM-DD (default: today IST)")
    args = ap.parse_args()

    now_ist  = datetime.datetime.now(_IST)
    date_str = args.date or now_ist.strftime("%Y-%m-%d")
    print(f"[{now_ist.strftime('%H:%M IST')}] EOD report for {date_str}")

    creds        = _creds()
    api_key      = creds["api_key"]
    access_token = creds["access_token"]
    topic        = _ntfy_topic()

    chart_log      = _load_chart_log(date_str)
    agent_flow_log = _load_agent_flow_log(date_str)
    cpr_log        = _load_cpr_log(date_str)

    chart_sigs = chart_log.get("signals", [])
    af_sigs    = agent_flow_log.get("signals", [])
    cpr_sigs   = cpr_log.get("signals", [])
    n_exec     = len([s for s in chart_sigs if s.get("auto_trade") == "placed"])

    print(f"  Chart Bot  : {len(chart_sigs)} signals  ({n_exec} executed)")
    print(f"  Agent Flow : {len(af_sigs)} signals")
    print(f"  CPR Scan   : {len(cpr_sigs)} signals")

    kite = None
    if api_key and access_token:
        kite = KiteConnect(api_key=api_key)
        kite.set_access_token(access_token)
        print(f"  Kite token : ...{access_token[-8:]}")
    else:
        print("  No Kite creds — performance columns will show '—'")

    print("  Building Excel workbook...")
    excel_bytes, stats = _build_excel(chart_log, agent_flow_log, cpr_log, kite, date_str)

    cb  = stats.get("cb",  {})
    af  = stats.get("af",  {})
    cpr = stats.get("cpr", {})
    t15_cb  = cb.get("c15", 0)  + cb.get("w15", 0)
    te_cb   = cb.get("ce",  0)  + cb.get("we",  0)
    t15_af  = af.get("c15", 0)  + af.get("w15", 0)
    te_af   = af.get("ce",  0)  + af.get("we",  0)
    t15_cpr = cpr.get("c15", 0) + cpr.get("w15", 0)
    te_cpr  = cpr.get("ce",  0) + cpr.get("we",  0)

    print(f"  Chart Bot  +15m : {cb.get('c15',0)}/{t15_cb} correct  "
          f"EOD: {cb.get('ce',0)}/{te_cb} correct")
    print(f"  Agent Flow +15m : {af.get('c15',0)}/{t15_af} correct  "
          f"EOD: {af.get('ce',0)}/{te_af} correct")
    print(f"  CPR Scan   +15m : {cpr.get('c15',0)}/{t15_cpr} correct  "
          f"EOD: {cpr.get('ce',0)}/{te_cpr} correct")

    print(f"  Sending via ntfy '{topic}'...")
    ok = _send_ntfy(topic, date_str, excel_bytes, stats)
    print(f"  ntfy: {'✅ sent' if ok else '❌ failed'}")
