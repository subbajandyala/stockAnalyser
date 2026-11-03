"""
chandu_bot.py — scan stocks / indices on the 10-minute chart and push the bullish list.

Bullish when ALL of these hold on the last CLOSED 10-min candle:
  1. Close above the CPR — above TC, CC (pivot) and BC  (previous day's CPR)
  2. Close above the 20 EMA
  3. 20 EMA above the 50 EMA
Bearish is the mirror:
  1. Close below the CPR — below TC, CC and BC
  2. Close below the 20 EMA
  3. 20 EMA below the 50 EMA

CPR from the previous trading day's daily candle:
  CC (pivot) = (H + L + C) / 3,  BC = (H + L) / 2,  TC = 2 × CC − BC

Usage (from /root/stockAnalyser):
  python scripts/chandu_bot.py              one scan now, push the result
  python scripts/chandu_bot.py --no-push    one scan, log only
  python scripts/chandu_bot.py --loop       scan after every 10-min candle close, 09:25–15:35
  python scripts/chandu_bot.py --symbols RELIANCE,NIFTY

Symbols: symbols.txt (edit with nano). Pushes go to ntfy topic CHANDU_NTFY_TOPIC
(.env, default "chandu-logic").
"""
from __future__ import annotations

import argparse
import datetime
import pathlib
import sys
import time

import pandas as pd
import requests

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core import kite_data as _kd  # noqa: E402
from core.kite_data import fetch_ohlcv, get_token  # noqa: E402

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
TF_MIN = 10
EMA_FAST, EMA_SLOW = 20, 50
HIST_DAYS = 12                 # 10-min history for the 50 EMA (≈ 37 candles a day)
API_GAP_SEC = 0.35             # Kite historical API allows ~3 requests a second


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


def load_symbols(arg: str | None) -> list[str]:
    if arg:
        return [s.strip().upper() for s in arg.split(",") if s.strip()]
    lines = (ROOT / "symbols.txt").read_text(encoding="utf-8").splitlines()
    return [l.strip().upper() for l in lines if l.strip() and not l.strip().startswith("#")]


def cpr(prev: pd.Series) -> tuple[float, float, float]:
    h, l, c = float(prev["High"]), float(prev["Low"]), float(prev["Close"])
    cc = (h + l + c) / 3
    bc = (h + l) / 2
    tc = 2 * cc - bc
    return tc, cc, bc


def check(symbol: str, now: datetime.datetime) -> dict:
    """Read one symbol; returns its numbers and whether each rule passes."""
    out = {"symbol": symbol, "ok": False}
    if get_token(symbol) is None:
        out["error"] = "symbol not found on Kite"
        return out
    upto = now.replace(tzinfo=None, microsecond=0)      # "to" = now (IST), never a future date
    m10 = fetch_ohlcv(symbol, f"{TF_MIN}minute", days=HIST_DAYS, to_date=upto)
    time.sleep(API_GAP_SEC)
    if m10.empty or len(m10) < EMA_SLOW + 5:
        out["error"] = f"no 10-min data — {_kd.LAST_ERROR or f'only {len(m10)} candles'}"
        return out
    # drop the candle still forming
    if (now - m10.index[-1].to_pydatetime()).total_seconds() < TF_MIN * 60:
        m10 = m10.iloc[:-1]
    session = m10.index[-1].date()
    day = fetch_ohlcv(symbol, "day", days=15, to_date=upto)
    time.sleep(API_GAP_SEC)
    prev = day[[d.date() < session for d in day.index]] if not day.empty else day
    if prev.empty:
        out["error"] = f"no previous day candle — {_kd.LAST_ERROR or 'none before ' + str(session)}"
        return out
    tc, cc, bc = cpr(prev.iloc[-1])
    close = float(m10["Close"].iloc[-1])
    e20 = float(m10["Close"].ewm(span=EMA_FAST, adjust=False).mean().iloc[-1])
    e50 = float(m10["Close"].ewm(span=EMA_SLOW, adjust=False).mean().iloc[-1])
    top, bot = max(tc, cc, bc), min(tc, cc, bc)
    bull = {"above CPR": close > top, "above EMA20": close > e20, "EMA20 > EMA50": e20 > e50}
    bear = {"below CPR": close < bot, "below EMA20": close < e20, "EMA20 < EMA50": e20 < e50}
    out.update({"candle": m10.index[-1], "close": close, "tc": tc, "cc": cc, "bc": bc,
                "cpr_top": top, "cpr_bot": bot, "ema20": e20, "ema50": e50,
                "bull_rules": bull, "bear_rules": bear,
                "ok": all(bull.values()), "bear": all(bear.values()),
                "vs_cpr_pct": (close / top - 1) * 100,          # bullish: % above the CPR top
                "below_cpr_pct": (1 - close / bot) * 100})       # bearish: % below the CPR bottom
    return out


