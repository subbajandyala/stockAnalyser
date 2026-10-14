"""
Sharekhan connection test — uses correct method signatures from SDK.
Run: /root/venv/bin/python scripts/test_sharekhan.py
"""
import json
import logging
import os
import pathlib
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

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
print(f"  API Key      : SET (...{api_key[-6:]})" if api_key else "  API Key      : NOT SET")
print(f"  AccessToken  : SET (...{access_token[-6:]})" if access_token else "  AccessToken  : NOT SET")
print(f"  Customer ID  : {customer_id}")
print(f"  Login ID     : {login_id}")

if not (api_key and access_token):
    print("\nERROR: creds missing. Run: /root/venv/bin/python scripts/sharekhan_token.py")
    sys.exit(1)

from SharekhanApi import SharekhanConnect
client = SharekhanConnect(api_key, access_token)
print("\nSharekhanConnect object created")

# ── Test 1: funds (simplest authenticated call) ───────────────────────────────
print("\n-- Test 1: funds (simplest auth check) --")
try:
    res = client.funds(customer_id)
    print(f"  Raw: {str(res)[:500]}")
    if isinstance(res, dict):
        status = res.get("status") or res.get("Status") or ""
        if isinstance(status, int) and status >= 400:
            print(f"  HTTP {status} -- token EXPIRED or API not activated")
            print("  --> Re-run: /root/venv/bin/python scripts/sharekhan_token.py")
        elif "error" in str(res).lower() or "invalid" in str(res).lower() or "unauthori" in str(res).lower():
            print("  Auth error -- token expired or API trading not activated on account")
            print("  --> Call Sharekhan support: 1800-22-7500 to enable API trading")
        else:
            print("  Token is VALID")
    elif res is None or res == b'' or res == '':
        print("  Empty response (204) -- API trading likely NOT activated on this account")
        print("  --> Call Sharekhan support: 1800-22-7500 to enable API trading for JANDYALA1987")
except Exception as e:
    print(f"  FAIL: {e}")

# ── Test 2: holdings ──────────────────────────────────────────────────────────
print("\n-- Test 2: holdings --")
try:
    res = client.holdings(customer_id)
    if isinstance(res, list):
        print(f"  OK: {len(res)} holdings")
        for h in res[:3]:
            print(f"    {h}")
    else:
        print(f"  Raw: {str(res)[:300]}")
except Exception as e:
    print(f"  FAIL: {e}")

# ── Test 3: trades ────────────────────────────────────────────────────────────
print("\n-- Test 3: trades --")
try:
    res = client.trades(customer_id)
    if isinstance(res, list):
        print(f"  OK: {len(res)} trades today")
    else:
        print(f"  Raw: {str(res)[:300]}")
except Exception as e:
    print(f"  FAIL: {e}")

# ── Test 4: master FNO ────────────────────────────────────────────────────────
print("\n-- Test 4: FNO master (instrument list) --")
try:
    res = client.master("FNO")
    if isinstance(res, list) and res:
        print(f"  OK: {len(res)} FNO instruments")
    elif res is None or res == [] or res == b'':
        print("  Empty -- API access not enabled or token expired")
    else:
        print(f"  Raw: {str(res)[:300]}")
except Exception as e:
    print(f"  FAIL: {e}")

print("\n" + "=" * 55)
print("SUMMARY:")
print("  If all tests show 'Empty' or auth errors, Sharekhan API trading")
print("  is not enabled on this account.")
print("  Call: 1800-22-7500 and request API trading activation for")
print(f"  Account: {login_id}  /  Customer ID: {customer_id}")
print("=" * 55)
