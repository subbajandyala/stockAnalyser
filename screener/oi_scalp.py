"""
OI + MA pullback scalp for F&O stocks — the OI monitor picks the side, the 5-minute chart times it.

Watchlist (from the OI monitor's latest scan, data/oi/<date>/latest.json):
  CE side: STRONG CE, or CE score ≥ WATCH_SCORE with both OI checks true
           (calls unwinding + puts added near ATM). PE side: the mirror.

Entry (last CLOSED 5-min candle, today, not the 09:15 candle), CE — PE is the mirror:
  1. trend     20 EMA above 50 EMA, close above the previous day's CPR top
  2. pullback  the candle's low comes within TOUCH_PCT of the 20 EMA (or dips under it) and it
               closes back above the 20 EMA — or the same at the 50 EMA (deeper pullback)
  3. room      the call wall (OI) is at least ROOM_PCT above the close
  → buy the ATM CE.

Exits, checked on every closed candle:
  stop      a candle closes below the EMA that was touched (its current value)
  target 1  price reaches the call wall (or R1 when there is no wall) → no exit, trailing starts
  trail     after target 1: 2 candles in a row close below the 10 SMA → exit
  OI flip   the OI monitor now shows the opposite writers (calls added + puts unwinding for a CE)
  hard stop the option premium falls HARD_SL_PCT below the fill (gap / fast fall safety)
  15:15     square-off
"""

from __future__ import annotations

import datetime

import pandas as pd

TOUCH_PCT = 0.001        # low within 0.1 % of the EMA counts as a touch
WATCH_SCORE = 4
ROOM_PCT = 1.0
HARD_SL_PCT = 30.0
ENTRY_FROM, ENTRY_UNTIL = datetime.time(9, 30), datetime.time(14, 45)
SQUARE_OFF = datetime.time(15, 15)


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
    df["sma10"] = df["close"].rolling(10).mean()
    return df


def day_levels(df: pd.DataFrame, day: datetime.date) -> dict | None:
    """Previous trading day's CPR and R1 / S1 from the intraday candles."""
    prev = [d for d in sorted(set(df.index.date)) if d < day]
    if not prev:
        return None
    p = df[df.index.date == prev[-1]]
    h, l, c = float(p["high"].max()), float(p["low"].min()), float(p["close"].iloc[-1])
    piv = (h + l + c) / 3
    bc = (h + l) / 2
    tc = 2 * piv - bc
    return {"pivot": piv, "top": max(tc, bc), "bottom": min(tc, bc), "r1": 2 * piv - l, "s1": 2 * piv - h}


def watch_side(row: dict) -> str | None:
    """CE / PE when the OI monitor's latest read puts this stock on the watchlist."""
    if row.get("signal") in ("CE", "PE"):
        return row["signal"]
    if row.get("ce_score", 0) >= WATCH_SCORE and all(row.get("ce_core", [])[:2]):
        return "CE"
    if row.get("pe_score", 0) >= WATCH_SCORE and all(row.get("pe_core", [])[:2]):
        return "PE"
    return None


def oi_flipped(row: dict | None, side: str) -> bool:
    """The writers turned against the trade: both OI checks of the opposite side are true."""
    if not row:
        return False
    other = row.get("pe_core" if side == "CE" else "ce_core", [])
    return len(other) >= 2 and all(other[:2])


def entry_signal(df: pd.DataFrame, side: str, levels: dict | None, wall: float | None,
                 day: datetime.date) -> dict | None:
    """Pullback entry on the last closed candle of df (df already has indicators)."""
    if levels is None or len(df) < 60:
        return None
    c = df.iloc[-1]
    t = df.index[-1]
    if t.date() != day or t.time() <= datetime.time(9, 15):
        return None
    close, low, high = float(c["close"]), float(c["low"]), float(c["high"])
    e20, e50 = float(c["ema20"]), float(c["ema50"])
    if side == "CE":
        if not (e20 > e50 and close > levels["top"]):
            return None
        touched = next((n for n, e in (("20 EMA", e20), ("50 EMA", e50))
                        if low <= e * (1 + TOUCH_PCT) and close >= e
                        and (n == "50 EMA" or low > e50)), None)
        room = wall is None or (wall / close - 1) * 100 >= ROOM_PCT
        target = wall if wall else levels["r1"]
    else:
        if not (e20 < e50 and close < levels["bottom"]):
            return None
        touched = next((n for n, e in (("20 EMA", e20), ("50 EMA", e50))
                        if high >= e * (1 - TOUCH_PCT) and close <= e
                        and (n == "50 EMA" or high < e50)), None)
        room = wall is None or (1 - wall / close) * 100 >= ROOM_PCT
        target = wall if wall else levels["s1"]
    if not touched or not room:
        return None
    if (side == "CE" and target <= close) or (side == "PE" and target >= close):
        return None
    return {"side": side, "ema": touched, "close": close, "target": float(target),
            "ema_value": e20 if touched == "20 EMA" else e50, "candle": str(t)}


def exit_signal(pos: dict, df: pd.DataFrame, oi_row: dict | None, now: datetime.datetime,
                opt_ltp: float | None) -> tuple[str | None, dict]:
    """(reason to exit or None, state updates) for an open trade on the last closed candle."""
    upd: dict = {}
    ce = pos["side"] == "CE"
    if now.time() >= SQUARE_OFF:
        return "15:15 square-off", upd
    if opt_ltp and pos.get("fill") and opt_ltp <= pos["fill"] * (1 - HARD_SL_PCT / 100):
        return f"hard stop — premium −{HARD_SL_PCT:g}%", upd
    if oi_flipped(oi_row, pos["side"]):
        return "OI flip — writers turned against the trade", upd
    if df is None or len(df) < 12:
        return None, upd
    c = df.iloc[-1]
    if str(df.index[-1]) <= pos.get("entry_candle", ""):
        return None, upd            # no new closed candle since the entry
    close = float(c["close"])
    ema = float(c["ema20" if pos["ema"] == "20 EMA" else "ema50"])
    if not pos.get("trailing"):
        if (close < ema) if ce else (close > ema):
            return f"stop — closed {'below' if ce else 'above'} the {pos['ema']} ({ema:.2f})", upd
        hit = float(c["high"]) >= pos["target"] if ce else float(c["low"]) <= pos["target"]
        if hit:
            upd["trailing"] = True
        return None, upd
    last2 = df.iloc[-2:]
    beyond = (last2["close"] < last2["sma10"]) if ce else (last2["close"] > last2["sma10"])
    if len(last2) == 2 and bool(beyond.all()):
        return "trail — 2 closes past the 10 SMA after target 1", upd
    return None, upd
