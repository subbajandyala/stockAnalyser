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

import contextlib
import datetime
import fcntl
import logging
import threading
import time

import pandas as pd
import requests as _requests
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

# Scalp targets in premium points (GTT levels)
_SCALP_TARGET_PTS = {"NIFTY": 15, "SENSEX": 30}
_SCALP_STOP_PTS   = {"NIFTY": 12, "SENSEX": 25}

# Ride targets — percentage of entry premium (CPR-confirmed trades)
_RIDE_TARGET_PCT = 0.60   # +60%
_RIDE_STOP_PCT   = 0.30   # -30%


_ORDER_LOCK_PATH = "/tmp/kite_order.lock"

_ATM_DELTA = 0.50  # approximate delta for ATM options


def _spot_to_option_levels(
    spot: float,
    entry_premium: float,
    chart_sl: float,
    chart_target: float,
    opt_type: str,
) -> tuple[float, float] | None:
    """Convert spot-level chart SL/target to option premium levels.

    ATM delta ≈ 0.5: each 1-point move in spot moves the ATM option ~0.5 pts.

    CE: gains when spot rises, loses when spot falls.
    PE: gains when spot falls, loses when spot rises.

    Returns (option_sl, option_target) rounded to nearest 0.05, or None if inputs
    are degenerate (SL on wrong side, zero premium, etc.).
    """
    if not (chart_sl > 0 and chart_target > 0 and spot > 0 and entry_premium > 0):
        return None

    if opt_type == "CE":
        sl_pts     = spot - chart_sl      # spot must fall this far to hit SL
        target_pts = chart_target - spot  # spot must rise this far to hit target
    else:  # PE
        sl_pts     = chart_sl - spot      # spot must rise this far to hit SL
        target_pts = spot - chart_target  # spot must fall this far to hit target

    if sl_pts <= 0 or target_pts <= 0:
        return None  # SL or target is on the wrong side

    option_sl     = round(round((entry_premium - _ATM_DELTA * sl_pts)     / 0.05) * 0.05, 2)
    option_target = round(round((entry_premium + _ATM_DELTA * target_pts) / 0.05) * 0.05, 2)

    # SL ceiling: must be at least 20% below entry (no tighter than 80% of entry)
    option_sl     = min(round(entry_premium * 0.80, 1), option_sl)
    # SL floor: no wider than 60% loss (SL at least 40% of entry)
    option_sl     = max(round(entry_premium * 0.40, 1), option_sl)
    option_target = min(round(entry_premium * 5.00, 1), option_target)
    option_sl     = max(0.05, option_sl)

    if option_sl >= entry_premium or option_target <= entry_premium:
        return None  # degenerate after clamp

    return option_sl, option_target


