"""
MA + CPR scalping — the manual Kite-chart method, as chart_bot's signal (CHART_SIGNAL=macpr).

Chart: the index on 3m, with simple MAs 10 / 20 / 50 and the previous day's CPR,
the same lines as the Kite chart (Pivot = (H+L+C)/3, BC = (H+L)/2, TC = 2P − BC).

  Side      the 50 MA: above it CE, below it PE.
  Entry     a candle closes across the 50 MA (the signal candle) and the NEXT candle
            also closes on that side (the confirmation candle) → buy right after the
            confirmation candle closes.
  CPR       a break of the CPR needs 2 candles closing beyond it (one candle poking
            through and falling back is a trap). With 2 closes above the CPR → no PE;
            2 closes below → no CE.
  Exit      a candle closes back across the 10 MA and the next one confirms it
            (2 closes past the 10 MA) → sell. Stop / target / square-off stay with the GTT.
  Range     optional (CHART_MACPR_FLIPS): skip when the 50 MA was crossed this many times
            in the 30 minutes before the signal (price flipping across the MAs = range).
  CPR width shown in the text: thin CPR (< 0.2 % of price) tends to trend, wide (> 0.4 %)
            to range. Logged only — it did not change results on 23–30 Oct.

Backtest, NIFTY 3m, 23 + 26–30 Oct 2026 (index points, candle-close fills, no costs):
12 trades, 9 won, +443 pts, average win +56, average loss −20. The momentum (IMI) and RSI
filters and the CPR-width filter were tested and all reduced the result, so they are off.
"""
from __future__ import annotations

import datetime

import pandas as pd

from screener.chart_analysis import _IST, fetch_candles, resolve_token

MA_FAST, MA_MID, MA_SLOW = 10, 20, 50
CLOSE_WINDOW_SEC = 90     # enter only this soon after the confirmation candle closed
FLIPS = 0                 # skip when the 50 MA was crossed ≥ FLIPS times in the last 30 min (0 = off)
FLIP_LOOKBACK = 10        # candles (30 min on 3m)
CPR_RULE = True           # 2 closes above CPR → no PE; 2 closes below → no CE
EXIT_MA = MA_FAST         # exit line (10 MA); 20 is the slower alternative
EXIT_CONFIRM = True       # exit needs 2 closes past the exit MA (False: the first close exits)
THIN_CPR_PCT, WIDE_CPR_PCT = 0.20, 0.40

_TF_SEC = {"1m": 60, "3m": 180, "5m": 300}


def _live_ltp(token: int) -> float | None:
    try:
        from screener.live_ticks import get_live_ltp
        return get_live_ltp(token)
    except Exception:
        return None


def cpr(df: pd.DataFrame, day: datetime.date) -> tuple[float, float, float] | None:
    """Previous trading day's CPR (pivot, low edge, high edge) from the intraday candles."""
    days = sorted({t.date() for t in df.index if t.date() < day})
    if not days:
        return None
    p = df[[t.date() == days[-1] for t in df.index]]
    h, l, c = float(p["High"].max()), float(p["Low"].min()), float(p["Close"].iloc[-1])
    piv = (h + l + c) / 3
    bc = (h + l) / 2
    tc = 2 * piv - bc
    return round(piv, 2), round(min(bc, tc), 2), round(max(bc, tc), 2)


