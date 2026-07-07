import yfinance as yf
import pandas as pd
import numpy as np
from screener.kite_hist import batch_quote_nse, patch_df_with_kite


def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _is_near(price: float, level: float, pct: float = 0.01) -> bool:
    return abs(price - level) / level <= pct


def _calc_cpr(high: float, low: float, close: float) -> dict:
    pivot = (high + low + close) / 3
    bc    = (high + low) / 2
    tc    = (pivot - bc) + pivot
    return {
        "Pivot": pivot,
        "BC":    min(bc, tc),
        "TC":    max(bc, tc),
    }


def analyze_ma_retracement(
    nse_symbol: str,
    touch_pct: float = 0.01,
    interval: str = "1d",
    period: str = "1y",
    kite_quote: dict | None = None,
) -> dict | None:
    try:
        df = yf.download(nse_symbol, period=period, interval=interval,
                         progress=False, auto_adjust=True)
        if df is None or len(df) < 30:
            return None

        df = df.copy()

        # Replace today's candle with live Kite price if available
        if kite_quote:
            df = patch_df_with_kite(df, kite_quote)

        close  = df["Close"].squeeze()
        high   = df["High"].squeeze()
        low    = df["Low"].squeeze()
        open_  = df["Open"].squeeze()
        volume = df["Volume"].squeeze()

        ema20 = _ema(close, 20)
        ema50 = _ema(close, 50)

        today_close  = float(close.iloc[-1])
        today_open   = float(open_.iloc[-1])
        today_low    = float(low.iloc[-1])
        today_ema20  = float(ema20.iloc[-1])

        prev_close   = float(close.iloc[-2])
        prev_low     = float(low.iloc[-2])
        prev_high    = float(high.iloc[-2])
        prev_ema20   = float(ema20.iloc[-2])

        # ── Uptrend ───────────────────────────────────────────────────────────
        slope_lookback = min(5, len(close) - 1)
        ema20_slope    = float(ema20.iloc[-1]) - float(ema20.iloc[-slope_lookback])
        is_uptrend = (
            today_close > today_ema20
            and float(ema20.iloc[-1]) > float(ema50.iloc[-1])
            and ema20_slope > 0
        )
        if not is_uptrend:
            return None

        # ── 20 MA touch (previous candle) ─────────────────────────────────────
        touched_20ma = (
            _is_near(prev_low, prev_ema20, pct=touch_pct)
            and prev_close >= prev_ema20 * 0.995
        )
        if not touched_20ma:
            return None

        # ── Continuation candle (today) ───────────────────────────────────────
        continuation = (
            today_close > today_open
            and today_close > prev_close
            and today_low  > prev_low
        )
        if not continuation:
            return None

        # ── CPR (from 2 candles ago) ──────────────────────────────────────────
        prev2_high  = float(high.iloc[-3])
        prev2_low   = float(low.iloc[-3])
        prev2_close = float(close.iloc[-3])
        cpr = _calc_cpr(prev2_high, prev2_low, prev2_close)

        cpr_support = (
            _is_near(prev_low, cpr["BC"],    pct=0.015)
            or _is_near(prev_low, cpr["Pivot"], pct=0.015)
            or _is_near(prev_low, cpr["TC"],    pct=0.015)
        )

        # ── Volume ────────────────────────────────────────────────────────────
        vol_avg   = float(volume.tail(20).mean())
        today_vol = float(volume.iloc[-1])
        vol_ratio = today_vol / vol_avg if vol_avg > 0 else 1.0

        pct_from_20ma = (today_close - today_ema20) / today_ema20 * 100
        touch_pct_val = (prev_low - prev_ema20) / prev_ema20 * 100
        change_1c     = (today_close - prev_close) / prev_close * 100

        if cpr_support and vol_ratio > 1.2:
            signal = "STRONG (20MA + CPR)"
        elif cpr_support:
            signal = "GOOD (20MA + CPR)"
        elif vol_ratio > 1.2:
            signal = "GOOD (20MA + Vol)"
        else:
            signal = "20MA Bounce"

        return {
            "Price":         round(today_close, 2),
            "Change%":       round(change_1c, 2),
            "EMA20":         round(today_ema20, 2),
            "% Above EMA20": round(pct_from_20ma, 2),
            "Touch%":        round(touch_pct_val, 2),
            "Vol Ratio":     round(vol_ratio, 2),
            "CPR Support":   "✅ Yes" if cpr_support else "—",
            "CPR BC":        round(cpr["BC"], 2),
            "CPR Pivot":     round(cpr["Pivot"], 2),
            "CPR TC":        round(cpr["TC"], 2),
            "Signal":        signal,
        }
    except Exception:
        return None


def run_ma_retracement_scan(
    symbols_df: pd.DataFrame,
    touch_pct: float = 0.01,
    interval: str = "1d",
    period: str = "1y",
    api_key: str = "",
    access_token: str = "",
) -> pd.DataFrame:
    # Batch-fetch live Kite quotes for all symbols in one API call
    kite_quotes: dict = {}
    if api_key and access_token:
        try:
            plain_syms = list(symbols_df["Symbol"].str.upper())
            kite_quotes = batch_quote_nse(api_key, access_token, plain_syms)
        except Exception:
            pass

    rows = []
    for _, row in symbols_df.iterrows():
        nse_key = f"NSE:{str(row['Symbol']).upper()}"
        quote   = kite_quotes.get(nse_key, {})
        result = analyze_ma_retracement(
            row["NSE_Symbol"],
            touch_pct=touch_pct,
            interval=interval,
            period=period,
            kite_quote=quote if quote else None,
        )
        if result:
            rows.append({"Symbol": row["Symbol"], "Company": row["Company"], **result})

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    signal_order = {
        "STRONG (20MA + CPR)": 0,
        "GOOD (20MA + CPR)":   1,
        "GOOD (20MA + Vol)":   2,
        "20MA Bounce":         3,
    }
    df["_rank"] = df["Signal"].map(signal_order).fillna(4)
    df = df.sort_values(["_rank", "Vol Ratio"], ascending=[True, False])
    return df.drop(columns=["_rank"]).reset_index(drop=True)
