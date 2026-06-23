"""
Kite Access Token Exchange Script
Run this script every morning to get a fresh Access Token.

Usage:
  python kite_token.py

Steps:
  1. Open the printed URL in your browser
  2. Login with your Zerodha credentials
  3. You'll be redirected to Google (or your redirect URL)
  4. Copy the `request_token` from the URL bar
  5. Paste it when prompted
  6. The script will print your new Access Token
  7. Paste the Access Token into the MarketPulse sidebar
"""

import hashlib
import webbrowser

API_KEY    = "plz6ik09bgb62mey"
API_SECRET = input("Enter your API Secret (from kite.zerodha.com/apps): ").strip()

login_url = f"https://kite.zerodha.com/connect/login?api_key={API_KEY}&v=3"
print(f"\n1. Opening login URL...\n   {login_url}")
try:
    webbrowser.open(login_url)
except Exception:
    print("   (Could not open browser automatically — open the URL manually)")

request_token = input("\n2. Paste the request_token from the redirect URL: ").strip()

checksum = hashlib.sha256(f"{API_KEY}{request_token}{API_SECRET}".encode()).hexdigest()

import urllib.request, json, urllib.parse

data = urllib.parse.urlencode({
    "api_key":       API_KEY,
    "request_token": request_token,
    "checksum":      checksum,
}).encode()

req = urllib.request.Request(
    "https://api.kite.trade/session/token",
    data=data,
    headers={"X-Kite-Version": "3", "Content-Type": "application/x-www-form-urlencoded"},
    method="POST",
)

try:
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read())
    access_token = result["data"]["access_token"]
    print(f"\n✅ Access Token: {access_token}")
    print("\n3. Paste this token into the MarketPulse sidebar → 'Zerodha Kite Connect' → Access Token")
except Exception as e:
    print(f"\n❌ Error: {e}")
    print("Make sure the request_token was copied correctly and used immediately (it expires in minutes).")
