"""
CPR + ADX Intraday Screener
===========================
Scores each stock -5 to +5 using:
  - CPR position (price vs Top CPR / Bottom CPR / inside band)
  - CPR width (narrow = trending day, wide = sideways)
  - ADX strength and DI direction

Returns a structured dict ready for notification and table display.
"""

import datetime
import numpy as np
import pandas as pd

from screener.kite_data import fetch_ohlcv as _fetch

_IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

# Nifty 50 F&O stocks with liquid options (25 most traded)
NIFTY50_FNO = [
    "RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS",
    "KOTAKBANK", "AXISBANK", "SBIN", "BAJFINANCE", "LT",
    "HCLTECH", "WIPRO", "TATAMOTORS", "MARUTI", "ASIANPAINT",
    "HINDUNILVR", "POWERGRID", "NTPC", "ONGC", "TATASTEEL",
    "JSWSTEEL", "ULTRACEMCO", "TITAN", "BAJAJFINSV", "ADANIENT",
]


# ── CPR calculation ───────────────────────────────────────────────────────────

def _cpr(high: float, low: float, close: float) -> dict:
    pivot = (high + low + close) / 3.0
    bc    = (high + low) / 2.0
    tc    = (pivot - bc) + pivot
    top   = max(tc, bc)
    bot   = min(tc, bc)
    width_pct = (top - bot) / pivot * 100 if pivot else 0
    return {"top": top, "pivot": pivot, "bot": bot, "width_pct": width_pct}


# ── ADX calculation ───────────────────────────────────────────────────────────

def _adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    hi = df["high"]
    lo = df["low"]
    cl = df["close"]

    tr   = pd.concat([hi - lo, (hi - cl.shift()).abs(), (lo - cl.shift()).abs()], axis=1).max(axis=1)
    dm_p = (hi - hi.shift()).clip(lower=0).where((hi - hi.shift()) > (lo.shift() - lo), 0)
    dm_m = (lo.shift() - lo).clip(lower=0).where((lo.shift() - lo) > (hi - hi.shift()), 0)

    atr  = tr.ewm(alpha=1 / period, adjust=False).mean()
    di_p = 100 * dm_p.ewm(alpha=1 / period, adjust=False).mean() / atr
    di_m = 100 * dm_m.ewm(alpha=1 / period, adjust=False).mean() / atr
    dx   = (100 * (di_p - di_m).abs() / (di_p + di_m).replace(0, np.nan)).fillna(0)
    adx  = dx.ewm(alpha=1 / period, adjust=False).mean()

    df = df.copy()
    df["adx"]  = adx
    df["di_p"] = di_p
    df["di_m"] = di_m
    return df


# ── Scoring ───────────────────────────────────────────────────────────────────

def _score(price: float, cpr: dict, adx_val: float, di_p: float, di_m: float) -> tuple[int, list[str]]:
    score   = 0
    reasons = []

    top = cpr["top"]
    bot = cpr["bot"]
    piv = cpr["pivot"]
    w   = cpr["width_pct"]

    # ── CPR position score ────────────────────────────────────────────────────
    buf = top * 0.002  # 0.2% buffer for "just broke above/below"
    if price > top + buf:
        score += 2
        reasons.append(f"Price above CPR top ({top:,.1f}) — bullish breakout")
    elif price >= bot and price <= top:
        score += 0
        reasons.append(f"Price inside CPR band ({bot:,.1f}–{top:,.1f}) — wait")
    elif price < bot - buf:
        score -= 2
        reasons.append(f"Price below CPR bottom ({bot:,.1f}) — bearish breakdown")
    elif price >= top * 0.998 and price <= top + buf:
        score += 1
        reasons.append(f"Price bouncing off CPR top ({top:,.1f}) — support")
    elif price >= bot - buf and price <= bot * 1.002:
        score -= 1
        reasons.append(f"Price at CPR bottom ({bot:,.1f}) — rejection risk")

    # ── CPR width score ───────────────────────────────────────────────────────
    if w < 0.3:
        score += 1
        reasons.append(f"Narrow CPR ({w:.2f}%) — trending day likely")
    elif w > 0.8:
        score -= 1
        reasons.append(f"Wide CPR ({w:.2f}%) — sideways day likely")

    # ── ADX score ─────────────────────────────────────────────────────────────
    if adx_val < 20:
        score += 0
        reasons.append(f"ADX {adx_val:.0f} — choppy, no trend")
    elif adx_val < 25:
        if di_p > di_m:
            score += 1
            reasons.append(f"ADX {adx_val:.0f} forming uptrend (DI+ {di_p:.0f} > DI− {di_m:.0f})")
        else:
            score -= 1
            reasons.append(f"ADX {adx_val:.0f} forming downtrend (DI− {di_m:.0f} > DI+ {di_p:.0f})")
    elif adx_val < 35:
        if di_p > di_m:
            score += 1
            reasons.append(f"ADX {adx_val:.0f} strong uptrend (DI+ {di_p:.0f} > DI− {di_m:.0f})")
        else:
            score -= 1
            reasons.append(f"ADX {adx_val:.0f} strong downtrend (DI− {di_m:.0f} > DI+ {di_p:.0f})")
    else:
        if di_p > di_m:
            score += 2
            reasons.append(f"ADX {adx_val:.0f} very strong uptrend (DI+ {di_p:.0f} > DI− {di_m:.0f})")
        else:
            score -= 2
            reasons.append(f"ADX {adx_val:.0f} very strong downtrend (DI− {di_m:.0f} > DI+ {di_p:.0f})")

    return score, reasons


