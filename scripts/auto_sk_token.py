"""
Sharekhan auto token refresh (no browser needed).
Logs in via /skapi/auth/login + TOTP via /skapi/auth/verifyOTP,
then exchanges the code for an access token and saves it to .env.

Run before 9:15 AM IST each trading day (or via cron like auto_token.py):
  30 2 * * * cd /root/stockAnalyser && /root/venv/bin/python scripts/auto_sk_token.py >> sk_token.log 2>&1

Requires in .env:
  SHAREKHAN_API_KEY
  SHAREKHAN_SECURE_KEY
  SHAREKHAN_LOGIN_ID   (or SHAREKHAN_CUSTOMER_ID)
  SHAREKHAN_PASSWORD
  SHAREKHAN_TOTP_SECRET
"""

import datetime
import json
import pathlib
import re
import sys
import time
import urllib.parse

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import requests
import pyotp

_ENV_PATH = pathlib.Path(__file__).parent.parent / ".env"
_IST      = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
_SK_BASE  = "https://api.sharekhan.com"


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
    env = _read_env()
    api_key     = env.get("SHAREKHAN_API_KEY", "")
    secret_key  = env.get("SHAREKHAN_SECURE_KEY", "")
    login_id    = env.get("SHAREKHAN_LOGIN_ID", "") or env.get("SHAREKHAN_CUSTOMER_ID", "")
    password    = env.get("SHAREKHAN_PASSWORD", "")
    totp_secret = env.get("SHAREKHAN_TOTP_SECRET", "")

    missing = [k for k, v in {
        "SHAREKHAN_API_KEY":     api_key,
        "SHAREKHAN_SECURE_KEY":  secret_key,
        "SHAREKHAN_LOGIN_ID":    login_id,
        "SHAREKHAN_PASSWORD":    password,
        "SHAREKHAN_TOTP_SECRET": totp_secret,
    }.items() if not v]
    if missing:
        print(f"ERROR: missing in .env: {', '.join(missing)}")
        sys.exit(1)

    now = datetime.datetime.now(_IST)
    print(f"[{now.strftime('%H:%M IST')}] Sharekhan auto token refresh ...")

    sess = requests.Session()
    sess.headers.update({
        "Content-Type": "application/json",
        "Accept":       "application/json",
        "User-Agent":   "Mozilla/5.0",
        "Referer":      f"{_SK_BASE}/skapi/auth/login.html",
    })

    # ── Step 1: Login ──────────────────────────────────────────────────────────
    login_payload = {
        "loginId":               login_id,
        "membershipPwd":         password,
        "apiKey":                api_key,
        "vendorKey":             "",
        "userId":                "",
        "versionId":             "",
        "state":                 "12345",
        "isLoginAfterActivation": "0",
    }

    print("  [1/3] POST /skapi/auth/login ...")
    r1 = sess.post(f"{_SK_BASE}/skapi/auth/login", json=login_payload, timeout=20)

    if not r1.ok:
        print(f"  ERROR: HTTP {r1.status_code} — {r1.text[:300]}")
        sys.exit(1)

    d1 = r1.json()
    status1 = d1.get("statusCode")
    print(f"         statusCode={status1}  totpStatus={d1.get('totpStatus')}")

    # A 408 with responseUrl means Sharekhan redirected immediately (no 2FA needed)
    if status1 == 408:
        response_url = d1.get("responseUrl", "")
        if "code=" in response_url:
            print("  INFO: No 2FA required — got code directly")
            _exchange_and_save(response_url, api_key, secret_key)
            return
        print(f"  ERROR: Unexpected 408 without code: {d1}")
        sys.exit(1)

    if status1 not in (200, None):
        print(f"  ERROR: Login failed: {d1}")
        sys.exit(1)

    # Extract requestSessionId from login response (field name varies)
    req_sess_id = (
        d1.get("requestSessionId")
        or d1.get("reqSessId")
        or d1.get("req_sess_id")
        or ""
    )

    # ── Step 2: TOTP verify ────────────────────────────────────────────────────
    totp_code = pyotp.TOTP(totp_secret).now()
    print(f"  [2/3] POST /skapi/auth/verifyOTP  (TOTP={totp_code}) ...")

    verify_payload = {
        "loginId":          login_id,
        "apiKey":           api_key,
        "vendorKey":        "",
        "userId":           "",
        "versionId":        "",
        "state":            "12345",
        "otp":              "",
        "totp":             totp_code,
        "validateBy":       "TOTP",
        "requestSessionId": req_sess_id,
    }

    r2 = sess.post(f"{_SK_BASE}/skapi/auth/verifyOTP", json=verify_payload, timeout=20)

    if not r2.ok:
        print(f"  ERROR: HTTP {r2.status_code} — {r2.text[:300]}")
        sys.exit(1)

    d2 = r2.json()
    status2 = d2.get("statusCode")
    print(f"         statusCode={status2}")

    response_url = d2.get("responseUrl", "")

    if status2 == 200 and response_url:
        _exchange_and_save(response_url, api_key, secret_key)
        return

    if status2 == 408 and response_url:
        _exchange_and_save(response_url, api_key, secret_key)
        return

    print(f"  ERROR: TOTP verify failed: {d2}")
    sys.exit(1)


def _exchange_and_save(response_url: str, api_key: str, secret_key: str) -> None:
    """Extract code from redirect URL, exchange for access token, save to .env."""
    # ── Step 3: Extract code ───────────────────────────────────────────────────
    parsed = urllib.parse.urlparse(response_url)
    qs = urllib.parse.parse_qs(parsed.query)
    request_token = (
        qs.get("code", qs.get("request_token", [""]))[0]
    )

    if not request_token:
        print(f"  ERROR: no code in responseUrl: {response_url}")
        sys.exit(1)

    print(f"  [3/3] Exchanging code ({len(request_token)} chars) for access token ...")

    try:
        from SharekhanApi import SharekhanConnect
    except ImportError:
        print("  ERROR: SharekhanApi not found — pip install shareconnect")
        sys.exit(1)

    sk = SharekhanConnect(api_key)
    try:
        enc = sk.generate_session(request_token, secret_key)
        token_data = sk.get_access_token(api_key, enc, "12345")
    except Exception as e:
        print(f"  ERROR: Token exchange — {e}")
        sys.exit(1)

    access_token = (
        token_data.get("accessToken")
        or token_data.get("access_token")
        or token_data.get("AccessToken")
        or ""
    )
    customer_id = (
        token_data.get("customerId")
        or token_data.get("customer_id")
        or ""
    )
    login_id_resp = (
        token_data.get("loginId")
        or token_data.get("login_id")
        or ""
    )

    if not access_token:
        print(f"  ERROR: access_token not in response: {token_data}")
        sys.exit(1)

    _write_env("SHAREKHAN_ACCESS_TOKEN", access_token)
    if customer_id:
        _write_env("SHAREKHAN_CUSTOMER_ID", customer_id)
    if login_id_resp:
        _write_env("SHAREKHAN_LOGIN_ID", login_id_resp)

    print(f"  ✓ SHAREKHAN_ACCESS_TOKEN updated (…{access_token[-6:]})")
    now = datetime.datetime.now(_IST)
    print(f"  Done at {now.strftime('%H:%M IST')}")


if __name__ == "__main__":
    main()
