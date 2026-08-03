"""
RPCI Screener — 11-condition stock analysis inspired by the RPCI (Rohan Price
Condition Indicator) from TradingView / stratlab.in.

Conditions evaluated:
  Valuation           — price vs 52-week range
  Earnings Power      — 6-month return proxy
  Momentum            — RSI + EMA alignment
  Price Contraction   — Bollinger Band squeeze
  Timeframe Alignment — daily + weekly EMA agreement
  Outperformance      — 3-month return vs NIFTY 50
  Institutional Candles — high-volume significant moves
  Short-term Extension  — PASS = NOT overbought near-term
  Long-term Extension   — PASS = NOT far above EMA200
  Stage Analysis      — Weinstein Stage (1 Accumulation → 4 Downtrend)
  Dow Theory (W)      — weekly higher-highs / higher-lows

Score = count of PASS; max 11.
Label: ≥9 Strongly favourable, ≥7 Favourable, ≥5 Neutral, else Unfavourable.
"""

import datetime
from typing import Callable, Optional

import numpy as np
import pandas as pd
import yfinance as yf

_NIFTY_SYM = "^NSEI"

CONDITIONS = [
    "Valuation",
    "Earnings Power",
    "Momentum",
    "Price Contraction",
    "Timeframe Alignment",
    "Outperformance",
    "Institutional Candles",
    "Short-term Extension",
    "Long-term Extension",
    "Stage Analysis",
    "Dow Theory (W)",
]


def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def _rsi(s: pd.Series, n: int = 14) -> pd.Series:
    d = s.diff()
    g = d.clip(lower=0).rolling(n).mean()
    l = (-d.clip(upper=0)).rolling(n).mean()
    return 100 - 100 / (1 + g / l.replace(0, 1e-9))


def rpci_momentum_series(close: pd.Series, high: pd.Series, low: pd.Series) -> pd.Series:
    """
    RPCI Momentum Indicator: EMA(8) − EMA(21) normalised by ATR(14), scaled ×10.
    Positive = bullish, Negative = bearish.
    """
    try:
        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low  - close.shift()).abs(),
        ], axis=1).max(axis=1)
        atr  = tr.rolling(14).mean()
        diff = _ema(close, 8) - _ema(close, 21)
        mom  = (diff / atr.replace(0, np.nan) * 10).clip(-50, 50)
        return mom.dropna()
    except Exception:
        return pd.Series(dtype=float)


