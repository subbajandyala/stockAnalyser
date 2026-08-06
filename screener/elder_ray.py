"""
Elder Ray Index Options Trading System
Based on Dr. Alexander Elder's Elder Ray indicator.

  Bull Power = High − EMA(13)   positive → bulls in control
  Bear Power = Low  − EMA(13)   negative → bears in control

Multi-timeframe signal engine (15m trend → 5m timing → 1m trigger):
  STRONG BUY CE : strong bullish confluence, score_ce ≥ 7, leads by 3+
  BUY CE        : moderate bullish setup, score_ce ≥ 5, leads by 1+
  STRONG BUY PE : strong bearish confluence
  BUY PE        : moderate bearish setup
  WATCH         : one side building, wait for confirmation
  WAIT          : no clear setup or market closed

Advanced features:
  - Divergence detection (price vs power)
  - VWAP bias filter
  - RSI zone check
  - EMA slope velocity scoring
  - Signal history tracking
"""

import datetime
from io import StringIO
from typing import Optional

import numpy as np
import pandas as pd
import requests
import yfinance as yf

_KITE_BASE = "https://api.kite.trade"
_IST       = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

INDEX_CONFIG: dict[str, dict] = {
    "NIFTY": {
        "yf":          "^NSEI",
        "kite_spot":   "NSE:NIFTY 50",
        "exch_opt":    "NFO",
        "strike_step": 50,
        "lot":         25,
        "color":       "#00d4aa",
    },
    "BANKNIFTY": {
        "yf":          "^NSEBANK",
        "kite_spot":   "NSE:NIFTY BANK",
        "exch_opt":    "NFO",
        "strike_step": 100,
        "lot":         15,
        "color":       "#79c0ff",
    },
    "SENSEX": {
        "yf":          "^BSESN",
        "kite_spot":   "BSE:SENSEX",
        "exch_opt":    "BFO",
        "strike_step": 100,
        "lot":         10,
        "color":       "#ffa657",
    },
}


# ── Indicators ────────────────────────────────────────────────────────────────

def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def _rsi(s: pd.Series, n: int = 14) -> pd.Series:
    d = s.diff()
    g = d.clip(lower=0).rolling(n).mean()
    l = (-d.clip(upper=0)).rolling(n).mean()
    return 100 - 100 / (1 + g / l.replace(0, 1e-9))


def _vwap(df: pd.DataFrame) -> pd.Series:
    """Intraday VWAP reset per calendar day."""
    tp  = (df["High"] + df["Low"] + df["Close"]) / 3
    vol = df["Volume"].replace(0, 1)
    dates = pd.Series(df.index.date, index=df.index)
    cum_tv  = (tp * vol).groupby(dates).cumsum()
    cum_vol = vol.groupby(dates).cumsum()
    return cum_tv / cum_vol


