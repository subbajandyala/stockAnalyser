#!/usr/bin/env python3
"""
auto_token.py — refreshes the Kite access token automatically.

Run via cron at 08:00 IST (02:30 UTC) daily:
    30 2 * * * cd /root/stockAnalyser && python3 auto_token.py >> token_refresh.log 2>&1

Reads from .env:
    KITE_USER_ID        Zerodha login ID  (e.g. YZ7243)
    KITE_PASSWORD       Zerodha password
    KITE_TOTP_SECRET    32-char base32 TOTP secret
    KITE_API_KEY        Kite Connect app key
    KITE_API_SECRET     Kite Connect app secret

Writes KITE_ACCESS_TOKEN back to .env, then restarts the ticker daemon.

Second account (optional): the same five settings prefixed KITE2_ (KITE2_USER_ID,
KITE2_PASSWORD, KITE2_TOTP_SECRET, KITE2_API_KEY, KITE2_API_SECRET) → writes
KITE2_ACCESS_TOKEN. Each account is refreshed on its own; one failing never
stops the other.
"""

import datetime
import os
import re
import subprocess
import sys
from pathlib import Path

import pyotp
import requests
from kiteconnect import KiteConnect

_IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
_ENV_FILE = Path(__file__).parent / ".env"


def _log(msg: str):
    ts = datetime.datetime.now(_IST).strftime("%Y-%m-%d %H:%M:%S IST")
    print(f"[{ts}] {msg}", flush=True)


def _load_env() -> dict:
    env: dict[str, str] = {}
    if _ENV_FILE.exists():
        for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    for k, v in os.environ.items():
        env[k] = v
    return env


def _save_token(token: str, prefix: str = "KITE_"):
    name = f"{prefix}ACCESS_TOKEN"
    text = _ENV_FILE.read_text(encoding="utf-8")
    if re.search(rf"^{name}=", text, re.MULTILINE):
        text = re.sub(rf"^{name}=.*$", f"{name}={token}", text, flags=re.MULTILINE)
    else:
        text = text.rstrip("\n") + f"\n{name}={token}\n"
    _ENV_FILE.write_text(text, encoding="utf-8")
    if prefix != "KITE_":
        return

    # Also update ~/.kite_creds.json so bots that read it get the fresh token
    import json
    creds_file = Path.home() / ".kite_creds.json"
    if creds_file.exists():
        try:
            data = json.loads(creds_file.read_text())
            data["access_token"] = token
            creds_file.write_text(json.dumps(data, indent=2))
        except Exception as e:
            _log(f"Warning: could not update ~/.kite_creds.json: {e}")


def _get_access_token(user_id: str, password: str, totp_secret: str,
                      api_key: str, api_secret: str) -> str:
    sess = requests.Session()

    # 1. Password login
    r = sess.post("https://kite.zerodha.com/api/login",
                  data={"user_id": user_id, "password": password})
    r.raise_for_status()
    body = r.json()
    if body.get("status") != "success":
        raise RuntimeError(f"Login failed: {body}")
    request_id = body["data"]["request_id"]
    _log("Password login OK")

    # 2. TOTP
    totp_code = pyotp.TOTP(totp_secret).now()
    r = sess.post("https://kite.zerodha.com/api/twofa", data={
        "user_id":      user_id,
        "request_id":   request_id,
        "twofa_value":  totp_code,
        "twofa_type":   "totp",
    })
    r.raise_for_status()
    body = r.json()
    if body.get("status") != "success":
        raise RuntimeError(f"2FA failed: {body}")
    _log("TOTP OK")

    # 3. Kite Connect OAuth redirect → grab request_token
    r = sess.get(
        f"https://kite.zerodha.com/connect/login?api_key={api_key}&v=3",
        allow_redirects=True,
    )
    request_token = None
    # Check the final URL and the redirect chain
    for url in [r.url] + [h.headers.get("Location", "") for h in r.history]:
        m = re.search(r"request_token=([A-Za-z0-9]+)", url)
        if m:
            request_token = m.group(1)
            break
    if not request_token:
        raise RuntimeError(f"request_token not found. Final URL: {r.url}")
    _log(f"request_token: {request_token[:8]}…")

    # 4. Generate access token
    kite = KiteConnect(api_key=api_key)
    data = kite.generate_session(request_token, api_secret=api_secret)
    return data["access_token"]


def _refresh(env: dict, prefix: str) -> bool:
    """Log in one account (KITE_ or KITE2_) and save its new token. True on success."""
    keys = ["USER_ID", "PASSWORD", "TOTP_SECRET", "API_KEY", "API_SECRET"]
    missing = [prefix + k for k in keys if not env.get(prefix + k)]
    if missing:
        _log(f"ERROR: Missing in .env: {missing}")
        return False
    _log(f"Starting token refresh ({prefix.rstrip('_')})…")
    try:
        token = _get_access_token(*(env[prefix + k] for k in keys))
    except Exception as e:
        _log(f"ERROR getting token ({prefix.rstrip('_')}): {e}")
        return False
    _save_token(token, prefix)
    _log(f"Token saved ({prefix.rstrip('_')}): {token[:8]}…")
    return True


def main():
    env = _load_env()
    ok = [_refresh(env, "KITE_")]
    if env.get("KITE2_USER_ID") or env.get("KITE2_API_KEY"):
        ok.append(_refresh(env, "KITE2_"))
    if not any(ok):
        sys.exit(1)

    # Restart ticker daemon and bots with the new token(s)
    script_dir = Path(__file__).parent
    subprocess.run(["bash", str(script_dir / "stop_droplet.sh")], check=False)
    import time; time.sleep(2)
    subprocess.run(["bash", str(script_dir / "start_droplet.sh")], check=False)
    _log("Ticker daemon restarted. Done.")
    if not all(ok):
        sys.exit(1)


if __name__ == "__main__":
    main()
