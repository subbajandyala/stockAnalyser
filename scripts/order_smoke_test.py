"""
Order smoke test — prove this machine can place API orders for the account in .env
(token valid, static IP whitelisted, segment enabled) without trading.

It places ONE real order that should not fill: a 1-lot LIMIT BUY of the nearest
futures contract at the day's lower circuit price, then cancels it ~2 s later.

  /root/venv/bin/python scripts/order_smoke_test.py                 # MCX CRUDEOILM (evenings)
  /root/venv/bin/python scripts/order_smoke_test.py --symbol CRUDEOIL
  /root/venv/bin/python scripts/order_smoke_test.py --exchange NFO --symbol NIFTY   # market hours

Reading the result:
  PASS  order accepted and cancelled            → API orders work from this IP
  PASS  rejected for funds / segment / margin   → API + IP work; the account needs money or the segment
  FAIL  "IP … not allowed" / "No IPs configured" → add this machine's IP on the Kite developer console
  FAIL  token / api_key errors                   → run auto_token.py
"""

import argparse
import datetime
import pathlib
import re
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from scripts.chart_bot import _env_setting, _kite_creds   # noqa: E402
from screener.kite_order import kite_for                  # noqa: E402

_IP_ERROR = re.compile(r"\bips?\b|no ips configured|not allowed to place orders", re.I)
_AUTH_ERRORS = ("api_key", "access_token", "token is invalid", "incorrect")


def _tick(price: float, tick: float) -> float:
    tick = tick or 0.05
    return round(round(price / tick) * tick, 2)


def main() -> None:
    ap = argparse.ArgumentParser(description="Place and cancel one far-from-market test order")
    ap.add_argument("--exchange", default="MCX")
    ap.add_argument("--symbol", default="CRUDEOILM", help="instrument name, e.g. CRUDEOILM, CRUDEOIL, NIFTY")
    args = ap.parse_args()

    api_key, token = _kite_creds()
    if not (api_key and token):
        sys.exit("FAIL  KITE_API_KEY / KITE_ACCESS_TOKEN missing in .env — run auto_token.py")
    name = _env_setting("KITE_NAME") or "this account"
    kite = kite_for(api_key, token)

    try:
        prof = kite.profile()
        print(f"Account      : {prof.get('user_id')} ({name})")
    except Exception as e:
        sys.exit(f"FAIL  token check: {e} — run auto_token.py")

    today = datetime.date.today()
    futs = [i for i in kite.instruments(args.exchange)
            if i.get("name") == args.symbol.upper() and i.get("instrument_type") == "FUT"
            and i.get("expiry") and i["expiry"] >= today]
    if not futs:
        sys.exit(f"FAIL  no {args.symbol} futures found on {args.exchange}")
    inst = min(futs, key=lambda i: i["expiry"])
    ts, tick = inst["tradingsymbol"], float(inst.get("tick_size") or 0.05)
    key = f"{args.exchange}:{ts}"
    q = kite.quote([key])[key]
    ltp = float(q.get("last_price") or 0)
    lower = float(q.get("lower_circuit_limit") or 0)
    price = _tick(lower if lower > 0 else ltp * 0.90, tick)
    print(f"Contract     : {ts} (expiry {inst['expiry']}), LTP {ltp:,.2f}, lower circuit {lower:,.2f}")
    qty = 1 if args.exchange == "MCX" else int(inst.get("lot_size") or 1)   # MCX qty is in lots
    print(f"Test order   : BUY 1 lot LIMIT {price:,.2f} (NRML) — far below market, cancelled right after")

    try:
        oid = kite.place_order(variety=kite.VARIETY_REGULAR, exchange=args.exchange, tradingsymbol=ts,
                               transaction_type=kite.TRANSACTION_TYPE_BUY, quantity=qty,
                               product=kite.PRODUCT_NRML, order_type=kite.ORDER_TYPE_LIMIT, price=price)
    except Exception as e:
        msg = str(e)
        low = msg.lower()
        if _IP_ERROR.search(msg):
            print(f"FAIL  Kite refused this machine's IP: {msg}")
            print("      Add this machine's IP under Profile → IP Whitelist on developers.kite.trade (Save).")
        elif any(s in low for s in _AUTH_ERRORS):
            print(f"FAIL  token / app problem: {msg} — run auto_token.py, check the app is active")
        else:
            print(f"PASS  API order reached Kite from this IP; Kite's reply: {msg}")
            print("      (not an IP/token problem — e.g. funds or segment; nothing was placed)")
        return

    print(f"Order placed : {oid}")
    time.sleep(2)
    status = ""
    try:
        status = kite.order_history(oid)[-1].get("status", "")
    except Exception:
        pass
    if status in ("REJECTED", "CANCELLED"):
        reason = kite.order_history(oid)[-1].get("status_message") or ""
        print(f"PASS  order reached the exchange checks and was {status}: {reason}")
        return
    if status == "COMPLETE":
        print("WARNING  the test order FILLED — close this position in Kite now:", ts)
        return
    try:
        kite.cancel_order(variety=kite.VARIETY_REGULAR, order_id=oid)
        print(f"PASS  order accepted ({status or 'OPEN'}) and cancelled — API orders work from this IP")
    except Exception as e:
        print(f"WARNING  order {oid} accepted but cancel failed ({e}) — cancel it in Kite → Orders")


if __name__ == "__main__":
    main()
