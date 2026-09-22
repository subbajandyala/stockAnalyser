"""
Auto-execution module for ATM options via Kite Connect.

Flow:
  1. Check open positions for symbol → skip if already in trade
  2. Resolve nearest-expiry ATM tradingsymbol from Kite instruments
  3. Place 1-lot MARKET BUY (NRML)
  4. Place GTT two-leg (OCO): SL at -30% premium, Target at +60% premium
  5. Return result dict (or dict with 'error' key on failure)

Lot sizes (NSE/BSE current rules):
  NIFTY  = 65 units
  SENSEX = 20 units

Usage:
  Set env var KITE_AUTO_TRADE=1 to enable auto-execution in bot scripts.
  Without it, bots run in signal-only / notify-only mode.
"""

import datetime
import logging
import time

import pandas as pd
from kiteconnect import KiteConnect

_LOG = logging.getLogger(__name__)

_IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

LOT_SIZE = {
    "NIFTY":  65,
    "SENSEX": 20,
}

_EXCHANGE = {
    "NIFTY":  "NFO",
    "SENSEX": "BFO",
}

_STRIKE_STEP = {
    "NIFTY":  50,
    "SENSEX": 100,
}

SL_PCT     = 0.30   # stop loss  = entry - 30%
TARGET_PCT = 0.60   # target     = entry + 60%


# ── Internal helpers ───────────────────────────────────────────────────

def _kite_client(api_key: str, access_token: str) -> KiteConnect:
    k = KiteConnect(api_key=api_key)
    k.set_access_token(access_token)
    return k


def _atm_strike(spot: float, symbol: str) -> int:
    step = _STRIKE_STEP[symbol]
    return int(round(spot / step) * step)


def _resolve_tradingsymbol(
    kite: KiteConnect, symbol: str, strike: int, opt_type: str
) -> str | None:
    """Return the nearest-expiry tradingsymbol for symbol+strike+type, or None."""
    exchange = _EXCHANGE[symbol]
    try:
        instruments = kite.instruments(exchange)
    except Exception as e:
        _LOG.error(f"instruments({exchange}) failed: {e}")
        return None

    df = pd.DataFrame(instruments)
    today = datetime.date.today()

    mask = (
        (df["name"] == symbol)
        & (df["instrument_type"] == opt_type)
        & (df["strike"].astype(float) == float(strike))
        & (df["expiry"] >= today)
    )
    filtered = df[mask].sort_values("expiry")
    if filtered.empty:
        _LOG.error(f"No instrument found: {symbol} {strike} {opt_type} on {exchange}")
        return None
    return str(filtered.iloc[0]["tradingsymbol"])


def _fill_price(kite: KiteConnect, order_id: str, exchange: str, ts: str) -> float:
    """Wait briefly for fill, return average_price; fall back to LTP."""
    time.sleep(2)   # give exchange 2s to fill a market order
    try:
        for h in reversed(kite.order_history(order_id)):
            if h.get("status") == "COMPLETE" and h.get("average_price", 0) > 0:
                return float(h["average_price"])
    except Exception:
        pass
    # fallback: LTP
    try:
        q = kite.ltp([f"{exchange}:{ts}"])
        return float(q[f"{exchange}:{ts}"]["last_price"])
    except Exception:
        return 0.0


# ── Public API ───────────────────────────────────────────────────────────────

def has_open_position(kite: KiteConnect, symbol: str) -> bool:
    """True if any net option position for this symbol is non-zero."""
    try:
        for pos in kite.positions().get("net", []):
            ts = pos.get("tradingsymbol", "")
            if ts.startswith(symbol) and int(pos.get("quantity", 0)) != 0:
                return True
    except Exception as e:
        _LOG.warning(f"Position check failed: {e}")
    return False


