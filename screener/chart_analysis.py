"""
Chart Analysis — SMC + Institutional indicators engine.

Computes for any Kite symbol / timeframe:
  EMA 20/50/200, VWAP (session-reset intraday), RSI 14, ADX 14
  Swing highs/lows (pivot n=3)
  BOS (Break of Structure) and CHoCH (Change of Character)
  Order Blocks (bullish / bearish)
  Fair Value Gaps (bullish / bearish, filled detection)
  Buy / Sell signal markers
"""

import datetime
from io import StringIO

import numpy as np
import pandas as pd
import requests

_IST       = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
_KITE_BASE = "https://api.kite.trade"

# Hardcoded spot index tokens (no instruments lookup needed)
_INDEX_TOKENS: dict[str, int] = {
    "NIFTY":     256265,
    "BANKNIFTY": 260105,
    "SENSEX":    265,
}

INTERVAL_CFG: dict[str, dict] = {
    "1m":  {"kite": "minute",   "days": 2,   "label": "1 Min"},
    "2m":  {"kite": "2minute",  "days": 3,   "label": "2 Min"},
    "5m":  {"kite": "5minute",  "days": 5,   "label": "5 Min"},
    "10m": {"kite": "10minute", "days": 7,   "label": "10 Min"},
    "15m": {"kite": "15minute", "days": 15,  "label": "15 Min"},
    "1H":  {"kite": "60minute", "days": 60,  "label": "1 Hour"},
    "1D":  {"kite": "day",      "days": 500, "label": "Daily"},
}


# ── Kite helpers ──────────────────────────────────────────────────────────────

def _hdrs(api_key: str, access_token: str) -> dict:
    return {"X-Kite-Version": "3", "Authorization": f"token {api_key}:{access_token}"}


def resolve_token(symbol: str, api_key: str, access_token: str) -> tuple[int, str]:
    """Return (instrument_token, display_name) for any symbol."""
    sym = symbol.upper().strip()
    if sym in _INDEX_TOKENS:
        return _INDEX_TOKENS[sym], sym

    hdrs = _hdrs(api_key, access_token)

    # NSE equities
    try:
        r = requests.get(f"{_KITE_BASE}/instruments/NSE", headers=hdrs, timeout=20)
        if r.ok:
            df = pd.read_csv(StringIO(r.text))
            m = df[(df["tradingsymbol"] == sym) & (df["instrument_type"] == "EQ")]
            if not m.empty:
                return int(m.iloc[0]["instrument_token"]), sym
    except Exception:
        pass

    # NFO (NIFTY options/futures)
    try:
        r = requests.get(f"{_KITE_BASE}/instruments/NFO", headers=hdrs, timeout=30)
        if r.ok:
            df = pd.read_csv(StringIO(r.text))
            m = df[df["tradingsymbol"] == sym]
            if not m.empty:
                return int(m.iloc[0]["instrument_token"]), sym
    except Exception:
        pass

    # BFO (SENSEX options/futures)
    try:
        r = requests.get(f"{_KITE_BASE}/instruments/BFO", headers=hdrs, timeout=30)
        if r.ok:
            df = pd.read_csv(StringIO(r.text))
            m = df[df["tradingsymbol"] == sym]
            if not m.empty:
                return int(m.iloc[0]["instrument_token"]), sym
    except Exception:
        pass

    raise ValueError(f"'{symbol}' not found in NSE / NFO / BFO instruments")


def fetch_candles(token: int, interval_key: str,
                  api_key: str, access_token: str) -> pd.DataFrame:
    cfg     = INTERVAL_CFG[interval_key]
    now_ist = datetime.datetime.now(_IST)
    from_dt = now_ist - datetime.timedelta(days=cfg["days"])
    params  = {
        "from":       from_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "to":         now_ist.strftime("%Y-%m-%d %H:%M:%S"),
        "continuous": 0,
        "oi":         0,
    }
    r = requests.get(
        f"{_KITE_BASE}/instruments/historical/{token}/{cfg['kite']}",
        headers=_hdrs(api_key, access_token),
        params=params,
        timeout=30,
    )
    if not r.ok:
        raise RuntimeError(f"Kite {r.status_code}: {r.text[:200]}")
    candles = r.json().get("data", {}).get("candles", [])
    if not candles:
        raise RuntimeError("No candle data — try a different timeframe")

    df = pd.DataFrame(candles,
                      columns=["datetime", "Open", "High", "Low", "Close", "Volume"])
    df["datetime"] = pd.to_datetime(df["datetime"])
    if df["datetime"].dt.tz is None:
        df["datetime"] = df["datetime"].dt.tz_localize("Asia/Kolkata")
    else:
        df["datetime"] = df["datetime"].dt.tz_convert("Asia/Kolkata")
    df = df.set_index("datetime").sort_index()

    if interval_key != "1D":
        df = df.between_time("09:15", "15:35")

    return df.tail(300)   # keep last 300 candles for performance