def _atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - df["Close"].shift()).abs(),
        (df["Low"]  - df["Close"].shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(span=n, adjust=False).mean()


def _ema_slope(series: pd.Series, bars: int = 3) -> float:
    s = series.dropna()
    if len(s) < bars + 1:
        return 0.0
    return float((s.iloc[-1] - s.iloc[-(bars + 1)]) / bars)


def compute_elder_ray(df: pd.DataFrame, period: int = 13) -> pd.DataFrame:
    """
    Enrich OHLCV DataFrame with Elder Ray columns + supporting indicators.
    Adds: EMA, Bull_Power, Bear_Power, RSI, VWAP, ATR,
          BP_Cross_Up, BP_Cross_Dn, BeP_Cross_Up, BeP_Cross_Dn.
    """
    df = df.copy()
    df["EMA"]        = _ema(df["Close"], period)
    df["Bull_Power"] = df["High"]  - df["EMA"]
    df["Bear_Power"] = df["Low"]   - df["EMA"]
    df["RSI"]        = _rsi(df["Close"])
    df["ATR"]        = _atr(df)

    if "Volume" in df.columns and df["Volume"].sum() > 0:
        df["VWAP"] = _vwap(df)
    else:
        df["VWAP"] = df["Close"]

    # Zero-line crossings
    bp   = df["Bull_Power"]
    bep  = df["Bear_Power"]
    df["BP_Cross_Up"]  = (bp  >= 0) & (bp.shift(1)  < 0)   # Bull Power crosses +ve
    df["BP_Cross_Dn"]  = (bp  <  0) & (bp.shift(1)  >= 0)  # Bull Power crosses -ve
    df["BeP_Cross_Up"] = (bep >= 0) & (bep.shift(1) < 0)   # Bear Power crosses +ve
    df["BeP_Cross_Dn"] = (bep <  0) & (bep.shift(1) >= 0)  # Bear Power crosses -ve

    return df


# ── Divergence detection ──────────────────────────────────────────────────────

def _detect_divergence(df: pd.DataFrame, lookback: int = 10) -> tuple[bool, bool, str, str]:
    """
    Returns (bullish_div, bearish_div, bull_msg, bear_msg).
    Bullish: price LL + Bear Power HL → bullish reversal.
    Bearish: price HH + Bull Power LH → bearish reversal.
    """
    if len(df) < lookback + 2:
        return False, False, "", ""

    recent = df.iloc[-lookback:]
    c  = recent["Close"]
    bp = recent["Bull_Power"]
    bep= recent["Bear_Power"]

    # Bearish: price HH but Bull Power LH (momentum weakening)
    ph = float(c.iloc[-3:].max())
    prev_ph = float(c.iloc[:-3].max())
    bpth = float(bp.iloc[-3:].max())
    prev_bpth = float(bp.iloc[:-3].max())
    bear_div = (ph > prev_ph) and (bpth < prev_bpth) and bpth > 0
    bear_msg = f"Price HH {ph:.0f} > {prev_ph:.0f} but Bull Power weakening ({bpth:+.1f} vs {prev_bpth:+.1f})" if bear_div else ""

    # Bullish: price LL but Bear Power HL (selling pressure weakening)
    pl = float(c.iloc[-3:].min())
    prev_pl = float(c.iloc[:-3].min())
    bepl = float(bep.iloc[-3:].min())
    prev_bepl = float(bep.iloc[:-3].min())
    bull_div = (pl < prev_pl) and (bepl > prev_bepl) and bepl < 0
    bull_msg = f"Price LL {pl:.0f} < {prev_pl:.0f} but Bear Power strengthening ({bepl:+.1f} vs {prev_bepl:+.1f})" if bull_div else ""

    return bull_div, bear_div, bull_msg, bear_msg


# ── Multi-timeframe scoring ───────────────────────────────────────────────────

def score_elder_ray(
    df_15m: pd.DataFrame,
    df_5m:  pd.DataFrame,
    df_1m:  Optional[pd.DataFrame] = None,
) -> dict:
    """
    Score Elder Ray signal across 15m (trend), 5m (timing), 1m (trigger).
    Returns full result dict with signal, scores, and factor breakdown.
    Max possible score per side: 13.
    """
    factors: list[tuple] = []   # (name, detail, direction, pts)
    sce = spe = 0               # CE score, PE score

    price_now = float(df_5m["Close"].iloc[-1])

    # ── 15m: Primary Trend ────────────────────────────────────────────────────
    slope15  = _ema_slope(df_15m["EMA"], bars=4)
    ema15    = float(df_15m["EMA"].iloc[-1])
    bull15   = float(df_15m["Bull_Power"].iloc[-1])
    bear15   = float(df_15m["Bear_Power"].iloc[-1])
    p_vs_ema = float(df_15m["Close"].iloc[-1]) - ema15

    if slope15 > 0 and p_vs_ema > 0:
        sce += 2
        factors.append(("15m Trend", f"EMA rising (slope +{slope15:.1f}), price above EMA", "BULL", 2))
    elif slope15 < 0 and p_vs_ema < 0:
        spe += 2
        factors.append(("15m Trend", f"EMA falling (slope {slope15:.1f}), price below EMA", "BEAR", 2))
    elif slope15 > 0:
        sce += 1
        factors.append(("15m Trend", f"EMA rising (slope +{slope15:.1f}) but price lagging", "BULL", 1))
    elif slope15 < 0:
        spe += 1
        factors.append(("15m Trend", f"EMA falling (slope {slope15:.1f}) but price ahead", "BEAR", 1))
    else:
        factors.append(("15m Trend", "EMA flat — no directional bias", "NEUTRAL", 0))

    # 15m power state
    if bull15 > 0 and bear15 > 0:
        sce += 1
        factors.append(("15m Power", f"Both positive — Bull {bull15:+.1f}, Bear {bear15:+.1f} (strong bulls)", "BULL", 1))
    elif bull15 < 0 and bear15 < 0:
        spe += 1
        factors.append(("15m Power", f"Both negative — Bull {bull15:+.1f}, Bear {bear15:+.1f} (strong bears)", "BEAR", 1))
    elif bull15 > 0 and bear15 < 0:
        factors.append(("15m Power", f"Normal: Bull {bull15:+.1f} / Bear {bear15:+.1f}", "NEUTRAL", 0))
    else:
        spe += 1
        factors.append(("15m Power", f"Bull Power negative ({bull15:+.1f}) — bears dominating", "BEAR", 1))

    # ── 5m: Entry Timing ─────────────────────────────────────────────────────
    slope5   = _ema_slope(df_5m["EMA"], bars=3)
    ema5     = float(df_5m["EMA"].iloc[-1])
    bull5    = float(df_5m["Bull_Power"].iloc[-1])
    bear5    = float(df_5m["Bear_Power"].iloc[-1])
    bull5_1  = float(df_5m["Bull_Power"].iloc[-2]) if len(df_5m) >= 2 else bull5
    bear5_1  = float(df_5m["Bear_Power"].iloc[-2]) if len(df_5m) >= 2 else bear5

    # Bear Power bounce (best CE buy timing: Bear dips then recovers toward 0)
    bep_bounce  = bear5 > bear5_1 and bear5 < 0 and slope5 >= 0
    bep_cross   = bool(df_5m["BeP_Cross_Up"].iloc[-1]) or (
        len(df_5m) >= 2 and bool(df_5m["BeP_Cross_Up"].iloc[-2]))

    if bep_cross:
        sce += 3
        factors.append(("5m CE Entry", f"Bear Power crossed zero ({bear5:+.1f}) — Elder Ray buy trigger", "BULL", 3))
    elif bep_bounce:
        sce += 2
        factors.append(("5m CE Setup", f"Bear Power bouncing ({bear5_1:+.1f}→{bear5:+.1f}) — setup forming", "BULL", 2))
    elif bull5 > 0 and slope5 > 0 and price_now > ema5:
        sce += 1
        factors.append(("5m CE Setup", f"Bull Power {bull5:+.1f}, EMA rising, price above EMA", "BULL", 1))

    # Bull Power reversal (best PE buy timing: Bull peaks then drops toward 0)
    bp_reversal = bull5 < bull5_1 and bull5 > 0 and slope5 <= 0
    bp_cross_dn = bool(df_5m["BP_Cross_Dn"].iloc[-1]) or (
        len(df_5m) >= 2 and bool(df_5m["BP_Cross_Dn"].iloc[-2]))

    if bp_cross_dn:
        spe += 3
        factors.append(("5m PE Entry", f"Bull Power crossed zero ({bull5:+.1f}) — Elder Ray sell trigger", "BEAR", 3))
    elif bp_reversal:
        spe += 2
        factors.append(("5m PE Setup", f"Bull Power reversing ({bull5_1:+.1f}→{bull5:+.1f}) — setup forming", "BEAR", 2))
    elif bear5 < 0 and slope5 < 0 and price_now < ema5:
        spe += 1
        factors.append(("5m PE Setup", f"Bear Power {bear5:+.1f}, EMA falling, price below EMA", "BEAR", 1))

    # ── 5m RSI confirmation ───────────────────────────────────────────────────
    rsi5 = float(df_5m["RSI"].iloc[-1]) if "RSI" in df_5m.columns else 50.0
    if 42 <= rsi5 <= 65:
        sce += 1
        factors.append(("5m RSI", f"{rsi5:.1f} — in CE buy zone (42–65), momentum building", "BULL", 1))
    elif 35 <= rsi5 <= 58:
        spe += 1
        factors.append(("5m RSI", f"{rsi5:.1f} — in PE buy zone (35–58), selling pressure", "BEAR", 1))
    elif rsi5 > 72:
        spe += 1
        factors.append(("5m RSI", f"{rsi5:.1f} — overbought, fade CE / favor PE", "BEAR", 1))
    elif rsi5 < 28:
        sce += 1
        factors.append(("5m RSI", f"{rsi5:.1f} — oversold, fade PE / favor CE", "BULL", 1))
    else:
        factors.append(("5m RSI", f"{rsi5:.1f} — neutral range", "NEUTRAL", 0))

    # ── VWAP bias ─────────────────────────────────────────────────────────────
    vwap5 = float(df_5m["VWAP"].iloc[-1]) if "VWAP" in df_5m.columns else price_now
    if price_now > vwap5 * 1.001:
        sce += 1
        factors.append(("VWAP", f"Price {price_now:.0f} above VWAP {vwap5:.0f} — institutional buying bias", "BULL", 1))
    elif price_now < vwap5 * 0.999:
        spe += 1
        factors.append(("VWAP", f"Price {price_now:.0f} below VWAP {vwap5:.0f} — institutional selling bias", "BEAR", 1))
    else:
        factors.append(("VWAP", f"Price ≈ VWAP {vwap5:.0f} — neutral pivot zone", "NEUTRAL", 0))

    # ── 1m: Trigger (optional) ────────────────────────────────────────────────
    has_1m = df_1m is not None and not df_1m.empty and len(df_1m) >= 3
    if has_1m:
        bull1   = float(df_1m["Bull_Power"].iloc[-1])
        bear1   = float(df_1m["Bear_Power"].iloc[-1])
        ema1    = float(df_1m["EMA"].iloc[-1])
        price1  = float(df_1m["Close"].iloc[-1])
        cross1c = bool(df_1m["BeP_Cross_Up"].iloc[-1]) or (
            len(df_1m) >= 2 and bool(df_1m["BeP_Cross_Up"].iloc[-2]))
        cross1p = bool(df_1m["BP_Cross_Dn"].iloc[-1]) or (
            len(df_1m) >= 2 and bool(df_1m["BP_Cross_Dn"].iloc[-2]))

        if cross1c:
            sce += 2
            factors.append(("1m Trigger", "1m Bear Power crossed +ve — CE buy confirmed now", "BULL", 2))
        elif bull1 > 0 and price1 > ema1 and bear1 > bear5_1:
            sce += 1
            factors.append(("1m Setup", f"1m price {price1:.0f} > EMA {ema1:.0f}, Bull Power {bull1:+.1f}", "BULL", 1))

        if cross1p:
            spe += 2
            factors.append(("1m Trigger", "1m Bull Power crossed -ve — PE buy confirmed now", "BEAR", 2))
        elif bear1 < 0 and price1 < ema1 and bull1 < bull5_1:
            spe += 1
            factors.append(("1m Setup", f"1m price {price1:.0f} < EMA {ema1:.0f}, Bear Power {bear1:+.1f}", "BEAR", 1))

        if not cross1c and not cross1p and abs(bull1) < 5 and abs(bear1) < 5:
            factors.append(("1m Setup", f"1m power neutral (Bull {bull1:+.1f}, Bear {bear1:+.1f}) — wait", "NEUTRAL", 0))
    else:
        factors.append(("1m Trigger", "1m data unavailable (market closed or pre-market)", "NEUTRAL", 0))

    # ── Divergence ────────────────────────────────────────────────────────────
    bull_div, bear_div, bull_dmsg, bear_dmsg = _detect_divergence(df_5m, lookback=12)
    if bull_div:
        sce += 2
        factors.append(("Divergence", f"Bullish: {bull_dmsg}", "BULL", 2))
    if bear_div:
        spe += 2
        factors.append(("Divergence", f"Bearish: {bear_dmsg}", "BEAR", 2))

    # ── ATR-normalised momentum velocity ─────────────────────────────────────
    if "ATR" in df_5m.columns and len(df_5m) >= 4:
        atr5   = float(df_5m["ATR"].iloc[-1])
        c_now  = float(df_5m["Close"].iloc[-1])
        c_prev = float(df_5m["Close"].iloc[-4])
        vel    = (c_now - c_prev) / max(atr5, 1e-9)   # bars of ATR gained in last 3 bars
        if vel > 1.2:
            sce += 1
            factors.append(("Momentum Vel", f"+{vel:.1f}× ATR in 3 bars — strong upward acceleration", "BULL", 1))
        elif vel < -1.2:
            spe += 1
            factors.append(("Momentum Vel", f"{vel:.1f}× ATR in 3 bars — strong downward acceleration", "BEAR", 1))
        else:
            factors.append(("Momentum Vel", f"{vel:+.2f}× ATR — moderate pace", "NEUTRAL", 0))

    # ── Signal determination ──────────────────────────────────────────────────
    if sce >= 8 and sce > spe + 3:
        signal, direction = "STRONG BUY CE", "CE"
    elif sce >= 5 and sce > spe + 1:
        signal, direction = "BUY CE", "CE"
    elif spe >= 8 and spe > sce + 3:
        signal, direction = "STRONG BUY PE", "PE"
    elif spe >= 5 and spe > sce + 1:
        signal, direction = "BUY PE", "PE"
    elif max(sce, spe) >= 3:
        signal    = "WATCH"
        direction = "CE" if sce >= spe else "PE"
    else:
        signal, direction = "WAIT", None

    return {
        "signal":    signal,
        "direction": direction,
        "score_ce":  sce,
        "score_pe":  spe,
        "factors":   factors,
        "bull_div":  bull_div,
        "bear_div":  bear_div,
        "rsi_5m":    rsi5,
        "vwap_5m":   vwap5,
        "price":     price_now,
        "ema_5m":    float(df_5m["EMA"].iloc[-1]),
        "bull5":     bull5,
        "bear5":     bear5,
        "slope_15m": slope15,
        "slope_5m":  slope5,
    }


# ── Data fetching ─────────────────────────────────────────────────────────────

def _mkt_hours_filter(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    idx = df.index
    if idx.tzinfo is None:
        idx = idx.tz_localize("UTC")
    idx = idx.tz_convert("Asia/Kolkata")
    df  = df.copy()
    df.index = idx
    return df.between_time("09:15", "15:35")


def fetch_elder_ray_data(symbol: str) -> dict:
    """
    Download 15m, 5m, 1m OHLCV for the given index and compute Elder Ray.
    Returns {"df_15m", "df_5m", "df_1m", "symbol", "error"}.
    """
    cfg    = INDEX_CONFIG.get(symbol, INDEX_CONFIG["NIFTY"])
    yf_sym = cfg["yf"]

    dfs: dict[str, pd.DataFrame] = {}
    errs: list[str] = []

    for key, period, interval in [
        ("df_15m", "5d",  "15m"),
        ("df_5m",  "2d",  "5m"),
        ("df_1m",  "1d",  "1m"),
    ]:
        try:
            raw = yf.download(yf_sym, period=period, interval=interval,
                              auto_adjust=True, progress=False)
            if isinstance(raw.columns, pd.MultiIndex):
                raw.columns = raw.columns.get_level_values(0)
            raw = _mkt_hours_filter(raw)
            if raw.empty:
                errs.append(f"No {interval} data")
                dfs[key] = pd.DataFrame()
            else:
                dfs[key] = compute_elder_ray(raw)
        except Exception as e:
            errs.append(f"{interval}: {e}")
            dfs[key] = pd.DataFrame()

    return {**dfs, "symbol": symbol, "error": "; ".join(errs) if errs else None}


def run_elder_ray_signal(symbol: str) -> dict:
    """
    Full Elder Ray analysis. Returns:
      signal, direction, score_ce, score_pe, factors,
      df_15m, df_5m, df_1m, atm, lot_size, ist_now, error.
    """
    data = fetch_elder_ray_data(symbol)

    if data["df_5m"].empty or data["df_15m"].empty:
        return {"error": data.get("error") or f"No data for {symbol}"}

    result = score_elder_ray(data["df_15m"], data["df_5m"], data.get("df_1m"))
    result.update({
        "df_15m":  data["df_15m"],
        "df_5m":   data["df_5m"],
        "df_1m":   data.get("df_1m"),
        "symbol":  symbol,
        "ist_now": datetime.datetime.now(_IST),
    })

    cfg  = INDEX_CONFIG.get(symbol, INDEX_CONFIG["NIFTY"])
    step = cfg["strike_step"]
    result["atm"]      = round(result["price"] / step) * step
    result["lot_size"] = cfg["lot"]

    return result


# ── Kite ATM option fetch ─────────────────────────────────────────────────────

def fetch_atm_option(api_key: str, access_token: str, symbol: str, spot: float) -> Optional[dict]:
    """
    Fetch ATM CE + PE quotes for nearest expiry via Kite.
    Returns {"atm", "expiry_str", "ce", "pe"} or None on error.
    """
    cfg  = INDEX_CONFIG.get(symbol, INDEX_CONFIG["NIFTY"])
    step = cfg["strike_step"]
    atm  = round(spot / step) * step
    exch = cfg["exch_opt"]

    hdrs = {"X-Kite-Version": "3", "Authorization": f"token {api_key}:{access_token}"}
    today = datetime.date.today()

    try:
        r = requests.get(f"{_KITE_BASE}/instruments/{exch}", headers=hdrs, timeout=30)
        if not r.ok:
            return None

        instr = pd.read_csv(StringIO(r.text))
        instr["expiry_dt"] = pd.to_datetime(instr["expiry"], errors="coerce")
        instr["strike"]    = pd.to_numeric(instr["strike"], errors="coerce")

        opts = instr[
            (instr["name"] == symbol) &
            (instr["instrument_type"].isin(["CE", "PE"])) &
            (instr["expiry_dt"].dt.date >= today)
        ]
        if opts.empty:
            return None

        near_exp  = opts["expiry_dt"].min()
        near_atm  = opts[(opts["expiry_dt"] == near_exp) & (opts["strike"] == atm)]
        if near_atm.empty:
            return None

        ts_list = [f"{exch}:{row['tradingsymbol']}" for _, row in near_atm.iterrows()]
        r2 = requests.get(f"{_KITE_BASE}/quote", headers=hdrs,
                          params={"i": ts_list}, timeout=15)
        if not r2.ok:
            return None

        quotes = r2.json().get("data", {})
        ce_d: Optional[dict] = None
        pe_d: Optional[dict] = None

        for ts, q in quotes.items():
            ts_name = ts.split(":")[-1]
            row = near_atm[near_atm["tradingsymbol"] == ts_name]
            if row.empty:
                continue
            itype = row.iloc[0]["instrument_type"]
            info  = {
                "ltp":    float(q.get("last_price",          0)),
                "oi":     int(  q.get("oi",                  0)),
                "chg":    float(q.get("net_change",          0)),
                "chg_pct":float(q.get("change",              0)),
                "iv":     float(q.get("implied_volatility",  0)),
                "vol":    int(  q.get("volume",              0)),
                "symbol": ts_name,
            }
            if itype == "CE":
                ce_d = info
            else:
                pe_d = info

        if ce_d is None and pe_d is None:
            return None

        # PCR at ATM
        ce_oi = ce_d["oi"] if ce_d else 1
        pe_oi = pe_d["oi"] if pe_d else 0
        pcr   = round(pe_oi / max(ce_oi, 1), 2)

        return {
            "atm":        atm,
            "expiry_str": near_exp.strftime("%d %b"),
            "ce":         ce_d,
            "pe":         pe_d,
            "pcr":        pcr,
        }

    except Exception:
        return None
