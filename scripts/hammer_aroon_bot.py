"""
Hammer + 15-min AROON Down scanner on Nifty 500 → ntfy push. Kite data only.

Replaces the Trendlyne "in hammer" screener (Trendlyne blocks requests from
servers with a human-verification page). Every run (cron, every 15 min in market
hours) it:
  1. loads the Nifty 500 list (NSE's CSV, cached a week in data/nifty500_list.csv)
  2. fetches ~10 days of 15-minute candles per stock from Kite (≤ 3 requests/s)
  3. keeps the stocks whose candle forms a hammer — by default today's daily candle
     so far, built from the 15-min candles (HAMMER_TF=15m checks the last 15-min
     candle instead)
  4. computes AROON Down (14) on completed 15-minute candles
  5. pushes the hammer stock with the highest AROON Down (plus the next few) to ntfy

Hammer: lower wick ≥ 2× body, upper wick ≤ body (or ≤ 10% of the range for a
tiny body), body ≤ 1/3 of the range, and the low at or below the lowest low of
the previous 5 candles (a hammer forms after a fall). AROON Down (14) =
100 × (14 − candles since the lowest low of the last 15) / 14.

Settings in /root/stockAnalyser/.env (optional):
  HAMMER_NTFY_TOPIC=revanth-trend-line
  HAMMER_TF=day                  # day | 15m
  HAMMER_TOP_N=3                 # stocks listed in the push

Cron (server clock is UTC; runs at 09:31, 09:46 … 15:16 IST, 1 min after each
15-min close; a full scan takes ~3–4 min):
  46,1,16,31 3-10 * * 1-5 cd /root/stockAnalyser && /root/venv/bin/python scripts/hammer_aroon_bot.py >> hammer_aroon.log 2>&1

Usage:
  python scripts/hammer_aroon_bot.py              # one scan (skips outside market hours)
  python scripts/hammer_aroon_bot.py --force      # scan now (uses the last session's candles)
  python scripts/hammer_aroon_bot.py --dry-run    # print, don't push
  python scripts/hammer_aroon_bot.py --limit 30   # first 30 stocks only (quick test)
"""

import argparse
import datetime
import io
import os
import pathlib
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import requests

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from screener.kite_data import _KITE_BASE, _hdrs, get_token   # noqa: E402
from screener.stocks import HEADERS as _NSE_HEADERS, NSE_NIFTY500_URL   # noqa: E402

_ROOT = pathlib.Path(__file__).parent.parent
_IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
_LIST_CACHE = _ROOT / "data" / "nifty500_list.csv"
MARKET_OPEN, MARKET_CLOSE = datetime.time(9, 15), datetime.time(15, 30)
AROON_N = 14
HISTORY_DAYS = 10          # calendar days of 15-min candles: enough for AROON and 5 prior days
REQ_PER_SEC = 3            # Kite historical-data limit


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


# ── Universe ──────────────────────────────────────────────────────────────────

def nifty500() -> list[str]:
    """Nifty 500 symbols: NSE's list, refreshed weekly, else the last saved copy."""
    fresh = _LIST_CACHE.exists() and time.time() - _LIST_CACHE.stat().st_mtime < 7 * 86400
    if not fresh:
        try:
            r = requests.get(NSE_NIFTY500_URL, headers=_NSE_HEADERS, timeout=15)
            r.raise_for_status()
            df = pd.read_csv(io.StringIO(r.text))
            if len(df) >= 400 and "Symbol" in df.columns:
                _LIST_CACHE.parent.mkdir(exist_ok=True)
                df.to_csv(_LIST_CACHE, index=False)
        except Exception as e:
            print(f"  Nifty 500 list download failed ({e}) — using the saved copy")
    if not _LIST_CACHE.exists():
        raise RuntimeError("no Nifty 500 list: NSE download failed and no saved copy in data/")
    return [s.strip() for s in pd.read_csv(_LIST_CACHE)["Symbol"].dropna()]


# ── Candles ───────────────────────────────────────────────────────────────────

class _RateLimit:
    def __init__(self, per_sec: int):
        self.gap, self.next, self.lock = 1.0 / per_sec, 0.0, threading.Lock()

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            delay = max(0.0, self.next - now)
            self.next = max(now, self.next) + self.gap
        if delay:
            time.sleep(delay)


def fetch_15m(symbol: str, now: datetime.datetime, limiter: _RateLimit, headers: dict) -> pd.DataFrame:
    token = get_token(symbol)
    if token is None:
        return pd.DataFrame()
    params = {"from": (now - datetime.timedelta(days=HISTORY_DAYS)).strftime("%Y-%m-%d 09:00:00"),
              "to": now.strftime("%Y-%m-%d %H:%M:%S")}
    for attempt in range(3):
        limiter.wait()
        try:
            r = requests.get(f"{_KITE_BASE}/instruments/historical/{token}/15minute",
                             headers=headers, params=params, timeout=20)
        except requests.RequestException:
            continue
        if r.status_code == 429:
            time.sleep(1 + attempt)
            continue
        if not r.ok:
            return pd.DataFrame()
        candles = r.json().get("data", {}).get("candles", [])
        if not candles:
            return pd.DataFrame()
        df = pd.DataFrame([c[:6] for c in candles], columns=["dt", "Open", "High", "Low", "Close", "Volume"])
        df["dt"] = pd.to_datetime(df["dt"]).dt.tz_localize(None)   # Kite sends IST
        return df.set_index("dt").sort_index()
    return pd.DataFrame()