# ── Indicators ────────────────────────────────────────────────────────────────

def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def _rsi(s: pd.Series, n: int = 14) -> pd.Series:
    d = s.diff()
    g = d.clip(lower=0).rolling(n).mean()
    l = (-d).clip(lower=0).rolling(n).mean()
    return 100 - 100 / (1 + g / l.replace(0, np.nan))


def _adx(df: pd.DataFrame, n: int = 14):
    h, l, c   = df["High"], df["Low"], df["Close"]
    pc        = c.shift(1)
    tr        = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    dm_p      = (h - h.shift(1)).clip(lower=0)
    dm_n      = (l.shift(1) - l).clip(lower=0)
    dm_p      = dm_p.where(dm_p > dm_n, 0)
    dm_n      = dm_n.where(dm_n > dm_p, 0)
    atr       = tr.ewm(span=n, adjust=False).mean()
    safe_atr  = atr.replace(0, np.nan)
    di_p      = 100 * dm_p.ewm(span=n, adjust=False).mean() / safe_atr
    di_n      = 100 * dm_n.ewm(span=n, adjust=False).mean() / safe_atr
    dx        = 100 * (di_p - di_n).abs() / (di_p + di_n).replace(0, np.nan)
    return dx.ewm(span=n, adjust=False).mean(), di_p, di_n


def _vwap(df: pd.DataFrame, intraday: bool = True) -> pd.Series:
    tp  = (df["High"] + df["Low"] + df["Close"]) / 3
    # Indices (NIFTY, SENSEX, BANKNIFTY) have zero volume in Kite historical data.
    # Fall back to cumulative / rolling typical price when volume is absent.
    has_volume = df["Volume"].sum() > 0
    if intraday:
        vwap = pd.Series(np.nan, index=df.index)
        for _date, idxs in df.groupby(df.index.date).groups.items():
            grp  = df.loc[idxs]
            tp_g = (grp["High"] + grp["Low"] + grp["Close"]) / 3
            if has_volume:
                v_g    = grp["Volume"].replace(0, np.nan)
                cumtp  = (tp_g * v_g).cumsum()
                cumvol = v_g.cumsum()
                vwap.loc[idxs] = (cumtp / cumvol).values
            else:
                # No volume: cumulative average of typical price (session-anchored)
                vwap.loc[idxs] = tp_g.expanding().mean().values
        return vwap
    else:
        if has_volume:
            vol = df["Volume"].replace(0, np.nan)
            return (tp * vol).rolling(20).sum() / vol.rolling(20).sum()
        else:
            return tp.rolling(20).mean()


def compute_indicators(df: pd.DataFrame, intraday: bool = True) -> pd.DataFrame:
    df = df.copy()
    df["EMA20"]  = _ema(df["Close"], 20)
    df["EMA50"]  = _ema(df["Close"], 50)
    df["EMA200"] = _ema(df["Close"], 200)
    df["VWAP"]   = _vwap(df, intraday=intraday)
    df["RSI"]    = _rsi(df["Close"])
    adx, di_p, di_n = _adx(df)
    df["ADX"]    = adx
    df["DI_pos"] = di_p
    df["DI_neg"] = di_n
    vol_avg      = df["Volume"].rolling(20).mean()
    df["VolAvg"] = vol_avg
    return df


# ── SMC Detection ─────────────────────────────────────────────────────────────

def detect_swings(df: pd.DataFrame, n: int = 3) -> pd.DataFrame:
    df   = df.copy()
    highs = df["High"].values
    lows  = df["Low"].values
    sh    = np.zeros(len(df), dtype=bool)
    sl    = np.zeros(len(df), dtype=bool)
    for i in range(n, len(df) - n):
        if highs[i] == highs[i - n: i + n + 1].max():
            sh[i] = True
        if lows[i] == lows[i - n: i + n + 1].min():
            sl[i] = True
    df["swing_high"] = sh
    df["swing_low"]  = sl
    return df


