"""
Sharekhan copy of NIFTY Pulse's option trades — straight through Sharekhan's API (no Tradetron, no daily
click: the token comes from scripts/auto_sk_token.py on the droplet's main checkout, cron 08:05 IST).

  buy(env, kite_symbol, strike, side, expiry, qty, ref)   → (filled_qty, line)
  sell(env, row, qty, ref)                               → (sold_qty, line)

Each order is LIMIT, PULSE_SK_SLIP_PCT (default 2) % through the reference price so it fills at once; its
status is read after PULSE_SK_WAIT s (default 4) and an order still open is cancelled. The contract is
looked up in Sharekhan's NF master by NIFTY + strike + CE/PE + expiry (cached per day).

.env / .env.local: PULSE_SK=1 turns it on (only together with live Kite orders). PULSE_SK_PRODUCT =
MARGIN (default) / BIGTRADE / INVESTMENT. SHAREKHAN_API_KEY / SHAREKHAN_ACCESS_TOKEN /
SHAREKHAN_CUSTOMER_ID / SHAREKHAN_LOGIN_ID as written by auto_sk_token.py.
Check once with scripts/sk_fno_test.py during market hours before turning it on.
"""

from __future__ import annotations

import datetime
import json
import pathlib
import time

EXCHANGE = "NF"
_MASTER = pathlib.Path("/tmp/sk_master_nf.json")
_MASTER_TTL = 20 * 3600
_OTHER = ("BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT", "NIFTYIT")


def client(env: dict):
    from SharekhanApi import SharekhanConnect
    # the SDK's 2nd positional argument is `state`: the token must be passed by name
    return SharekhanConnect(env.get("SHAREKHAN_API_KEY", "").strip(),
                            access_token=env.get("SHAREKHAN_ACCESS_TOKEN", "").strip())


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


def _date(v) -> datetime.date | None:
    s = str(v or "").strip()[:10]
    for f in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d%b%Y", "%d-%b-%Y"):
        try:
            return datetime.datetime.strptime(s, f).date()
        except ValueError:
            pass
    return None


def find(sk, strike: float, side: str, expiry: datetime.date) -> dict | None:
    """Sharekhan's NIFTY option row for strike / CE|PE / expiry."""
    for r in master(sk):
        sym = str(_g(r, "tradingSymbol", "TradingSymbol", "symbol") or "").upper()
        if "NIFTY" not in sym or any(o in sym for o in _OTHER):
            continue
        if str(_g(r, "optionType", "OptionType") or "").upper() != side.upper():
            continue
        try:
            if abs(float(_g(r, "strikePrice", "StrikePrice", "strike") or -1) - float(strike)) > 0.01:
                continue
        except ValueError:
            continue
        if _date(_g(r, "expiry", "Expiry", "expiryDate")) == expiry:
            return r
    return None


def _px(x: float) -> str:
    return f"{max(0.05, round(round(x / 0.05) * 0.05, 2)):.2f}"


def params(env: dict, row: dict, buy: bool, qty: int, price: str) -> dict:
    cid = env.get("SHAREKHAN_CUSTOMER_ID", "").strip()
    exp = _date(_g(row, "expiry", "Expiry", "expiryDate"))
    return {
        "customerId": int(cid) if cid.isdigit() else cid,
        "scripCode": int(_g(row, "scripCode", "ScripCode", "scripcode")),
        "tradingSymbol": str(_g(row, "tradingSymbol", "TradingSymbol", "symbol")),
        "exchange": EXCHANGE,
        "transactionType": "B" if buy else "S",
        "quantity": int(qty),
        "disclosedQty": 0,
        "price": price,
        "triggerPrice": "0",
        "rmsCode": "ANY",
        "afterHour": "N",
        "orderType": "NORMAL",
        "channelUser": (env.get("SHAREKHAN_LOGIN_ID") or cid).strip(),
        "validity": "GFD",
        "requestType": "NEW",
        "productType": env.get("PULSE_SK_PRODUCT", "MARGIN").strip().upper(),
        "instrumentType": "OI",
        "strikePrice": str(_g(row, "strikePrice", "StrikePrice", "strike")),
        "optionType": str(_g(row, "optionType", "OptionType")).upper(),
        "expiry": exp.strftime("%d/%m/%Y") if exp else str(_g(row, "expiry", "Expiry")),
    }


