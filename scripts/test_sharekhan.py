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
print(f"  API Key       : {'SET (…' + api_key[-6:]  + ')' if api_key      else 'NOT SET ❌'}")
print(f"  Access Token  : {'SET (…' + access_token[-6:] + ')' if access_token else 'NOT SET ❌'}")
print(f"  Customer ID   : {customer_id or 'NOT SET ❌'}")
print(f"  Login ID      : {login_id    or 'NOT SET ❌'}")
print()

if not (api_key and access_token):
    print("ERROR: API key or access token missing.")
    print("Run: /root/venv/bin/python scripts/sharekhan_token.py")
    sys.exit(1)

# ── Try import ────────────────────────────────────────────────────────────────
try:
    from SharekhanApi import SharekhanConnect
    print("✅ SharekhanApi package found")
except ImportError:
    print("❌ SharekhanApi not installed — run: pip install shareconnect")
    sys.exit(1)

# ── Connect ───────────────────────────────────────────────────────────────────
try:
    client = SharekhanConnect(api_key, access_token)
    print("✅ SharekhanConnect object created")
except Exception as e:
    print(f"❌ SharekhanConnect failed: {e}")
    sys.exit(1)

# ── Test 1: Get profile ───────────────────────────────────────────────────────
print("\n── Test 1: Get user profile ──")
try:
    profile = client.get_profile()
    print(f"  Raw response: {json.dumps(profile, indent=2)[:400]}")
    if isinstance(profile, dict):
        status = profile.get("status") or profile.get("Status") or ""
        name   = (profile.get("clientName") or profile.get("name")
                  or profile.get("data", {}).get("clientName") if isinstance(profile.get("data"), dict) else "")
        if isinstance(status, int) and status >= 400:
            print(f"  ❌ HTTP {status} — token likely expired. Re-run sharekhan_token.py")
        elif "error" in str(profile).lower() or "invalid" in str(profile).lower():
            print(f"  ❌ Auth error — token likely expired. Re-run sharekhan_token.py")
        else:
            print(f"  ✅ Connected — name: {name or '(see raw above)'}")
except Exception as e:
    print(f"  ❌ get_profile failed: {e}")

# ── Test 2: Get positions ─────────────────────────────────────────────────────
print("\n── Test 2: Get positions ──")
try:
    pos = client.positions()
    count = len(pos) if isinstance(pos, list) else "?"
    print(f"  ✅ Positions response received — {count} positions")
    if isinstance(pos, list) and pos:
        print(f"  First: {json.dumps(pos[0], indent=2)[:300]}")
except Exception as e:
    print(f"  ❌ positions() failed: {e}")

# ── Test 3: Get orders ────────────────────────────────────────────────────────
print("\n── Test 3: Get today's orders ──")
try:
    orders = client.orders()
    count = len(orders) if isinstance(orders, list) else "?"
    print(f"  ✅ Orders response received — {count} orders today")
except Exception as e:
    print(f"  ❌ orders() failed: {e}")

print("\n" + "=" * 55)
print("Done.")
