"""
CPR Retracement Screener

Detects two patterns in daily NIFTY 500 stocks:

  📈 CPR Support Bounce  — stock was falling, low dipped to TC/Pivot/BC, reversed UP
  📉 CPR Resistance Rejection — stock was rising, high tagged TC/Pivot/BC, reversed DOWN

CPR (Central Pivot Range) for each day is calculated from the prior day's OHLC:
    Pivot = (H + L + C) / 3
    BC    = (H + L) / 2
    TC    = (Pivot - BC) + Pivot

Two patterns are detected, in order of preference:
  Pattern A: TODAY's candle itself touches CPR and closes in reversal direction.
  Pattern B: YESTERDAY's candle touched CPR, TODAY confirms with a full reversal candle.
"""

import yfinance as yf
import pandas as pd
import numpy as np
from screener.kite_hist import batch_quote_nse, patch_df_with_kite


def _calc_cpr(high: float, low: float, close: float) -> dict:
    pivot = (high + low + close) / 3
    bc    = (high + low) / 2
    tc    = (pivot - bc) + pivot
    return {"TC": max(bc, tc), "Pivot": pivot, "BC": min(bc, tc)}


def _nearest_cpr_touch(price: float, cpr: dict, pct: float) -> tuple[bool, str, float]:
    """Return (touched, level_name, level_price) for the CPR level nearest to price."""
    best_name  = ""
    best_level = 0.0
    best_dist  = float("inf")
    for name in ("TC", "Pivot", "BC"):
        level = cpr[name]
        dist  = abs(price - level) / level
        if dist < best_dist:
            best_dist, best_name, best_level = dist, name, level
    if best_dist <= pct:
        return True, best_name, best_level
    return False, "", 0.0


def analyze_cpr_retracement(
    nse_symbol: str,
    touch_pct: float = 0.01,
    kite_quote: dict | None = None,
) -> dict | None:
    try:
        df = yf.download(nse_symbol, period="3mo", interval="1d",
                         progress=False, auto_adjust=True)
        if df is None or len(df) < 8:
            return None

        df = df.copy()
        if kite_quote:
            df = patch_df_with_kite(df, kite_quote)

        close  = df["Close"].squeeze()
        high   = df["High"].squeeze()
        low    = df["Low"].squeeze()
        open_  = df["Open"].squeeze()
        volume = df["Volume"].squeeze()

        if len(close) < 6:
            return None

        # ── Candle extracts ───────────────────────────────────────────────────
        today_close  = float(close.iloc[-1])
        today_open   = float(open_.iloc[-1])
        today_high   = float(high.iloc[-1])
        today_low    = float(low.iloc[-1])

        prev_close   = float(close.iloc[-2])
        prev_open    = float(open_.iloc[-2])
        prev_high    = float(high.iloc[-2])
        prev_low     = float(low.iloc[-2])

        prev2_close  = float(close.iloc[-3])
        prev2_high   = float(high.iloc[-3])
        prev2_low    = float(low.iloc[-3])

        # CPR active today = calculated from yesterday (iloc[-2]) OHLC
        cpr_today = _calc_cpr(prev_high, prev_low, prev_close)

        # CPR active yesterday = calculated from 2-days-ago (iloc[-3]) OHLC
        cpr_yest = _calc_cpr(
            float(high.iloc[-3]), float(low.iloc[-3]), float(close.iloc[-3])
        )

        vol_avg   = float(volume.tail(20).mean())
        vol_ratio = float(volume.iloc[-1]) / vol_avg if vol_avg > 0 else 1.0
        change_1d = (today_close - prev_close) / prev_close * 100

        # ── Recent trend context (last 5 candles before today) ────────────────
        lookback_close = float(close.iloc[-6])   # 5 days ago
        trend_pct = (prev_close - lookback_close) / lookback_close * 100

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # PATTERN A  —  TODAY'S candle touches CPR (cpr_today) and reverses
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

        # A-BULL: today's low touched CPR, today closed above (hammer/bounce)
        hit, level_name, level_price = _nearest_cpr_touch(today_low, cpr_today, touch_pct)
        if hit:
            was_falling  = trend_pct < -0.3 or prev_close < lookback_close
            bull_candle  = today_close > today_open and today_close > prev_close
            held_support = today_close >= cpr_today["BC"] * (1 - touch_pct)
            if was_falling and bull_candle and held_support:
                return _build(
                    signal="📈 CPR Bounce",
                    direction="BULL",
                    pattern="Today",
                    level_name=level_name,
                    level_price=level_price,
                    cpr=cpr_today,
                    touch_price=today_low,
                    price=today_close,
                    change_1d=change_1d,
                    vol_ratio=vol_ratio,
                    trend_pct=trend_pct,
                )

        # A-BEAR: today's high touched CPR, today closed below (shooting-star/rejection)
        hit, level_name, level_price = _nearest_cpr_touch(today_high, cpr_today, touch_pct)
        if hit:
            was_rising    = trend_pct > 0.3 or prev_close > lookback_close
            bear_candle   = today_close < today_open and today_close < prev_close
            held_resist   = today_close <= cpr_today["TC"] * (1 + touch_pct)
            if was_rising and bear_candle and held_resist:
                return _build(
                    signal="📉 CPR Rejection",
                    direction="BEAR",
                    pattern="Today",
                    level_name=level_name,
                    level_price=level_price,
                    cpr=cpr_today,
                    touch_price=today_high,
                    price=today_close,
                    change_1d=change_1d,
                    vol_ratio=vol_ratio,
                    trend_pct=trend_pct,
                )

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # PATTERN B  —  YESTERDAY touched CPR (cpr_yest), TODAY confirms
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

        # B-BULL: yesterday's low touched cpr_yest, yesterday held support,
        #         today is a full bull candle
        hit, level_name, level_price = _nearest_cpr_touch(prev_low, cpr_yest, touch_pct)
        if hit:
            prev_trend   = (prev_close - lookback_close) / lookback_close * 100
            was_falling  = prev_trend < -0.3 or prev2_close > prev_close or prev_open > prev_close
            held_support = prev_close >= cpr_yest["BC"] * (1 - touch_pct)
            bull_confirm = today_close > today_open and today_close > prev_close
            if was_falling and held_support and bull_confirm:
                return _build(
                    signal="📈 CPR Bounce",
                    direction="BULL",
                    pattern="2-Day",
                    level_name=level_name,
                    level_price=level_price,
                    cpr=cpr_yest,
                    touch_price=prev_low,
                    price=today_close,
                    change_1d=change_1d,
                    vol_ratio=vol_ratio,
                    trend_pct=prev_trend,
                )

        # B-BEAR: yesterday's high touched cpr_yest, yesterday closed below TC,
        #         today is a full bear candle
        hit, level_name, level_price = _nearest_cpr_touch(prev_high, cpr_yest, touch_pct)
        if hit:
            prev_trend    = (prev_close - lookback_close) / lookback_close * 100
            was_rising    = prev_trend > 0.3 or prev2_close < prev_close or prev_open < prev_close
            held_resist   = prev_close <= cpr_yest["TC"] * (1 + touch_pct)
            bear_confirm  = today_close < today_open and today_close < prev_close
            if was_rising and held_resist and bear_confirm:
                return _build(
                    signal="📉 CPR Rejection",
                    direction="BEAR",
                    pattern="2-Day",
                    level_name=level_name,
                    level_price=level_price,
                    cpr=cpr_yest,
                    touch_price=prev_high,
                    price=today_close,
                    change_1d=change_1d,
                    vol_ratio=vol_ratio,
                    trend_pct=prev_trend,
                )

        return None
    except Exception:
        return None