@contextlib.contextmanager
def _order_lock():
    """Cross-process advisory lock — held only during position-check + order placement."""
    with open(_ORDER_LOCK_PATH, "w") as _f:
        fcntl.flock(_f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(_f, fcntl.LOCK_UN)


# ── Internal helpers ───────────────────────────────────────────────────

def _kite_client(api_key: str, access_token: str) -> KiteConnect:
    k = KiteConnect(api_key=api_key)
    k.set_access_token(access_token)
    return k


def _atm_strike(spot: float, symbol: str) -> int:
    step = _STRIKE_STEP[symbol]
    return int(round(spot / step) * step)


def _resolve_contract(
    kite: KiteConnect, symbol: str, strike: int, opt_type: str,
    skip_expiry_day: bool = False,
) -> dict | None:
    """Return {"ts", "token", "expiry", "is_expiry_day"} for the nearest expiry.

    skip_expiry_day=True picks the next expiry when the nearest one expires today
    (avoids 0DTE gamma/theta). Expiry dates come from Kite, so holiday shifts are handled.
    """
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
    row = filtered.iloc[0]
    is_expiry_day = row["expiry"] == today
    if is_expiry_day and skip_expiry_day and len(filtered) > 1:
        row = filtered.iloc[1]
        _LOG.info(f"{symbol} expiry day — using next expiry {row['expiry']}")
    return {
        "ts":            str(row["tradingsymbol"]),
        "token":         int(row["instrument_token"]),
        "expiry":        row["expiry"],
        "is_expiry_day": bool(is_expiry_day),
    }


def _resolve_tradingsymbol(
    kite: KiteConnect, symbol: str, strike: int, opt_type: str
) -> str | None:
    """Return the nearest-expiry tradingsymbol for symbol+strike+type, or None."""
    c = _resolve_contract(kite, symbol, strike, opt_type)
    return c["ts"] if c else None


def is_expiry_day(kite: KiteConnect, symbol: str) -> bool:
    """True if the symbol's nearest weekly option expires today."""
    exchange = _EXCHANGE.get(symbol)
    if not exchange:
        return False
    try:
        df = pd.DataFrame(kite.instruments(exchange))
        exp = df[(df["name"] == symbol) & (df["instrument_type"].isin(["CE", "PE"]))]["expiry"]
        today = datetime.date.today()
        upcoming = exp[exp >= today]
        return bool(not upcoming.empty and upcoming.min() == today)
    except Exception as e:
        _LOG.warning(f"Expiry check failed for {symbol}: {e}")
        return False


def _tick(x: float) -> float:
    return round(round(x / 0.05) * 0.05, 2)


def _option_atr(kite: KiteConnect, token: int, period: int = 14) -> float | None:
    """ATR of the option's own 5-minute candles (last few sessions)."""
    now = datetime.datetime.now(_IST).replace(tzinfo=None)
    try:
        candles = kite.historical_data(token, now - datetime.timedelta(days=5), now, "5minute")
    except Exception as e:
        _LOG.warning(f"Option ATR fetch failed for {token}: {e}")
        return None
    if len(candles) < period + 1:
        return None
    df = pd.DataFrame(candles)
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    atr = float(tr.tail(period).mean())
    return atr if atr > 0 else None


def compute_option_levels(
    symbol: str,
    entry: float,
    opt_type: str,
    spot: float | None = None,
    chart_sl: float | None = None,
    chart_target: float | None = None,
    atr: float | None = None,
    trade_mode: str = "scalp",
) -> dict:
    """Return {"sl", "t1", "source"} in option-premium terms.

    With both chart levels and option ATR, the SL is the WIDER of the two (fewer
    noise stop-outs; position sizing absorbs the extra distance), and T1 is at
    least 1.5× the risk.
    """
    chart = None
    if chart_sl and chart_target and spot:
        chart = _spot_to_option_levels(spot, entry, chart_sl, chart_target, opt_type)
    atr_sl = entry - 1.5 * atr if atr else None

    if chart or atr_sl:
        candidates = [x for x in ((chart[0] if chart else None), atr_sl) if x]
        sl = min(candidates)
        sl = max(entry * 0.40, min(entry * 0.85, sl))
        risk = entry - sl
        t1 = chart[1] if chart else entry + 2.5 * atr
        t1 = min(entry * 5.0, max(t1, entry + 1.5 * risk))
        source = "chart+atr" if (chart and atr_sl) else ("chart" if chart else "atr")
    elif trade_mode == "ride":
        sl, t1, source = entry * (1 - _RIDE_STOP_PCT), entry * (1 + _RIDE_TARGET_PCT), "ride"
    else:
        sl = entry - _SCALP_STOP_PTS.get(symbol, 7)
        t1 = entry + _SCALP_TARGET_PTS.get(symbol, 10)
        source = "scalp"
    sl = max(0.05, _tick(sl))
    return {"sl": sl, "t1": _tick(t1), "source": source}


# Limit price of each GTT leg, relative to its trigger. A sell limit exactly at the
# trigger can stay unfilled when the premium gaps through it, so both legs sit below.
_GTT_SL_LIMIT_BUFFER     = 0.05   # SL leg sells at trigger − 5%
_GTT_TARGET_LIMIT_BUFFER = 0.01   # target leg sells at trigger − 1%


def gtt_leg_prices(sl: float, target: float) -> tuple[float, float]:
    """Limit prices for the (SL, target) legs of an OCO GTT."""
    return (max(0.05, _tick(sl * (1 - _GTT_SL_LIMIT_BUFFER))),
            max(0.05, _tick(target * (1 - _GTT_TARGET_LIMIT_BUFFER))))


def place_oco_gtt(kite: KiteConnect, symbol: str, ts: str, qty: int,
                  sl: float, target: float, last_price: float):
    """Place a two-leg (SL / target) GTT for a long option position. Returns GTT id."""
    exchange = _EXCHANGE.get(symbol, "NFO")
    sl_limit, tgt_limit = gtt_leg_prices(sl, target)
    return kite.place_gtt(
        trigger_type=kite.GTT_TYPE_OCO,
        tradingsymbol=ts,
        exchange=exchange,
        trigger_values=[sl, target],
        last_price=last_price,
        orders=[
            {"transaction_type": kite.TRANSACTION_TYPE_SELL, "quantity": qty,
             "product": kite.PRODUCT_NRML, "order_type": kite.ORDER_TYPE_LIMIT, "price": sl_limit},
            {"transaction_type": kite.TRANSACTION_TYPE_SELL, "quantity": qty,
             "product": kite.PRODUCT_NRML, "order_type": kite.ORDER_TYPE_LIMIT, "price": tgt_limit},
        ],
    )


def _calc_qty(kite: KiteConnect, symbol: str, ltp: float, capital_fraction: float = 1.0) -> int:
    """Return total units to buy based on available cash (95% utilisation).

    Never returns less than 1 lot: with zero or insufficient balance the order is
    still sent so Kite records it (and rejects it) in the order book.
    """
    lot = LOT_SIZE[symbol]
    cost_per_lot = ltp * lot
    try:
        margins = kite.margins()
        # margins() returns {"equity": {...}, "commodity": {...}}
        # F&O trades (NFO/BFO) draw from the equity segment's live balance
        eq = margins.get("equity", {})
        available = float(eq.get("available", {}).get("live_balance", 0))
    except Exception as e:
        _LOG.warning(f"Margin fetch failed ({e}) — defaulting to 1 lot")
        return lot
    if cost_per_lot <= 0:
        return lot  # degenerate LTP; shouldn't happen
    usable   = max(available, 0) * 0.95 * capital_fraction
    max_lots = int(usable / cost_per_lot)
    if max_lots == 0:
        _LOG.warning(
            f"Insufficient balance: ₹{available:.0f} available, "
            f"₹{cost_per_lot:.0f} needed for 1 lot of {symbol} @ ₹{ltp:.1f} — sending 1 lot anyway"
        )
        return lot
    _LOG.info(
        f"Balance ₹{available:.0f} × 95% → "
        f"₹{usable:.0f} usable → {max_lots} lot(s) of {symbol} @ ₹{ltp} (₹{cost_per_lot:.0f}/lot)"
    )
    return max_lots * lot


def _fill_price(kite: KiteConnect, order_id: str, exchange: str, ts: str) -> tuple[float, str]:
    """Wait up to 8s for limit fill. Returns (avg_price, "") on fill, else (0.0, reason).

    An order still open after 8s is cancelled: leaving it open blocks margin and
    causes false 'GTT hit' detections on the next position check. The caller must
    not place a GTT when the price is 0.
    """
    for attempt in range(4):
        time.sleep(2)
        try:
            for h in reversed(kite.order_history(order_id)):
                status = h.get("status", "")
                if status == "COMPLETE" and h.get("average_price", 0) > 0:
                    return float(h["average_price"]), ""
                if status in ("REJECTED", "CANCELLED"):
                    reason = h.get("status_message") or h.get("status_message_raw") or ""
                    _LOG.warning(f"Order {order_id} {status}: {reason}")
                    return 0.0, f"Kite {status}: {reason}".strip(": ")
        except Exception:
            pass
    # Order still open after 8s — cancel it to avoid zombie pending orders
    try:
        kite.cancel_order(variety=kite.VARIETY_REGULAR, order_id=order_id)
        _LOG.warning(f"Order {order_id} not filled in 8s — cancelled to prevent margin lock")
    except Exception as e:
        _LOG.warning(f"Could not cancel unfilled order {order_id}: {e}")
    return 0.0, "not filled within 8s — cancelled to avoid margin lock"


# ── Public API ───────────────────────────────────────────────────────────────

def has_open_position(
    kite: KiteConnect,
    symbol: str,
    only_ts: str | None = None,
    product: str | None = None,
) -> bool:
    """True if an option position for this symbol is non-zero.

    only_ts:  when set, checks only that exact contract (e.g. NIFTY2541524950CE)
    product:  when set, checks only that product type ("NRML" or "MIS")
              — use this to keep chart_bot (NRML) and RSI scalp (MIS) independent
    """
    try:
        for pos in kite.positions().get("net", []):
            ts      = pos.get("tradingsymbol", "")
            qty     = int(pos.get("quantity", 0))
            prod    = pos.get("product", "")
            if qty == 0:
                continue
            if product and prod != product:
                continue
            if only_ts:
                if ts == only_ts:
                    return True
            else:
                if ts.startswith(symbol):
                    return True
    except Exception as e:
        _LOG.warning(f"Position check failed: {e}")
    return False


def position_pnl_pct(kite: KiteConnect, symbol: str, only_ts: str | None = None) -> float | None:
    """Return unrealised P&L % for the open option position, or None if not found.

    only_ts: check only that exact contract (so manual positions are never read).
    """
    try:
        for pos in kite.positions().get("net", []):
            ts  = pos.get("tradingsymbol", "")
            qty = int(pos.get("quantity", 0))
            if qty == 0:
                continue
            if (only_ts and ts == only_ts) or (not only_ts and ts.startswith(symbol)):
                avg = float(pos.get("average_price", 0))
                cur = float(pos.get("last_price", avg))
                return (cur - avg) / avg if avg > 0 else 0.0
    except Exception as e:
        _LOG.warning(f"P&L fetch failed: {e}")
    return None


def contract_pnl(kite: KiteConnect, ts: str) -> float | None:
    """Total day P&L (₹, realised + unrealised) for one contract, or None if not found."""
    try:
        for pos in kite.positions().get("net", []):
            if pos.get("tradingsymbol") == ts:
                return float(pos.get("pnl", 0))
    except Exception as e:
        _LOG.warning(f"Contract P&L fetch failed for {ts}: {e}")
    return None


def sell_partial(kite: KiteConnect, symbol: str, ts: str, qty: int) -> str | None:
    """LIMIT SELL qty units of a long NRML position at LTP × 0.99. Returns order id."""
    exchange = _EXCHANGE.get(symbol, "NFO")
    try:
        ltp = float(kite.ltp([f"{exchange}:{ts}"])[f"{exchange}:{ts}"]["last_price"])
        return kite.place_order(
            variety=kite.VARIETY_REGULAR,
            exchange=exchange,
            tradingsymbol=ts,
            transaction_type=kite.TRANSACTION_TYPE_SELL,
            quantity=qty,
            product=kite.PRODUCT_NRML,
            order_type=kite.ORDER_TYPE_LIMIT,
            price=_tick(ltp * 0.99),
        )
    except Exception as e:
        _LOG.error(f"Partial sell failed for {ts}: {e}")
        return None


def exit_position(symbol: str, api_key: str, access_token: str,
                  gtt_id: int | None = None, only_ts: str | None = None) -> dict:
    """
    Exit the bot's open NRML option position for symbol.

    If only_ts is given, exits only that specific tradingsymbol (the exact
    contract the bot placed). This prevents touching any manual positions.

    Steps:
      1. Cancel GTT (if gtt_id given) so it doesn't fire after exit.
      2. Place a LIMIT SELL at LTP × 0.99 (rounded to 0.05).

    Returns {"exits": [...]} on success, {"error": "..."} on failure.
    """
    exchange = _EXCHANGE.get(symbol)
    if not exchange:
        return {"error": f"Unsupported symbol: {symbol}"}

    kite = _kite_client(api_key, access_token)

    # Step 1: cancel GTT to avoid double-sell after exit
    if gtt_id:
        try:
            kite.delete_gtt(gtt_id)
            _LOG.info(f"GTT {gtt_id} cancelled before exit")
        except Exception as e:
            _LOG.warning(f"GTT cancel failed ({e}) — proceeding with exit anyway")

    # Step 2: fetch open positions
    try:
        net = kite.positions().get("net", [])
    except Exception as e:
        return {"error": f"Could not fetch positions: {e}"}

    exits = []
    for pos in net:
        ts  = pos.get("tradingsymbol", "")
        qty = int(pos.get("quantity", 0))
        if qty == 0:
            continue
        # Only exit the bot's specific contract if known; else fall back to prefix match
        if only_ts:
            if ts != only_ts:
                continue
        elif not ts.startswith(symbol):
            continue

        # Fetch LTP for limit price
        try:
            ltp_data   = kite.ltp([f"{exchange}:{ts}"])
            ltp        = float(ltp_data[f"{exchange}:{ts}"]["last_price"])
            exit_limit = round(round(ltp * 0.99 / 0.05) * 0.05, 2)
        except Exception as e:
            return {"error": f"Could not fetch LTP for {ts} exit: {e}"}

        try:
            order_id = kite.place_order(
                variety=kite.VARIETY_REGULAR,
                exchange=exchange,
                tradingsymbol=ts,
                transaction_type=kite.TRANSACTION_TYPE_SELL,
                quantity=abs(qty),
                product=kite.PRODUCT_NRML,
                order_type=kite.ORDER_TYPE_LIMIT,
                price=exit_limit,
            )
            exits.append({"tradingsymbol": ts, "qty": abs(qty), "order_id": order_id, "exit_limit": exit_limit})
            _LOG.info(f"Exit order placed: SELL {abs(qty)} x {ts} @ LIMIT {exit_limit}")
        except Exception as e:
            return {"error": f"Exit order failed for {ts}: {e}"}

    if not exits:
        return {"error": f"No open position found for {symbol}"}

    return {"exits": exits}


def place_order_for_signal(
    symbol: str,
    direction: str,
    spot: float,
    api_key: str,
    access_token: str,
    capital_fraction: float = 1.0,
    trade_mode: str = "scalp",
    chart_sl: float | None = None,
    chart_target: float | None = None,
    expiry_day_mode: str = "same",
    partial_booking: bool = False,
    use_option_atr: bool = False,
) -> dict:
    """
    Place ATM option order + GTT for the given signal.

    Args:
        symbol:           "NIFTY" or "SENSEX"
        direction:        "CE" / "CALL" / "STRONG BUY CE" → CE
                          "PE" / "PUT"  / "STRONG BUY PE"  → PE
        spot:             current spot price (used to compute ATM strike)
        api_key:          Kite API key
        access_token:     Kite access token (daily)
        capital_fraction: fraction of available balance to use (0.5 = 50/50 split)
        expiry_day_mode:  "same" (nearest expiry), "next" (skip 0DTE), "skip" (no trade on expiry day)
        partial_booking:  GTT targets T2; caller books half at T1 (needs ≥ 2 lots)
        use_option_atr:   widen SL using the option's own 5m ATR

    Returns dict with keys:
        order_id, tradingsymbol, strike, opt_type, qty, entry_price, sl, target, gtt_id,
        t1, partial_qty, expiry, is_expiry_day, levels_source
    On error: {"error": "<reason>"}
    """
    if symbol not in LOT_SIZE:
        return {"error": f"Unsupported symbol: {symbol}. Supported: {list(LOT_SIZE)}"}

    opt_type = "CE" if any(x in direction.upper() for x in ("CE", "CALL", "BUY CE")) else "PE"
    exchange = _EXCHANGE[symbol]
    lot      = LOT_SIZE[symbol]

    kite = _kite_client(api_key, access_token)

    with _order_lock():
        # 1. Resolve ATM contract (optionally skipping today's expiry)
        strike   = _atm_strike(spot, symbol)
        contract = _resolve_contract(kite, symbol, strike, opt_type,
                                     skip_expiry_day=(expiry_day_mode == "next"))
        if not contract:
            return {"error": f"Could not find tradingsymbol for {symbol} {strike} {opt_type}"}
        if expiry_day_mode == "skip" and contract["is_expiry_day"]:
            return {"error": f"{symbol} expiry day — new entries disabled (expiry_day_mode=skip)"}
        ts = contract["ts"]

        # 2. Fetch option LTP and compute limit price (1% above to ensure fill)
        try:
            ltp_data = kite.ltp([f"{exchange}:{ts}"])
            option_ltp = float(ltp_data[f"{exchange}:{ts}"]["last_price"])
        except Exception as e:
            return {"error": f"Could not fetch option LTP for {ts}: {e}"}

        limit_price = _tick(option_ltp * 1.01)

        # 3. SL/target levels at LTP (shifted to the real fill below)
        atr    = _option_atr(kite, contract["token"]) if use_option_atr else None
        levels = compute_option_levels(symbol, option_ltp, opt_type, spot,
                                       chart_sl, chart_target, atr, trade_mode)

        # Quantity: as many lots as 95% of the available balance buys (min 1 lot)
        qty         = _calc_qty(kite, symbol, option_ltp, capital_fraction)
        lots        = qty // lot
        partial_qty = (lots // 2) * lot if (partial_booking and lots >= 2) else 0

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
            return {"error": f"Order rejected by Kite for {qty} × {ts}: {e}", "sent": True}

    _LOG.info(f"Order placed: {order_id}")

    # 5. Get fill price (wait up to 8s; cancel order if still not filled)
    entry_price, reason = _fill_price(kite, order_id, exchange, ts)
    if not entry_price:
        return {"error": f"Order {order_id} for {qty} × {ts} {reason}", "sent": True, "order_id": order_id}

    # 6. Shift the LTP-based levels by the fill slippage so distances are preserved
    diff         = entry_price - option_ltp
    sl_price     = max(0.05, _tick(levels["sl"] + diff))
    t1_price     = _tick(levels["t1"] + diff)
    target_price = t1_price
    if partial_qty:
        # GTT holds the runner target; the bot books partial_qty at T1 itself
        target_price = _tick(min(entry_price * 5.0, entry_price + 2 * (t1_price - entry_price)))
    _LOG.info(
        f"Levels ({levels['source']}): entry={entry_price} SL={sl_price} "
        f"T1={t1_price} GTT target={target_price} partial_qty={partial_qty}"
    )

    # 7. GTT two-leg (OCO): [sl_trigger, target_trigger]
    gtt_id = None
    try:
        gtt_id = place_oco_gtt(kite, symbol, ts, qty, sl_price, target_price, entry_price)
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
        "t1":            t1_price,
        "partial_qty":   partial_qty,
        "gtt_id":        gtt_id,
        "trade_mode":    trade_mode,
        "expiry":        str(contract["expiry"]),
        "is_expiry_day": contract["is_expiry_day"],
        "levels_source": levels["source"],
    }


def _send_rsi_exit_ntfy(ts: str, entry_ltp: float, exit_ltp: float, ntfy_topic: str) -> None:
    pnl_pts = round(exit_ltp - entry_ltp, 2)
    pnl_pct = round((exit_ltp - entry_ltp) / entry_ltp * 100, 1) if entry_ltp else 0
    sign    = "+" if pnl_pts >= 0 else ""
    emoji   = "✅" if pnl_pts >= 0 else "🔴"
    body = (
        f"{emoji} RSI Scalp EXIT — {ts}\n"
        f"Entry    : ₹{entry_ltp:.2f}\n"
        f"Exit LTP : ₹{exit_ltp:.2f}\n"
        f"P&L      : {sign}{pnl_pts:.2f} pts ({sign}{pnl_pct:.1f}%)\n"
        f"Duration : ~60 seconds (MIS auto-exit)"
    )
    try:
        _requests.post(
            f"https://ntfy.sh/{ntfy_topic}",
            data=body.encode("utf-8"),
            headers={
                "Title":        f"RSI Scalp EXIT {emoji} {ts}",
                "Priority":     "high",
                "Tags":         "white_check_mark" if pnl_pts >= 0 else "x",
                "Content-Type": "text/plain; charset=utf-8",
            },
            timeout=10,
        )
    except Exception as e:
        _LOG.warning(f"Exit ntfy failed: {e}")


def _try_exit_once(kite: KiteConnect, exchange: str, ts: str,
                   buy_order_id: str, entry_ltp: float, ntfy_topic: str,
                   attempt: int) -> bool:
    """Attempt one MARKET SELL exit. Returns True if position was exited or not found."""
    try:
        for pos in kite.positions().get("net", []):
            if pos.get("tradingsymbol") == ts and int(pos.get("quantity", 0)) > 0:
                open_qty = int(pos["quantity"])
                kite.place_order(
                    variety=kite.VARIETY_REGULAR,
                    exchange=exchange,
                    tradingsymbol=ts,
                    transaction_type=kite.TRANSACTION_TYPE_SELL,
                    quantity=open_qty,
                    product=kite.PRODUCT_MIS,
                    order_type=kite.ORDER_TYPE_MARKET,
                )
                _LOG.info(f"RSI scalp EXIT (attempt {attempt}): SELL {open_qty}×{ts} @ MARKET")
                if ntfy_topic:
                    try:
                        q       = kite.ltp([f"{exchange}:{ts}"])
                        exit_lt = float(q.get(f"{exchange}:{ts}", {}).get("last_price", entry_ltp))
                    except Exception:
                        exit_lt = entry_ltp
                    _send_rsi_exit_ntfy(ts, entry_ltp, exit_lt, ntfy_topic)
                return True
        # Position not found — BUY was never filled; cancel it (only on first attempt)
        if attempt == 1:
            try:
                kite.cancel_order(variety=kite.VARIETY_REGULAR, order_id=buy_order_id)
                _LOG.info(f"RSI scalp: BUY not filled after 60s, cancelled {buy_order_id}")
            except Exception:
                pass
        return True  # nothing left to do
    except Exception as e:
        _LOG.error(f"RSI scalp exit error for {ts} (attempt {attempt}): {e}")
        return False  # signal caller to retry


def _exit_rsi_scalp(
    api_key: str, access_token: str, exchange: str, ts: str,
    buy_order_id: str, entry_ltp: float, ntfy_topic: str = "",
) -> None:
    """Called in a daemon thread — waits 60s then exits MIS position at MARKET.
    Retries once more after another 60s if the first attempt hits an API error."""
    time.sleep(60)
    kite = _kite_client(api_key, access_token)
    ok = _try_exit_once(kite, exchange, ts, buy_order_id, entry_ltp, ntfy_topic, attempt=1)
    if not ok:
        # API error on first attempt — retry after another 60s
        time.sleep(60)
        kite = _kite_client(api_key, access_token)  # fresh client in case token was refreshed
        _try_exit_once(kite, exchange, ts, buy_order_id, entry_ltp, ntfy_topic, attempt=2)


def place_rsi_scalp_order(
    symbol: str,
    opt_type: str,
    spot: float,
    api_key: str,
    access_token: str,
    ntfy_topic: str = "",
) -> dict:
    """BUY ATM option MIS on RSI 70 crossover; auto-exit after 60 seconds.

    No GTT, no SL, no target — exit is a MARKET SELL fired 60s after entry.
    Uses MIS product so position is always squared off by EOD even if thread fails.
    """
    if symbol not in LOT_SIZE:
        return {"error": f"Unsupported symbol: {symbol}"}
    exchange = _EXCHANGE[symbol]
    kite = _kite_client(api_key, access_token)

    with _order_lock():
        # MIS only — chart_bot NRML positions never block RSI scalp and vice versa
        if has_open_position(kite, symbol, product="MIS"):
            return {"error": f"Open {symbol} MIS position exists — skipping RSI scalp"}

        strike = _atm_strike(spot, symbol)
        ts = _resolve_tradingsymbol(kite, symbol, strike, opt_type)
        if not ts:
            return {"error": f"No tradingsymbol for {symbol} {strike} {opt_type}"}

        try:
            ltp_data   = kite.ltp([f"{exchange}:{ts}"])
            option_ltp = float(ltp_data[f"{exchange}:{ts}"]["last_price"])
        except Exception as e:
            return {"error": f"LTP fetch failed for {ts}: {e}"}

        limit_price = round(round(option_ltp * 1.01 / 0.05) * 0.05, 2)
        qty         = _calc_qty(kite, symbol, option_ltp)

        try:
            order_id = kite.place_order(
                variety=kite.VARIETY_REGULAR,
                exchange=exchange,
                tradingsymbol=ts,
                transaction_type=kite.TRANSACTION_TYPE_BUY,
                quantity=qty,
                product=kite.PRODUCT_MIS,
                order_type=kite.ORDER_TYPE_LIMIT,
                price=limit_price,
            )
        except Exception as e:
            return {"error": f"BUY order failed: {e}"}

    _LOG.info(f"RSI scalp BUY: {qty}×{ts} @ LIMIT {limit_price} | order {order_id}")

    t = threading.Thread(
        target=_exit_rsi_scalp,
        args=(api_key, access_token, exchange, ts, order_id, option_ltp, ntfy_topic),
        daemon=True,
    )
    t.start()

    return {
        "order_id":     order_id,
        "tradingsymbol": ts,
        "strike":       strike,
        "opt_type":     opt_type,
        "qty":          qty,
        "entry_ltp":    option_ltp,
        "limit_price":  limit_price,
        "exit_in_sec":  60,
    }


def exit_orphan_mis_positions(
    api_key: str,
    access_token: str,
    known_ts: set | None = None,
) -> list:
    """Exit open MIS positions left from a previous crashed run.

    known_ts: set of tradingsymbols the bot placed. When provided, only those
    contracts are exited — manual positions are never touched. Pass None to
    exit all (legacy behaviour, not recommended).
    """
    kite   = _kite_client(api_key, access_token)
    exited = []
    try:
        net = kite.positions().get("net", [])
    except Exception as e:
        _LOG.warning(f"Orphan position check failed: {e}")
        return exited
    for pos in net:
        ts      = pos.get("tradingsymbol", "")
        qty     = int(pos.get("quantity", 0))
        product = pos.get("product", "")
        if qty <= 0 or product != "MIS":
            continue
        # Only exit positions the bot placed — never touch manual positions
        if known_ts is not None and ts not in known_ts:
            continue
        for symbol, exchange in _EXCHANGE.items():
            if ts.startswith(symbol):
                try:
                    order_id = kite.place_order(
                        variety=kite.VARIETY_REGULAR,
                        exchange=exchange,
                        tradingsymbol=ts,
                        transaction_type=kite.TRANSACTION_TYPE_SELL,
                        quantity=qty,
                        product=kite.PRODUCT_MIS,
                        order_type=kite.ORDER_TYPE_MARKET,
                    )
                    _LOG.info(f"Orphan MIS exit: SELL {qty}×{ts} @ MARKET | {order_id}")
                    exited.append({"tradingsymbol": ts, "qty": qty, "order_id": order_id})
                except Exception as e:
                    _LOG.error(f"Orphan exit failed for {ts}: {e}")
    return exited


def resolve_and_fetch_ltp(
    kite: KiteConnect, symbol: str, strike: int, opt_type: str
) -> tuple[str, float]:
    """Resolve the nearest-expiry tradingsymbol and return (ts, ltp). ('', 0.0) on error."""
    exchange = _EXCHANGE.get(symbol, "NFO")
    ts = _resolve_tradingsymbol(kite, symbol, strike, opt_type)
    if not ts:
        return "", 0.0
    try:
        q   = kite.ltp([f"{exchange}:{ts}"])
        ltp = float(q.get(f"{exchange}:{ts}", {}).get("last_price", 0.0))
        return ts, ltp
    except Exception as e:
        _LOG.warning(f"LTP fetch failed for {ts}: {e}")
        return ts, 0.0


def fetch_option_ltp(kite: KiteConnect, symbol: str, ts: str) -> float:
    """Fetch current LTP for a known option tradingsymbol. Returns 0.0 on error."""
    exchange = _EXCHANGE.get(symbol, "NFO")
    try:
        q = kite.ltp([f"{exchange}:{ts}"])
        return float(q.get(f"{exchange}:{ts}", {}).get("last_price", 0.0))
    except Exception as e:
        _LOG.warning(f"LTP fetch failed for {ts}: {e}")
        return 0.0