def _stars(score: int) -> str:
    a = abs(score)
    if a >= 5: return "★★★★★"
    if a >= 4: return "★★★★"
    if a >= 3: return "★★★"
    if a >= 2: return "★★"
    return "★"


def _sl_target(price: float, atr: float, direction: str) -> tuple[float, float]:
    sl_pts  = round(1.5 * atr, 2)
    tgt_pts = round(3.0 * atr, 2)
    if direction == "BUY":
        return round(price - sl_pts, 2), round(price + tgt_pts, 2)
    return round(price + sl_pts, 2), round(price - tgt_pts, 2)


# ── Main analysis function ────────────────────────────────────────────────────

def analyze_cpr_adx(symbol: str, api_key: str = "", access_token: str = "") -> dict:
    """
    Returns dict with keys:
      symbol, price, cpr, adx, di_p, di_m, score, direction,
      signal, stars, sl, target, rr, reasons, error

    api_key / access_token are unused — kite_data reads creds from env.
    Kept in signature for API consistency with other analyze_* functions.
    """
    try:
        # Daily candle for yesterday's OHLC (CPR base)
        df_day = _fetch(symbol, interval="day", days=5)
        if df_day is None or len(df_day) < 2:
            return {"symbol": symbol, "error": "insufficient daily data"}

        prev = df_day.iloc[-2]
        cpr  = _cpr(float(prev["High"]), float(prev["Low"]), float(prev["Close"]))

        # 15m candles for ADX + current price
        df_15 = _fetch(symbol, interval="15minute", days=3)
        if df_15 is None or len(df_15) < 20:
            return {"symbol": symbol, "error": "insufficient 15m data"}

        # Rename columns to lowercase for internal calc
        df_15 = df_15.rename(columns={"Open": "open", "High": "high", "Low": "low",
                                       "Close": "close", "Volume": "volume"})
        df_15 = _adx(df_15)
        last  = df_15.iloc[-1]
        price = float(last["close"])
        adx_v = float(last["adx"])
        di_p  = float(last["di_p"])
        di_m  = float(last["di_m"])

        # ATR from last 14 bars for SL/target
        hi = df_15["high"]
        lo = df_15["low"]
        cl = df_15["close"]
        tr = pd.concat([hi - lo, (hi - cl.shift()).abs(), (lo - cl.shift()).abs()], axis=1).max(axis=1)
        atr = float(tr.tail(14).mean())

        score, reasons = _score(price, cpr, adx_v, di_p, di_m)

        if score >= 3:
            direction = "BUY"
            signal    = "BULLISH"
        elif score <= -3:
            direction = "SHORT"
            signal    = "BEARISH"
        else:
            direction = "WAIT"
            signal    = "NEUTRAL"

        sl, target = _sl_target(price, atr, direction) if direction != "WAIT" else (0.0, 0.0)
        rr = round(abs(target - price) / abs(price - sl), 1) if direction != "WAIT" and abs(price - sl) > 0 else 0.0

        return {
            "symbol":    symbol,
            "price":     price,
            "cpr":       cpr,
            "adx":       round(adx_v, 1),
            "di_p":      round(di_p, 1),
            "di_m":      round(di_m, 1),
            "atr":       round(atr, 2),
            "score":     score,
            "direction": direction,
            "signal":    signal,
            "stars":     _stars(score),
            "sl":        sl,
            "target":    target,
            "rr":        rr,
            "reasons":   reasons,
            "error":     None,
        }

    except Exception as e:
        return {"symbol": symbol, "error": str(e)}
