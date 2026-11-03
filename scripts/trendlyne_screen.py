"""
trendlyne_screen.py — Srikanth's Trendlyne screener, rebuilt on Kite daily data, Nifty 500.

Trendlyne query                                            How it is done here
  Current Price > 0.98 × Day EMA20                         daily EMA20 from Kite daily closes
  Current Price < 1.02 × Day EMA20                         (price within ±2 % of the 20 EMA)
  0 < (1Yr High − Price) / Price × 100 < 5                 1-yr high = highest high of the last
                                                           365 days (incl. today); 0–5 % below it
  Current Price > Day EMA50
  Day EMA20 > Day EMA50
  Market Capitalization > 1000 (₹ crore)                   universe = Nifty 500 (every member is
                                                           far above ₹1,000 cr)
  Trendlyne Momentum Score > 60                            OUR OWN momentum score > 60 (below) —
                                                           Trendlyne's score is not public and
                                                           its site blocks servers

Momentum score (0–100), ranked across the Nifty 500 on each run:
  25  1-month return   percentile rank
  25  3-month return   percentile rank
  20  6-month return   percentile rank
  15  RSI(14) daily    40 → 0 … 70+ → full
  15  near 1-yr high   at the high → full … 20 % below → 0
It is in the spirit of Trendlyne's (price strength vs other stocks) — not the same number.

Current price = the latest daily candle's close (during market hours Kite's daily candle is
today's, so it is the live price at the time of the run).

Usage (from the repo folder):
  python scripts/trendlyne_screen.py              scan, push to ntfy, save data/trendlyne_<date>.csv
  python scripts/trendlyne_screen.py --no-push
  python scripts/trendlyne_screen.py --symbols RELIANCE,TCS    test on a few stocks
Settings (.env, optional): TRENDLYNE_NTFY_TOPIC (default srikanth-trendlyne),
TRENDLYNE_MIN_SCORE (default 60).
"""
from __future__ import annotations

import argparse
import datetime
import io
import pathlib
import sys
import time

import numpy as np
import pandas as pd
import requests

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core import kite_data as _kd  # noqa: E402

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
DATA = ROOT / "data"
LIST_CACHE = DATA / "nifty500.csv"
LIST_URLS = ("https://archives.nseindia.com/content/indices/ind_nifty500list.csv",
             "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv")
NSE_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                             "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
               "Accept": "text/csv,*/*", "Referer": "https://www.nseindia.com/"}
API_GAP_SEC = 0.35            # Kite historical API: ~3 requests a second
HIST_DAYS = 400               # daily candles: 1-yr high + 6-month return + EMA warm-up


def env(name: str, default: str = "") -> str:
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                if k.strip() == name:
                    return v.strip().strip('"').strip("'")
    return default


def nifty500() -> list[str]:
    """NSE's Nifty 500 list, cached a week in data/nifty500.csv (old copy used if NSE is down)."""
    DATA.mkdir(exist_ok=True)
    fresh = LIST_CACHE.exists() and time.time() - LIST_CACHE.stat().st_mtime < 7 * 86400
    if not fresh:
        for url in LIST_URLS:
            try:
                r = requests.get(url, headers=NSE_HEADERS, timeout=20)
                if r.ok and "Symbol" in r.text[:500]:
                    LIST_CACHE.write_text(r.text, encoding="utf-8")
                    break
            except Exception as e:
                print(f"  Nifty 500 list: {url} failed ({e})")
    if not LIST_CACHE.exists():
        raise SystemExit("Nifty 500 list not available (NSE unreachable and no cached copy)")
    df = pd.read_csv(io.StringIO(LIST_CACHE.read_text(encoding="utf-8")))
    df.columns = [c.strip() for c in df.columns]
    return [s.strip().upper() for s in df["Symbol"].dropna()]


def rsi(close: pd.Series, n: int = 14) -> float:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return float((100 - 100 / (1 + rs)).iloc[-1])


def ret(close: pd.Series, days: int) -> float | None:
    """% return over `days` calendar days (close then vs now)."""
    t0 = close.index[-1] - pd.Timedelta(days=days)
    past = close[close.index <= t0]
    return (float(close.iloc[-1]) / float(past.iloc[-1]) - 1) * 100 if len(past) else None


def read(symbol: str, now: datetime.datetime) -> dict:
    out = {"symbol": symbol}
    if _kd.get_token(symbol) is None:
        out["error"] = "not on Kite"
        return out
    df = _kd.fetch_ohlcv(symbol, "day", days=HIST_DAYS, to_date=now.replace(tzinfo=None, microsecond=0))
    time.sleep(API_GAP_SEC)
    if df.empty or len(df) < 60:
        out["error"] = _kd.LAST_ERROR or f"only {len(df)} daily candles"
        return out
    c = df["Close"]
    price = float(c.iloc[-1])
    yr = df[df.index > df.index[-1] - pd.Timedelta(days=365)]
    e20 = float(c.ewm(span=20, adjust=False).mean().iloc[-1])
    e50 = float(c.ewm(span=50, adjust=False).mean().iloc[-1])
    hi = float(yr["High"].max())
    out.update({"date": df.index[-1].date(), "price": price, "ema20": e20, "ema50": e50, "yr_high": hi,
                "below_high_pct": (hi - price) / price * 100, "vs_ema20_pct": (price / e20 - 1) * 100,
                "rsi": rsi(c), "r1m": ret(c, 30), "r3m": ret(c, 91), "r6m": ret(c, 182)})
    out["rules"] = {
        "within ±2% of EMA20": 0.98 * e20 < price < 1.02 * e20,
        "0–5% below 1-yr high": 0 < out["below_high_pct"] < 5,
        "above EMA50": price > e50,
        "EMA20 > EMA50": e20 > e50,
    }
    return out


