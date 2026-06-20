import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, timedelta


def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _calc_cpr(high: float, low: float, close: float) -> dict:
    pivot = (high + low + close) / 3
    bc = (high + low) / 2
    tc = (pivot - bc) + pivot
    return {
        "Pivot": pivot,
        "BC": min(bc, tc),
        "TC": max(bc, tc),
        "CPR_Width": abs(tc - bc),
    }


def _is_near(price: float, level: float, pct: float = 0.01) -> bool:
    return abs(price - level) / level <= pct


def analyze_ma_retracement(nse_symbol: str, touch_pct: float = 0.01) -> dict | None:
    try:
        df = yf.download(nse_symbol, period="6mo", interval="1d", progress=False, auto_adjust=True)
        if df is None or len(df) < 25:
            return None

        df = df.copy()
        close = df["Close"].squeeze()
        high = df["High"].squeeze()
        low = df["Low"].squeeze()
        open_ = df["Open"].squeeze()
        volume = df["Volume"].squeeze()

        ema20 = _ema(close, 20)

        # We need at least 2 completed candles to check touch + continuation
        # Index: -1 = today (latest), -2 = yesterday, -3 = day before yesterday
        today_close = float(close.iloc[-1])
        today_open = float(open_.iloc[-1])
        today_high = float(high.iloc[-1])
        today_low = float(low.iloc[-1])
        today_ema20 = float(ema20.iloc[-1])

        prev_close = float(close.iloc[-2])
        prev_open = float(open_.iloc[-2])
        prev_high = float(high.iloc[-2])
        prev_low = float(low.iloc[-2])
        prev_ema20 = float(ema20.iloc[-2])

        # ── Uptrend check ─────────────────────────────────────────────────────
        ema50 = _ema(close, 50)
        ema20_slope = float(ema20.iloc[-1]) - float(ema20.iloc[-5])
        is_uptrend = (
            today_close > today_ema20
            and float(ema20.iloc[-1]) > float(ema50.iloc[-1])
            and ema20_slope > 0
        )
        if not is_uptrend:
            return None

        # ── 20 MA touch check (previous candle's low touched 20 MA) ──────────
        # The candle that touched 20 MA: low is within ±touch_pct of EMA20
        # AND the candle closed above EMA20 (bounced, not broken)
        touched_20ma = _is_near(prev_low, prev_ema20, pct=touch_pct) and prev_close >= prev_ema20 * 0.995

        if not touched_20ma:
            return None

        # ── Continuation candle check (today is moving up) ───────────────────
        continuation = (
            today_close > today_open          # today is a bullish candle
            and today_close > prev_close      # closing higher than previous
            and today_low > prev_low          # higher low
        )
        if not continuation:
            return None

        # ── CPR check (daily CPR from 2 days ago H/L/C) ─────────────────────
        prev2_high = float(high.iloc[-3])
        prev2_low = float(low.iloc[-3])
        prev2_close = float(close.iloc[-3])
        cpr = _calc_cpr(prev2_high, prev2_low, prev2_close)

        # CPR support: previous candle's low was near CPR band (BC or Pivot)
        cpr_support = (
            _is_near(prev_low, cpr["BC"], pct=0.015)
            or _is_near(prev_low, cpr["Pivot"], pct=0.015)
            or _is_near(prev_low, cpr["TC"], pct=0.015)
        )

        # ── Volume confirmation ───────────────────────────────────────────────
        vol_20d_avg = float(volume.tail(20).mean())
        today_vol = float(volume.iloc[-1])
        vol_ratio = today_vol / vol_20d_avg if vol_20d_avg > 0 else 1.0

        # ── Distance from 20 MA ───────────────────────────────────────────────
        pct_from_20ma = (today_close - today_ema20) / today_ema20 * 100

        # ── Signal strength ───────────────────────────────────────────────────
        if cpr_support and vol_ratio > 1.2:
            signal = "STRONG (20MA + CPR)"
        elif cpr_support:
            signal = "GOOD (20MA + CPR)"
        elif vol_ratio > 1.2:
            signal = "GOOD (20MA + Vol)"
        else:
            signal = "20MA Bounce"

        high_52w = float(close.tail(252).max())
        change_1d = (today_close - prev_close) / prev_close * 100

        return {
            "Price": round(today_close, 2),
            "Change 1D%": round(change_1d, 2),
            "EMA20": round(today_ema20, 2),
            "% Above EMA20": round(pct_from_20ma, 2),
            "Prev Low": round(prev_low, 2),
            "Touch%": round((prev_low - prev_ema20) / prev_ema20 * 100, 2),
            "Vol Ratio": round(vol_ratio, 2),
            "CPR Support": "✅ Yes" if cpr_support else "—",
            "CPR BC": round(cpr["BC"], 2),
            "CPR Pivot": round(cpr["Pivot"], 2),
            "CPR TC": round(cpr["TC"], 2),
            "52W High": round(high_52w, 2),
            "Signal": signal,
        }
    except Exception:
        return None


def run_ma_retracement_scan(symbols_df: pd.DataFrame, touch_pct: float = 0.01) -> pd.DataFrame:
    rows = []
    for _, row in symbols_df.iterrows():
        result = analyze_ma_retracement(row["NSE_Symbol"], touch_pct=touch_pct)
        if result:
            rows.append({
                "Symbol": row["Symbol"],
                "Company": row["Company"],
                **result,
            })

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)

    # Sort: CPR support first, then by volume ratio
    signal_order = {
        "STRONG (20MA + CPR)": 0,
        "GOOD (20MA + CPR)": 1,
        "GOOD (20MA + Vol)": 2,
        "20MA Bounce": 3,
    }
    df["_rank"] = df["Signal"].map(signal_order).fillna(4)
    df = df.sort_values(["_rank", "Vol Ratio"], ascending=[True, False])
    df = df.drop(columns=["_rank"])
    return df.reset_index(drop=True)
