"""
Trendlyne "in hammer" screener → top 15-min AROON Down stock → ntfy push.

Every run (cron, every 15 min in market hours) it:
  1. fetches the screener's JSON — the same call Trendlyne's screener page makes
     (screener 880561 "in hammer", Nifty 500 universe)
  2. picks the stock with the highest 15min AROON Down (fifteen_min_ta___AROOND_14)
  3. pushes it to ntfy

Outside Mon–Fri 09:15–15:30 IST it exits without fetching. If the session cookie
expires it sends one high-priority warning (not one every 15 min) until a run
succeeds again.

The endpoint is internal to trendlyne.com, not a public API: it needs your
logged-in cookie and can change without notice. Keep the frequency modest.

Needs in /root/stockAnalyser/.env (never committed, chmod 600):
  TRENDLYNE_COOKIE=<whole cookie: header of the tl-all-in-one-screener-data-get request>
Optional:
  TRENDLYNE_NTFY_TOPIC=revanth-trend-line
  TRENDLYNE_SCREEN_ID=880561
  TRENDLYNE_GROUP_NAME=NIFTY500
  TRENDLYNE_GROUP_SLUG=nifty-500

Getting the cookie: Chrome, logged in → open the screener → DevTools → Network →
reload → click tl-all-in-one-screener-data-get → Request Headers → copy the whole
`cookie:` value.

Cron (server clock is UTC; runs at 09:16, 09:31 … IST, 1 min after each 15-min close):
  46,1,16,31 3-10 * * 1-5 cd /root/stockAnalyser && /root/venv/bin/python scripts/trendlyne_top.py >> trendlyne_top.log 2>&1

Usage:
  python scripts/trendlyne_top.py            # one run (skips outside market hours)
  python scripts/trendlyne_top.py --force    # run now regardless of the time
  python scripts/trendlyne_top.py --dry-run  # print, don't push
"""

import argparse
import datetime
import os
import pathlib
import re
import sys

import requests

_ROOT = pathlib.Path(__file__).parent.parent
_IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
_ERR_STATE = _ROOT / ".trendlyne_last_error"
AROON_KEY = "fifteen_min_ta___AROOND_14"
MARKET_OPEN, MARKET_CLOSE = datetime.time(9, 15), datetime.time(15, 30)


def _env_value(name: str, default: str = "") -> str:
    v = os.environ.get(name, "")
    if v:
        return v
    env_path = _ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, val = line.split("=", 1)
                if k.strip().removeprefix("export ").strip() == name:
                    return val.strip().strip('"').strip("'")
    return default


def notify(topic: str, title: str, body: str, priority: str = "default", tags: str = "") -> None:
    r = requests.post(f"https://ntfy.sh/{topic}", data=body.encode("utf-8"), timeout=10,
                      headers={"Title": title.encode("utf-8"), "Priority": priority, "Tags": tags,
                               "Content-Type": "text/plain; charset=utf-8"})
    r.raise_for_status()


def alert_error_once(topic: str, msg: str, dry_run: bool) -> None:
    """Push an error only when it differs from the last one, so a dead cookie doesn't spam."""
    print(f"ERROR: {msg}")
    prev = _ERR_STATE.read_text() if _ERR_STATE.exists() else ""
    if prev != msg and not dry_run:
        notify(topic, "Trendlyne screener ERROR", msg, priority="high", tags="warning")
        _ERR_STATE.write_text(msg)


def fetch_screener(cookie: str, screen_id: str, group: str, slug: str) -> requests.Response:
    """The screener page's own data call. Tried as GET first; Trendlyne answers 405 when it
    wants a POST, which then goes with the same fields as form data plus the CSRF token."""
    url = "https://trendlyne.com/fundamentals/tl-all-in-one-screener-data-get/"
    params = {"screenpk": screen_id, "perPageCount": "500", "groupType": "index",
              "groupName": group, "groupSlug": slug}
    m = re.search(r"(?:^|;\s*)csrftoken=([^;]+)", cookie)
    headers = {
        "Cookie": cookie,
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/130 Safari/537.36",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": "https://trendlyne.com",
        "Referer": f"https://trendlyne.com/fundamentals/stock-screener/{screen_id}/",
    }
    if m:
        headers["X-CSRFToken"] = m.group(1)
    res = requests.get(url, params=params, headers=headers, timeout=30)
    if res.status_code == 405:
        res = requests.post(url, params=params, data=params, headers=headers, timeout=30)
    return res


