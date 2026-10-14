"""
Quick Sharekhan connection test.
Run: /root/venv/bin/python scripts/test_sharekhan.py
"""
import json
import logging
import os
import pathlib
import sys

logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s: %(message)s")
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

# ── Load creds from .env ──────────────────────────────────────────────────────
_ENV_PATH = pathlib.Path(__file__).parent.parent / ".env"

def _read_env() -> dict:
    vals = {}
    if _ENV_PATH.exists():
        for line in _ENV_PATH.read_text().splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip().strip('"').strip("'")
    return vals

env = _read_env()

api_key      = os.environ.get("SHAREKHAN_API_KEY")      or env.get("SHAREKHAN_API_KEY", "")
access_token = os.environ.get("SHAREKHAN_ACCESS_TOKEN") or env.get("SHAREKHAN_ACCESS_TOKEN", "")
customer_id  = os.environ.get("SHAREKHAN_CUSTOMER_ID")  or env.get("SHAREKHAN_CUSTOMER_ID", "")
login_id     = os.environ.get("SHAREKHAN_LOGIN_ID")     or env.get("SHAREKHAN_LOGIN_ID", "")

print("=" * 55)
print("Sharekhan Connection Test")
print("=" * 55)
print(f"  API Key       : {'SET (...' + api_key[-6:]  + ')' if api_key      else 'NOT SET'}")
print(f"  Access Token  : {'SET (...' + access_token[-6:] + ')' if access_token else 'NOT SET'}")
print(f"  Customer ID   : {customer_id or 'NOT SET'}")
print(f"  Login ID      : {login_id    or 'NOT SET'}")
print()

if not (api_key and access_token):
    print("ERROR: API key or access token missing.")
    print("Run: /root/venv/bin/python scripts/sharekhan_token.py")
    sys.exit(1)

# ── Try import ────────────────────────────────────────────────────────────────
try:
    from SharekhanApi import SharekhanConnect
    print("SharekhanApi package found")
except ImportError:
    print("SharekhanApi not installed -- run: pip install shareconnect")
    sys.exit(1)

# ── Connect ───────────────────────────────────────────────────────────────────
try:
    client = SharekhanConnect(api_key, access_token)
    print("SharekhanConnect object created")
except Exception as e:
    print(f"SharekhanConnect failed: {e}")
    sys.exit(1)

# ── Discover available methods ────────────────────────────────────────────────
print("\n-- Available methods on SharekhanConnect --")
methods = [m for m in dir(client) if not m.startswith("_")]
print("  " + ", ".join(methods))

# ── Test 1: Script master (the call used in production) ──────────────────────
print("\n-- Test 1: FNO script master (production call) --")
try:
    master = client.master("FNO")
    if isinstance(master, list):
        print(f"  OK: master('FNO') returned {len(master)} instruments")
    else:
        print(f"  WARN: master('FNO') returned: {str(master)[:400]}")
except Exception as e:
    print(f"  FAIL: master('FNO'): {e}")

# ── Test 2: Try every plausible profile method ────────────────────────────────
print("\n-- Test 2: User profile --")
for name in ["getProfile", "get_profile", "profile", "userProfile", "getUserDetails"]:
    if not hasattr(client, name):
        continue
    try:
        res = getattr(client, name)()
        print(f"  OK: {name}() -> {str(res)[:300]}")
        break
    except Exception as e:
        print(f"  FAIL: {name}(): {e}")

# ── Test 3: Positions / holdings ──────────────────────────────────────────────
print("\n-- Test 3: Positions / holdings --")
for name in ["positions", "getPositions", "holdings", "getHoldings", "get_positions"]:
    if not hasattr(client, name):
        continue
    try:
        res = getattr(client, name)()
        count = len(res) if isinstance(res, list) else "?"
        print(f"  OK: {name}() -> {count} items")
        break
    except Exception as e:
        print(f"  FAIL: {name}(): {e}")

# ── Test 4: Orders ────────────────────────────────────────────────────────────
print("\n-- Test 4: Today's orders --")
for name in ["orders", "getOrders", "orderBook", "getOrderBook", "get_orders"]:
    if not hasattr(client, name):
        continue
    try:
        res = getattr(client, name)()
        count = len(res) if isinstance(res, list) else "?"
        print(f"  OK: {name}() -> {count} orders")
        break
    except Exception as e:
        print(f"  FAIL: {name}(): {e}")

print("\n" + "=" * 55)
print("Done.")
