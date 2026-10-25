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

Hammer and AROON follow TA-Lib (CDLHAMMER, AROON 14), the definitions screeners
normally use: small body (< the 10-candle average body), lower wick > body, upper
wick < 10% of the 10-candle average range, body at or near the previous candle's
low. AROON Down (14) = 100 × (14 − candles since the lowest low of the last 15) / 14.
Ranking: highest AROON Down first; stocks tied at the top are listed in the push.

Settings in /root/stockAnalyser/.env (optional):
  HAMMER_NTFY_TOPIC=revanth-trend-line
  HAMMER_TF=day                  # day | 15m
  HAMMER_TOP_N=3                 # stocks listed in the push

Cron (server clock is UTC; runs 1 min after each 15-min close, 09:16 … 15:31 IST —
the 09:16 run has no completed candle yet and stays quiet; a full scan takes ~3–4 min):
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
MARKET_OPEN, MARKET_CLOSE = datetime.time(9, 15), datetime.time(15, 35)   # 15:31 run reads the last candle
AROON_N = 14
HISTORY_DAYS = 21          # calendar days of 15-min candles: ≥ 10 prior daily candles for the hammer
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

def is_hammer(c: pd.Series, hist: pd.DataFrame) -> bool:
    """TA-Lib CDLHAMMER rules (the usual screener definition), on candle c after the
    candles in hist (same timeframe, oldest first, ≥ 10 of them):
      small body        body < average body of the previous 10 candles
      long lower wick   lower wick > body
      tiny upper wick   upper wick < 10% of the average range of the previous 10
      at the lows       body bottom ≤ previous candle's low + 20% of the 5-candle average range
    """
    if len(hist) < 10:
        return False
    o, h, l, cl = float(c["Open"]), float(c["High"]), float(c["Low"]), float(c["Close"])
    body = abs(cl - o)
    lower, upper = min(o, cl) - l, h - max(o, cl)
    last10 = hist.tail(10)
    avg_body10 = float((last10["Close"] - last10["Open"]).abs().mean())
    avg_range10 = float((last10["High"] - last10["Low"]).mean())
    avg_range5 = float((hist["High"] - hist["Low"]).tail(5).mean())
    return (body < avg_body10
            and lower > body
            and upper < 0.1 * avg_range10
            and min(o, cl) <= float(hist["Low"].iloc[-1]) + 0.2 * avg_range5)


def aroon_down(lows: pd.Series, n: int = AROON_N) -> float | None:
    """TA-Lib AROON down: window of n+1 candles, the most recent lowest low counts."""
    window = lows.tail(n + 1)
    if len(window) < n + 1:
        return None
    since_low = int(window.values[::-1].argmin())   # 0 = the latest candle made the low
    return round(100.0 * (n - since_low) / n, 2)


def evaluate(symbol: str, df: pd.DataFrame, now: datetime.datetime, tf: str,
             session: datetime.date) -> dict | None:
    """session = the trading day to read (today in market hours; last session with --force)."""
    df = completed(df, now)
    df = df[df.index.date <= session]
    if len(df) < AROON_N + 1 or df.index[-1].date() != session:
        return None                       # no completed candle of this session yet
    if tf == "15m":
        candle, hist = df.iloc[-1], df.iloc[:-1]
    else:
        days = daily_from_15m(df)
        candle, hist = days.loc[session], days[days.index < session]
    if not is_hammer(candle, hist):
        return None
    ad = aroon_down(df["Low"])
    if ad is None:
        return None
    prev = df[df.index.date < session]
    prev_close = float(prev["Close"].iloc[-1]) if not prev.empty else None
    ltp = float(df["Close"].iloc[-1])
    return {"symbol": symbol, "aroon_down": ad, "ltp": ltp,
            "day_chg": (ltp / prev_close - 1) * 100 if prev_close else None,
            "candle_time": df.index[-1]}


# ── Main ──────────────────────────────────────────────────────────────────────

def scan(now: datetime.datetime, tf: str, session: datetime.date | None,
         limit: int | None = None) -> tuple[list[dict], int]:
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
            day = session or df.index[-1].date()     # --force: the stock's last session
            return evaluate(sym, df, now, tf, day)
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
    hits, got = scan(now, tf, None if args.force else now.date(), args.limit)
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
    tied = [h["symbol"] for h in hits if h["aroon_down"] == top["aroon_down"]]
    lines = [f"{h['symbol']}: AROON Down {h['aroon_down']:g}, LTP {h['ltp']:,.2f}"
             + (f", day {h['day_chg']:+.2f}%" if h["day_chg"] is not None else "")
             for h in hits[:top_n]]
    if len(tied) > 1:
        lines.append(f"(tied at {top['aroon_down']:g}: {', '.join(tied[:10])}"
                     + (f" +{len(tied) - 10} more" if len(tied) > 10 else "") + ")")
    body = "\n".join(lines) + f"\n\n{len(hits)} hammer stocks in Nifty 500 ({'daily' if tf == 'day' else '15-min'} candle)"
    title = f"Top: {top['symbol']} — AROON Down {top['aroon_down']:g} ({label})"
    print(title + "\n  " + "\n  ".join(lines))
    if not args.dry_run:
        notify(topic, title, body, tags="hammer,chart_with_downwards_trend")


if __name__ == "__main__":
    main()
