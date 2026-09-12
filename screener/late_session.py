"""
Late Session Blaster — signal engine for the last 45–60 minutes of trading.

Covers:
  • NSE last hour  (2:30–3:30 PM IST) — NIFTY / BANKNIFTY
  • BSE CAS window (3:00–4:00 PM IST) — SENSEX Closing Auction Session

FLIP GUARD — avoids the "PE then CE in 1 minute" problem:
  A direction reversal (CE→PE or PE→CE) is accepted ONLY when:
    1. The opposite direction has appeared for ≥ MIN_CONFIRMS consecutive refreshes  AND
    2. At least COOLDOWN_SECS (300 s = 5 min) have passed since the last direction change.
  While pending: signal stays "WATCH" with a timer showing how long until the flip is allowed.

Scoring (sum capped at -10 … +10):
  F1  5m EMA trend + candle streak         max ±3
  F2  vs VWAP (0.1 %/0.3 % bands)         max ±2
  F3  Volume surge × price direction       max ±2
  F4  Price velocity (pts per 5-min bar)   max ±2
  F5  Elder Ray Bull/Bear Power            max ±1
  F6  CAS amplifier (SENSEX only)          max ±2  [only when score already ≥±4]
"""

import datetime
from typing import Optional

import numpy as np
import pandas as pd
import yfinance as yf

_IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

INDEX_CONFIG: dict[str, dict] = {
    "NIFTY": {
        "yf": "^NSEI",    "step": 50,  "lot": 25,
        "color": "#00d4aa", "cas": False,
        "label": "NIFTY 50",
    },
    "BANKNIFTY": {
        "yf": "^NSEBANK", "step": 100, "lot": 15,
        "color": "#79c0ff", "cas": False,
        "label": "BANK NIFTY",
    },
    "SENSEX": {
        "yf": "^BSESN",   "step": 100, "lot": 10,
        "color": "#ffa657", "cas": True,
        "label": "SENSEX",
    },
}

COOLDOWN_SECS = 300   # 5 min before allowing a direction flip
MIN_CONFIRMS  = 3     # consecutive refreshes in the new direction


def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def _vwap_today(df: pd.DataFrame) -> float:
    """VWAP from today's bars (any interval)."""
    try:
        idx = df.index
        if idx.tz is None:
            idx = idx.tz_localize("UTC")
        idx = idx.tz_convert(_IST)
        today = datetime.date.today()
        mask  = pd.Series(idx.date, index=df.index) == today
        d = df[mask]
        if d.empty or "Volume" not in d.columns:
            return float("nan")
        tp  = (d["High"] + d["Low"] + d["Close"]) / 3
        vol = d["Volume"].replace(0, np.nan)
        return float((tp * vol).sum() / vol.sum())
    except Exception:
        return float("nan")


def atm_strike(spot: float, step: int) -> int:
    return int(round(spot / step) * step)