def error(resp) -> str | None:
    if not isinstance(resp, dict):
        return f"unexpected reply: {str(resp)[:160]}"
    st = resp.get("status")
    if (isinstance(st, int) and st >= 400) or str(resp.get("message", "")).lower() == "error":
        return f"{st} {resp.get('errorType') or resp.get('message') or ''}".strip()
    return None


def order_id(resp) -> str:
    for r in _rows(resp) + [resp if isinstance(resp, dict) else {}]:
        v = _g(r, "orderId", "OrderId", "order_id", "sharekhanOrderID")
        if v:
            return str(v)
    return ""


def status(sk, cid, oid: str) -> str:
    try:
        rows = _rows(sk.exchange(EXCHANGE, cid, oid))
    except Exception:
        return ""
    for r in reversed(rows):
        s = _g(r, "orderStatus", "OrderStatus", "status", "Status")
        if s:
            return str(s)
    return ""


def _send(env: dict, sk, row: dict, buy: bool, qty: int, ref: float) -> tuple[int, str]:
    slip = float(env.get("PULSE_SK_SLIP_PCT", "2")) / 100
    price = _px(ref * (1 + slip if buy else 1 - slip))
    p = params(env, row, buy, qty, price)
    what = f"{'BUY' if buy else 'SELL'} {p['tradingSymbol']} × {qty} @ ₹{price}"
    try:
        resp = sk.placeOrder(p)
    except Exception as ex:
        return 0, f"{what}: order failed: {ex}"
    err = error(resp)
    if err:
        return 0, f"{what}: refused: {err}"
    oid = order_id(resp)
    if not oid:
        return qty, f"{what}: sent (no order id in the reply: {str(resp)[:120]}) — check Sharekhan"
    time.sleep(float(env.get("PULSE_SK_WAIT", "4")))
    st = status(sk, p["customerId"], oid)
    low = st.lower()
    if any(w in low for w in ("reject", "cancel")):
        return 0, f"{what}: {st} (order {oid})"
    if st and not any(w in low for w in ("executed", "traded", "complete", "filled")):
        try:
            sk.cancelOrder({**p, "orderId": oid, "requestType": "CANCEL"})
            return 0, f"{what}: not filled ({st}) — cancelled (order {oid})"
        except Exception as ex:
            return 0, f"{what}: still open ({st}), cancel failed: {ex} — check Sharekhan"
    return qty, f"{what}: {st or 'sent'} (order {oid})"


def buy(env: dict, strike: float, side: str, expiry: datetime.date, qty: int, ref: float) -> tuple[int, dict | None, str]:
    """→ (filled qty, Sharekhan row, line)."""
    try:
        sk = client(env)
        row = find(sk, strike, side, expiry)
    except Exception as ex:
        return 0, None, f"Sharekhan: lookup failed: {ex}"
    if not row:
        return 0, None, f"Sharekhan: NIFTY {strike:g} {side} {expiry} not in its NF master"
    got, line = _send(env, sk, row, True, qty, ref)
    return got, row, f"Sharekhan {'✓' if got else '✗'} {line}"


def sell(env: dict, row: dict, qty: int, ref: float) -> tuple[int, str]:
    try:
        sk = client(env)
    except Exception as ex:
        return 0, f"Sharekhan ✗ sell: {ex}"
    got, line = _send(env, sk, row, False, qty, ref)
    return got, f"Sharekhan {'✓' if got else '✗'} {line}"