def push(topic: str, title: str, body: str) -> None:
    try:
        requests.post(f"https://ntfy.sh/{topic}", data=body.encode("utf-8")[:3900],
                      headers={"Title": title.encode("utf-8"), "Tags": "chart_with_upwards_trend"},
                      timeout=10)
    except Exception as e:
        print(f"  ntfy error: {e}")


def fmt(x: float) -> str:
    return f"{x:,.2f}" if x < 1000 else f"{x:,.1f}"


def scan(symbols: list[str], do_push: bool) -> None:
    now = datetime.datetime.now(IST)
    print(f"\n[{now:%Y-%m-%d %H:%M:%S}] chandu scan — {len(symbols)} symbols, {TF_MIN}-min chart")
    res = []
    for s in symbols:
        res.append(check(s, now))
        errs = [r.get("error", "") for r in res[-3:]]
        if len(res) >= 3 and all(e and ("Kite 4" in e or "missing" in e) for e in errs) and len(set(errs)) == 1:
            print(f"  same Kite error 3 times — stopping this scan: {errs[0]}")
            break
    good = sorted((r for r in res if r["ok"]), key=lambda r: -r["vs_cpr_pct"])
    bad = sorted((r for r in res if r.get("bear")), key=lambda r: -r["below_cpr_pct"])
    for r in res:
        if r.get("error"):
            print(f"  {r['symbol']:12s} ERROR  {r['error']}")
            continue
        tag = "BULLISH" if r["ok"] else "BEARISH" if r["bear"] else "   -   "
        why = ""
        if tag.strip() == "-":
            nb = [k for k, v in r["bull_rules"].items() if not v]
            ns = [k for k, v in r["bear_rules"].items() if not v]
            why = f" | bull fails: {', '.join(nb)} | bear fails: {', '.join(ns)}"
        print(f"  {r['symbol']:12s} {tag} close {fmt(r['close']):>10} | "
              f"CPR TC {fmt(r['tc'])} CC {fmt(r['cc'])} BC {fmt(r['bc'])} | EMA20 {fmt(r['ema20'])} "
              f"EMA50 {fmt(r['ema50'])}{why}")
    cand = next((r["candle"] for r in res if r.get("candle") is not None), None)
    when = f"{cand:%d %b %H:%M}" if cand is not None else f"{now:%H:%M}"
    errs = [r["symbol"] for r in res if r.get("error")]
    n = len(res) - len(errs)
    print(f"  → {len(good)} bullish, {len(bad)} bearish, {n - len(good) - len(bad)} neither, of {n}"
          + (f" ({len(errs)} errors: {', '.join(errs)})" if errs else ""))
    if not do_push:
        return
    title = f"Chandu: {len(good)} bullish · {len(bad)} bearish of {n} ({when} 10-min candle)"
    up = [f"{r['symbol']}  {fmt(r['close'])}  CPR+{r['vs_cpr_pct']:.2f}%  "
          f"EMA20 {fmt(r['ema20'])} > 50 {fmt(r['ema50'])}" for r in good]
    dn = [f"{r['symbol']}  {fmt(r['close'])}  CPR−{r['below_cpr_pct']:.2f}%  "
          f"EMA20 {fmt(r['ema20'])} < 50 {fmt(r['ema50'])}" for r in bad]
    body = (f"🟢 BULLISH ({len(good)})\n" + ("\n".join(up) or "none")
            + f"\n\n🔴 BEARISH ({len(bad)})\n" + ("\n".join(dn) or "none")
            + "\n\nBull: close > CPR (TC, CC, BC) · close > EMA20 · EMA20 > EMA50"
            + "\nBear: close < CPR (TC, CC, BC) · close < EMA20 · EMA20 < EMA50")
    if errs:
        body += f"\nNot found: {', '.join(errs)}"
    push(env("CHANDU_NTFY_TOPIC", "chandu-logic"), title, body)
    print(f"  pushed to ntfy topic {env('CHANDU_NTFY_TOPIC', 'chandu-logic')}")


def main() -> None:
    ap = argparse.ArgumentParser(description="chandu-logic 10-min bullish scan")
    ap.add_argument("--loop", action="store_true", help="scan after every 10-min close, 09:25–15:35")
    ap.add_argument("--no-push", action="store_true", help="log only, no ntfy push")
    ap.add_argument("--symbols", help="comma list instead of symbols.txt")
    a = ap.parse_args()
    symbols = load_symbols(a.symbols)
    if not a.loop:
        scan(symbols, not a.no_push)
        return
    print(f"chandu_bot loop — scans {TF_MIN}-min candles 09:25–15:35, {len(symbols)} symbols")
    last = None
    while True:
        now = datetime.datetime.now(IST)
        mins = now.hour * 60 + now.minute
        slot = (mins - 15) // TF_MIN          # 10-min candles start at 09:15
        if now.weekday() < 5 and 9 * 60 + 25 <= mins <= 15 * 60 + 35 and now.second >= 15 and slot != last:
            last = slot
            try:
                scan(symbols, not a.no_push)
            except Exception as e:
                print(f"  scan failed: {e}")
        time.sleep(20)


if __name__ == "__main__":
    main()