def score_signal(
    df_5m: pd.DataFrame,
    df_1m: Optional[pd.DataFrame],
    is_cas: bool = False,
) -> dict:
    """
    Compute the directional score and supporting factors.

    Returns:
        score        int  -10 … +10 (positive = CE, negative = PE)
        factors      list of {name, value, bias, pts}
        spot, vwap, velocity, volume_ratio
    """
    factors: list[dict] = []
    score = 0

    close = df_5m["Close"].dropna()
    high  = df_5m["High"].dropna()
    low   = df_5m["Low"].dropna()
    vol   = df_5m["Volume"].dropna()

    if len(close) < 6:
        return {
            "score": 0, "factors": [], "error": "Insufficient data",
            "spot": float("nan"), "vwap": float("nan"),
            "velocity": 0.0, "volume_ratio": 1.0,
        }

    spot = float(close.iloc[-1])

    # ── F1: 5m EMA trend + candle streak  (max ±3) ───────────────────────────
    e9  = _ema(close, 9)
    e21 = _ema(close, 21)
    tail4 = df_5m.tail(4)
    bull4 = int((tail4["Close"] > tail4["Open"]).sum())
    bear4 = int((tail4["Close"] < tail4["Open"]).sum())
    above = spot > float(e9.iloc[-1]) > float(e21.iloc[-1])
    below = spot < float(e9.iloc[-1]) < float(e21.iloc[-1])

    if above and bull4 >= 3:
        s = 3;  lbl = f"Strong uptrend ({bull4}/4 green)";  bias = "CE"
    elif above and bull4 >= 2:
        s = 2;  lbl = f"Uptrend ({bull4}/4 green)";         bias = "CE"
    elif above:
        s = 1;  lbl = "EMA stack bullish";                  bias = "CE"
    elif below and bear4 >= 3:
        s = -3; lbl = f"Strong downtrend ({bear4}/4 red)";  bias = "PE"
    elif below and bear4 >= 2:
        s = -2; lbl = f"Downtrend ({bear4}/4 red)";         bias = "PE"
    elif below:
        s = -1; lbl = "EMA stack bearish";                  bias = "PE"
    else:
        s = 0;  lbl = "Mixed / ranging";                    bias = "—"

    factors.append({"name": "5m EMA Trend",  "value": lbl,  "bias": bias, "pts": s})
    score += s

    # ── F2: VWAP position  (max ±2) ──────────────────────────────────────────
    vwap = (
        _vwap_today(df_1m)
        if (df_1m is not None and not df_1m.empty)
        else _vwap_today(df_5m)
    )
    if not np.isnan(vwap) and vwap > 0:
        pct = (spot - vwap) / vwap * 100
        if pct > 0.3:
            s = 2; lbl = f"+{pct:.2f}% above VWAP"; bias = "CE"
        elif pct > 0.1:
            s = 1; lbl = f"+{pct:.2f}% above VWAP"; bias = "CE"
        elif pct < -0.3:
            s = -2; lbl = f"{pct:.2f}% below VWAP"; bias = "PE"
        elif pct < -0.1:
            s = -1; lbl = f"{pct:.2f}% below VWAP"; bias = "PE"
        else:
            s = 0;  lbl = f"{pct:+.2f}% (at VWAP)"; bias = "—"
        factors.append({"name": "vs VWAP", "value": lbl, "bias": bias, "pts": s})
        score += s
    else:
        vwap = float("nan")

    # ── F3: Volume surge × price direction  (max ±2) ─────────────────────────
    volume_ratio = 1.0
    if len(vol) >= 10:
        avg_vol      = float(vol.iloc[-10:-1].mean())
        cur_vol      = float(vol.iloc[-1])
        volume_ratio = cur_vol / avg_vol if avg_vol > 0 else 1.0
        price_dir    = 1 if float(close.iloc[-1]) >= float(df_5m["Open"].iloc[-1]) else -1

        if volume_ratio >= 2.5:
            s = 2 * price_dir
        elif volume_ratio >= 1.5:
            s = 1 * price_dir
        else:
            s = 0

        bias = "CE" if s > 0 else ("PE" if s < 0 else "—")
        lbl  = f"{volume_ratio:.1f}× avg" + (" 🔥" if volume_ratio >= 2.5 else "")
        factors.append({"name": "Volume Surge", "value": lbl, "bias": bias, "pts": s})
        score += s

    # ── F4: Price velocity — last 3 bars  (max ±2) ───────────────────────────
    velocity = 0.0
    if len(close) >= 4:
        velocity = (float(close.iloc[-1]) - float(close.iloc[-4])) / 3
        va = abs(velocity)
        if va >= 40:
            s = 2 if velocity > 0 else -2
            lbl = f"{velocity:+.1f} pts/5m  🚀"
        elif va >= 20:
            s = 1 if velocity > 0 else -1
            lbl = f"{velocity:+.1f} pts/5m"
        else:
            s = 0; lbl = f"{velocity:+.1f} pts/5m (slow)"
        bias = "CE" if s > 0 else ("PE" if s < 0 else "—")
        factors.append({"name": "Velocity", "value": lbl, "bias": bias, "pts": s})
        score += s

    # ── F5: Elder Ray Bull / Bear Power  (max ±1) ────────────────────────────
    try:
        ema13    = _ema(close, 13)
        bull_pw  = float(high.iloc[-1])  - float(ema13.iloc[-1])
        bear_pw  = float(low.iloc[-1])   - float(ema13.iloc[-1])
        if bull_pw > 0 and bear_pw < 0 and bull_pw / spot * 100 > 0.1:
            s = 1; lbl = f"Bull +{bull_pw/spot*100:.2f}%"; bias = "CE"
        elif bull_pw < 0 and bear_pw < 0 and abs(bear_pw) / spot * 100 > 0.1:
            s = -1; lbl = f"Bear {bear_pw/spot*100:.2f}%";  bias = "PE"
        else:
            s = 0; lbl = "Mixed"; bias = "—"
        factors.append({"name": "Elder Ray", "value": lbl, "bias": bias, "pts": s})
        score += s
    except Exception:
        pass

    # ── F6: CAS amplifier (SENSEX, 3–4 PM only)  (max ±2) ───────────────────
    if is_cas:
        # Only amplify when trend is already clear — prevents noise boost
        if score >= 4:
            s = 2;  lbl = "Trend sustained into CAS auction ↑"; bias = "CE"
        elif score <= -4:
            s = -2; lbl = "Trend sustained into CAS auction ↓"; bias = "PE"
        elif score >= 2:
            s = 1;  lbl = "Mild bull bias into CAS";            bias = "CE"
        elif score <= -2:
            s = -1; lbl = "Mild bear bias into CAS";            bias = "PE"
        else:
            s = 0;  lbl = "No clear direction for CAS";         bias = "—"
        factors.append({"name": "CAS Factor", "value": lbl, "bias": bias, "pts": s})
        score += s

    score = max(-10, min(10, score))
    return {
        "score": score, "factors": factors,
        "spot": spot, "vwap": vwap,
        "velocity": velocity, "volume_ratio": volume_ratio,
    }


