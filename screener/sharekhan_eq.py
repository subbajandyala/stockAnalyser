"""
Direct Sharekhan stock orders for the OI equity bot — no Tradetron, no daily click: the token comes from
scripts/auto_sk_token.py (cron 08:05 IST, TOTP).

  enter(e, name, side, qty, ref, tick) → (ok, line)   CE → BUY, PE → SELL short; NSE cash, intraday
  exit_(e, name, side, qty, ref, tick) → (ok, line)   closes it (long → SELL, short → BUY)

Orders are LIMIT, SK_EQ_SLIP_PCT (default 0.5) % through the reference price so they fill at once, like a
market order (exchanges refuse true market orders from algos). After SK_EQ_WAIT seconds (default 4) the
order's status is read; one still open is cancelled.

.env: SK_EQ_AUTO=1 turns it on. SK_EQ_PRODUCT = BIGTRADE (Sharekhan's intraday product, default) or
INVESTMENT (delivery). Uses SHAREKHAN_API_KEY / SHAREKHAN_ACCESS_TOKEN / SHAREKHAN_CUSTOMER_ID /
SHAREKHAN_LOGIN_ID. Test once with scripts/sk_eq_test.py before turning it on.
"""

from __future__ import annotations

import json
import pathlib
import time

EXCHANGE = "NC"                                   # Sharekhan code for NSE cash
_MASTER = pathlib.Path("/tmp/sk_master_nc.json")
_MASTER_TTL = 20 * 3600


def client(e: dict):
    from SharekhanApi import SharekhanConnect
    # the SDK's 2nd positional argument is `state`: the token must be passed by name
    return SharekhanConnect(e.get("SHAREKHAN_API_KEY", "").strip(),
                            access_token=e.get("SHAREKHAN_ACCESS_TOKEN", "").strip())


def _rows(resp) -> list:
    if isinstance(resp, list):
        return resp
    if isinstance(resp, dict):
        d = resp.get("data")
        if isinstance(d, list):
            return d
        if isinstance(d, dict):
            return [d]
    return []


def _g(row: dict, *keys):
    for k in keys:
        if row.get(k) not in (None, ""):
            return row[k]
    return None


def master(sk) -> list:
    if _MASTER.exists() and time.time() - _MASTER.stat().st_mtime < _MASTER_TTL:
        try:
            return json.loads(_MASTER.read_text())
        except Exception:
            pass
    rows = _rows(sk.master(EXCHANGE))
    if rows:
        _MASTER.write_text(json.dumps(rows))
    return rows


def scrip(sk, name: str) -> dict | None:
    """The NSE cash row of `name` (EQ series first)."""
    hits = [r for r in master(sk)
            if str(_g(r, "tradingSymbol", "TradingSymbol", "symbol") or "").upper() == name.upper()]
    hits.sort(key=lambda r: str(_g(r, "series", "Series") or "EQ").upper() != "EQ")
    return hits[0] if hits else None


def _px(x: float, tick: float) -> str:
    tick = tick or 0.05
    return f"{round(round(x / tick) * tick, 2):.2f}"


def _params(e: dict, row: dict, name: str, buy: bool, qty: int, price: str) -> dict:
    cid = e.get("SHAREKHAN_CUSTOMER_ID", "").strip()
    return {
        "customerId": int(cid) if cid.isdigit() else cid,
        "scripCode": int(_g(row, "scripCode", "ScripCode", "scripcode")),
        "tradingSymbol": name,
        "exchange": EXCHANGE,
        "transactionType": "B" if buy else "S",
        "quantity": int(qty),
        "disclosedQty": 0,
        "price": price,
        "triggerPrice": "0",
        "rmsCode": "ANY",
        "afterHour": "N",
        "orderType": "NORMAL",
        "channelUser": (e.get("SHAREKHAN_LOGIN_ID") or cid).strip(),
        "validity": "GFD",
        "requestType": "NEW",
        "productType": e.get("SK_EQ_PRODUCT", "BIGTRADE").strip().upper(),
    }


def _error(resp) -> str | None:
    if not isinstance(resp, dict):
        return f"unexpected reply: {str(resp)[:160]}"
    st = resp.get("status")
    if (isinstance(st, int) and st >= 400) or str(resp.get("message", "")).lower() == "error":
        return f"{st} {resp.get('errorType') or resp.get('message') or ''}".strip()
    return None


def _order_id(resp) -> str:
    rows = _rows(resp) or [resp if isinstance(resp, dict) else {}]
    for r in rows + [resp if isinstance(resp, dict) else {}]:
        v = _g(r, "orderId", "OrderId", "order_id", "sharekhanOrderID")
        if v:
            return str(v)
    return ""


def _status(sk, cid: str, oid: str) -> str:
    """Latest status text of the order ('' when it cannot be read)."""
    try:
        rows = _rows(sk.exchange(EXCHANGE, cid, oid))
    except Exception:
        return ""
    for r in reversed(rows):
        s = _g(r, "orderStatus", "OrderStatus", "status", "Status")
        if s:
            return str(s)
    return ""


def _send(e: dict, name: str, buy: bool, qty: int, ref: float, tick: float) -> tuple[bool, str]:
    if qty <= 0 or ref <= 0:
        return False, "qty / price 0"
    try:
        sk = client(e)
        row = scrip(sk, name)
        if not row:
            return False, f"{name} not in Sharekhan's NC master"
        slip = float(e.get("SK_EQ_SLIP_PCT", "0.5")) / 100
        price = _px(ref * (1 + slip if buy else 1 - slip), tick)
        p = _params(e, row, name, buy, qty, price)
        resp = sk.placeOrder(p)
    except Exception as ex:
        return False, f"order failed: {ex}"
    err = _error(resp)
    if err:
        return False, f"refused: {err}"
    oid = _order_id(resp)
    if not oid:
        return True, f"{'BUY' if buy else 'SELL'} {qty} @ ₹{price} sent (no order id in reply: {str(resp)[:120]})"
    time.sleep(float(e.get("SK_EQ_WAIT", "4")))
    st = _status(sk, p["customerId"], oid)
    low = st.lower()
    if any(w in low for w in ("reject", "cancel")):
        return False, f"{'BUY' if buy else 'SELL'} {qty} @ ₹{price} {st} (order {oid})"
    if st and not any(w in low for w in ("fully executed", "executed", "traded", "complete", "filled")):
        try:
            sk.cancelOrder({**p, "orderId": oid, "requestType": "CANCEL"})
            return False, f"{'BUY' if buy else 'SELL'} {qty} @ ₹{price} not filled ({st}) — cancelled (order {oid})"
        except Exception as ex:
            return False, f"{'BUY' if buy else 'SELL'} {qty} @ ₹{price} still open ({st}), cancel failed: {ex}"
    return True, f"{'BUY' if buy else 'SELL'} {qty} @ ₹{price} {st or 'sent'} (order {oid})"


def enter(e: dict, name: str, side: str, qty: int, ref: float, tick: float = 0.05) -> tuple[bool, str]:
    ok, txt = _send(e, name, side == "CE", qty, ref, tick)
    return ok, f"Sharekhan {name} {'long' if side == 'CE' else 'short'}: {'✓' if ok else '✗'} {txt}"


def exit_(e: dict, name: str, side: str, qty: int, ref: float, tick: float = 0.05) -> tuple[bool, str]:
    ok, txt = _send(e, name, side != "CE", qty, ref, tick)
    return ok, f"Sharekhan {name} exit: {'✓' if ok else '✗'} {txt}"
