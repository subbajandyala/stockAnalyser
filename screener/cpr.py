"""
CPR (Central Pivot Range) for index trading.

Fetches yesterday's OHLC for NIFTY/SENSEX via Kite historical data,
computes TC/PP/BC, and provides a confirmation check for Elder Ray signals.

  PP = (High + Low + Close) / 3
  BC = (High + Low) / 2
  TC = (PP - BC) + PP

Narrow CPR: width < _NARROW_PCT of PP  → trending day expected
Wide CPR:   width >= _NARROW_PCT        → choppy/sideways day
"""

import datetime
import logging

_LOG = logging.getLogger(__name__)

_IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

# Kite instrument tokens for the indices
_TOKEN = {
    "NIFTY":  256265,   # NSE:NIFTY 50
    "SENSEX": 265,      # BSE:SENSEX
}

# CPR width below this % of PP = narrow (trending day)
_NARROW_PCT = 0.30


def fetch_yesterday_ohlc(kite, symbol: str) -> dict | None:
    token = _TOKEN.get(symbol)
    if not token:
        _LOG.error(f"No instrument token for {symbol}")
        return None
    today     = datetime.date.today()
    from_date = today - datetime.timedelta(days=7)
    to_date   = today - datetime.timedelta(days=1)
    try:
        candles = kite.historical_data(token, from_date, to_date, "day")
        if not candles:
            _LOG.warning(f"No historical candles returned for {symbol}")
            return None
        last = candles[-1]
        return {
            "date":  str(last["date"])[:10],
            "high":  float(last["high"]),
            "low":   float(last["low"]),
            "close": float(last["close"]),
        }
    except Exception as e:
        _LOG.error(f"historical_data failed for {symbol}: {e}")
        return None


def compute_cpr(high: float, low: float, close: float) -> dict:
    pp        = (high + low + close) / 3
    bc        = (high + low) / 2
    tc_raw    = (pp - bc) + pp
    tc        = max(bc, tc_raw)
    bc        = min(bc, tc_raw)
    width_pct = abs(tc - bc) / pp * 100
    return {
        "pp":        round(pp, 2),
        "tc":        round(tc, 2),
        "bc":        round(bc, 2),
        "width_pct": round(width_pct, 3),
        "is_narrow": width_pct < _NARROW_PCT,
    }


def cpr_confirms(spot: float, cpr: dict, direction: str) -> bool:
    """
    Returns True if spot price is on the correct side of CPR for the direction.
    CE signal confirmed when spot > TC (bullish side).
    PE signal confirmed when spot < BC (bearish side).
    """
    if direction == "CE":
        return spot > cpr["tc"]
    if direction == "PE":
        return spot < cpr["bc"]
    return False


def load_cpr_for_symbol(kite, symbol: str) -> dict | None:
    """Fetch yesterday's OHLC and return computed CPR dict, or None on failure."""
    ohlc = fetch_yesterday_ohlc(kite, symbol)
    if not ohlc:
        return None
    cpr = compute_cpr(ohlc["high"], ohlc["low"], ohlc["close"])
    cpr["ohlc_date"] = ohlc["date"]
    _LOG.info(
        f"{symbol} CPR ({ohlc['date']}): TC={cpr['tc']} PP={cpr['pp']} BC={cpr['bc']}"
        f"  width={cpr['width_pct']}%  {'NARROW' if cpr['is_narrow'] else 'WIDE'}"
    )
    return cpr