def detect_structure(df: pd.DataFrame) -> tuple[pd.DataFrame, list]:
    df = df.copy()
    for col in ("bos_up", "bos_down", "choch_up", "choch_down"):
        df[col] = False

    events: list[dict] = []
    trend         = "neutral"
    confirmed_sh  = None
    confirmed_sl  = None
    last_sh_val   = None
    last_sl_val   = None

    for i in range(len(df)):
        row = df.iloc[i]
        idx = df.index[i]

        if row["swing_high"]:
            last_sh_val = float(row["High"])
        if row["swing_low"]:
            last_sl_val = float(row["Low"])

        # Initialise confirmed levels from first available swings
        if confirmed_sh is None and last_sh_val is not None:
            confirmed_sh = last_sh_val
        if confirmed_sl is None and last_sl_val is not None:
            confirmed_sl = last_sl_val

        if confirmed_sh is None or confirmed_sl is None:
            continue

        close = float(row["Close"])

        if close > confirmed_sh:
            if trend == "down":
                df.at[idx, "choch_up"] = True
                events.append({"type": "CHOCH_UP",  "index": idx, "level": confirmed_sh})
            else:
                df.at[idx, "bos_up"]   = True
                events.append({"type": "BOS_UP",    "index": idx, "level": confirmed_sh})
            trend        = "up"
            confirmed_sh = last_sh_val if last_sh_val else confirmed_sh

        elif close < confirmed_sl:
            if trend == "up":
                df.at[idx, "choch_down"] = True
                events.append({"type": "CHOCH_DOWN", "index": idx, "level": confirmed_sl})
            else:
                df.at[idx, "bos_down"]   = True
                events.append({"type": "BOS_DOWN",   "index": idx, "level": confirmed_sl})
            trend        = "down"
            confirmed_sl = last_sl_val if last_sl_val else confirmed_sl

        # Keep confirmed levels updated in direction of trend
        if trend == "up" and last_sh_val:
            confirmed_sh = max(confirmed_sh, last_sh_val)
        elif trend == "down" and last_sl_val:
            confirmed_sl = min(confirmed_sl, last_sl_val)

    return df, events


def detect_order_blocks(df: pd.DataFrame, events: list, max_obs: int = 6) -> list:
    obs: list[dict] = []
    for ev in events:
        pos = df.index.get_loc(ev["index"])
        if ev["type"] in ("BOS_UP", "CHOCH_UP"):
            # Bullish OB: last bearish candle before the up-break
            for j in range(pos - 1, max(0, pos - 20), -1):
                c = df.iloc[j]
                if c["Close"] < c["Open"]:
                    obs.append({
                        "type":   "bullish",
                        "top":    float(c["Open"]),
                        "bottom": float(c["Close"]),
                        "index":  df.index[j],
                    })
                    break
        elif ev["type"] in ("BOS_DOWN", "CHOCH_DOWN"):
            # Bearish OB: last bullish candle before the down-break
            for j in range(pos - 1, max(0, pos - 20), -1):
                c = df.iloc[j]
                if c["Close"] > c["Open"]:
                    obs.append({
                        "type":   "bearish",
                        "top":    float(c["Close"]),
                        "bottom": float(c["Open"]),
                        "index":  df.index[j],
                    })
                    break
    return obs[-max_obs:]


def detect_fvg(df: pd.DataFrame, max_fvgs: int = 8) -> list:
    fvgs: list[dict] = []
    for i in range(1, len(df) - 1):
        prev  = df.iloc[i - 1]
        after = df.iloc[i + 1]
        idx   = df.index[i]
        # Bullish FVG: gap between prev high and next low
        if float(after["Low"]) > float(prev["High"]):
            fvgs.append({
                "type":   "bullish",
                "top":    float(after["Low"]),
                "bottom": float(prev["High"]),
                "index":  idx,
                "filled": False,
            })
        # Bearish FVG: gap between prev low and next high
        elif float(after["High"]) < float(prev["Low"]):
            fvgs.append({
                "type":   "bearish",
                "top":    float(prev["Low"]),
                "bottom": float(after["High"]),
                "index":  idx,
                "filled": False,
            })

    # Mark filled (price returned to zone)
    for fvg in fvgs:
        pos = df.index.get_loc(fvg["index"])
        for j in range(pos + 2, len(df)):
            lo, hi = float(df["Low"].iloc[j]), float(df["High"].iloc[j])
            if fvg["type"] == "bullish" and lo <= fvg["top"]:
                fvg["filled"] = True
                break
            elif fvg["type"] == "bearish" and hi >= fvg["bottom"]:
                fvg["filled"] = True
                break

    return [f for f in fvgs if not f["filled"]][-max_fvgs:]


