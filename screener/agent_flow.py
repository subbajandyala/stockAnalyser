"""
Agent Flow — Institutional Option Buying AI.

Analyzes live market data (OHLCV + option chain via Kite) and returns
a high-probability CALL / PUT buying recommendation, or NO TRADE.
"""

import datetime
import io
from typing import Optional

import numpy as np
import pandas as pd
import requests
from screener.kite_data import _KITE_BASE, _hdrs, fetch_ohlcv as _kite_ohlcv
from screener.option_chain import (
    fetch_option_chain, get_expiries, parse_chain,
    atm_strike, calc_pcr, calc_max_pain,
)

_IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

INSTRUMENTS = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"]

_FUT_EXCHANGE = {
    "NIFTY": "NFO", "BANKNIFTY": "NFO", "FINNIFTY": "NFO",
    "MIDCPNIFTY": "NFO", "SENSEX": "BFO",
}
_FUT_INSTR_CACHE: dict = {}
_FUT_INSTR_TTL = 3600


# ── Data helpers ──────────────────────────────────────────────────────────────

def _fetch_ohlcv(symbol: str) -> pd.DataFrame:
    df = _kite_ohlcv(symbol, interval="5minute", days=2)
    if df.empty:
        return df
    today = datetime.date.today()
    mask = df.index.date == today
    df_today = df[mask].copy()
    return df_today if len(df_today) >= 10 else df.tail(60).copy()


def _fetch_vix() -> float:
    try:
        df = _kite_ohlcv("INDIAVIX", interval="day", days=5)
        if not df.empty:
            return round(float(df["Close"].dropna().iloc[-1]), 2)
    except Exception:
        pass
    return 14.5


def _ema(series: pd.Series, n: int) -> pd.Series:
    return series.ewm(span=n, adjust=False).mean()


def _vwap(df: pd.DataFrame) -> float:
    if df.empty or "Volume" not in df.columns or df["Volume"].sum() == 0:
        return 0.0
    tp = (df["High"] + df["Low"] + df["Close"]) / 3
    return float((tp * df["Volume"]).sum() / df["Volume"].sum())


# ── Futures instruments + 5m candles ─────────────────────────────────────────

def _load_fut_instruments(exchange: str) -> pd.DataFrame:
    now = datetime.datetime.now(_IST).timestamp()
    cached = _FUT_INSTR_CACHE.get(exchange, {})
    if cached.get("ts", 0) + _FUT_INSTR_TTL > now:
        return cached["df"]
    try:
        r = requests.get(
            f"{_KITE_BASE}/instruments/{exchange}",
            headers=_hdrs(),
            timeout=30,
        )
        if not r.ok:
            return cached.get("df", pd.DataFrame())
        df = pd.read_csv(io.StringIO(r.text))
        _FUT_INSTR_CACHE[exchange] = {"df": df, "ts": now}
        return df
    except Exception:
        return cached.get("df", pd.DataFrame())


def _fetch_fut_5m(token: int) -> pd.DataFrame:
    to_d = datetime.date.today()
    fr_d = to_d - datetime.timedelta(days=3)
    try:
        r = requests.get(
            f"{_KITE_BASE}/instruments/historical/{token}/5minute",
            headers=_hdrs(),
            params={
                "from": fr_d.strftime("%Y-%m-%d %H:%M:%S"),
                "to":   to_d.strftime("%Y-%m-%d %H:%M:%S"),
                "oi":   "1",
            },
            timeout=30,
        )
        if not r.ok:
            return pd.DataFrame()
        candles = r.json().get("data", {}).get("candles", [])
        if not candles:
            return pd.DataFrame()
    except Exception:
        return pd.DataFrame()

    rows = [c[:7] if len(c) >= 7 else c[:6] + [0] for c in candles]
    df = pd.DataFrame(rows, columns=["datetime", "Open", "High", "Low", "Close", "Volume", "OI"])
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.set_index("datetime").sort_index()
    for col in ["Open", "High", "Low", "Close", "Volume", "OI"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["Close"])


