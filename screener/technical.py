import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, timedelta


def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def analyze_stock(nse_symbol: str, breakout_pct: float = 0.05) -> dict | None:
    try:
        df = yf.download(nse_symbol, period="1y", interval="1d", progress=False, auto_adjust=True)
        if df is None or len(df) < 60:
            return None

        df = df.copy()
        close = df["Close"].squeeze()
        volume = df["Volume"].squeeze()

        ema20 = _ema(close, 20)
        ema50 = _ema(close, 50)
        ema200 = _ema(close, 200)
        rsi = _rsi(close)

        latest_close = float(close.iloc[-1])
        latest_ema20 = float(ema20.iloc[-1])
        latest_ema50 = float(ema50.iloc[-1])
        latest_ema200 = float(ema200.iloc[-1]) if len(ema200.dropna()) > 0 else None
        latest_rsi = float(rsi.iloc[-1])

        high_52w = float(close.tail(252).max())
        low_52w = float(close.tail(252).min())
        pct_from_high = (high_52w - latest_close) / high_52w * 100

        # Uptrend: price > EMA20 > EMA50 and EMA20 slope is positive
        ema20_slope = float(ema20.iloc[-1]) - float(ema20.iloc[-5])
        is_uptrend = (
            latest_close > latest_ema20
            and latest_ema20 > latest_ema50
            and ema20_slope > 0
        )

        # Volume surge: last 5-day avg volume vs 20-day avg
        vol_5d = float(volume.tail(5).mean())
        vol_20d = float(volume.tail(20).mean())
        volume_ratio = vol_5d / vol_20d if vol_20d > 0 else 1.0

        # Consolidation: price range in last 20 days < 8%
        recent_close = close.tail(20)
        consolidation_range = (recent_close.max() - recent_close.min()) / recent_close.min() * 100
        is_consolidating = consolidation_range < 8.0

        # Breakout ready: within X% of 52W high OR consolidating with rising volume
        near_52w_high = pct_from_high <= breakout_pct * 100
        breakout_ready = near_52w_high or (is_consolidating and volume_ratio > 1.2)

        # Signal
        if is_uptrend and breakout_ready and volume_ratio > 1.3:
            signal = "STRONG BUY"
        elif is_uptrend and breakout_ready:
            signal = "BUY"
        elif is_uptrend:
            signal = "WATCH"
        else:
            signal = "SKIP"

        change_1d = (latest_close - float(close.iloc[-2])) / float(close.iloc[-2]) * 100
        change_1w = (latest_close - float(close.iloc[-6])) / float(close.iloc[-6]) * 100

        return {
            "Price": round(latest_close, 2),
            "Change 1D%": round(change_1d, 2),
            "Change 1W%": round(change_1w, 2),
            "EMA20": round(latest_ema20, 2),
            "EMA50": round(latest_ema50, 2),
            "RSI": round(latest_rsi, 1),
            "52W High": round(high_52w, 2),
            "52W Low": round(low_52w, 2),
            "% from 52W High": round(pct_from_high, 2),
            "Vol Ratio (5d/20d)": round(volume_ratio, 2),
            "Consolidation Range%": round(consolidation_range, 1),
            "Uptrend": is_uptrend,
            "Breakout Ready": breakout_ready,
            "Signal": signal,
        }
    except Exception:
        return None


def analyze_stocks(trending: list[dict], breakout_pct: float = 0.05) -> pd.DataFrame:
    rows = []
    for item in trending:
        tech = analyze_stock(item["NSE_Symbol"], breakout_pct=breakout_pct)
        if tech is None:
            continue
        row = {
            "Symbol": item["Symbol"],
            "Company": item["Company"],
            "News Mentions": item["News_Mentions"],
            "Top Headline": item["Headlines"][0] if item["Headlines"] else "",
            **tech,
        }
        rows.append(row)

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    # Sort: Signal priority then news mentions
    signal_order = {"STRONG BUY": 0, "BUY": 1, "WATCH": 2, "SKIP": 3}
    df["_rank"] = df["Signal"].map(signal_order)
    df = df.sort_values(["_rank", "News Mentions"], ascending=[True, False])
    df = df.drop(columns=["_rank"])
    return df.reset_index(drop=True)