def generate_signals(df: pd.DataFrame, obs: list, events: list) -> pd.DataFrame:
    df = df.copy()
    df["buy_signal"]  = False
    df["sell_signal"] = False
    if len(df) < 20:
        return df

    n   = len(df)
    b_obs = [o for o in obs if o["type"] == "bullish"]
    s_obs = [o for o in obs if o["type"] == "bearish"]

    # Collect recent event types (last 15 candles)
    recent = set()
    for ev in events:
        try:
            p = df.index.get_loc(ev["index"])
            if p >= n - 15:
                recent.add(ev["type"])
        except Exception:
            pass

    for i in range(20, n):
        row   = df.iloc[i]
        close = float(row["Close"])
        rsi   = float(row.get("RSI", 50) or 50)
        adx   = float(row.get("ADX", 0)  or 0)
        vwap  = float(row.get("VWAP", 0) or 0)

        rsi_ok = 35 <= rsi <= 68
        adx_ok = adx > 18

        # BUY: near bullish OB or swing low + bullish structure + above VWAP
        near_b_ob  = any(o["bottom"] * 0.995 <= close <= o["top"] * 1.005 for o in b_obs)
        bull_struct = bool(recent & {"BOS_UP", "CHOCH_UP"})
        above_vwap  = vwap > 0 and close > vwap
        if (near_b_ob or row.get("swing_low", False)) and bull_struct and above_vwap and rsi_ok and adx_ok:
            df.at[df.index[i], "buy_signal"] = True

        # SELL: near bearish OB or swing high + bearish structure + below VWAP
        near_s_ob   = any(o["bottom"] * 0.995 <= close <= o["top"] * 1.005 for o in s_obs)
        bear_struct = bool(recent & {"BOS_DOWN", "CHOCH_DOWN"})
        below_vwap  = vwap > 0 and close < vwap
        if (near_s_ob or row.get("swing_high", False)) and bear_struct and below_vwap and rsi_ok and adx_ok:
            df.at[df.index[i], "sell_signal"] = True

    return df


# ── Summary ───────────────────────────────────────────────────────────────────

def build_summary(df: pd.DataFrame, events: list) -> dict:
    last   = df.iloc[-1]
    close  = float(last["Close"])
    ema20  = float(last.get("EMA20",  0) or 0)
    ema50  = float(last.get("EMA50",  0) or 0)
    ema200 = float(last.get("EMA200", 0) or 0)
    rsi    = round(float(last.get("RSI", 0) or 0), 1)
    adx    = round(float(last.get("ADX", 0) or 0), 1)
    _vwap_raw = last.get("VWAP", 0)
    vwap      = 0 if (not _vwap_raw or (isinstance(_vwap_raw, float) and np.isnan(_vwap_raw))) else round(float(_vwap_raw), 1)

    if ema20 > ema50 > ema200 and close > ema20:
        trend = "Bullish"
    elif ema20 < ema50 < ema200 and close < ema20:
        trend = "Bearish"
    elif close > ema50:
        trend = "Mildly Bullish"
    elif close < ema50:
        trend = "Mildly Bearish"
    else:
        trend = "Sideways"

    structure = events[-1]["type"].replace("_", " ") if events else "—"

    if "Bullish" in trend:
        bias = "BUY"
    elif "Bearish" in trend:
        bias = "SELL"
    else:
        bias = "NEUTRAL"

    sl_vals = df["Low"][df["swing_low"]].values
    sh_vals = df["High"][df["swing_high"]].values
    support    = round(float(sl_vals[-1]), 1) if len(sl_vals) else 0
    resistance = round(float(sh_vals[-1]), 1) if len(sh_vals) else 0

    return {
        "trend": trend, "structure": structure, "bias": bias,
        "rsi": rsi, "adx": adx, "vwap": vwap,
        "support": support, "resistance": resistance,
        "close": round(close, 1),
    }


