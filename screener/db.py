"""
Firestore data layer.

Collections:
  scan_history/{auto-id}   — screener scan results
  trade_log/{auto-id}      — Agent Flow trade recommendations
  watchlist/{symbol}       — user watchlist entries
"""

from __future__ import annotations

import datetime
import os
from typing import Any

_db = None


def _client():
    global _db
    if _db is None:
        from google.cloud import firestore
        project = os.environ.get("GOOGLE_CLOUD_PROJECT")
        _db = firestore.Client(project=project)
    return _db


def _now() -> str:
    return datetime.datetime.utcnow().isoformat() + "Z"


# ── Scan history ──────────────────────────────────────────────────────────────

def save_scan(screener: str, result_count: int, symbols: list[str], meta: dict | None = None) -> str:
    """Save a screener run. Returns the document ID."""
    doc = {
        "screener":     screener,
        "result_count": result_count,
        "symbols":      symbols[:50],  # cap to avoid Firestore 1MB limit
        "meta":         meta or {},
        "ts":           _now(),
    }
    ref = _client().collection("scan_history").add(doc)
    return ref[1].id


def get_scan_history(screener: str | None = None, limit: int = 20) -> list[dict]:
    """Return recent scan history, newest first."""
    col = _client().collection("scan_history")
    q = col.order_by("ts", direction="DESCENDING").limit(limit)
    if screener:
        q = col.where("screener", "==", screener).order_by("ts", direction="DESCENDING").limit(limit)
    return [{"id": d.id, **d.to_dict()} for d in q.stream()]


# ── Trade log ─────────────────────────────────────────────────────────────────

def save_trade(analysis: dict) -> str:
    """Persist an Agent Flow recommendation. Returns document ID."""
    doc = {
        "symbol":      analysis.get("symbol"),
        "bias":        analysis.get("bias"),
        "confidence":  analysis.get("confidence"),
        "no_trade":    analysis.get("no_trade", True),
        "direction":   analysis.get("direction"),
        "strike":      analysis.get("strike"),
        "ltp":         analysis.get("ltp"),
        "expiry":      analysis.get("expiry"),
        "targets":     analysis.get("targets"),
        "rating":      analysis.get("rating"),
        "probability": analysis.get("probability"),
        "signals":     analysis.get("signals", []),
        "vix":         analysis.get("vix"),
        "trend":       analysis.get("trend"),
        "ts":          _now(),
    }
    ref = _client().collection("trade_log").add(doc)
    return ref[1].id


def get_trade_log(symbol: str | None = None, limit: int = 30) -> list[dict]:
    """Return recent trade recommendations, newest first."""
    col = _client().collection("trade_log")
    q = col.order_by("ts", direction="DESCENDING").limit(limit)
    if symbol:
        q = col.where("symbol", "==", symbol).order_by("ts", direction="DESCENDING").limit(limit)
    return [{"id": d.id, **d.to_dict()} for d in q.stream()]


# ── Watchlist ─────────────────────────────────────────────────────────────────

def add_to_watchlist(symbol: str, note: str = "") -> None:
    _client().collection("watchlist").document(symbol).set({
        "symbol": symbol,
        "note":   note,
        "added":  _now(),
    })


def remove_from_watchlist(symbol: str) -> None:
    _client().collection("watchlist").document(symbol).delete()


def get_watchlist() -> list[dict]:
    return [d.to_dict() for d in _client().collection("watchlist").stream()]


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