def _futures_buildup(symbol: str) -> dict:
    """
    4-quadrant futures OI analysis:
      Price UP  + OI UP   → Long Buildup   (fresh longs — strong bullish)
      Price UP  + OI DOWN → Short Covering (shorts exiting — weak bullish)
      Price DOWN + OI UP  → Short Buildup  (fresh shorts — strong bearish)
      Price DOWN + OI DOWN→ Long Unwinding (longs exiting — weak bearish)
    """
    exchange = _FUT_EXCHANGE.get(symbol, "NFO")
    df_instr = _load_fut_instruments(exchange)
    if df_instr.empty:
        return {"signal": "Unknown"}

    try:
        df_instr["expiry_dt"] = pd.to_datetime(df_instr["expiry"]).dt.date
        today = datetime.date.today()
        futs = df_instr[
            (df_instr["name"].str.strip().str.upper() == symbol.upper()) &
            (df_instr["instrument_type"] == "FUT") &
            (df_instr["expiry_dt"] >= today)
        ]
        if futs.empty:
            return {"signal": "Unknown"}
        near = futs.sort_values("expiry_dt").iloc[0]
        token = int(near["instrument_token"])
    except Exception:
        return {"signal": "Unknown"}

    df = _fetch_fut_5m(token)
    if df is None or len(df) < 3:
        return {"signal": "Unknown"}

    curr_oi   = float(df["OI"].iloc[-1])
    prev_oi   = float(df["OI"].iloc[-2])
    curr_close = float(df["Close"].iloc[-1])
    prev_close = float(df["Close"].iloc[-2])

    # If OI data absent, fall back to price-only signals
    if curr_oi == 0 and prev_oi == 0:
        return {"signal": "Unknown", "fut_price": round(curr_close, 2)}

    price_up = curr_close > prev_close
    oi_up    = curr_oi    > prev_oi

    if price_up and oi_up:
        signal = "Long Buildup"
    elif price_up and not oi_up:
        signal = "Short Covering"
    elif not price_up and oi_up:
        signal = "Short Buildup"
    else:
        signal = "Long Unwinding"

    return {
        "signal":     signal,
        "fut_price":  round(curr_close, 2),
        "price_chg":  round(curr_close - prev_close, 2),
        "oi_chg":     int(curr_oi - prev_oi),
    }


# ── Market structure ──────────────────────────────────────────────────────────

def _market_structure(df: pd.DataFrame) -> str:
    """Return UPTREND / DOWNTREND / SIDEWAYS based on recent pivot highs/lows."""
    if len(df) < 12:
        return "SIDEWAYS"
    highs = df["High"].values
    lows  = df["Low"].values
    ph, pl = [], []
    w = 3
    for i in range(w, len(highs) - w):
        if highs[i] == max(highs[i - w: i + w + 1]):
            ph.append(highs[i])
        if lows[i] == min(lows[i - w: i + w + 1]):
            pl.append(lows[i])
    if len(ph) >= 2 and len(pl) >= 2:
        if ph[-1] > ph[-2] and pl[-1] > pl[-2]:
            return "UPTREND"
        if ph[-1] < ph[-2] and pl[-1] < pl[-2]:
            return "DOWNTREND"
    return "SIDEWAYS"


# ── OI analysis ───────────────────────────────────────────────────────────────