# ── Trade Verdict ─────────────────────────────────────────────────────────────

def generate_verdict(df: pd.DataFrame, events: list, obs: list,
                     fvgs: list, summary: dict, cpr: dict | None = None) -> dict:
    last   = df.iloc[-1]
    close  = float(last["Close"])
    ema20  = float(last.get("EMA20",  0) or 0)
    ema50  = float(last.get("EMA50",  0) or 0)
    ema200 = float(last.get("EMA200", 0) or 0)
    rsi    = float(last.get("RSI",   50) or 50)
    adx    = float(last.get("ADX",    0) or 0)
    di_pos = float(last.get("DI_pos", 0) or 0)
    di_neg = float(last.get("DI_neg", 0) or 0)
    vwap   = float(last.get("VWAP",   0) or 0)
    if np.isnan(vwap): vwap = 0

    score   = 0
    reasons = []

    # Recent structure (last 3 events carry the most weight)
    recent_types = [e["type"] for e in events[-8:]] if events else []
    last3_types  = recent_types[-3:] if recent_types else []
    has_bull = any(t in ("BOS_UP",   "CHOCH_UP")   for t in last3_types)
    has_bear = any(t in ("BOS_DOWN", "CHOCH_DOWN")  for t in last3_types)
    last_ev  = recent_types[-1] if recent_types else ""

    if has_bull and not has_bear:
        score += 2
        reasons.append({"text": f"Bullish structure — {last_ev.replace('_',' ')}", "ok": True})
    elif has_bear and not has_bull:
        score -= 2
        reasons.append({"text": f"Bearish structure — {last_ev.replace('_',' ')}", "ok": False})
    else:
        reasons.append({"text": "Mixed / unclear structure", "ok": None})

    # EMA alignment
    if ema20 > ema50 > ema200 and close > ema20:
        score += 2
        reasons.append({"text": "EMA aligned bullish (20 > 50 > 200, price above all)", "ok": True})
    elif ema20 < ema50 < ema200 and close < ema20:
        score -= 2
        reasons.append({"text": "EMA aligned bearish (20 < 50 < 200, price below all)", "ok": False})
    elif ema20 > ema50 and close > ema50:
        score += 1
        reasons.append({"text": "EMA20 > EMA50, price above EMA50", "ok": True})
    elif ema20 < ema50 and close < ema50:
        score -= 1
        reasons.append({"text": "EMA20 < EMA50, price below EMA50", "ok": False})
    else:
        reasons.append({"text": "EMA alignment mixed", "ok": None})

    # VWAP
    if vwap > 0:
        if close > vwap * 1.001:
            score += 1
            reasons.append({"text": f"Above VWAP ({vwap:,.0f}) — bulls in control", "ok": True})
        elif close < vwap * 0.999:
            score -= 1
            reasons.append({"text": f"Below VWAP ({vwap:,.0f}) — bears in control", "ok": False})
        else:
            reasons.append({"text": "At VWAP — neutral zone", "ok": None})

    # RSI
    if np.isnan(rsi): rsi = 50
    if rsi > 70:
        score -= 1
        reasons.append({"text": f"RSI {rsi:.0f} — overbought, potential fall", "ok": False})
    elif rsi < 30:
        score += 1
        reasons.append({"text": f"RSI {rsi:.0f} — oversold, watch for bounce", "ok": None})
    elif 35 <= rsi <= 60:
        score += 1
        reasons.append({"text": f"RSI {rsi:.0f} — healthy zone, room to move", "ok": True})
    else:
        reasons.append({"text": f"RSI {rsi:.0f} — neutral", "ok": None})

    # ADX + DI
    if np.isnan(adx): adx = 0
    if adx > 25:
        if di_pos > di_neg:
            score += 1
            reasons.append({"text": f"ADX {adx:.0f} — strong uptrend (DI+ dominant)", "ok": True})
        else:
            score -= 1
            reasons.append({"text": f"ADX {adx:.0f} — strong downtrend (DI− dominant)", "ok": False})
    elif adx < 20:
        reasons.append({"text": f"ADX {adx:.0f} — choppy / no trend", "ok": None})
    else:
        reasons.append({"text": f"ADX {adx:.0f} — trend forming", "ok": None})

    # Order Block proximity
    b_obs = [o for o in obs if o["type"] == "bullish"]
    s_obs = [o for o in obs if o["type"] == "bearish"]
    near_bull_ob = any(o["bottom"] * 0.997 <= close <= o["top"] * 1.005 for o in b_obs)
    near_bear_ob = any(o["bottom"] * 0.995 <= close <= o["top"] * 1.003 for o in s_obs)
    if near_bull_ob:
        score += 1
        reasons.append({"text": "Price at Bullish Order Block — institutional buy zone", "ok": True})
    if near_bear_ob:
        score -= 1
        reasons.append({"text": "Price at Bearish Order Block — institutional sell zone", "ok": False})

    # CPR position
    if cpr:
        _cp = cpr.get("price_position", "")
        _tc = cpr.get("tc", 0)
        _bc = cpr.get("bc", 0)
        if _cp == "above":
            score += 1
            reasons.append({"text": f"Above CPR ({_bc:,.0f}–{_tc:,.0f}) — bullish day bias", "ok": True})
        elif _cp == "below":
            score -= 1
            reasons.append({"text": f"Below CPR ({_bc:,.0f}–{_tc:,.0f}) — bearish day bias", "ok": False})
        else:
            reasons.append({"text": f"Inside CPR ({_bc:,.0f}–{_tc:,.0f}) — choppy/indecision", "ok": None})

    # Verdict
    if score >= 5:
        verdict, v_col = "STRONG BUY",  "#00d4aa"
    elif score >= 3:
        verdict, v_col = "BUY",         "#3fb950"
    elif score <= -5:
        verdict, v_col = "STRONG SELL", "#f85149"
    elif score <= -3:
        verdict, v_col = "SELL",        "#f0883e"
    else:
        verdict, v_col = "WAIT",        "#8b949e"

    confidence = min(100, max(0, int((abs(score) / 7) * 100)))

    # Entry / SL / Targets
    sl_vals = df["Low"][df["swing_low"]].values
    sh_vals = df["High"][df["swing_high"]].values
    entry_zone = stop_loss = rr = None
    targets: list[float] = []

    if verdict in ("BUY", "STRONG BUY"):
        if b_obs:
            ob = b_obs[-1]
            entry_zone = (round(ob["bottom"], 1), round(ob["top"], 1))
            stop_loss  = round(ob["bottom"] * 0.998, 1)
        elif len(sl_vals):
            stop_loss  = round(float(sl_vals[-1]), 1)
            entry_zone = (round(close * 0.9995, 1), round(close * 1.0005, 1))
        else:
            stop_loss  = round(close * 0.995, 1)
            entry_zone = (round(close, 1), round(close, 1))
        t1 = round(float(sh_vals[-1]), 1) if len(sh_vals) else round(close * 1.005, 1)
        t2 = round(float(max(sh_vals[-2:]) if len(sh_vals) >= 2 else t1 * 1.005), 1)
        targets = [t1, t2]
        if stop_loss and entry_zone:
            risk   = entry_zone[1] - stop_loss
            reward = t1 - entry_zone[1]
            rr = round(reward / risk, 1) if risk > 0 else None

    elif verdict in ("SELL", "STRONG SELL"):
        if s_obs:
            ob = s_obs[-1]
            entry_zone = (round(ob["bottom"], 1), round(ob["top"], 1))
            stop_loss  = round(ob["top"] * 1.002, 1)
        elif len(sh_vals):
            stop_loss  = round(float(sh_vals[-1]), 1)
            entry_zone = (round(close * 0.9995, 1), round(close * 1.0005, 1))
        else:
            stop_loss  = round(close * 1.005, 1)
            entry_zone = (round(close, 1), round(close, 1))
        t1 = round(float(sl_vals[-1]), 1) if len(sl_vals) else round(close * 0.995, 1)
        t2 = round(float(min(sl_vals[-2:]) if len(sl_vals) >= 2 else t1 * 0.995), 1)
        targets = [t1, t2]
        if stop_loss and entry_zone:
            risk   = stop_loss - entry_zone[0]
            reward = entry_zone[0] - t1
            rr = round(reward / risk, 1) if risk > 0 else None

    return {
        "verdict": verdict, "color": v_col, "score": score,
        "confidence": confidence, "reasons": reasons,
        "entry_zone": entry_zone, "stop_loss": stop_loss,
        "targets": targets, "rr": rr,
    }


