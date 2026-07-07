import yfinance as yf
import pandas as pd
import numpy as np
from screener.kite_hist import batch_quote_nse, patch_df_with_kite


def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _is_near(price: float, level: float, pct: float = 0.01) -> bool:
    return abs(price - level) / level <= pct


def _monthly_cpr(nse_symbol: str) -> dict | None:
    """Fetch previous month's OHLC and compute monthly CPR."""
    try:
        df = yf.download(nse_symbol, period="3mo", interval="1mo",
                         progress=False, auto_adjust=True)
        if df is None or len(df) < 2:
            return None
        # Use the second-to-last candle = completed previous month
        prev_month = df.iloc[-2]
        h = float(prev_month["High"].squeeze())
        l = float(prev_month["Low"].squeeze())
        c = float(prev_month["Close"].squeeze())
        pivot = (h + l + c) / 3
        bc    = (h + l) / 2
        tc    = (pivot - bc) + pivot
        return {
            "Monthly Pivot": round(pivot, 2),
            "Monthly BC":    round(min(bc, tc), 2),
            "Monthly TC":    round(max(bc, tc), 2),
        }
    except Exception:
        return None


def analyze_ma50_support(
    nse_symbol: str,
    touch_pct: float = 0.01,
    interval: str = "1d",
    period: str = "1y",
    kite_quote: dict | None = None,
) -> dict | None:
    try:
        df = yf.download(nse_symbol, period=period, interval=interval,
                         progress=False, auto_adjust=True)
        if df is None or len(df) < 60:
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

        ema20  = _ema(close, 20)
        ema50  = _ema(close, 50)
        ema200 = _ema(close, 200)

        today_close  = float(close.iloc[-1])
        today_open   = float(open_.iloc[-1])
        today_low    = float(low.iloc[-1])
        today_ema20  = float(ema20.iloc[-1])
        today_ema50  = float(ema50.iloc[-1])
        today_ema200 = float(ema200.iloc[-1])

        prev_close  = float(close.iloc[-2])
        prev_low    = float(low.iloc[-2])
        prev_ema50  = float(ema50.iloc[-2])

        # ── Uptrend: EMA20 > EMA50 > EMA200 ──────────────────────────────────
        is_uptrend = (
            today_ema20 > today_ema50
            and today_ema50 > today_ema200
        )
        if not is_uptrend:
            return None

        # ── Touch: previous candle's low within ±tolerance% of EMA50 ─────────
        touched_ema50 = (
            _is_near(prev_low, prev_ema50, pct=touch_pct)
            and prev_close >= prev_ema50 * 0.995   # closed above EMA50
        )
        if not touched_ema50:
            return None

        # ── Continuation: current candle bullish and closing higher ───────────
        continuation = (
            today_close > today_open
            and today_close > prev_close
            and today_low  > prev_low
        )
        if not continuation:
            return None

        # ── Monthly CPR ───────────────────────────────────────────────────────
        monthly_cpr = _monthly_cpr(nse_symbol)

        above_monthly_cpr = False
        if monthly_cpr:
            above_monthly_cpr = today_close > monthly_cpr["Monthly TC"]

        if not above_monthly_cpr:
            return None

        # ── Volume ────────────────────────────────────────────────────────────
        vol_avg   = float(volume.tail(20).mean())
        today_vol = float(volume.iloc[-1])
        vol_ratio = today_vol / vol_avg if vol_avg > 0 else 1.0

        pct_above_ema50  = (today_close - today_ema50)  / today_ema50  * 100
        pct_above_ema200 = (today_close - today_ema200) / today_ema200 * 100
        touch_pct_val    = (prev_low - prev_ema50)       / prev_ema50   * 100
        change_1d        = (today_close - prev_close)    / prev_close   * 100
        high_52w         = float(close.tail(252).max())

        # ── Signal ────────────────────────────────────────────────────────────
        if above_monthly_cpr and vol_ratio > 1.3:
            signal = "STRONG (50MA + Monthly CPR)"
        elif above_monthly_cpr and vol_ratio > 1.0:
            signal = "BUY (50MA + Monthly CPR)"
        else:
            signal = "WATCH"

        result = {
            "Price":             round(today_close, 2),
            "Change 1D%":        round(change_1d, 2),
            "EMA 20":            round(today_ema20, 2),
            "EMA 50":            round(today_ema50, 2),
            "EMA 200":           round(today_ema200, 2),
            "% Above EMA50":     round(pct_above_ema50, 2),
            "% Above EMA200":    round(pct_above_ema200, 2),
            "Touch%":            round(touch_pct_val, 2),
            "Vol Ratio":         round(vol_ratio, 2),
            "52W High":          round(high_52w, 2),
            "Above Monthly CPR": "✅ Yes" if above_monthly_cpr else "—",
            "Signal":            signal,
        }
        if monthly_cpr:
            result.update(monthly_cpr)

        return result
    except Exception:
        return None


def run_ma50_support_scan(
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
        result = analyze_ma50_support(
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
        "STRONG (50MA + Monthly CPR)": 0,
        "BUY (50MA + Monthly CPR)":    1,
        "WATCH":                        2,
    }
    df["_rank"] = df["Signal"].map(signal_order).fillna(3)
    df = df.sort_values(["_rank", "Vol Ratio"], ascending=[True, False])
    return df.drop(columns=["_rank"]).reset_index(drop=True)