def completed(df: pd.DataFrame, now: datetime.datetime) -> pd.DataFrame:
    """Drop the 15-min candle that is still forming."""
    cutoff = now.replace(tzinfo=None) - datetime.timedelta(minutes=15)
    return df[df.index <= cutoff]


def daily_from_15m(df: pd.DataFrame) -> pd.DataFrame:
    g = df.groupby(df.index.date)
    return pd.DataFrame({"Open": g["Open"].first(), "High": g["High"].max(),
                         "Low": g["Low"].min(), "Close": g["Close"].last()})


# ── Rules ─────────────────────────────────────────────────────────────────────

def is_hammer(c: pd.Series, prior: pd.DataFrame) -> bool:
    o, h, l, cl = float(c["Open"]), float(c["High"]), float(c["Low"]), float(c["Close"])
    rng = h - l
    if rng <= 0:
        return False
    body = abs(cl - o)
    lower, upper = min(o, cl) - l, h - max(o, cl)
    if body > rng / 3 or lower < 2 * body or lower < 0.5 * rng:
        return False
    if upper > max(body, 0.1 * rng):
        return False
    return prior.empty or l <= float(prior["Low"].tail(5).min())


def aroon_down(lows: pd.Series, n: int = AROON_N) -> float | None:
    window = lows.tail(n + 1)
    if len(window) < n + 1:
        return None
    since_low = int(window.values[::-1].argmin())   # 0 = the latest candle made the low
    return round(100.0 * (n - since_low) / n, 2)


def evaluate(symbol: str, df: pd.DataFrame, now: datetime.datetime, tf: str) -> dict | None:
    df = completed(df, now)
    if len(df) < AROON_N + 1:
        return None
    today = now.date() if df.index[-1].date() == now.date() else df.index[-1].date()
    if tf == "15m":
        candle, prior = df.iloc[-1], df.iloc[-6:-1]
    else:
        days = daily_from_15m(df)
        if today not in days.index:
            return None
        candle, prior = days.loc[today], days[days.index < today]
    if not is_hammer(candle, prior):
        return None
    ad = aroon_down(df["Low"])
    if ad is None:
        return None
    first_open = float(df[df.index.date == today]["Open"].iloc[0]) if (df.index.date == today).any() else None
    prev_days = daily_from_15m(df[df.index.date < today])
    prev_close = float(prev_days["Close"].iloc[-1]) if not prev_days.empty else first_open
    ltp = float(df["Close"].iloc[-1])
    return {"symbol": symbol, "aroon_down": ad, "ltp": ltp,
            "day_chg": (ltp / prev_close - 1) * 100 if prev_close else None,
            "candle_time": df.index[-1]}


# ── Main ──────────────────────────────────────────────────────────────────────

def scan(now: datetime.datetime, tf: str, limit: int | None = None) -> tuple[list[dict], int]:
    symbols = nifty500()[:limit] if limit else nifty500()
    headers = _hdrs()
    limiter = _RateLimit(REQ_PER_SEC)
    got = 0

    def one(sym: str) -> dict | None:
        nonlocal got
        df = fetch_15m(sym, now, limiter, headers)
        if df.empty:
            return None
        got += 1
        try:
            return evaluate(sym, df, now, tf)
        except Exception as e:
            print(f"  {sym}: {e}")
            return None

    with ThreadPoolExecutor(max_workers=REQ_PER_SEC) as pool:
        hits = [h for h in pool.map(one, symbols) if h]
    return sorted(hits, key=lambda h: -h["aroon_down"]), got


def main() -> None:
    ap = argparse.ArgumentParser(description="Nifty 500 hammer stocks ranked by 15-min AROON Down → ntfy")
    ap.add_argument("--force", action="store_true", help="run outside market hours")
    ap.add_argument("--dry-run", action="store_true", help="print, don't push")
    ap.add_argument("--limit", type=int, help="scan only the first N stocks (testing)")
    args = ap.parse_args()

    now = datetime.datetime.now(_IST)
    if not args.force and (now.weekday() >= 5 or not MARKET_OPEN <= now.time() <= MARKET_CLOSE):
        print(f"{now:%a %H:%M} IST — market closed, skipping")
        return
    topic = _env_value("HAMMER_NTFY_TOPIC", "revanth-trend-line")
    tf = _env_value("HAMMER_TF", "day").lower()
    top_n = int(_env_value("HAMMER_TOP_N", "3") or 3)

    t0 = time.time()
    hits, got = scan(now, tf, args.limit)
    label = f"{now:%H:%M} IST"
    print(f"{label} — {got} stocks with candles, {len(hits)} hammers ({tf}), {time.time() - t0:.0f}s")
    if got == 0:
        msg = "Kite returned no candles — token expired (run auto_token.py) or market holiday"
        print(f"ERROR: {msg}")
        if not args.dry_run and not args.force:
            notify(topic, "Hammer scan ERROR", msg, priority="high", tags="warning")
        return
    if not hits:
        return    # nothing to report this round

    top = hits[0]
    lines = [f"{h['symbol']}: AROON Down {h['aroon_down']:g}, LTP {h['ltp']:,.2f}"
             + (f", day {h['day_chg']:+.2f}%" if h["day_chg"] is not None else "")
             for h in hits[:top_n]]
    body = "\n".join(lines) + f"\n\n{len(hits)} hammer stocks in Nifty 500 ({'daily' if tf == 'day' else '15-min'} candle)"
    title = f"Top: {top['symbol']} — AROON Down {top['aroon_down']:g} ({label})"
    print(title + "\n  " + "\n  ".join(lines))
    if not args.dry_run:
        notify(topic, title, body, tags="hammer,chart_with_downwards_trend")


if __name__ == "__main__":
    main()
