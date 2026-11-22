"""One-time live check of direct Sharekhan stock orders (screener/sharekhan_eq.py) — run during market hours.

  /root/venv/bin/python scripts/sk_eq_test.py ITC              # 1-share BUY limit 2 % BELOW the price (won't fill),
                                                               # prints Sharekhan's raw replies, then cancels it
  /root/venv/bin/python scripts/sk_eq_test.py ITC --roundtrip  # 1 share BUY then SELL at once, the way the bot does
                                                               # (costs brokerage on 1 share)

Prints the raw replies so the field names can be checked. Turn SK_EQ_AUTO=1 on only after both work.
"""

import argparse
import json
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from screener import sharekhan_eq as skq  # noqa: E402


def env() -> dict:
    e = {}
    for p in (ROOT / ".env", ROOT / ".env.local"):
        if p.exists():
            for line in p.read_text().splitlines():
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    e[k.strip()] = v.strip().strip('"').strip("'")
    return e


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("name", nargs="?", default="ITC")
    ap.add_argument("--roundtrip", action="store_true")
    a = ap.parse_args()
    e = env()
    name = a.name.upper()

    from kiteconnect import KiteConnect
    kite = KiteConnect(e.get("KITE_API_KEY", ""))
    kite.set_access_token(e.get("KITE_ACCESS_TOKEN", ""))
    ltp = float(kite.ltp([f"NSE:{name}"])[f"NSE:{name}"]["last_price"])
    print(f"{name} LTP ₹{ltp}  · product {e.get('SK_EQ_PRODUCT', 'BIGTRADE')}")

    sk = skq.client(e)
    row = skq.scrip(sk, name)
    print("master row:", json.dumps(row)[:400] if row else "NOT FOUND")
    if not row:
        sys.exit(1)

    if a.roundtrip:
        ok, line = skq.enter(e, name, "CE", 1, ltp)
        print(line)
        if not ok:
            sys.exit(1)
        ok, line = skq.exit_(e, name, "CE", 1, ltp)
        print(line)
        print("✓ round trip done" if ok else "✗ exit failed — SELL 1 share at Sharekhan by hand")
        return

    price = skq._px(ltp * 0.98, 0.05)
    p = skq._params(e, row, name, True, 1, price)
    print("placeOrder params:", json.dumps({k: v for k, v in p.items() if k not in ("customerId", "channelUser")}))
    resp = sk.placeOrder(p)
    print("placeOrder reply:", json.dumps(resp)[:600])
    err = skq._error(resp)
    if err:
        print(f"✗ refused: {err}")
        sys.exit(1)
    oid = skq._order_id(resp)
    print("order id:", oid or "NOT FOUND in reply")
    if not oid:
        sys.exit("✗ cancel it at Sharekhan by hand")
    time.sleep(3)
    try:
        print("status reply:", json.dumps(sk.exchange(skq.EXCHANGE, p["customerId"], oid))[:600])
    except Exception as ex:
        print("status read failed:", ex)
    resp = sk.cancelOrder({**p, "orderId": oid, "requestType": "CANCEL"})
    print("cancelOrder reply:", json.dumps(resp)[:400])
    print("status after cancel:", skq._status(sk, p["customerId"], oid) or "n/a")


if __name__ == "__main__":
    main()