# ── CPR (Central Pivot Range) ─────────────────────────────────────────────────

def compute_cpr(df: pd.DataFrame, interval_key: str) -> dict:
    """
    Compute CPR + standard pivots for the current period.
    Intraday (1m-1H): previous day's H/L/C → today's CPR.
    Daily (1D):       previous week's H/L/C → this week's CPR.
    """
    intraday = interval_key != "1D"

    try:
        if intraday:
            unique_dates = sorted(set(df.index.date))
            if len(unique_dates) < 2:
                return {}
            prev_date = unique_dates[-2]
            _dates = pd.array(df.index.date)
            prev = df[_dates == prev_date]
            if prev.empty:
                return {}
            h = float(prev["High"].max())
            l = float(prev["Low"].min())
            c = float(prev["Close"].iloc[-1])
            period_lbl = "Daily CPR (prev day)"
        else:
            # Group by ISO year-week
            _wk = df.index.to_series().apply(lambda x: (x.isocalendar()[0], x.isocalendar()[1]))
            unique_weeks = sorted(_wk.unique())
            if len(unique_weeks) < 2:
                return {}
            prev_week = unique_weeks[-2]
            prev = df[_wk == prev_week]
            if prev.empty:
                return {}
            h = float(prev["High"].max())
            l = float(prev["Low"].min())
            c = float(prev["Close"].iloc[-1])
            period_lbl = "Weekly CPR (prev week)"
    except Exception:
        return {}

    pivot = (h + l + c) / 3
    bc    = (h + l) / 2
    tc    = (pivot - bc) + pivot
    if tc < bc:
        tc, bc = bc, tc       # safety swap

    r1 = 2 * pivot - l
    r2 = pivot + (h - l)
    r3 = h + 2 * (pivot - l)
    s1 = 2 * pivot - h
    s2 = pivot - (h - l)
    s3 = l - 2 * (h - pivot)

    width_pct = (tc - bc) / pivot * 100 if pivot > 0 else 0
    # Narrow CPR (< 0.25%) signals a trending day; wide CPR signals range/chop
    is_narrow = width_pct < 0.25

    close = float(df["Close"].iloc[-1])
    if close > tc:
        price_pos, pos_col, pos_lbl = "above", "#00d4aa", "Above CPR → Bullish bias"
    elif close < bc:
        price_pos, pos_col, pos_lbl = "below", "#f85149", "Below CPR → Bearish bias"
    else:
        price_pos, pos_col, pos_lbl = "inside", "#8b949e", "Inside CPR → Choppy zone"

    width_lbl = "Narrow → Trending day expected" if is_narrow else "Wide → Choppy day expected"

    return {
        "pivot": round(pivot, 1), "tc": round(tc, 1), "bc": round(bc, 1),
        "r1": round(r1, 1), "r2": round(r2, 1), "r3": round(r3, 1),
        "s1": round(s1, 1), "s2": round(s2, 1), "s3": round(s3, 1),
        "width_pct": round(width_pct, 3), "is_narrow": is_narrow,
        "price_position": price_pos, "pos_color": pos_col,
        "pos_label": pos_lbl, "width_label": width_lbl,
        "period_label": period_lbl,
    }


# ── Main entry point ──────────────────────────────────────────────────────────

def analyze_chart(symbol: str, interval: str,
                  api_key: str, access_token: str) -> dict:
    try:
        token, display_name = resolve_token(symbol, api_key, access_token)
    except ValueError as e:
        return {"error": str(e)}

    intraday = interval != "1D"

    try:
        df = fetch_candles(token, interval, api_key, access_token)
    except RuntimeError as e:
        return {"error": str(e)}

    if df.empty or len(df) < 30:
        return {"error": "Not enough data — try a longer timeframe or check the symbol"}

    df       = compute_indicators(df, intraday=intraday)
    df       = detect_swings(df, n=3)
    df, evts = detect_structure(df)
    obs      = detect_order_blocks(df, evts)
    fvgs     = detect_fvg(df)
    df       = generate_signals(df, obs, evts)
    summary  = build_summary(df, evts)
    cpr      = compute_cpr(df, interval)
    verdict  = generate_verdict(df, evts, obs, fvgs, summary, cpr=cpr)

    return {
        "df": df, "events": evts, "obs": obs, "fvgs": fvgs,
        "summary": summary, "verdict": verdict, "cpr": cpr,
        "symbol": display_name, "interval": interval,
        "error": None,
    }