def place_order_for_signal(
    symbol: str,
    direction: str,
    spot: float,
    api_key: str,
    access_token: str,
) -> dict:
    """
    Place 1-lot ATM option order + GTT for the given signal.

    Args:
        symbol:    "NIFTY" or "SENSEX"
        direction: "CE" / "CALL" / "STRONG BUY CE" → CE
                   "PE" / "PUT"  / "STRONG BUY PE"  → PE
        spot:      current spot price (used to compute ATM strike)
        api_key:   Kite API key
        access_token: Kite access token (daily)

    Returns dict with keys:
        order_id, tradingsymbol, strike, opt_type, qty, entry_price, sl, target, gtt_id
    On error: {"error": "<reason>"}
    """
    if symbol not in LOT_SIZE:
        return {"error": f"Unsupported symbol: {symbol}. Supported: {list(LOT_SIZE)}"}

    opt_type = "CE" if any(x in direction.upper() for x in ("CE", "CALL", "BUY CE")) else "PE"
    exchange = _EXCHANGE[symbol]
    qty      = LOT_SIZE[symbol]

    kite = _kite_client(api_key, access_token)

    # 1. Existing position guard
    if has_open_position(kite, symbol):
        return {"error": f"Open {symbol} position already exists — skipping"}

    # 2. Resolve ATM tradingsymbol
    strike = _atm_strike(spot, symbol)
    ts = _resolve_tradingsymbol(kite, symbol, strike, opt_type)
    if not ts:
        return {"error": f"Could not find tradingsymbol for {symbol} {strike} {opt_type}"}

    # 3. Fetch option LTP and compute limit price (1% above to ensure fill)
    try:
        ltp_data = kite.ltp([f"{exchange}:{ts}"])
        option_ltp = float(ltp_data[f"{exchange}:{ts}"]["last_price"])
    except Exception as e:
        return {"error": f"Could not fetch option LTP for {ts}: {e}"}

    # Round to nearest 0.05 (NSE tick size for options)
    limit_price = round(round(option_ltp * 1.01 / 0.05) * 0.05, 2)

    _LOG.info(f"Placing order: BUY {qty} x {ts} @ LIMIT {limit_price} (NRML)")

    # 4. Place limit buy order (NRML so GTT works; market orders blocked via API)
    try:
        order_id = kite.place_order(
            variety=kite.VARIETY_REGULAR,
            exchange=exchange,
            tradingsymbol=ts,
            transaction_type=kite.TRANSACTION_TYPE_BUY,
            quantity=qty,
            product=kite.PRODUCT_NRML,
            order_type=kite.ORDER_TYPE_LIMIT,
            price=limit_price,
        )
    except Exception as e:
        return {"error": f"Order placement failed: {e}"}

    _LOG.info(f"Order placed: {order_id}")

    # 5. Get fill price (wait briefly for limit fill)
    entry_price = _fill_price(kite, order_id, exchange, ts)
    if not entry_price:
        entry_price = limit_price  # use limit price as proxy if fill not confirmed
    if not entry_price:
        return {
            "order_id":      order_id,
            "tradingsymbol": ts,
            "error":         "Order placed but fill price unavailable — GTT not set",
        }

    # 6. SL and target levels
    sl_price     = round(entry_price * (1 - SL_PCT), 1)
    target_price = round(entry_price * (1 + TARGET_PCT), 1)

    # 7. GTT two-leg (OCO): [sl_trigger, target_trigger]
    gtt_id = None
    try:
        gtt_id = kite.place_gtt(
            trigger_type=kite.GTT_TYPE_OCO,
            tradingsymbol=ts,
            exchange=exchange,
            trigger_values=[sl_price, target_price],
            last_price=entry_price,
            orders=[
                {
                    "transaction_type": kite.TRANSACTION_TYPE_SELL,
                    "quantity":         qty,
                    "product":          kite.PRODUCT_NRML,
                    "order_type":       kite.ORDER_TYPE_LIMIT,
                    "price":            sl_price,
                },
                {
                    "transaction_type": kite.TRANSACTION_TYPE_SELL,
                    "quantity":         qty,
                    "product":          kite.PRODUCT_NRML,
                    "order_type":       kite.ORDER_TYPE_LIMIT,
                    "price":            target_price,
                },
            ],
        )
        _LOG.info(f"GTT placed: {gtt_id}  SL={sl_price}  Target={target_price}")
    except Exception as e:
        _LOG.warning(f"GTT failed (order {order_id} is live, set SL manually): {e}")

    return {
        "order_id":      order_id,
        "tradingsymbol": ts,
        "strike":        strike,
        "opt_type":      opt_type,
        "qty":           qty,
        "entry_price":   entry_price,
        "sl":            sl_price,
        "target":        target_price,
        "gtt_id":        gtt_id,
    }