def _score_conditions(
    close_d: pd.Series,
    high_d:  pd.Series,
    low_d:   pd.Series,
    vol_d:   pd.Series,
    nifty_close: pd.Series,
    close_w: Optional[pd.Series] = None,
    high_w:  Optional[pd.Series] = None,
    low_w:   Optional[pd.Series] = None,
) -> dict:
    """Return {condition_name: {"result": str, "pass": bool}} for all 11 conditions."""
    c: dict = {}
    p = float(close_d.iloc[-1])

    # 1. Valuation — price distance from 52-week high
    try:
        n52   = min(252, len(close_d))
        hi52  = float(close_d.iloc[-n52:].max())
        off   = (hi52 - p) / hi52 * 100
        if off > 30:
            c["Valuation"] = {"result": "Undervalued", "pass": True}
        elif off < 5:
            c["Valuation"] = {"result": "Overvalued",  "pass": False}
        else:
            c["Valuation"] = {"result": "Fair value",  "pass": True}
    except Exception:
        c["Valuation"] = {"result": "N/A", "pass": False}

    # 2. Earnings Power — 6-month price return proxy
    try:
        n6 = min(126, len(close_d) - 1)
        ret = (p / float(close_d.iloc[-(n6 + 1)]) - 1) * 100
        if ret > 15:
            c["Earnings Power"] = {"result": "Strong",   "pass": True}
        elif ret > 0:
            c["Earnings Power"] = {"result": "Moderate", "pass": True}
        else:
            c["Earnings Power"] = {"result": "Weak",     "pass": False}
    except Exception:
        c["Earnings Power"] = {"result": "N/A", "pass": False}

    # 3. Momentum — RSI + EMA alignment
    try:
        rsi = float(_rsi(close_d).iloc[-1])
        e20 = float(_ema(close_d, 20).iloc[-1])
        e50 = float(_ema(close_d, 50).iloc[-1])
        if rsi > 60 and e20 > e50 and p > e20:
            c["Momentum"] = {"result": "Strong momentum",    "pass": True}
        elif rsi > 50:
            c["Momentum"] = {"result": "Moderate momentum",  "pass": True}
        else:
            c["Momentum"] = {"result": "Weak / no momentum", "pass": False}
    except Exception:
        c["Momentum"] = {"result": "N/A", "pass": False}

    # 4. Price Contraction — Bollinger Band squeeze (width ≤ 25th pct)
    try:
        sma20 = close_d.rolling(20).mean()
        std20 = close_d.rolling(20).std()
        bw    = (2 * std20 / sma20 * 100).dropna()
        if len(bw) >= 50:
            sq = bool(float(bw.iloc[-1]) <= float(bw.quantile(0.25)))
            c["Price Contraction"] = {"result": "YES" if sq else "NO", "pass": sq}
        else:
            c["Price Contraction"] = {"result": "N/A", "pass": False}
    except Exception:
        c["Price Contraction"] = {"result": "N/A", "pass": False}

    # 5. Timeframe Alignment — EMA20 > EMA50 daily AND (weekly if available)
    try:
        e20d = float(_ema(close_d, 20).iloc[-1])
        e50d = float(_ema(close_d, 50).iloc[-1])
        d_up = e20d > e50d and p > e20d

        w_up = True   # optimistic if no weekly data
        if close_w is not None and len(close_w) >= 20:
            e10w = float(_ema(close_w, 10).iloc[-1])
            e20w = float(_ema(close_w, 20).iloc[-1])
            w_up = bool(e10w > e20w and float(close_w.iloc[-1]) > e10w)

        aligned = d_up and w_up
        c["Timeframe Alignment"] = {"result": "YES" if aligned else "NO", "pass": aligned}
    except Exception:
        c["Timeframe Alignment"] = {"result": "N/A", "pass": False}

    # 6. Outperformance — 3-month return vs NIFTY 50
    try:
        if len(nifty_close) >= 60 and len(close_d) >= 60:
            n3 = min(63, min(len(close_d), len(nifty_close)) - 1)
            sr   = (p / float(close_d.iloc[-(n3 + 1)]) - 1) * 100
            nr   = (float(nifty_close.iloc[-1]) / float(nifty_close.iloc[-(n3 + 1)]) - 1) * 100
            diff = sr - nr
            op   = diff > 0
            c["Outperformance"] = {
                "result": f"YES (+{diff:.1f}%)" if op else f"NO ({diff:.1f}%)",
                "pass":   op,
            }
        else:
            c["Outperformance"] = {"result": "N/A", "pass": False}
    except Exception:
        c["Outperformance"] = {"result": "N/A", "pass": False}

    # 7. Institutional Candles — vol > 2× avg AND |move| > 1.5% in last 30 days
    try:
        va  = vol_d.rolling(20).mean()
        mv  = close_d.pct_change().abs() * 100
        cnt = int(((vol_d > va * 2) & (mv > 1.5)).rolling(30).sum().iloc[-1])
        c["Institutional Candles"] = {"result": str(cnt), "pass": cnt >= 1}
    except Exception:
        c["Institutional Candles"] = {"result": "0", "pass": False}

    # 8. Short-term Extension — PASS = NOT extended (RSI < 70 AND < 15% above EMA20)
    try:
        rsi_v = float(_rsi(close_d).iloc[-1])
        e20_v = float(_ema(close_d, 20).iloc[-1])
        ext   = rsi_v > 70 or (p / e20_v - 1) * 100 > 15
        c["Short-term Extension"] = {"result": "YES" if ext else "NO", "pass": not ext}
    except Exception:
        c["Short-term Extension"] = {"result": "N/A", "pass": False}

    # 9. Long-term Extension — PASS = NOT extended vs EMA200 (< 30% above)
    try:
        if len(close_d) >= 200:
            e200 = float(_ema(close_d, 200).iloc[-1])
            ext  = (p / e200 - 1) * 100 > 30
            c["Long-term Extension"] = {"result": "YES" if ext else "NO", "pass": not ext}
        else:
            c["Long-term Extension"] = {"result": "N/A", "pass": False}
    except Exception:
        c["Long-term Extension"] = {"result": "N/A", "pass": False}

    # 10. Stage Analysis — Weinstein via 150-day (30-week) MA
    try:
        if len(close_d) >= 150:
            ma150  = close_d.rolling(150).mean()
            ma50   = close_d.rolling(50).mean()
            m150n  = float(ma150.iloc[-1])
            m150p  = float(ma150.iloc[-20])  # ~4 weeks ago
            m50n   = float(ma50.iloc[-1])
            if p > m150n and m150n > m150p and m50n > m150n:
                c["Stage Analysis"] = {"result": "Stage 2 - Uptrend",      "pass": True}
            elif p < m150n and m150n < m150p:
                c["Stage Analysis"] = {"result": "Stage 4 - Downtrend",    "pass": False}
            elif p > m150n:
                c["Stage Analysis"] = {"result": "Stage 3 - Distribution", "pass": False}
            else:
                c["Stage Analysis"] = {"result": "Stage 1 - Accumulation", "pass": False}
        else:
            c["Stage Analysis"] = {"result": "N/A", "pass": False}
    except Exception:
        c["Stage Analysis"] = {"result": "N/A", "pass": False}

    # 11. Dow Theory (W) — weekly HH + HL (falls back to daily if weekly unavailable)
    try:
        src_h = high_w  if (high_w  is not None and len(high_w)  >= 8) else high_d
        src_l = low_w   if (low_w   is not None and len(low_w)   >= 8) else low_d
        h8 = src_h.iloc[-8:].values.astype(float)
        l8 = src_l.iloc[-8:].values.astype(float)
        hh = float(h8[-4:].max()) > float(h8[:4].max())
        hl = float(l8[-4:].min()) > float(l8[:4].min())
        if hh and hl:
            c["Dow Theory (W)"] = {"result": "Uptrend",   "pass": True}
        elif not hh and not hl:
            c["Dow Theory (W)"] = {"result": "Downtrend", "pass": False}
        else:
            c["Dow Theory (W)"] = {"result": "Sideways",  "pass": False}
    except Exception:
        c["Dow Theory (W)"] = {"result": "N/A", "pass": False}

    return c


