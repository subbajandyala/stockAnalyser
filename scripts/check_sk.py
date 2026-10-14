"""Check Sharekhan credentials and available SDK methods."""
import pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

env = {}
for line in (pathlib.Path(__file__).parent.parent / ".env").read_text().splitlines():
    line = line.strip()
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")

print("SHAREKHAN_CUSTOMER_ID :", env.get("SHAREKHAN_CUSTOMER_ID", "NOT SET"))
print("SHAREKHAN_LOGIN_ID    :", env.get("SHAREKHAN_LOGIN_ID", "NOT SET"))
print("SHAREKHAN_API_KEY     :", env.get("SHAREKHAN_API_KEY", "NOT SET")[:8] + "...")

from SharekhanApi import SharekhanConnect
sk = SharekhanConnect(env["SHAREKHAN_API_KEY"], env["SHAREKHAN_ACCESS_TOKEN"])
methods = [m for m in dir(sk) if not m.startswith("_")]
print("\nSDK methods:", methods)

# Try any read-only method
cid = env.get("SHAREKHAN_CUSTOMER_ID", "")
for fn in ["limits", "positions", "holdings", "orders", "trades"]:
    if fn in methods:
        print(f"\nTrying {fn}({cid}) ...")
        try:
            r = getattr(sk, fn)(cid)
            print(f"  Result: {r}")
        except Exception as e:
            print(f"  Error : {e}")
        break