def momentum_scores(rows: list[dict]) -> None:
    """Fill row['score'] (0–100) — percentile ranks across this run's universe."""
    df = pd.DataFrame([r for r in rows if "rules" in r])
    if df.empty:
        return
    pct = lambda col: df[col].rank(pct=True).fillna(0)
    rsi_part = ((df["rsi"] - 40) / 30).clip(0, 1)
    high_part = (1 - df["below_high_pct"].clip(lower=0) / 20).clip(0, 1)
    score = 25 * pct("r1m") + 25 * pct("r3m") + 20 * pct("r6m") + 15 * rsi_part + 15 * high_part
    for sym, s in zip(df["symbol"], score.round(1)):
        next(r for r in rows if r["symbol"] == sym)["score"] = float(s)


def push(topic: str, title: str, body: str) -> None:
    try:
        requests.post(f"https://ntfy.sh/{topic}", data=body.encode("utf-8")[:3900],
                      headers={"Title": title.encode("utf-8"), "Tags": "mag"}, timeout=10)
    except Exception as e:
        print(f"  ntfy error: {e}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Trendlyne-style daily screener on Nifty 500 (Kite data)")
    ap.add_argument("--no-push", action="store_true")
    ap.add_argument("--symbols", help="comma list instead of the Nifty 500")
    a = ap.parse_args()
    now = datetime.datetime.now(IST)
    min_score = float(env("TRENDLYNE_MIN_SCORE", "60"))
    syms = [s.strip().upper() for s in a.symbols.split(",")] if a.symbols else nifty500()
    print(f"[{now:%Y-%m-%d %H:%M:%S}] trendlyne screen — {len(syms)} stocks, daily chart, score > {min_score:g}")
    rows = []
    for i, s in enumerate(syms, 1):
        rows.append(read(s, now))
        if i % 50 == 0:
            print(f"  … {i}/{len(syms)}")
        last3 = [r.get("error", "") for r in rows[-3:]]
        if len(rows) >= 3 and all(e.startswith("Kite 4") or "missing" in e for e in last3) and len(set(last3)) == 1:
            raise SystemExit(f"Same Kite error 3 times — stopping: {last3[0]}")
    momentum_scores(rows)
    for r in rows:
        if "rules" in r:
            r["score_ok"] = r.get("score", 0) > min_score
            r["pass"] = all(r["rules"].values()) and r["score_ok"]
    good = sorted((r for r in rows if r.get("pass")), key=lambda r: -r["score"])
    errs = [r["symbol"] for r in rows if r.get("error")]
    n = len(rows) - len(errs)
    four = sum(1 for r in rows if "rules" in r and all(r["rules"].values()))
    for r in good:
        print(f"  ✓ {r['symbol']:12s} {r['price']:>10,.2f} | EMA20 {r['ema20']:,.2f} ({r['vs_ema20_pct']:+.1f}%) "
              f"EMA50 {r['ema50']:,.2f} | 1-yr high {r['yr_high']:,.2f} (−{r['below_high_pct']:.1f}%) | "
              f"score {r['score']:.0f} (RSI {r['rsi']:.0f}, 3M {r['r3m'] or 0:+.0f}%)")
    print(f"  → {len(good)} pass all rules ({four} pass the 4 price rules) of {n}"
          + (f"; {len(errs)} not read: {', '.join(errs[:15])}{' …' if len(errs) > 15 else ''}" if errs else ""))
    DATA.mkdir(exist_ok=True)
    out = DATA / f"trendlyne_{now:%Y%m%d_%H%M}.csv"
    cols = ["symbol", "pass", "score", "price", "ema20", "vs_ema20_pct", "ema50", "yr_high", "below_high_pct",
            "rsi", "r1m", "r3m", "r6m", "date", "error"]
    pd.DataFrame(rows).reindex(columns=cols).sort_values("score", ascending=False).to_csv(out, index=False)
    print(f"  saved {out}")
    if a.no_push:
        return
    when = f"{good[0]['date']:%d %b}" if good else f"{now:%d %b}"
    title = f"Trendlyne screen: {len(good)} stocks ({when}, Nifty 500)"
    lines = [f"{r['symbol']}  ₹{r['price']:,.1f}  score {r['score']:.0f}  "
             f"EMA20 {r['vs_ema20_pct']:+.1f}%  high −{r['below_high_pct']:.1f}%" for r in good[:45]]
    body = "\n".join(lines) or "No stock passes all rules today."
    if len(good) > 45:
        body += f"\n… and {len(good) - 45} more (see the CSV on the droplet)"
    body += (f"\n\n{four} pass the 4 price rules; momentum score > {min_score:g} (our own score, "
             "not Trendlyne's)")
    push(env("TRENDLYNE_NTFY_TOPIC", "srikanth-trendlyne"), title, body)
    print(f"  pushed to ntfy topic {env('TRENDLYNE_NTFY_TOPIC', 'srikanth-trendlyne')}")


if __name__ == "__main__":
    main()
