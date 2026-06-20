import yfinance as yf
import pandas as pd
import numpy as np


def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def analyze_ma_crossover(nse_symbol: str) -> dict | None:
    """
    Detect 20 EMA crossing above 50 EMA on daily timeframe for position trading.

    Criteria:
    - Price > 200 EMA  (long-term uptrend)
    - 20 EMA crossed above 50 EMA within last 1–5 candles
    - Crossover confirmed over 2–3 consecutive candles (fake-breakout filter)
    - Volume surge on crossover day vs 20-day avg
    - RSI > 30 (not deeply oversold / trapped)
    """
    try:
        df = yf.download(nse_symbol, period="1y", interval="1d",
                         progress=False, auto_adjust=True)
        if df is None or len(df) < 60:
            return None

        df     = df.copy()
        close  = df["Close"].squeeze()
        volume = df["Volume"].squeeze()

        ema20  = _ema(close, 20)
        ema50  = _ema(close, 50)
        ema200 = _ema(close, 200)
        rsi    = _rsi(close)

        latest_close  = float(close.iloc[-1])
        latest_ema20  = float(ema20.iloc[-1])
        latest_ema50  = float(ema50.iloc[-1])
        latest_ema200 = float(ema200.iloc[-1])
        latest_rsi    = float(rsi.iloc[-1])

        # ── Filter 1: Price above 200 EMA ────────────────────────────────────
        if latest_close <= latest_ema200:
            return None

        # ── Filter 2: RSI > 30 ────────────────────────────────────────────────
        if latest_rsi <= 30:
            return None

        # ── Filter 3: Detect crossover within last 5 candles ─────────────────
        # crossover = ema20 was below ema50 before, now above
        cross_day = None
        for i in range(1, 6):  # check last 5 candles
            cur_above  = float(ema20.iloc[-i])  > float(ema50.iloc[-i])
            prev_below = float(ema20.iloc[-i-1]) <= float(ema50.iloc[-i-1])
            if cur_above and prev_below:
                cross_day = i  # 1 = today, 2 = yesterday, etc.
                break

        if cross_day is None:
            return None

        # ── Filter 4: Fake breakout check — 20 EMA stayed above 50 EMA ───────
        # For crossover that happened N days ago, check all days since stayed above
        confirmed_days = 0
        for i in range(1, cross_day + 1):
            if float(ema20.iloc[-i]) > float(ema50.iloc[-i]):
                confirmed_days += 1
            else:
                break  # broke back below — fake breakout

        # Need at least 2 confirmed days (crossover + 1 follow-through)
        if confirmed_days < 2 and cross_day > 1:
            return None

        # ── Filter 5: Volume surge on crossover day ───────────────────────────
        vol_avg         = float(volume.tail(20).mean())
        vol_cross_day   = float(volume.iloc[-cross_day])
        vol_today       = float(volume.iloc[-1])
        vol_ratio_cross = vol_cross_day / vol_avg if vol_avg > 0 else 1.0
        vol_ratio_today = vol_today     / vol_avg if vol_avg > 0 else 1.0

        # ── Momentum: price action since crossover ─────────────────────────────
        price_at_cross  = float(close.iloc[-cross_day])
        pct_move_since  = (latest_close - price_at_cross) / price_at_cross * 100

        # Distance from 200 EMA (buffer above it)
        pct_above_200   = (latest_close - latest_ema200) / latest_ema200 * 100

        # Gap between 20 EMA and 50 EMA (momentum of crossover)
        ema_gap_pct     = (latest_ema20 - latest_ema50) / latest_ema50 * 100

        high_52w = float(close.tail(252).max())
        low_52w  = float(close.tail(252).min())
        change_1d = (latest_close - float(close.iloc[-2])) / float(close.iloc[-2]) * 100

        # ── Signal strength ───────────────────────────────────────────────────
        has_vol_surge = vol_ratio_cross > 1.3 or vol_ratio_today > 1.3

        if cross_day <= 2 and has_vol_surge and confirmed_days >= 2:
            signal = "STRONG BUY"
        elif cross_day <= 3 and confirmed_days >= 2:
            signal = "BUY"
        elif confirmed_days >= 2:
            signal = "WATCH"
        else:
            signal = "WATCH"

        return {
            "Price":            round(latest_close, 2),
            "Change 1D%":       round(change_1d, 2),
            "EMA 20":           round(latest_ema20, 2),
            "EMA 50":           round(latest_ema50, 2),
            "EMA 200":          round(latest_ema200, 2),
            "% Above EMA200":   round(pct_above_200, 2),
            "EMA Gap%":         round(ema_gap_pct, 2),
            "RSI":              round(latest_rsi, 1),
            "Cross Day":        cross_day,
            "Confirmed Days":   confirmed_days,
            "Vol on Cross":     round(vol_ratio_cross, 2),
            "Vol Today":        round(vol_ratio_today, 2),
            "Move Since Cross%": round(pct_move_since, 2),
            "52W High":         round(high_52w, 2),
            "52W Low":          round(low_52w, 2),
            "Signal":           signal,
        }
    except Exception:
        return None


def run_crossover_scan(symbols_df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for _, row in symbols_df.iterrows():
        result = analyze_ma_crossover(row["NSE_Symbol"])
        if result:
            rows.append({
                "Symbol":  row["Symbol"],
                "Company": row["Company"],
                **result,
            })

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    signal_order = {"STRONG BUY": 0, "BUY": 1, "WATCH": 2}
    df["_rank"] = df["Signal"].map(signal_order).fillna(3)
    # Sort: signal first, then recency of crossover, then volume
    df = df.sort_values(["_rank", "Cross Day", "Vol on Cross"], ascending=[True, True, False])
    return df.drop(columns=["_rank"]).reset_index(drop=True)