def read(df: pd.DataFrame) -> dict:
    """Read CLOSED candles only. The last row is the confirmation candle, the one before it the signal."""
    out = {"signal": None, "label": "MA50 + CPR", "pattern": None, "range_mult": 0.0,
           "candle_time": None, "mid": None, "text": ""}
    if len(df) < MA_SLOW + 3:
        out["text"] = f"only {len(df)} candles — need {MA_SLOW + 3}"
        return out
    c = df["Close"].astype(float)
    ma = {n: c.rolling(n).mean() for n in (MA_FAST, MA_MID, MA_SLOW)}
    side = (c > ma[MA_SLOW]).astype(int) * 2 - 1          # +1 above the 50 MA, −1 below
    i, j = len(df) - 1, len(df) - 2
    t = df.index[i]
    lv = cpr(df, t.date())
    close = float(c.iloc[i])
    out.update({"candle_time": t, "close": close, "spot": close, "high": float(df["High"].iloc[i]),
                "low": float(df["Low"].iloc[i]), "lc_time": t, "lc_close": close,
                "ma10": round(float(ma[MA_FAST].iloc[i]), 2), "ma20": round(float(ma[MA_MID].iloc[i]), 2),
                "ma50": round(float(ma[MA_SLOW].iloc[i]), 2)})
    # exit reading for an open trade: last 2 closes past the exit MA
    em = ma[EXIT_MA]
    below = [float(c.iloc[k]) < float(em.iloc[k]) for k in (j, i)]
    above = [float(c.iloc[k]) > float(em.iloc[k]) for k in (j, i)]
    out["exit_CE"] = all(below) if EXIT_CONFIRM else below[-1]
    out["exit_PE"] = all(above) if EXIT_CONFIRM else above[-1]
    lv_txt = ""
    cpr_above = cpr_below = False
    if lv:
        piv, lo, hi = lv
        out.update({"cpr_p": piv, "cpr_lo": lo, "cpr_hi": hi,
                    "cpr_width_pct": round((hi - lo) / close * 100, 3)})
        cpr_above = all(float(c.iloc[k]) > hi for k in (j, i))
        cpr_below = all(float(c.iloc[k]) < lo for k in (j, i))
        w = out["cpr_width_pct"]
        kind = "thin" if w < THIN_CPR_PCT else "wide" if w > WIDE_CPR_PCT else "normal"
        where = "above CPR" if cpr_above else "below CPR" if cpr_below else "at / inside CPR"
        lv_txt = f" | CPR {lo:,.0f}–{hi:,.0f} ({kind} {w:.2f}%), {where}"
    base = (f"{t:%H:%M} close {close:,.1f} | MA10 {out['ma10']:,.0f} MA20 {out['ma20']:,.0f} "
            f"MA50 {out['ma50']:,.0f}{lv_txt}")
    crossed = side.iloc[j] != side.iloc[j - 1]
    if not crossed or side.iloc[i] != side.iloc[j]:
        where = "above" if side.iloc[i] > 0 else "below"
        out["text"] = base + f" — {where} the 50 MA, no fresh confirmed cross"
        return out
    sig = "CE" if side.iloc[j] > 0 else "PE"
    out["pattern"] = "ma50_cross_up" if sig == "CE" else "ma50_cross_down"
    out["stop"] = out["ma50"]
    sig_txt = (f"{df.index[j]:%H:%M} crossed the 50 MA {'up' if sig == 'CE' else 'down'}, "
               f"{t:%H:%M} confirmed")
    if FLIPS:
        flips = int((side.iloc[max(1, j - FLIP_LOOKBACK):j] != side.shift().iloc[max(1, j - FLIP_LOOKBACK):j]).sum())
        if flips >= FLIPS:
            out["text"] = base + f" — {sig_txt}, but {flips} crosses in 30 min (range), no trade"
            return out
    if CPR_RULE and sig == "PE" and cpr_above:
        out["text"] = base + f" — {sig_txt}, but 2 closes above the CPR: no PE"
        return out
    if CPR_RULE and sig == "CE" and cpr_below:
        out["text"] = base + f" — {sig_txt}, but 2 closes below the CPR: no CE"
        return out
    out["signal"] = sig
    out["label"] = f"MA50 cross {'up' if sig == 'CE' else 'down'} (confirmed)"
    out["text"] = base + f" — {sig_txt} → BUY {sig}"
    return out


def scan(symbol: str, interval: str, api_key: str, access_token: str,
         now: datetime.datetime | None = None) -> dict:
    """Live reading on the index chart. Same keys chart_bot reads from candles.scan()."""
    try:
        token, _ = resolve_token(symbol, api_key, access_token)
        df = fetch_candles(token, interval, api_key, access_token)
    except Exception as e:
        return {"error": str(e)}
    now = now or datetime.datetime.now(_IST)
    if df.index.tz is not None and now.tzinfo is None:
        now = now.replace(tzinfo=df.index.tz)
    tf = _TF_SEC.get(interval, 180)
    closed = df
    if len(df) and (now - df.index[-1]).total_seconds() < tf:
        closed = df.iloc[:-1]                      # drop the forming candle
    res = read(closed)
    res.update({"error": None, "forming": False, "source": "spot", "basis": 0.0})
    if res["signal"]:
        age = (now - res["candle_time"]).total_seconds() - tf      # seconds since it closed
        if age > CLOSE_WINDOW_SEC:
            res["text"] += f" — closed {age:.0f}s ago, too late to enter"
            res["signal"] = None
        else:
            ltp = _live_ltp(token)
            if ltp:
                res["spot"] = ltp
    return res
