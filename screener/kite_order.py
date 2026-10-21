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
# Used when the chart gives no SL/target: SL 20% below entry, target keeps the
# original 1.25 reward:risk (was a fixed 12/15 pts on NIFTY, too tight for options)
_FALLBACK_STOP_PCT   = 0.20
_FALLBACK_TARGET_PCT = 0.25

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


def _tick(x: float) -> float:
    return round(round(x / 0.05) * 0.05, 2)


# A GTT sell limit exactly at its trigger can stay unfilled when the premium gaps
# through it, so the SL leg sells up to 5% below its trigger and the target leg 1%.
def gtt_leg_prices(sl: float, target: float) -> tuple[float, float]:
    """Limit prices for the (SL, target) legs of an OCO GTT."""
    return max(0.05, _tick(sl * 0.95)), max(0.05, _tick(target * 0.99))


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


_INSTR_CACHE: dict[str, tuple[datetime.date, pd.DataFrame]] = {}


def _option_instruments(kite: KiteConnect, exchange: str) -> pd.DataFrame | None:
    """Index option rows from Kite's instrument master, downloaded once per day.

    The full master is tens of thousands of rows and takes seconds to fetch, so
    it is cached (index options only) instead of re-downloaded on every order.
    """
    today = datetime.date.today()
    hit = _INSTR_CACHE.get(exchange)
    if hit and hit[0] == today:
        return hit[1]
    try:
        df = pd.DataFrame(kite.instruments(exchange))
    except Exception as e:
        _LOG.error(f"instruments({exchange}) failed: {e}")
        return hit[1] if hit else None
    df = df[df["name"].isin(list(LOT_SIZE) + ["BANKNIFTY"]) & df["instrument_type"].isin(["CE", "PE"])]
    df = df[["name", "instrument_type", "strike", "expiry", "tradingsymbol"]].copy()
    _INSTR_CACHE[exchange] = (today, df)
    return df


def _resolve_tradingsymbol(
    kite: KiteConnect, symbol: str, strike: int, opt_type: str
) -> str | None:
    """Return the nearest-expiry tradingsymbol for symbol+strike+type, or None."""
    exchange = _EXCHANGE[symbol]
    df = _option_instruments(kite, exchange)
    if df is None:
        return None
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
    # On the symbol's own expiry day, skip the 0DTE contract (wild premium swings)
    if filtered.iloc[0]["expiry"] == today and len(filtered) > 1:
        _LOG.info(f"{symbol} expiry day — using next weekly {filtered.iloc[1]['expiry']}")
        return str(filtered.iloc[1]["tradingsymbol"])
    return str(filtered.iloc[0]["tradingsymbol"])