def _oi_analysis(df_chain: pd.DataFrame, spot: float) -> dict:
    if df_chain.empty:
        return {"signals": [], "atm": spot, "pcr": 1.0, "max_pain": spot,
                "resistance": spot, "support": spot}

    atm  = atm_strike(df_chain, spot)
    pcr  = calc_pcr(df_chain)
    mp   = calc_max_pain(df_chain)

    ce_wall = float(df_chain.loc[df_chain["CE OI"].idxmax(), "Strike"])
    pe_wall = float(df_chain.loc[df_chain["PE OI"].idxmax(), "Strike"])

    above = df_chain[df_chain["Strike"] > spot]
    below = df_chain[df_chain["Strike"] < spot]

    fresh_ce_write = not above[above["CE Chng OI"] > 0].empty
    fresh_pe_write = not below[below["PE Chng OI"] > 0].empty
    ce_unwind      = not above[above["CE Chng OI"] < 0].empty
    pe_unwind      = not below[below["PE Chng OI"] < 0].empty

    signals = []
    if fresh_pe_write:  signals.append("Fresh Put Writing")
    if fresh_ce_write:  signals.append("Fresh Call Writing")
    if ce_unwind:       signals.append("Call Unwinding")
    if pe_unwind:       signals.append("Put Unwinding")
    if pcr > 1.3:       signals.append("High PCR — Bullish Bias")
    elif pcr < 0.8:     signals.append("Low PCR — Bearish Bias")

    # Volume spikes
    avg_ce_vol = df_chain["CE Vol"].mean()
    avg_pe_vol = df_chain["PE Vol"].mean()
    if df_chain["CE Vol"].max() > avg_ce_vol * 3:
        signals.append("CE Volume Spike")
    if df_chain["PE Vol"].max() > avg_pe_vol * 3:
        signals.append("PE Volume Spike")

    return {
        "atm":          atm,
        "pcr":          pcr,
        "max_pain":     mp,
        "resistance":   ce_wall,
        "support":      pe_wall,
        "fresh_ce":     fresh_ce_write,
        "fresh_pe":     fresh_pe_write,
        "ce_unwind":    ce_unwind,
        "pe_unwind":    pe_unwind,
        "signals":      signals,
        "top_ce":       df_chain.nlargest(5, "CE OI")[["Strike", "CE OI", "CE Chng OI", "CE Vol"]].reset_index(drop=True),
        "top_pe":       df_chain.nlargest(5, "PE OI")[["Strike", "PE OI", "PE Chng OI", "PE Vol"]].reset_index(drop=True),
    }


# ── Bias engine ───────────────────────────────────────────────────────────────

def _market_bias(trend: str, oi: dict, spot: float,
                 vwap: float, ema20: float, ema50: float, vix: float,
                 fut_buildup: str = "Unknown") -> dict:
    bull, bear = 0, 0

    # Trend weight 25
    if trend == "UPTREND":    bull += 25
    elif trend == "DOWNTREND": bear += 25

    # EMA alignment weight 20
    if spot > ema20 > ema50:  bull += 20
    elif spot < ema20 < ema50: bear += 20
    elif spot > ema20:         bull += 10
    elif spot < ema20:         bear += 10

    # VWAP weight 15
    if vwap > 0:
        if spot > vwap: bull += 15
        else:           bear += 15

    # OI weight 25
    if oi.get("fresh_pe") and not oi.get("pe_unwind"):  bull += 15
    if oi.get("fresh_ce") and not oi.get("ce_unwind"):  bear += 15
    if oi.get("ce_unwind"):  bull += 10
    if oi.get("pe_unwind"):  bear += 10
    pcr = oi.get("pcr", 1.0)
    if pcr > 1.2:    bull += 10
    elif pcr < 0.8:  bear += 10

    # Futures OI buildup weight 15 (strong) / 7 (weak)
    if fut_buildup == "Long Buildup":    bull += 15
    elif fut_buildup == "Short Covering": bull += 7
    elif fut_buildup == "Short Buildup":  bear += 15
    elif fut_buildup == "Long Unwinding": bear += 7

    # VIX penalty (uncertain market)
    if vix > 20:
        bull = int(bull * 0.85)
        bear = int(bear * 0.85)

    total = bull + bear
    if total == 0:
        return {"bias": "Neutral", "confidence": 40, "bull": 0, "bear": 0}

    conf = round(max(bull, bear) / total * 100)
    net  = bull - bear
    if net >= 45:   bias = "Strong Bullish"
    elif net >= 20: bias = "Moderately Bullish"
    elif net <= -45: bias = "Strong Bearish"
    elif net <= -20: bias = "Moderately Bearish"
    else:           bias = "Neutral"

    return {"bias": bias, "confidence": min(conf, 95), "bull": bull, "bear": bear}


# ── Strike & targets ──────────────────────────────────────────────────────────

