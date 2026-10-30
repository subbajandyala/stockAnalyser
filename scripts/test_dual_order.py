"""
Dual-broker connectivity test: SENSEX ATM CE on Kite (BFO) + Sharekhan.
Places a ₹1 LIMIT order — will NOT fill. Cancel from broker apps after confirming IDs.
Run: /root/venv/bin/python scripts/test_dual_order.py
"""
import datetime, json, pathlib, sys, time as _time
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

def _read_env():
    vals = {}
    for line in (pathlib.Path(__file__).parent.parent / ".env").read_text().splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip().strip('"').strip("'")
    return vals

env = _read_env()

from kiteconnect import KiteConnect
kite = KiteConnect(api_key=env["KITE_API_KEY"])
kite.set_access_token(env["KITE_ACCESS_TOKEN"])

# ── Show enabled segments ──────────────────────────────────────────────────────
profile = kite.profile()
print(f"Account     : {profile['user_name']} ({profile['email']})")
print(f"Exchanges   : {profile.get('exchanges', [])}")
print(f"Order types : {profile.get('order_types', [])}")
print(f"Products    : {profile.get('products', [])}")
print()

# ── SENSEX spot + ATM strike ───────────────────────────────────────────────────
spot = kite.quote(["BSE:SENSEX"])["BSE:SENSEX"]["last_price"]
strike = int(round(spot / 100) * 100)
print(f"SENSEX spot : {spot:.0f}")
print(f"ATM strike  : {strike}")

# ── Nearest Thursday expiry ───────────────────────────────────────────────────
today = datetime.date.today()
days = (3 - today.weekday()) % 7
if days == 0 and datetime.datetime.now().hour >= 15:
    days = 7
expiry = today + datetime.timedelta(days=days)
print(f"Expiry      : {expiry}")

# ── Look up exact BFO symbol ──────────────────────────────────────────────────
CACHE = pathlib.Path("/tmp/kite_instr_bfo.json")
if not CACHE.exists() or _time.time() - CACHE.stat().st_mtime > 86400:
    print("Fetching BFO instruments ...")
    instr = kite.instruments("BFO")
    CACHE.write_text(json.dumps(instr, default=str))
else:
    instr = json.loads(CACHE.read_text())

candidates = [
    i for i in instr
    if "SENSEX" in str(i.get("tradingsymbol", "")).upper()
    and i.get("instrument_type") == "CE"
    and i.get("strike") == strike
    and str(i.get("expiry", ""))[:10] == str(expiry)
]
if not candidates:
    print(f"ERROR: No BFO instrument for SENSEX {strike} CE expiry {expiry}")
    sys.exit(1)

kite_sym = candidates[0]["tradingsymbol"]
print(f"Kite symbol : {kite_sym}\n")

# ── KITE: try NRML first, then MIS ───────────────────────────────────────────
for product in ["NRML", "MIS"]:
    print(f"[KITE] BUY 20 x {kite_sym} @ ₹1 LIMIT BFO {product} ...")
    try:
        oid = kite.place_order(
            tradingsymbol=kite_sym,
            exchange="BFO",
            transaction_type="BUY",
            quantity=20,
            order_type="LIMIT",
            product=product,
            price=1.0,
            variety="regular",
        )
        print(f"[KITE] OK  order_id={oid}  (product={product})")
        break
    except Exception as e:
        print(f"[KITE] FAIL ({product})  {e}")

# ── SHAREKHAN: try NRML, then MARGIN ─────────────────────────────────────────
print()
from screener.sharekhan_order import place_sk_order
import screener.sharekhan_order as _sk_mod

for pt in ["NRML", "MARGIN", "MIS"]:
    print(f"[SHAREKHAN] BUY 20 x SENSEX {strike} CE @ ~₹1  productType={pt} ...")
    # Temporarily override productType for this test
    _orig = None
    try:
        import screener.sharekhan_order as _m
        _orig = None  # we'll patch via env override not possible — just call directly
    except Exception:
        pass

    # Inline order with patched productType
    try:
        from screener.sharekhan_order import _sk_creds, _sk_client, _atm_strike, _nearest_expiry, _load_master, _find_scrip, _LOT_SIZE, _SK_EXCHANGE, _INSTR_TYPE
        creds = _sk_creds()
        client = _sk_client(creds["SHAREKHAN_API_KEY"], creds["SHAREKHAN_ACCESS_TOKEN"])
        sk_strike = _atm_strike(spot, "SENSEX")
        exp_dt = _nearest_expiry("SENSEX")
        exp_str = exp_dt.strftime("%d/%m/%Y")
        master = _load_master(client)
        scrip = _find_scrip(master, "SENSEX", sk_strike, "CE", exp_dt)
        scrip_code = (scrip.get("scripCode") or scrip.get("ScripCode") or 0) if scrip else 0
        ts = (scrip.get("tradingSymbol") or scrip.get("TradingSymbol") or f"SENSEX{exp_dt.strftime('%d%b%y').upper()}{sk_strike}CE") if scrip else f"SENSEX{exp_dt.strftime('%d%b%y').upper()}{sk_strike}CE"
        cid = creds["SHAREKHAN_CUSTOMER_ID"]
        lid = creds["SHAREKHAN_LOGIN_ID"] or cid
        params = {
            "customerId": cid, "scripCode": scrip_code, "tradingSymbol": ts,
            "exchange": "BF", "transactionType": "B", "quantity": 20,
            "disclosedQty": 0, "price": "1.0", "triggerPrice": "0",
            "rmsCode": "ANY", "afterHour": "N", "orderType": "NORMAL",
            "channelUser": lid, "validity": "GFD", "requestType": "NEW",
            "productType": pt, "instrumentType": "OI",
            "strikePrice": str(sk_strike), "optionType": "CE", "expiry": exp_str,
        }
        result = client.placeOrder(params)
        print(f"  Raw: {result}")
        oid = result.get("orderId") or result.get("order_id") or result.get("BrokerOrderId") or ""
        if oid:
            print(f"[SHAREKHAN] OK  order_id={oid}  (productType={pt})")
            break
        elif isinstance(result, dict) and (result.get("status", 0) >= 400 or result.get("message") == "error"):
            print(f"[SHAREKHAN] FAIL ({pt})  {result}")
        else:
            print(f"[SHAREKHAN] FAIL ({pt})  no order_id in response")
    except Exception as e:
        print(f"[SHAREKHAN] FAIL ({pt})  {e}")

print("\n⚠  Cancel any placed orders from Kite + Sharekhan apps.")