def raw_signal(score: int) -> str:
    if score >= 7:  return "STRONG BUY CE"
    if score >= 4:  return "BUY CE"
    if score <= -7: return "STRONG BUY PE"
    if score <= -4: return "BUY PE"
    if score >= 2:  return "WATCH CE"
    if score <= -2: return "WATCH PE"
    return "WATCH"


def _dir(sig: str) -> str:
    """Extract direction from signal string."""
    if "CE" in sig: return "CE"
    if "PE" in sig: return "PE"
    return "WATCH"


def apply_flip_guard(
    new_raw:          str,
    prev_signal:      str,
    prev_signal_time,
    pending_count:    int,
) -> tuple[str, int, str]:
    """
    Flip-prevention gate.

    Args:
        new_raw        : raw signal from score_signal
        prev_signal    : last accepted signal
        prev_signal_time: datetime when prev_signal was set
        pending_count  : consecutive refreshes we've seen the new direction

    Returns:
        (final_signal, new_pending_count, status_msg)
        status_msg is shown to the user when a flip is pending.
    """
    now      = datetime.datetime.now(_IST)
    new_dir  = _dir(new_raw)
    prev_dir = _dir(prev_signal)

    # Same direction (or no prior signal) → accept immediately, reset count
    if prev_dir == "WATCH" or new_dir == "WATCH" or new_dir == prev_dir:
        return new_raw, pending_count + 1 if new_dir == prev_dir else 1, ""

    # Direction reversal — check confirmation requirements
    elapsed = (now - prev_signal_time).total_seconds() if prev_signal_time else COOLDOWN_SECS + 1
    new_pending = pending_count + 1

    if new_pending >= MIN_CONFIRMS and elapsed >= COOLDOWN_SECS:
        # Confirmed flip — accept and reset count
        return new_raw, 1, ""

    # Not yet confirmed — suppress, show status
    need_confirms = max(0, MIN_CONFIRMS - new_pending)
    need_secs     = max(0, int(COOLDOWN_SECS - elapsed))
    msg = (
        f"Flip {prev_dir}→{new_dir} pending: "
        f"{need_confirms} more confirm{'s' if need_confirms != 1 else ''} needed"
        + (f", {need_secs}s cooldown remaining" if need_secs > 0 else "")
    )
    return "WATCH", new_pending, msg


def fetch_data(index: str) -> dict:
    """Download 5m and 1m data for the underlying index."""
    cfg = INDEX_CONFIG.get(index)
    if not cfg:
        return {"error": f"Unknown index: {index}"}
    sym = cfg["yf"]
    try:
        df_5m = yf.download(sym, period="1d", interval="5m",
                            auto_adjust=True, progress=False)
        df_1m = yf.download(sym, period="1d", interval="1m",
                            auto_adjust=True, progress=False)
        for df in (df_5m, df_1m):
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
        if df_5m.empty or len(df_5m) < 6:
            return {"error": f"No intraday data for {sym}. Market may be closed."}
        return {"df_5m": df_5m, "df_1m": df_1m, "error": None}
    except Exception as e:
        return {"error": str(e)}


def session_window() -> tuple[bool, bool, int]:
    """
    Returns (in_late_session, in_cas, mins_to_nse_close).
    in_late_session : 14:30–15:30 IST  (active trading window we watch)
    in_cas          : 15:00–16:00 IST  (BSE Closing Auction Session)
    """
    now      = datetime.datetime.now(_IST)
    t        = now.time()
    close_dt = now.replace(hour=15, minute=30, second=0, microsecond=0)
    mins_left = max(0, int((close_dt - now).total_seconds() / 60))
    in_late  = datetime.time(14, 30) <= t <= datetime.time(15, 30)
    in_cas   = datetime.time(15,  0) <= t <= datetime.time(16,  0)
    return in_late, in_cas, mins_left