def _best_strike(df_chain: pd.DataFrame, spot: float, direction: str) -> dict:
    """Return ATM or slight-OTM strike with best liquidity."""
    atm = atm_strike(df_chain, spot)
    step = df_chain["Strike"].diff().abs().median() or 50

    if direction == "CALL":
        candidates = df_chain[
            (df_chain["Strike"] >= atm - step * 0.5) &
            (df_chain["CE Vol"] > 0)
        ].sort_values("Strike").head(4)
        if candidates.empty:
            candidates = df_chain[df_chain["Strike"] >= atm].sort_values("Strike").head(1)
        row = candidates.sort_values("CE Vol", ascending=False).iloc[0] if not candidates.empty else None
        if row is None:
            return {}
        return {"strike": float(row["Strike"]), "ltp": float(row["CE LTP"]),
                "vol": float(row["CE Vol"]), "iv": float(row.get("CE IV", 0))}
    else:
        candidates = df_chain[
            (df_chain["Strike"] <= atm + step * 0.5) &
            (df_chain["PE Vol"] > 0)
        ].sort_values("Strike", ascending=False).head(4)
        if candidates.empty:
            candidates = df_chain[df_chain["Strike"] <= atm].sort_values("Strike", ascending=False).head(1)
        row = candidates.sort_values("PE Vol", ascending=False).iloc[0] if not candidates.empty else None
        if row is None:
            return {}
        return {"strike": float(row["Strike"]), "ltp": float(row["PE LTP"]),
                "vol": float(row["PE Vol"]), "iv": float(row.get("PE IV", 0))}


def _targets(ltp: float) -> dict:
    if ltp <= 0:
        return {}
    sl  = round(ltp * 0.68, 1)
    t1  = round(ltp * 1.40, 1)
    t2  = round(ltp * 1.85, 1)
    t3  = round(ltp * 2.50, 1)
    risk = ltp - sl
    rr = round((t2 - ltp) / risk, 1) if risk > 0 else 0
    return {"sl": sl, "t1": t1, "t2": t2, "t3": t3, "rr": rr,
            "entry_min": round(ltp * 0.98, 1), "entry_max": round(ltp * 1.03, 1)}


# ── Trade checklist ───────────────────────────────────────────────────────────

def _checklist(trend: str, oi: dict, vol: float, ltp: float, vix: float, rr: float) -> dict:
    return {
        "Trend Confirmed":   trend in ("UPTREND", "DOWNTREND"),
        "OI Supports Move":  len(oi.get("signals", [])) >= 1,
        "Volume OK":         vol > 200,
        "Premium Expanding": ltp > 1.0,
        "Liquidity Good":    vol > 500,
        "RR ≥ 1:2":          rr >= 2.0,
        "VIX Manageable":    vix < 22,
    }


def _rating(confidence: int, n_pass: int) -> int:
    if confidence >= 82 and n_pass == 7: return 5
    if confidence >= 72 and n_pass >= 6: return 4
    if confidence >= 62 and n_pass >= 5: return 3
    if confidence >= 52:                 return 2
    return 1


def _risk_assessment(vix: float, vol: float, expiry: str, iv: float) -> dict:
    """Return risk/theta/liquidity/bid-ask labels for the trade card."""
    # Risk level: driven by VIX
    if vix > 20:   risk = "High"
    elif vix > 15: risk = "Moderate"
    else:          risk = "Low"

    # Theta risk: days to nearest expiry
    try:
        exp_dt = datetime.datetime.strptime(expiry, "%d-%b-%Y").date()
        dte = (exp_dt - datetime.date.today()).days
    except Exception:
        dte = 30
    if dte == 0:   theta = "Very High"
    elif dte <= 2: theta = "High"
    elif dte <= 5: theta = "Moderate"
    else:          theta = "Low"

    # Liquidity: option volume
    if vol > 5000:   liquidity = "Excellent"
    elif vol > 1000: liquidity = "Good"
    elif vol > 200:  liquidity = "Fair"
    else:            liquidity = "Poor"

    # Bid-Ask spread proxy (we don't have direct bid/ask; use volume as proxy)
    if vol > 3000:   bid_ask = "Excellent"
    elif vol > 800:  bid_ask = "Good"
    else:            bid_ask = "Moderate"

    return {"risk": risk, "theta_risk": theta, "liquidity": liquidity, "bid_ask": bid_ask}