def _label(score: int, total: int = 11) -> str:
    if score >= 9:   return "Strongly favourable"
    if score >= 7:   return "Favourable"
    if score >= 5:   return "Neutral"
    return "Unfavourable"


def analyse_single(symbol: str) -> dict:
    """
    Full single-stock RPCI analysis including weekly data and momentum histogram.
    Downloads 3 data-sets; suitable for a detail view (not bulk scan).
    Returns {"symbol", "conditions", "score", "total", "label",
             "momentum_hist", "close", "high", "low", "price", "error"}.
    """
    nse_sym = f"{symbol}.NS"
    try:
        daily  = yf.download(nse_sym, period="2y", interval="1d",  auto_adjust=True, progress=False)
        weekly = yf.download(nse_sym, period="3y", interval="1wk", auto_adjust=True, progress=False)
        nifty  = yf.download(_NIFTY_SYM, period="1y", interval="1d", auto_adjust=True, progress=False)

        for df in (daily, weekly, nifty):
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)

        if daily.empty or len(daily) < 60:
            return {"error": f"Insufficient data for {symbol}"}

        cd = daily["Close"].dropna()
        hd = daily["High"].dropna()
        ld = daily["Low"].dropna()
        vd = daily["Volume"].dropna()
        cw = weekly["Close"].dropna() if not weekly.empty else None
        hw = weekly["High"].dropna()  if not weekly.empty else None
        lw = weekly["Low"].dropna()   if not weekly.empty else None
        nc = nifty["Close"].dropna()  if not nifty.empty  else pd.Series(dtype=float)

        conds = _score_conditions(cd, hd, ld, vd, nc, cw, hw, lw)
        score = sum(1 for v in conds.values() if v["pass"])

        return {
            "symbol":        symbol,
            "conditions":    conds,
            "score":         score,
            "total":         len(conds),
            "label":         _label(score),
            "momentum_hist": rpci_momentum_series(cd, hd, ld),
            "close":         cd,
            "high":          hd,
            "low":           ld,
            "price":         float(cd.iloc[-1]),
            "error":         None,
        }
    except Exception as e:
        return {"error": str(e)}


