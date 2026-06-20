import yfinance as yf
import pandas as pd
import numpy as np


def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def analyze_stock(
    nse_symbol: str,
    breakout_pct: float = 0.05,
    interval: str = "1d",
    period: str = "1y",
) -> dict | None:
    try:
        df = yf.download(nse_symbol, period=period, interval=interval,
                         progress=False, auto_adjust=True)
        if df is None or len(df) < 30:
            return None

        df = df.copy()
        close  = df["Close"].squeeze()
        volume = df["Volume"].squeeze()

        ema20 = _ema(close, 20)
        ema50 = _ema(close, 50)
        rsi   = _rsi(close)

        latest_close = float(close.iloc[-1])
        latest_ema20 = float(ema20.iloc[-1])
        latest_ema50 = float(ema50.iloc[-1])
        latest_rsi   = float(rsi.iloc[-1])

        period_high = float(close.max())
        period_low  = float(close.min())
        pct_from_high = (period_high - latest_close) / period_high * 100

        slope_lookback = min(5, len(close) - 1)
        ema20_slope = float(ema20.iloc[-1]) - float(ema20.iloc[-slope_lookback])
        is_uptrend = (
            latest_close > latest_ema20
            and latest_ema20 > latest_ema50
            and ema20_slope > 0
        )

        # Volume: recent 5 candles vs last 20 candles
        vol_recent = float(volume.tail(5).mean())
        vol_avg    = float(volume.tail(20).mean())
        volume_ratio = vol_recent / vol_avg if vol_avg > 0 else 1.0

        # Consolidation: price range over last 20 candles < 8%
        recent_close = close.tail(20)
        consolidation_range = (recent_close.max() - recent_close.min()) / recent_close.min() * 100
        is_consolidating = consolidation_range < 8.0

        near_high    = pct_from_high <= breakout_pct * 100
        breakout_ready = near_high or (is_consolidating and volume_ratio > 1.2)

        if is_uptrend and breakout_ready and volume_ratio > 1.3:
            signal = "STRONG BUY"
        elif is_uptrend and breakout_ready:
            signal = "BUY"
        elif is_uptrend:
            signal = "WATCH"
        else:
            signal = "SKIP"

        change_last  = (latest_close - float(close.iloc[-2])) / float(close.iloc[-2]) * 100
        lookback_idx = min(6, len(close) - 1)
        change_prev  = (latest_close - float(close.iloc[-lookback_idx])) / float(close.iloc[-lookback_idx]) * 100

        return {
            "Price":               round(latest_close, 2),
            "Change Last%":        round(change_last, 2),
            "Change Prev 5%":      round(change_prev, 2),
            "EMA20":               round(latest_ema20, 2),
            "EMA50":               round(latest_ema50, 2),
            "RSI":                 round(latest_rsi, 1),
            "Period High":         round(period_high, 2),
            "Period Low":          round(period_low, 2),
            "% from High":         round(pct_from_high, 2),
            "Vol Ratio":           round(volume_ratio, 2),
            "Consolidation Range%": round(consolidation_range, 1),
            "Uptrend":             is_uptrend,
            "Breakout Ready":      breakout_ready,
            "Signal":              signal,
        }
    except Exception:
        return None