# ── Public entry point ────────────────────────────────────────────────────────

def run_agent_analysis(symbol: str, api_key: str, access_token: str) -> dict:
    """
    Full pipeline. Returns dict with all fields for the Agent Flow UI card.
    Keys always present: symbol, error, no_trade, bias, confidence.
    """
    base = {"symbol": symbol, "error": "", "no_trade": False, "bias": "Neutral", "confidence": 0}

    # 1. OHLCV + technicals
    df = _fetch_ohlcv(symbol)
    if df.empty:
        return {**base, "error": f"No intraday OHLCV for {symbol}. Market may be closed."}

    spot   = float(df["Close"].iloc[-1])
    vwap_v = round(_vwap(df), 2)
    ema20  = round(float(_ema(df["Close"], 20).iloc[-1]), 2)
    ema50  = round(float(_ema(df["Close"], 50).iloc[-1]), 2)
    vix    = _fetch_vix()
    trend  = _market_structure(df)

    # 2. Option chain
    try:
        raw = fetch_option_chain(symbol, api_key=api_key, access_token=access_token)
    except Exception as exc:
        return {**base,
                "spot": spot, "vwap": vwap_v, "ema20": ema20, "ema50": ema50,
                "vix": vix, "trend": trend,
                "error": f"Option chain fetch failed: {exc}"}

    expiries = get_expiries(raw)
    if not expiries:
        return {**base, "error": "No expiries in option chain."}

    expiry = expiries[0]
    df_chain, chain_spot = parse_chain(raw, expiry)
    if chain_spot > 0:
        spot = chain_spot

    # 3. OI analysis
    oi = _oi_analysis(df_chain, spot)

    # 3b. Futures OI buildup (4-quadrant)
    fut_data   = _futures_buildup(symbol)
    fut_signal = fut_data.get("signal", "Unknown")
    if fut_signal not in ("Unknown",):
        oi["signals"].append(fut_signal)

    # 4. Bias (includes futures buildup weight)
    bias_r = _market_bias(trend, oi, spot, vwap_v, ema20, ema50, vix, fut_signal)
    bias, conf = bias_r["bias"], bias_r["confidence"]

    common = {**base,
              "bias": bias, "confidence": conf,
              "spot": spot, "vwap": vwap_v, "ema20": ema20, "ema50": ema50,
              "vix": vix, "trend": trend, "expiry": expiry,
              "oi": oi, "df_chain": df_chain,
              "support": oi["support"], "resistance": oi["resistance"],
              "fut_buildup": fut_data}

    # 5. Direction
    if bias in ("Strong Bullish", "Moderately Bullish"):
        direction = "CALL"
    elif bias in ("Strong Bearish", "Moderately Bearish"):
        direction = "PUT"
    else:
        return {**common, "no_trade": True}

    # 6. Strike
    sk = _best_strike(df_chain, spot, direction)
    if not sk or sk["ltp"] < 1.0:
        return {**common, "no_trade": True,
                "error": "No liquid ATM strike found — market may be illiquid."}

    ltp = sk["ltp"]

    # 7. Targets
    tgt = _targets(ltp)
    if not tgt:
        return {**common, "no_trade": True}

    # 8. Checklist
    checks = _checklist(trend, oi, sk["vol"], ltp, vix, tgt["rr"])
    all_pass = all(checks.values())
    n_pass   = sum(checks.values())

    if not all_pass:
        return {**common, "no_trade": True, "checklist": checks,
                "direction": direction, "strike": sk["strike"], "ltp": ltp}

    # 9. Assemble trade
    prob   = min(conf - 4, 91)
    rating = _rating(conf, n_pass)
    risk_a = _risk_assessment(vix, sk["vol"], expiry, sk["iv"])

    return {**common,
            "no_trade":   False,
            "direction":  direction,
            "strike":     sk["strike"],
            "ltp":        ltp,
            "iv":         sk["iv"],
            "vol":        sk["vol"],
            "targets":    tgt,
            "checklist":  checks,
            "rating":     rating,
            "probability": prob,
            "signals":    oi["signals"],
            **risk_a}
