"""
Sharekhan daily token refresh (run once before market hours).

Steps:
  1. Prints the login URL.
  2. You open it in a browser, log in, and are redirected to your
     Redirect URL with ?code=<request_token>&state=... in the address bar.
  3. Paste that full redirect URL (or just the request_token value) here.
  4. The script exchanges it for an access token and saves it to .env.

Run:
  /root/venv/bin/python scripts/sharekhan_token.py
"""

import os
import pathlib
import re
import sys
import urllib.parse

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


def _write_env(key: str, value: str) -> None:
    text = _ENV_PATH.read_text() if _ENV_PATH.exists() else ""
    pattern = rf"^{key}=.*$"
    new_line = f"{key}={value}"
    if re.search(pattern, text, re.MULTILINE):
        text = re.sub(pattern, new_line, text, flags=re.MULTILINE)
    else:
        text = text.rstrip() + f"\n{new_line}\n"
    _ENV_PATH.write_text(text)


def main() -> None:
    try:
        from SharekhanApi import SharekhanConnect
    except ImportError:
        print("ERROR: SharekhanApi not found — run: pip install shareconnect")
        sys.exit(1)

    env = _read_env()
    api_key    = env.get("SHAREKHAN_API_KEY", "")
    secret_key = env.get("SHAREKHAN_SECURE_KEY", "")

    if not api_key:
        api_key = input("Enter SHAREKHAN_API_KEY: ").strip()
    if not secret_key:
        secret_key = input("Enter SHAREKHAN_SECURE_KEY: ").strip()

    if not (api_key and secret_key):
        print("ERROR: API key and Secure key are required.")
        sys.exit(1)

    sk = SharekhanConnect(api_key)
    login_url = sk.login_url()

    print("\n" + "="*60)
    print("STEP 1 — Open this URL in your browser and log in:")
    print()
    print(login_url)
    print()
    print("STEP 2 — After login you will be redirected. Copy the full")
    print("         URL from the browser address bar and paste it below.")
    print("         (It contains ?code=... or ?request_token=...)")
    print("="*60 + "\n")

    redirect_input = input("Paste the redirect URL (or just the token): ").strip()

    # Extract request_token from the redirect URL or treat the input as the token
    request_token = ""
    if redirect_input.startswith("http"):
        parsed = urllib.parse.urlparse(redirect_input)
        qs = urllib.parse.parse_qs(parsed.query)
        request_token = (
            qs.get("code", qs.get("request_token", [""]))[0]
        )
    else:
        request_token = redirect_input

    if not request_token:
        print("ERROR: Could not extract request_token from the URL.")
        sys.exit(1)

    print(f"\nRequest token: {request_token[:12]}…")
    print("Exchanging for access token …")

    try:
        session_data = sk.generate_session_without_versionId(request_token, secret_key)
    except Exception as e:
        print(f"ERROR: generate_session failed — {e}")
        sys.exit(1)

    state = session_data.get("state") or session_data.get("State") or ""

    try:
        token_data = sk.get_access_token(api_key, session_data, state)
    except Exception as e:
        # Some SDK versions return access_token directly in session_data
        token_data = session_data

    access_token = (
        token_data.get("accessToken")
        or token_data.get("access_token")
        or token_data.get("AccessToken")
        or ""
    )
    customer_id = (
        token_data.get("customerId")
        or token_data.get("customer_id")
        or token_data.get("CustomerId")
        or session_data.get("customerId")
        or ""
    )
    login_id = (
        token_data.get("loginId")
        or token_data.get("login_id")
        or token_data.get("LoginId")
        or customer_id
        or ""
    )

    if not access_token:
        print(f"ERROR: access_token not found in response.\nFull response: {token_data}")
        sys.exit(1)

    _write_env("SHAREKHAN_ACCESS_TOKEN", access_token)
    if customer_id:
        _write_env("SHAREKHAN_CUSTOMER_ID", customer_id)
    if login_id:
        _write_env("SHAREKHAN_LOGIN_ID", login_id)

    print(f"\n✓ Access token saved to .env  (…{access_token[-6:]})")
    if customer_id:
        print(f"✓ Customer ID saved: {customer_id}")
    print("\nSharekhan token refresh complete. Bots will pick it up on the next run.")


if __name__ == "__main__":
    main()