def _build(
    signal: str, direction: str, pattern: str,
    level_name: str, level_price: float, cpr: dict,
    touch_price: float, price: float, change_1d: float,
    vol_ratio: float, trend_pct: float,
) -> dict:
    return {
        "Signal":      signal,
        "Direction":   direction,
        "Pattern":     pattern,
        "CPR Level":   level_name,
        "Level Price": round(level_price, 2),
        "CPR TC":      round(cpr["TC"], 2),
        "CPR Pivot":   round(cpr["Pivot"], 2),
        "CPR BC":      round(cpr["BC"], 2),
        "Touch Price": round(touch_price, 2),
        "Price":       round(price, 2),
        "Change%":     round(change_1d, 2),
        "5D Trend%":   round(trend_pct, 2),
        "Vol Ratio":   round(vol_ratio, 2),
    }


def run_cpr_retracement_scan(
    symbols_df: pd.DataFrame,
    touch_pct: float = 0.01,
    api_key: str = "",
    access_token: str = "",
) -> pd.DataFrame:
    """
    Scan NIFTY 500 for CPR support bounces and resistance rejections.
    Returns a DataFrame sorted by signal type then volume ratio.
    """
    kite_quotes: dict = {}
    if api_key and access_token:
        try:
            plain_syms  = list(symbols_df["Symbol"].str.upper())
            kite_quotes = batch_quote_nse(api_key, access_token, plain_syms)
        except Exception:
            pass

    rows = []
    for _, row in symbols_df.iterrows():
        nse_key = f"NSE:{str(row['Symbol']).upper()}"
        quote   = kite_quotes.get(nse_key, {})
        result  = analyze_cpr_retracement(
            row["NSE_Symbol"],
            touch_pct=touch_pct,
            kite_quote=quote if quote else None,
        )
        if result:
            rows.append({"Symbol": row["Symbol"], "Company": row["Company"], **result})

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    signal_order = {"📈 CPR Bounce": 0, "📉 CPR Rejection": 1}
    df["_rank"] = df["Signal"].map(signal_order).fillna(2)
    df = df.sort_values(["_rank", "Vol Ratio"], ascending=[True, False])
    return df.drop(columns=["_rank"]).reset_index(drop=True)