def _calc_qty(kite: KiteConnect, symbol: str, ltp: float, capital_fraction: float = 1.0) -> int:
    """Return total units to buy based on available cash (95% utilisation, min 1 lot).

    capital_fraction: 0.5 when splitting between two symbols, 1.0 for single trade.
    Falls back to 1 lot on zero balance or API error so order always fires.
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
    if available <= 0 or cost_per_lot <= 0:
        return lot
    usable   = available * 0.95 * capital_fraction
    max_lots = int(usable / cost_per_lot)
    lots     = max(1, max_lots)
    _LOG.info(
        f"Balance ₹{available:.0f} × 95% → "
        f"₹{usable:.0f} usable → {lots} lot(s) of {symbol} @ ₹{ltp} (₹{cost_per_lot:.0f}/lot)"
    )
    return lots * lot


def _fill_price(kite: KiteConnect, order_id: str, exchange: str, ts: str) -> tuple[float, str]:
    """Wait up to 8s for the fill. Returns (avg_price, "") if filled, else (0.0, reason).

    A rejected order returns Kite's reason. An order still open after 8s is
    cancelled so it doesn't lock margin. The caller must not place a GTT
    unless a real fill price is returned.
    """
    for _ in range(4):
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
    try:
        kite.cancel_order(variety=kite.VARIETY_REGULAR, order_id=order_id)
        _LOG.warning(f"Order {order_id} not filled in 8s — cancelled")
    except Exception as e:
        _LOG.warning(f"Could not cancel unfilled order {order_id}: {e}")
    return 0.0, "not filled within 8s — cancelled"


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
        return None   # unknown — callers must not treat an API error as "closed"
    return False


def contract_pnl(kite: KiteConnect, ts: str) -> float | None:
    """Day P&L (₹, realised + unrealised) of one contract, or None if unavailable."""
    try:
        for pos in kite.positions().get("net", []):
            if pos.get("tradingsymbol") == ts:
                return float(pos.get("pnl", 0))
    except Exception as e:
        _LOG.warning(f"Contract P&L fetch failed for {ts}: {e}")
    return None


def open_qty(kite: KiteConnect, ts: str) -> int | None:
    """Net quantity currently held in one contract (0 if flat), or None on API error."""
    try:
        for pos in kite.positions().get("net", []):
            if pos.get("tradingsymbol") == ts:
                return int(pos.get("quantity", 0))
        return 0
    except Exception as e:
        _LOG.warning(f"Quantity fetch failed for {ts}: {e}")
        return None


def delete_gtts_for(kite: KiteConnect, ts: str) -> tuple[list, list]:
    """Delete every ACTIVE GTT on one contract. Returns (deleted_ids, failures).

    Used once the bot is flat: a GTT left on a contract we no longer hold would
    SELL it when triggered and open a naked short.
    """
    deleted, failed = [], []
    try:
        gtts = kite.get_gtts()
    except Exception as e:
        return deleted, [f"GTT list failed: {e}"]
    for g in gtts:
        if g.get("status") != "active" or g.get("condition", {}).get("tradingsymbol") != ts:
            continue
        try:
            kite.delete_gtt(g["id"])
            deleted.append(g["id"])
        except Exception as e:
            failed.append(f"{g['id']}: {e}")
    return deleted, failed


def place_oco_gtt(kite: KiteConnect, symbol: str, ts: str, qty: int,
                  sl: float, target: float, last_price: float):
    """Place a fresh OCO GTT (SL / target) for a long option position."""
    return kite.place_gtt(
        trigger_type=kite.GTT_TYPE_OCO, tradingsymbol=ts,
        exchange=_EXCHANGE.get(symbol, "NFO"), trigger_values=[sl, target],
        last_price=last_price, orders=_oco_orders(kite, qty, sl, target),
    )


def sell_and_confirm(kite: KiteConnect, symbol: str, ts: str, qty: int,
                     discount: float = 0.01) -> tuple[bool, str]:
    """LIMIT SELL qty at LTP × (1 − discount) and wait up to 8 s for the fill.

    Returns (filled, info). An unfilled order is cancelled, so the caller can
    safely keep / restore the GTT and retry on the next scan.
    """
    exchange = _EXCHANGE.get(symbol, "NFO")
    try:
        ltp = float(kite.ltp([f"{exchange}:{ts}"])[f"{exchange}:{ts}"]["last_price"])
        oid = kite.place_order(
            variety=kite.VARIETY_REGULAR, exchange=exchange, tradingsymbol=ts,
            transaction_type=kite.TRANSACTION_TYPE_SELL, quantity=qty,
            product=kite.PRODUCT_NRML, order_type=kite.ORDER_TYPE_LIMIT,
            price=_tick(ltp * (1 - discount)),
        )
    except Exception as e:
        return False, f"sell order failed: {e}"
    price, reason = _fill_price(kite, oid, exchange, ts)
    return (True, f"sold {qty} @ ₹{price:.2f}") if price else (False, reason)


def position_pnl_pct(kite: KiteConnect, symbol: str) -> float | None:
    """Return unrealised P&L % for the open option position, or None if not found."""
    try:
        for pos in kite.positions().get("net", []):
            ts  = pos.get("tradingsymbol", "")
            qty = int(pos.get("quantity", 0))
            if ts.startswith(symbol) and qty != 0:
                avg = float(pos.get("average_price", 0))
                cur = float(pos.get("last_price", avg))
                return (cur - avg) / avg if avg > 0 else 0.0
    except Exception as e:
        _LOG.warning(f"P&L fetch failed: {e}")
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
    trailing: bool = False,
) -> dict:
    """
    Place ATM option order + GTT for the given signal.

    trailing=True: the GTT gets the SL and a far target; the returned "t1" is the
    take-profit activation level the caller manages (book half, trail the rest).

    Args:
        symbol:           "NIFTY" or "SENSEX"
        direction:        "CE" / "CALL" / "STRONG BUY CE" → CE
                          "PE" / "PUT"  / "STRONG BUY PE"  → PE
        spot:             current spot price (used to compute ATM strike)
        api_key:          Kite API key
        access_token:     Kite access token (daily)
        capital_fraction: fraction of available balance to use (0.5 = 50/50 split)

    Returns dict with keys:
        order_id, tradingsymbol, strike, opt_type, qty, entry_price, sl, target, gtt_id
    On error: {"error": "<reason>"}
    """
    if symbol not in LOT_SIZE:
        return {"error": f"Unsupported symbol: {symbol}. Supported: {list(LOT_SIZE)}"}

    opt_type = "CE" if any(x in direction.upper() for x in ("CE", "CALL", "BUY CE")) else "PE"
    exchange = _EXCHANGE[symbol]

    kite = _kite_client(api_key, access_token)

    with _order_lock():
        # 1. Resolve ATM tradingsymbol
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

        # Dynamic lot sizing based on available balance and capital fraction
        qty = _calc_qty(kite, symbol, option_ltp, capital_fraction)

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

    # 5. Confirm the fill — no GTT for a rejected / unfilled order
    entry_price, reason = _fill_price(kite, order_id, exchange, ts)
    if not entry_price:
        return {"error": f"Order {order_id} for {qty} × {ts} {reason}", "sent": True,
                "order_id": order_id, "tradingsymbol": ts}

    # 6. SL and target — chart-derived levels preferred; fall back to scalp/ride
    chart_levels = None
    if chart_sl and chart_target and spot:
        chart_levels = _spot_to_option_levels(spot, entry_price, chart_sl, chart_target, opt_type)
        if chart_levels:
            sl_price, target_price = chart_levels
            _LOG.info(
                f"GTT levels from chart: SL={sl_price} Target={target_price}"
                f" (spot SL={chart_sl:.1f} T={chart_target:.1f})"
            )

    if not chart_levels:
        if trade_mode == "ride":
            sl_price     = round(entry_price * (1 - _RIDE_STOP_PCT), 1)
            target_price = round(entry_price * (1 + _RIDE_TARGET_PCT), 1)
        else:
            sl_price     = _tick(entry_price * (1 - _FALLBACK_STOP_PCT))
            target_price = _tick(entry_price * (1 + _FALLBACK_TARGET_PCT))

    # 7. GTT two-leg (OCO): [sl_trigger, target_trigger]
    # trailing=True: target_price becomes the activation level (T1) handled by the
    # bot (half booked, rest trailed), so the GTT's target leg sits far away.
    t1_price  = target_price
    gtt_target = target_price
    if trailing:
        gtt_target = _tick(max(entry_price * _TRAIL_FAR_TARGET_X, t1_price * 1.5))
    gtt_id = None
    try:
        gtt_id = kite.place_gtt(
            trigger_type=kite.GTT_TYPE_OCO,
            tradingsymbol=ts,
            exchange=exchange,
            trigger_values=[sl_price, gtt_target],
            last_price=entry_price,
            orders=_oco_orders(kite, qty, sl_price, gtt_target),
        )
        _LOG.info(f"GTT placed: {gtt_id}  SL={sl_price}  Target={gtt_target}  T1={t1_price}")
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
        "target":        gtt_target,
        "t1":            t1_price,
        "gtt_id":        gtt_id,
        "trade_mode":    trade_mode,
    }


# ── GTT / position helpers for trailing take profit ──────────────────────────

_TRAIL_FAR_TARGET_X = 3.0   # GTT target placeholder while the bot trails (3× entry)


def _oco_orders(kite: KiteConnect, qty: int, sl: float, target: float) -> list[dict]:
    sl_lim, tgt_lim = gtt_leg_prices(sl, target)
    leg = lambda price: {"transaction_type": kite.TRANSACTION_TYPE_SELL, "quantity": qty,
                         "product": kite.PRODUCT_NRML, "order_type": kite.ORDER_TYPE_LIMIT,
                         "price": price}
    return [leg(sl_lim), leg(tgt_lim)]


def modify_oco_gtt(kite: KiteConnect, symbol: str, ts: str, gtt_id, qty: int,
                   sl: float, target: float, last_price: float):
    """Change an existing OCO GTT in place (no window without a stop loss)."""
    return kite.modify_gtt(
        trigger_id=int(gtt_id),
        trigger_type=kite.GTT_TYPE_OCO,
        tradingsymbol=ts,
        exchange=_EXCHANGE.get(symbol, "NFO"),
        trigger_values=[sl, target],
        last_price=last_price,
        orders=_oco_orders(kite, qty, sl, target),
    )


def sell_partial(kite: KiteConnect, symbol: str, ts: str, qty: int) -> str | None:
    """LIMIT SELL qty units of the bot's NRML position at LTP × 0.99. Returns order id."""
    exchange = _EXCHANGE.get(symbol, "NFO")
    try:
        ltp = float(kite.ltp([f"{exchange}:{ts}"])[f"{exchange}:{ts}"]["last_price"])
        return kite.place_order(
            variety=kite.VARIETY_REGULAR, exchange=exchange, tradingsymbol=ts,
            transaction_type=kite.TRANSACTION_TYPE_SELL, quantity=qty,
            product=kite.PRODUCT_NRML, order_type=kite.ORDER_TYPE_LIMIT,
            price=_tick(ltp * 0.99),
        )
    except Exception as e:
        _LOG.error(f"Partial sell failed for {ts}: {e}")
        return None


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
        open_pos = has_open_position(kite, symbol, product="MIS")
        if open_pos is None:
            return {"error": f"{symbol} position check failed — skipping RSI scalp"}
        if open_pos:
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
