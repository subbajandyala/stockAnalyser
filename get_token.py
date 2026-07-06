"""
Zerodha Kite Connect — Access Token Generator
Run this every morning before market open to refresh your access token.

Usage (Windows):
    python get_token.py

It will:
  1. Ask for your API Key and API Secret (or read from .env)
  2. Open the Kite login page in your browser
  3. Ask you to paste the redirect URL after login
  4. Generate the access token
  5. Save it to token.txt (and update .env if present)
"""

import hashlib
import json
import os
import sys
import webbrowser
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

KITE_BASE = "https://api.kite.trade"
TOKEN_FILE = Path(__file__).parent / "token.txt"
ENV_FILE   = Path(__file__).parent / ".env"


def _load_env_var(key: str) -> str:
    """Read a key from .env file if it exists."""
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _update_env(key: str, value: str) -> None:
    """Write or update a key=value pair in .env."""
    lines = []
    found = False
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith(key + "="):
                lines.append(f'{key}="{value}"')
                found = True
            else:
                lines.append(line)
    if not found:
        lines.append(f'{key}="{value}"')
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _checksum(api_key: str, request_token: str, api_secret: str) -> str:
    raw = api_key + request_token + api_secret
    return hashlib.sha256(raw.encode()).hexdigest()


def main():
    print("=" * 60)
    print("  Zerodha Kite — Access Token Generator")
    print("=" * 60)
    print()

    # --- Credentials ---
    api_key = _load_env_var("KITE_API_KEY")
    if not api_key:
        api_key = input("Enter your Kite API Key: ").strip()
    else:
        print(f"API Key loaded from .env: {api_key[:6]}{'*' * (len(api_key) - 6)}")

    api_secret = _load_env_var("KITE_API_SECRET")
    if not api_secret:
        api_secret = input("Enter your Kite API Secret: ").strip()
    else:
        print("API Secret loaded from .env  (**hidden**)")

    if not api_key or not api_secret:
        print("\n[ERROR] API Key and Secret are required. Exiting.")
        sys.exit(1)

    # --- Open login URL ---
    login_url = f"https://kite.zerodha.com/connect/login?api_key={api_key}&v=3"
    print(f"\nOpening Kite login in your browser...")
    print(f"  {login_url}\n")
    webbrowser.open(login_url)

    print("After you log in, Kite will redirect you to a URL like:")
    print("  http://127.0.0.1/?request_token=XXXXXX&action=login&status=success")
    print()

    # --- Get redirect URL from user ---
    redirect_url = input("Paste the full redirect URL here: ").strip()

    # Extract request_token
    try:
        parsed = urlparse(redirect_url)
        params = parse_qs(parsed.query)
        request_token = params["request_token"][0]
    except (KeyError, IndexError):
        # Maybe user pasted just the token
        if len(redirect_url) > 20 and " " not in redirect_url and "?" not in redirect_url:
            request_token = redirect_url
        else:
            print("\n[ERROR] Could not extract request_token from URL.")
            print("Make sure you pasted the full redirect URL.")
            sys.exit(1)

    print(f"\nRequest token: {request_token[:10]}...")

    # --- Exchange for access token ---
    print("Exchanging for access token...")
    checksum = _checksum(api_key, request_token, api_secret)

    r = requests.post(
        f"{KITE_BASE}/session/token",
        headers={"X-Kite-Version": "3"},
        data={
            "api_key":       api_key,
            "request_token": request_token,
            "checksum":      checksum,
        },
        timeout=15,
    )

    if not r.ok:
        try:
            err = r.json().get("message", r.text)
        except Exception:
            err = r.text
        print(f"\n[ERROR] Kite returned {r.status_code}: {err}")
        sys.exit(1)

    data = r.json().get("data", {})
    access_token = data.get("access_token", "")
    user_name    = data.get("user_name", "")
    user_id      = data.get("user_id", "")
    login_time   = data.get("login_time", "")

    if not access_token:
        print(f"\n[ERROR] No access_token in response: {r.text}")
        sys.exit(1)

    # --- Save results ---
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # token.txt — plain text for easy copy-paste
    TOKEN_FILE.write_text(
        f"# Generated: {ts}\n"
        f"KITE_API_KEY={api_key}\n"
        f"KITE_ACCESS_TOKEN={access_token}\n",
        encoding="utf-8",
    )

    # Update .env if it exists or user wants to create it
    if ENV_FILE.exists():
        _update_env("KITE_API_KEY", api_key)
        _update_env("KITE_ACCESS_TOKEN", access_token)
        print(f"\n.env updated with new access token.")
    else:
        create_env = input("\nCreate .env file with credentials? (y/n): ").strip().lower()
        if create_env == "y":
            _update_env("KITE_API_KEY", api_key)
            _update_env("KITE_API_SECRET", api_secret)
            _update_env("KITE_ACCESS_TOKEN", access_token)
            print(".env created.")

    print()
    print("=" * 60)
    print(f"  SUCCESS — Access token generated!")
    print("=" * 60)
    if user_name:
        print(f"  User     : {user_name} ({user_id})")
    if login_time:
        print(f"  Login    : {login_time}")
    print(f"  Saved to : {TOKEN_FILE}")
    print()
    print(f"  Access Token:")
    print(f"  {access_token}")
    print()
    print("  Copy this token into the Streamlit app sidebar,")
    print("  or load it from token.txt / .env automatically.")
    print("=" * 60)

    # Keep window open on Windows double-click
    if sys.platform == "win32":
        input("\nPress Enter to exit...")


if __name__ == "__main__":
    main()
