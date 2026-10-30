"""Diagnose Sharekhan API authentication — shows request headers and tries raw REST call."""
import pathlib, sys, json
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

env = {}
for line in (pathlib.Path(__file__).parent.parent / ".env").read_text().splitlines():
    line = line.strip()
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")

api_key   = env.get("SHAREKHAN_API_KEY", "")
token     = env.get("SHAREKHAN_ACCESS_TOKEN", "")
cid       = env.get("SHAREKHAN_CUSTOMER_ID", "")
login_id  = env.get("SHAREKHAN_LOGIN_ID", "")

print("Customer ID :", cid)
print("Login ID    :", login_id)
print("Token len   :", len(token), "  ends:", token[-12:])

# ── Check what headers the SDK builds ────────────────────────────────────────
from SharekhanApi import SharekhanConnect
sk = SharekhanConnect(api_key, token)
try:
    hdrs = sk.requestHeaders()
    print("\nSDK request headers:", json.dumps(hdrs, indent=2))
except Exception as e:
    print(f"\nrequestHeaders() error: {e}")
    # Try to inspect the object directly
    for attr in ["Authorization", "access_token", "api_key", "feed_token"]:
        v = getattr(sk, attr, None)
        if v:
            s = str(v)
            print(f"  sk.{attr} = ...{s[-12:]}" if len(s) > 20 else f"  sk.{attr} = {v}")

# ── Raw REST call — try different auth header formats ─────────────────────────
import requests
BASE = "https://api.sharekhan.com"

print("\n── Raw REST tests ──────────────────────────────────────────────")
for auth_style in [
    {"Authorization": f"Bearer {token}", "api_key": api_key},
    {"Authorization": token, "api_key": api_key},
    {"access_token": token, "api_key": api_key},
]:
    hdrs = {"Content-Type": "application/json", "Accept": "application/json", **auth_style}
    label = list(auth_style.keys())[0]
    try:
        r = requests.get(
            f"{BASE}/skapi/customer/v1/holdings/",
            headers=hdrs,
            params={"customerId": cid},
            timeout=10,
        )
        print(f"  [{label}] HTTP {r.status_code}  body: {r.text[:120]}")
        if r.ok:
            print("  ✓ This auth format works!")
            break
    except Exception as e:
        print(f"  [{label}] error: {e}")
