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

# SENSEX spot + ATM strike
spot = kite.quote(["BSE:SENSEX"])["BSE:SENSEX"]["last_price"]
strike = int(round(spot / 100) * 100)
print(f"SENSEX spot : {spot:.0f}")
print(f"ATM strike  : {strike}")

# Nearest Thursday expiry
today = datetime.date.today()
days = (3 - today.weekday()) % 7
if days == 0 and datetime.datetime.now().hour >= 15:
    days = 7
expiry = today + datetime.timedelta(days=days)
print(f"Expiry      : {expiry}")

# Look up exact BFO symbol from Kite instruments
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
print(f"Kite symbol : {kite_sym}")

# --- Kite: BUY 20 @ ₹1 MIS ---
print(f"\n[KITE] BUY 20 x {kite_sym} @ ₹1 LIMIT BFO MIS ...")
try:
    oid = kite.place_order(
        tradingsymbol=kite_sym,
        exchange="BFO",
        transaction_type="BUY",
        quantity=20,
        order_type="LIMIT",
        product="MIS",
        price=1.0,
        variety="regular",
    )
    print(f"[KITE] OK  order_id={oid}")
except Exception as e:
    print(f"[KITE] FAIL  {e}")

# --- Sharekhan: BUY 20 @ ~₹1 MARGIN ---
print(f"\n[SHAREKHAN] BUY 20 x SENSEX {strike} CE @ ~₹1 MARGIN ...")
from screener.sharekhan_order import place_sk_order
# option_ltp=0.98 → limit rounds to ₹1.00
r = place_sk_order("SENSEX", "CE", spot, option_ltp=0.98)
if "error" in r:
    print(f"[SHAREKHAN] FAIL  {r['error']}")
else:
    print(f"[SHAREKHAN] OK  order_id={r['order_id']}  symbol={r['tradingsymbol']}")
    print(f"  SL  order_id={r['sl_order_id']}  trigger=₹{r['sl_price']}")
    print(f"  TGT order_id={r['target_order_id']}  limit=₹{r['target_price']}")

print("\n⚠  Orders at ₹1 will NOT fill. Cancel from Kite + Sharekhan apps.")
