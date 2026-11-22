"""One-time live check of the Sharekhan copy of NIFTY Pulse (core/sharekhan_fno.py) — run in market hours.

  /root/venv/bin/python scripts/sk_fno_test.py              # finds the NIFTY ATM CE the bot would use in Sharekhan's
                                                            # master, places 1 lot BUY 20 % below the ask (won't fill),
                                                            # prints Sharekhan's raw replies, then cancels it
  /root/venv/bin/python scripts/sk_fno_test.py --roundtrip  # 1 lot BUY then SELL at once, the way the bot does
                                                            # (costs the spread + brokerage on 1 lot)

Turn PULSE_SK=1 on only after both work.
"""

import argparse
import datetime
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import pulse as P           # noqa: E402
from core import sharekhan_fno as skf  # noqa: E402
from scripts.pulse_bot import load_env, holidays  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--roundtrip", action="store_true")
    ap.add_argument("--side", default="CE", choices=["CE", "PE"])
    a = ap.parse_args()
    env = load_env()
    cfg = P.Config.from_env(env)

    from kiteconnect import KiteConnect
    kite = KiteConnect(env.get("KITE_API_KEY", ""))
    kite.set_access_token(env.get("KITE_ACCESS_TOKEN", ""))
    today = datetime.date.today()
    nfo = [r for r in kite.instruments("NFO") if r["name"] == "NIFTY"]
    exp = P.choose_expiry(sorted({r["expiry"] for r in nfo if r["instrument_type"] in ("CE", "PE")}), today, cfg,
                          holidays())
    spot = kite.ltp(["NSE:NIFTY 50"])["NSE:NIFTY 50"]["last_price"]
    strike = round(spot / 50) * 50
    inst = next(r for r in nfo if r["expiry"] == exp and r["instrument_type"] == a.side and float(r["strike"]) == strike)
    key = f"NFO:{inst['tradingsymbol']}"
    q = kite.quote([key])[key]
    ask = q["depth"]["sell"][0]["price"] or q["last_price"]
    lot = int(inst["lot_size"])
    print(f"NIFTY {spot} → {inst['tradingsymbol']} ask ₹{ask} lot {lot} · expiry {exp} · "
          f"product {env.get('PULSE_SK_PRODUCT', 'MARGIN')}")

    sk = skf.client(env)
    row = skf.find(sk, strike, a.side, exp)
    print("Sharekhan row:", json.dumps(row)[:400] if row else "NOT FOUND")
    if not row:
        m = skf.master(sk)
        print(f"master: {len(m)} rows; first NIFTY rows:",
              json.dumps([r for r in m if "NIFTY" in str(r).upper()][:3])[:800])
        sys.exit(1)

    if a.roundtrip:
        got, row, line = skf.buy(env, strike, a.side, exp, lot, ask)
        print(line)
        if not got:
            sys.exit(1)
        bid = kite.quote([key])[key]["depth"]["buy"][0]["price"] or ask
        sold, line = skf.sell(env, row, got, bid)
        print(line)
        print("✓ round trip done" if sold == got else "✗ sell failed — SELL it in Sharekhan by hand")
        return

    price = skf._px(ask * 0.8)
    p = skf.params(env, row, True, lot, price)
    print("placeOrder params:", json.dumps({k: v for k, v in p.items() if k not in ("customerId", "channelUser")}))
    resp = sk.placeOrder(p)
    print("placeOrder reply:", json.dumps(resp)[:600])
    err = skf.error(resp)
    if err:
        sys.exit(f"✗ refused: {err}")
    oid = skf.order_id(resp)
    print("order id:", oid or "NOT FOUND in the reply")
    if not oid:
        sys.exit("✗ cancel it in Sharekhan by hand")
    time.sleep(3)
    try:
        print("status reply:", json.dumps(sk.exchange(skf.EXCHANGE, p["customerId"], oid))[:600])
    except Exception as ex:
        print("status read failed:", ex)
    print("cancelOrder reply:", json.dumps(sk.cancelOrder({**p, "orderId": oid, "requestType": "CANCEL"}))[:400])
    print("status after cancel:", skf.status(sk, p["customerId"], oid) or "n/a")


if __name__ == "__main__":
    main()