def top_by_aroon_down(body: dict) -> tuple[dict | None, str]:
    """(top row as {column: value}, error). Rows without an AROON Down value are ignored."""
    headers = [h.get("unique_name") for h in body.get("tableHeaders", [])]
    if AROON_KEY not in headers:
        return None, f"column {AROON_KEY} not in the screener response — add '15min AROON Down' to the screener"
    rows = []
    for r in body.get("tableData", []):
        row = dict(zip(headers, r))
        try:
            row["_aroon"] = float(row.get(AROON_KEY))
        except (TypeError, ValueError):
            continue
        rows.append(row)
    if not rows:
        return None, ""
    return max(rows, key=lambda x: x["_aroon"]), ""


def main() -> None:
    ap = argparse.ArgumentParser(description="Top 15-min AROON Down stock from a Trendlyne screener → ntfy")
    ap.add_argument("--force", action="store_true", help="run outside market hours")
    ap.add_argument("--dry-run", action="store_true", help="print the result, don't push")
    args = ap.parse_args()

    now = datetime.datetime.now(_IST)
    if not args.force and (now.weekday() >= 5 or not MARKET_OPEN <= now.time() <= MARKET_CLOSE):
        print(f"{now:%a %H:%M} IST — market closed, skipping")
        return

    topic = _env_value("TRENDLYNE_NTFY_TOPIC", "revanth-trend-line")
    cookie = _env_value("TRENDLYNE_COOKIE")
    if not cookie:
        return alert_error_once(topic, "TRENDLYNE_COOKIE missing in .env — see scripts/trendlyne_top.py", args.dry_run)
    screen_id = _env_value("TRENDLYNE_SCREEN_ID", "880561")

    try:
        res = fetch_screener(cookie, screen_id, _env_value("TRENDLYNE_GROUP_NAME", "NIFTY500"),
                             _env_value("TRENDLYNE_GROUP_SLUG", "nifty-500"))
        body = res.json().get("body") if res.ok else None
    except (requests.RequestException, ValueError) as e:
        return alert_error_once(topic, f"fetch failed: {e}", args.dry_run)
    if not isinstance(body, dict) or not isinstance(body.get("tableData"), list):
        hint = ("the cookie has probably expired: copy a new one into TRENDLYNE_COOKIE in .env"
                if res.status_code in (200, 401, 403) else "Trendlyne refused the request")
        print(f"  {res.request.method} → HTTP {res.status_code}, reply starts: "
              + " ".join(res.text[:200].split()))
        return alert_error_once(topic, f"fetch failed (HTTP {res.status_code}) — {hint}", args.dry_run)

    top, err = top_by_aroon_down(body)
    if err:
        return alert_error_once(topic, err, args.dry_run)
    _ERR_STATE.unlink(missing_ok=True)   # recovered

    label = f"{now:%H:%M} IST"
    if top is None:
        print(f"{label} — no stocks in the screener")
        if not args.dry_run:
            notify(topic, f"Trendlyne {label}", "No stocks in the screener right now.", tags="zzz")
        return

    sym = top.get("NSEcode") or top.get("shortname") or "?"
    chg = top.get("day_changeP")
    msg = (f"{top.get('shortname', sym)} ({sym})\n15min AROON Down: {top['_aroon']:g}\n"
           f"LTP: {top.get('currentPrice', '?')}"
           + (f" | Day {float(chg):+.2f}%" if chg not in (None, "") else ""))
    print(f"{label} → " + msg.replace("\n", " | "))
    if not args.dry_run:
        notify(topic, f"Top: {sym} — AROON Down {top['_aroon']:g} ({label})", msg, tags="chart_with_downwards_trend")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:   # never die silently under cron
        print(f"Script error: {e}", file=sys.stderr)
        try:
            alert_error_once(_env_value("TRENDLYNE_NTFY_TOPIC", "revanth-trend-line"), f"script error: {e}", False)
        except Exception:
            pass
        sys.exit(1)
