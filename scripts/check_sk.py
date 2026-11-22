"""Check the Sharekhan access token in .env: one read-only call (fund limits), no orders.

  /root/venv/bin/python scripts/check_sk.py          # NSE cash limits (exchange code NC)
  /root/venv/bin/python scripts/check_sk.py NF       # another exchange code

Prints only the last characters of the key and token. HTTP 403 "Access Denied" with a fresh token
usually means the request's IP is not the one registered for the Sharekhan API app.
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).parent.parent
env = {}
for line in (ROOT / ".env").read_text().splitlines():
    line = line.strip()
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")

api_key = env.get("SHAREKHAN_API_KEY", "")
token = env.get("SHAREKHAN_ACCESS_TOKEN", "")
cid = env.get("SHAREKHAN_CUSTOMER_ID", "")
exchange = sys.argv[1] if len(sys.argv) > 1 else "NC"

print("Customer ID :", cid or "NOT SET")
print("API key     :", f"set (…{api_key[-4:]})" if api_key else "NOT SET")
print("Token       :", f"set, {len(token)} chars (…{token[-6:]})" if token else "NOT SET")
if not (api_key and token and cid):
    sys.exit("Missing SHAREKHAN_API_KEY / SHAREKHAN_ACCESS_TOKEN / SHAREKHAN_CUSTOMER_ID — run scripts/auto_sk_token.py")

from SharekhanApi import SharekhanConnect

# The SDK's 2nd positional argument is `state`, not the token: pass access_token by name.
sk = SharekhanConnect(api_key, access_token=token)
try:
    r = sk.funds(exchange, cid)
except Exception as e:
    sys.exit(f"✗ funds({exchange}) failed: {e}")

status = r.get("status") if isinstance(r, dict) else None
if status == 200:
    d = (r.get("data") or [{}])[0]
    print(f"✓ Token works — funds({exchange}): cash balance ₹{d.get('currentCashBalance')}")
else:
    print(f"✗ funds({exchange}) → {str(r)[:300]}")
    sys.exit(1)
