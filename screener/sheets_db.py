"""
Google Sheets data layer — no infrastructure required.

Sheets structure (all in one Google Spreadsheet):
  Tab "Trade Log"    — Agent Flow recommendations
  Tab "Scan History" — screener run summaries
  Tab "Watchlist"    — saved symbols

Setup:
  1. Create a Google Service Account and download the JSON key.
  2. Share your Google Sheet with the service account email (Editor).
  3. Add to Streamlit secrets:
       GOOGLE_SHEET_ID = "your-sheet-id-from-url"
       GOOGLE_CREDS_JSON = '''{ ... paste service account JSON here ... }'''
"""

from __future__ import annotations

import datetime
import json
import os
from typing import Any

import streamlit as st

_TRADE_HEADERS = [
    "Timestamp", "Symbol", "Bias", "Confidence", "Direction",
    "Strike", "LTP", "Expiry", "SL", "T1", "T2", "T3", "RR",
    "Probability", "Rating", "Signals", "Trend", "VIX",
]
_SCAN_HEADERS  = ["Timestamp", "Screener", "Result Count", "Symbols"]
_WATCH_HEADERS = ["Symbol", "Note", "Added"]


def _now() -> str:
    ist = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
    return datetime.datetime.now(ist).strftime("%Y-%m-%d %H:%M:%S")


def _creds_json() -> dict | None:
    raw = ""
    try:
        raw = st.secrets.get("GOOGLE_CREDS_JSON", "")
    except Exception:
        pass
    if not raw:
        raw = os.environ.get("GOOGLE_CREDS_JSON", "")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _sheet_id() -> str:
    try:
        return st.secrets.get("GOOGLE_SHEET_ID", "")
    except Exception:
        return os.environ.get("GOOGLE_SHEET_ID", "")


def _open_sheet():
    import gspread
    from google.oauth2.service_account import Credentials

    creds_info = _creds_json()
    if not creds_info:
        raise RuntimeError("GOOGLE_CREDS_JSON not set in Streamlit secrets")
    sid = _sheet_id()
    if not sid:
        raise RuntimeError("GOOGLE_SHEET_ID not set in Streamlit secrets")

    scopes = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_info, scopes=scopes)
    gc = gspread.authorize(creds)
    return gc.open_by_key(sid)


def _get_or_create_tab(spreadsheet, title: str, headers: list[str]):
    try:
        ws = spreadsheet.worksheet(title)
    except Exception:
        ws = spreadsheet.add_worksheet(title=title, rows=1000, cols=len(headers))
        ws.append_row(headers)
    return ws


# ── Trade Log ─────────────────────────────────────────────────────────────────

def save_trade(analysis: dict) -> None:
    tgt = analysis.get("targets") or {}
    row = [
        _now(),
        analysis.get("symbol", ""),
        analysis.get("bias", ""),
        analysis.get("confidence", ""),
        analysis.get("direction", "NO TRADE"),
        analysis.get("strike", ""),
        analysis.get("ltp", ""),
        analysis.get("expiry", ""),
        tgt.get("sl", ""),
        tgt.get("t1", ""),
        tgt.get("t2", ""),
        tgt.get("t3", ""),
        tgt.get("rr", ""),
        analysis.get("probability", ""),
        analysis.get("rating", ""),
        ", ".join(analysis.get("signals") or []),
        analysis.get("trend", ""),
        analysis.get("vix", ""),
    ]
    sh = _open_sheet()
    ws = _get_or_create_tab(sh, "Trade Log", _TRADE_HEADERS)
    ws.append_row(row)


def get_trade_log(limit: int = 50) -> list[dict]:
    sh = _open_sheet()
    ws = _get_or_create_tab(sh, "Trade Log", _TRADE_HEADERS)
    records = ws.get_all_records()
    return list(reversed(records))[:limit]


# ── Scan History ──────────────────────────────────────────────────────────────

def save_scan(screener: str, result_count: int, symbols: list[str]) -> None:
    row = [_now(), screener, result_count, ", ".join(symbols[:30])]
    sh = _open_sheet()
    ws = _get_or_create_tab(sh, "Scan History", _SCAN_HEADERS)
    ws.append_row(row)


def get_scan_history(limit: int = 30) -> list[dict]:
    sh = _open_sheet()
    ws = _get_or_create_tab(sh, "Scan History", _SCAN_HEADERS)
    records = ws.get_all_records()
    return list(reversed(records))[:limit]


# ── Watchlist ─────────────────────────────────────────────────────────────────

def add_to_watchlist(symbol: str, note: str = "") -> None:
    sh = _open_sheet()
    ws = _get_or_create_tab(sh, "Watchlist", _WATCH_HEADERS)
    existing = [r["Symbol"] for r in ws.get_all_records()]
    if symbol not in existing:
        ws.append_row([symbol, note, _now()])


def remove_from_watchlist(symbol: str) -> None:
    sh = _open_sheet()
    ws = _get_or_create_tab(sh, "Watchlist", _WATCH_HEADERS)
    cell = ws.find(symbol)
    if cell:
        ws.delete_rows(cell.row)


def get_watchlist() -> list[dict]:
    sh = _open_sheet()
    ws = _get_or_create_tab(sh, "Watchlist", _WATCH_HEADERS)
    return ws.get_all_records()


# ── Safe wrappers (never crash the UI) ───────────────────────────────────────

def try_save_trade(analysis: dict) -> None:
    try:
        save_trade(analysis)
    except Exception:
        pass


def try_save_scan(screener: str, result_count: int, symbols: list[str]) -> None:
    try:
        save_scan(screener, result_count, symbols)
    except Exception:
        pass
