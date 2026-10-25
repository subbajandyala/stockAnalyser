"""
"In hammer" screener on Nifty 500, rebuilt on Kite data → ntfy push.
A hammer-shaped day: well off the low (long lower wick), back near the high.

Replaces the Trendlyne "in hammer" screener (Trendlyne blocks requests from
servers with a human-verification page). Every run (cron, every 15 min in market
hours) it:
  1. loads the Nifty 500 list (NSE's CSV, cached a week in data/nifty500_list.csv)
  2. fetches ~3 weeks of 15-minute candles per stock from Kite (≤ 3 requests/s)
  3. keeps the stocks that pass all of the screener's conditions:
       current price ≥ day low  × (1 + 3%)     bounced at least 3% off the day's low
       current price >  15-min Supertrend      15-min trend is up
       day high − current price < 1% of day high   closing in on the day's high (small upper wick)
  4. ranks them by 15-min AROON Down (14), highest first
  5. pushes the top stock (plus the next few) to ntfy

Current price = the latest price (the forming 15-min candle's close); day high and
low include the forming candle. Supertrend (10, 3) and AROON Down (14) use completed
15-min candles. Supertrend: ATR(10, Wilder) bands around (high+low)/2, ×3, with the
usual carry-forward rules. AROON Down (14) = 100 × (14 − candles since the lowest low
of the last 15) / 14. Stocks tied at the top AROON Down are listed in the push.

Settings in /root/stockAnalyser/.env (optional):
  HAMMER_NTFY_TOPIC=revanth-kite-hammer-aroon
  HAMMER_ABOVE_LOW_PCT=3         # condition 1: % above the day low
  HAMMER_NEAR_HIGH_PCT=1         # condition 3: within this % of the day high
  HAMMER_ST_PERIOD=10            # Supertrend ATR period
  HAMMER_ST_MULT=3               # Supertrend multiplier
  HAMMER_TOP_N=3                 # stocks listed in the push

Cron (server clock is UTC; runs 1 min after each 15-min close, 09:16 … 15:31 IST —
a full scan takes ~3 min):
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
HISTORY_DAYS = 21          # calendar days of 15-min candles: plenty for Supertrend and AROON to settle
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


def supertrend(df: pd.DataFrame, period: int = 10, mult: float = 3.0) -> float | None:
    """Latest Supertrend line value (the usual TradingView / Trendlyne definition)."""
    if len(df) < period + 2:
        return None
    h, l, c = df["High"].values, df["Low"].values, df["Close"].values
    tr = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(df))]
    atr = [None] * len(df)
    atr[period - 1] = sum(tr[:period]) / period
    for i in range(period, len(df)):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period       # Wilder's smoothing
    upper = lower = st = None
    up_trend = True
    for i in range(period - 1, len(df)):
        mid = (h[i] + l[i]) / 2
        bu, bl = mid + mult * atr[i], mid - mult * atr[i]
        if upper is None:
            upper, lower = bu, bl
        else:
            upper = bu if (bu < upper or c[i - 1] > upper) else upper
            lower = bl if (bl > lower or c[i - 1] < lower) else lower
        if st is None:
            up_trend = c[i] > upper
        elif up_trend and c[i] < lower:
            up_trend = False
        elif not up_trend and c[i] > upper:
            up_trend = True
        st = lower if up_trend else upper
    return float(st)


def aroon_down(lows: pd.Series, n: int = AROON_N) -> float | None:
    """TA-Lib AROON down: window of n+1 candles, the most recent lowest low counts."""
    window = lows.tail(n + 1)
    if len(window) < n + 1:
        return None
    since_low = int(window.values[::-1].argmin())   # 0 = the latest candle made the low
    return round(100.0 * (n - since_low) / n, 2)


def evaluate(symbol: str, df: pd.DataFrame, now: datetime.datetime, session: datetime.date,
             rules: dict) -> dict | None:
    """session = the trading day to read (today in market hours; last session with --force)."""
    df = df[df.index.date <= session]
    day = df[df.index.date == session]
    if day.empty:
        return None                       # no candle of this session yet
    price = float(day["Close"].iloc[-1])   # latest price (forming candle's close)
    day_low, day_high = float(day["Low"].min()), float(day["High"].max())
    done = completed(df, now)
    if len(done) < AROON_N + 1:
        return None
    st = supertrend(done, rules["st_period"], rules["st_mult"])
    if st is None:
        return None
    if not (price >= day_low * (1 + rules["above_low"] / 100)
            and price > st
            and day_high - price < day_high * rules["near_high"] / 100):
        return None
    ad = aroon_down(done["Low"])
    if ad is None:
        return None
    prev = df[df.index.date < session]
    prev_close = float(prev["Close"].iloc[-1]) if not prev.empty else None
    return {"symbol": symbol, "aroon_down": ad, "ltp": price, "supertrend": st,
            "off_low": (price / day_low - 1) * 100, "off_high": (1 - price / day_high) * 100,
            "day_chg": (price / prev_close - 1) * 100 if prev_close else None}


# ── Main ──────────────────────────────────────────────────────────────────────

def scan(now: datetime.datetime, session: datetime.date | None, rules: dict,
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
            return evaluate(sym, df, now, day, rules)
        except Exception as e:
            print(f"  {sym}: {e}")
            return None

    with ThreadPoolExecutor(max_workers=REQ_PER_SEC) as pool:
        hits = [h for h in pool.map(one, symbols) if h]
    return sorted(hits, key=lambda h: -h["aroon_down"]), got


def main() -> None:
    ap = argparse.ArgumentParser(description="In-hammer screener on Nifty 500, ranked by 15-min AROON Down → ntfy")
    ap.add_argument("--force", action="store_true", help="run outside market hours")
    ap.add_argument("--dry-run", action="store_true", help="print, don't push")
    ap.add_argument("--limit", type=int, help="scan only the first N stocks (testing)")
    args = ap.parse_args()

    now = datetime.datetime.now(_IST)
    if not args.force and (now.weekday() >= 5 or not MARKET_OPEN <= now.time() <= MARKET_CLOSE):
        print(f"{now:%a %H:%M} IST — market closed, skipping")
        return
    topic = _env_value("HAMMER_NTFY_TOPIC", "revanth-kite-hammer-aroon")
    rules = {"above_low": float(_env_value("HAMMER_ABOVE_LOW_PCT", "3")),
             "near_high": float(_env_value("HAMMER_NEAR_HIGH_PCT", "1")),
             "st_period": int(_env_value("HAMMER_ST_PERIOD", "10")),
             "st_mult": float(_env_value("HAMMER_ST_MULT", "3"))}
    top_n = int(_env_value("HAMMER_TOP_N", "3") or 3)

    t0 = time.time()
    hits, got = scan(now, None if args.force else now.date(), rules, args.limit)
    label = f"{now:%H:%M} IST"
    print(f"{label} — {got} stocks with candles, {len(hits)} pass the screener, {time.time() - t0:.0f}s")
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
             + f", +{h['off_low']:.1f}% off low, -{h['off_high']:.1f}% from high"
             for h in hits[:top_n]]
    if len(tied) > 1:
        lines.append(f"(tied at {top['aroon_down']:g}: {', '.join(tied[:10])}"
                     + (f" +{len(tied) - 10} more" if len(tied) > 10 else "") + ")")
    body = "\n".join(lines) + (f"\n\n{len(hits)} Nifty 500 stocks ≥ {rules['above_low']:g}% off the day low, "
                                f"above 15-min Supertrend, within {rules['near_high']:g}% of the day high")
    title = f"Top: {top['symbol']} — AROON Down {top['aroon_down']:g} ({label})"
    print(title + "\n  " + "\n  ".join(lines))
    if not args.dry_run:
        notify(topic, title, body, tags="hammer,chart_with_downwards_trend")


if __name__ == "__main__":
    main()