def run_rpci_scan(
    symbols: list[str],
    progress_cb: Optional[Callable[[str, int], None]] = None,
) -> pd.DataFrame:
    """
    Batch RPCI scan using a single multi-ticker yfinance download.
    Weekly data is skipped in batch mode (too slow for 500 stocks) — the
    detail view (analyse_single) fetches it.
    Returns DataFrame sorted by score desc with columns:
      symbol, price, score, total, label,
      _{condition} (bool), _{condition}_val (str) for each of 11 conditions.
    """
    def _p(msg: str, pct: int):
        if progress_cb:
            progress_cb(msg, pct)

    _p("Downloading 2y daily data for all symbols…", 5)
    tickers = [f"{s}.NS" for s in symbols]

    try:
        raw = yf.download(
            tickers, period="2y", interval="1d",
            auto_adjust=True, progress=False, threads=True,
        )
    except Exception as e:
        _p(f"Download failed: {e}", 0)
        return pd.DataFrame()

    _p("Downloading NIFTY 50 benchmark…", 45)
    try:
        nraw = yf.download(_NIFTY_SYM, period="1y", interval="1d", auto_adjust=True, progress=False)
        if isinstance(nraw.columns, pd.MultiIndex):
            nraw.columns = nraw.columns.get_level_values(0)
        nifty_close = nraw["Close"].dropna()
    except Exception:
        nifty_close = pd.Series(dtype=float)

    _p("Computing 11 conditions per stock…", 50)
    is_multi = isinstance(raw.columns, pd.MultiIndex)
    rows: list[dict] = []

    for i, (sym, tick) in enumerate(zip(symbols, tickers)):
        try:
            if is_multi:
                cd_raw = raw["Close"]
                cd = (cd_raw[tick] if tick in cd_raw.columns else pd.Series(dtype=float)).dropna()
                hd = (raw["High"][tick]   if "High"   in raw and tick in raw["High"].columns   else cd).dropna()
                ld = (raw["Low"][tick]    if "Low"    in raw and tick in raw["Low"].columns    else cd).dropna()
                vd = (raw["Volume"][tick] if "Volume" in raw and tick in raw["Volume"].columns else pd.Series(dtype=float)).dropna()
            else:
                cd = raw["Close"].dropna()
                hd = raw.get("High",   raw["Close"]).dropna()
                ld = raw.get("Low",    raw["Close"]).dropna()
                vd = raw.get("Volume", pd.Series(dtype=float)).dropna()

            if len(cd) < 60:
                continue

            conds = _score_conditions(cd, hd, ld, vd, nifty_close)
            score = sum(1 for v in conds.values() if v["pass"])

            row: dict = {
                "symbol": sym,
                "price":  round(float(cd.iloc[-1]), 2),
                "score":  score,
                "total":  len(conds),
                "label":  _label(score),
            }
            for cname, cv in conds.items():
                row[f"_{cname}"]     = cv["pass"]
                row[f"_{cname}_val"] = cv["result"]
            rows.append(row)

        except Exception:
            pass

        if i % 25 == 0:
            _p(f"Processed {i + 1}/{len(symbols)} stocks…", 50 + int(i / max(len(symbols), 1) * 40))

    _p("Sorting results…", 95)
    if not rows:
        return pd.DataFrame()

    return pd.DataFrame(rows).sort_values("score", ascending=False).reset_index(drop=True)
