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

Retracements (step 4), one scenario per EMA (20 and 50), today's candles only:
  Bullish: 20 EMA above 50 EMA. The touch candle itself is the signal — no confirmation
  candle: its low comes within 0.1 % of the EMA (or under it) and it closes above the EMA or at
  par (no more than 0.1 % below it), above the CPR (20 EMA scenario: low stays above the 50 EMA).
  Listed in every push until a candle closes more than 0.1 % below that EMA, at / below the CPR
  top, or 20 EMA < 50 EMA.
  Bearish: the mirror (high within 0.1 % of the EMA or over it, close below or at par, below the
  CPR bottom, 20 EMA below 50 EMA).
  The day's first candle (09:15) never counts as a touch — on gap days the EMAs still carry
  yesterday's prices (CHANDU_FIRST_TOUCH=1 to allow it).

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
TOUCH_PCT = 0.001              # a "touch" = within 0.1 % of the EMA
AWAY_PCT = 0.002               # CPR retracement: must first close this far beyond the CPR today
MIN_CPR_PCT = 0.0005           # CPR retracement only when the CPR is at least 0.05 % of the price wide
                               # (CHANDU_MIN_CPR_PCT, in %) — a razor-thin CPR is "touched" by any wiggle


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


def retracements(m10: pd.DataFrame, e20: pd.Series, e50: pd.Series, top: float, bot: float,
                 session: datetime.date, first_touch: bool = False) -> dict:
    """Walk today's closed candles; the retracements still valid at the last one.

    The touch candle itself is the signal (no confirmation candle): its low reaches the EMA
    (within 0.1 % or under it) and it closes above the EMA or at par with it (no more than
    0.1 % below). Bearish is the mirror.
    Returns {("bull"|"bear", 20|50): {touch, extreme, ema_at_touch}}.
    """
    c, h, l = (m10[k].to_numpy(dtype=float) for k in ("Close", "High", "Low"))
    a20, a50 = e20.to_numpy(dtype=float), e50.to_numpy(dtype=float)
    idx = m10.index
    day = [i for i in range(len(m10)) if idx[i].date() == session]
    active: dict = {}
    was_above = was_below = False   # closed clearly beyond the CPR earlier today
    for i in day:
        for n, ema in ((20, a20), (50, a50)):
            kb, ks = ("bull", n), ("bear", n)
            par_lo, par_hi = ema[i] * (1 - TOUCH_PCT), ema[i] * (1 + TOUCH_PCT)
            # end of an active retracement: a close clearly past the EMA, back at the CPR,
            # or the EMAs crossed
            if kb in active and (c[i] < par_lo or c[i] <= top or a20[i] <= a50[i]):
                active.pop(kb)
            if ks in active and (c[i] > par_hi or c[i] >= bot or a20[i] >= a50[i]):
                active.pop(ks)
            if not first_touch and i == day[0]:
                continue            # 09:15: the EMAs still carry yesterday's prices
            # bullish touch: low at / under the EMA, close above or at par, above the CPR
            if kb not in active and (l[i] <= par_hi and c[i] >= par_lo and c[i] > top and a20[i] > a50[i]
                                     and (n == 50 or l[i] > a50[i] * (1 + TOUCH_PCT))):
                active[kb] = {"touch": idx[i], "extreme": l[i], "ema_at_touch": ema[i]}
            # bearish touch: high at / over the EMA, close below or at par, below the CPR
            if ks not in active and (h[i] >= par_lo and c[i] <= par_hi and c[i] < bot and a20[i] < a50[i]
                                     and (n == 50 or h[i] < a50[i] * (1 - TOUCH_PCT))):
                active[ks] = {"touch": idx[i], "extreme": h[i], "ema_at_touch": ema[i]}
        # CPR retracement — a pullback to the CPR after closing clearly beyond it earlier today
        kb, ks = ("bull", "cpr"), ("bear", "cpr")
        if kb in active and c[i] < top * (1 - TOUCH_PCT):
            active.pop(kb)                              # closed back inside the CPR
        if ks in active and c[i] > bot * (1 + TOUCH_PCT):
            active.pop(ks)
        if (first_touch or i != day[0]) and (top - bot) >= c[i] * MIN_CPR_PCT:
            if (kb not in active and was_above and l[i] <= top * (1 + TOUCH_PCT)
                    and c[i] >= top * (1 - TOUCH_PCT) and a20[i] > a50[i]):
                active[kb] = {"touch": idx[i], "extreme": l[i], "ema_at_touch": top}
            if (ks not in active and was_below and h[i] >= bot * (1 - TOUCH_PCT)
                    and c[i] <= bot * (1 + TOUCH_PCT) and a20[i] < a50[i]):
                active[ks] = {"touch": idx[i], "extreme": h[i], "ema_at_touch": bot}
        was_above = was_above or c[i] > top * (1 + AWAY_PCT)
        was_below = was_below or c[i] < bot * (1 - AWAY_PCT)
    return active


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
    s20 = m10["Close"].ewm(span=EMA_FAST, adjust=False).mean()
    s50 = m10["Close"].ewm(span=EMA_SLOW, adjust=False).mean()
    e20, e50 = float(s20.iloc[-1]), float(s50.iloc[-1])
    top, bot = max(tc, cc, bc), min(tc, cc, bc)
    out["retr"] = retracements(m10, s20, s50, top, bot, session, env("CHANDU_FIRST_TOUCH") == "1")
    bull = {"above CPR": close > top, "above EMA20": close > e20, "EMA20 > EMA50": e20 > e50}
    bear = {"below CPR": close < bot, "below EMA20": close < e20, "EMA20 < EMA50": e20 < e50}
    out.update({"candle": m10.index[-1], "close": close, "tc": tc, "cc": cc, "bc": bc,
                "cpr_top": top, "cpr_bot": bot, "ema20": e20, "ema50": e50,
                "bull_rules": bull, "bear_rules": bear,
                "ok": all(bull.values()), "bear": all(bear.values()),
                "vs_cpr_pct": (close / top - 1) * 100,          # bullish: % above the CPR top
                "below_cpr_pct": (1 - close / bot) * 100})       # bearish: % below the CPR bottom
    return out


def line_name(k) -> str:
    """Name of the retracement line: EMA20 / EMA50 / CPR top / CPR bottom."""
    if k[1] == "cpr":
        return "CPR top" if k[0] == "bull" else "CPR bottom"
    return f"EMA{k[1]}"


PUSH_MAX_BYTES = 3500   # ntfy turns messages over ~4 KB into attachments / cuts them


def _chunks(body: str, limit: int = PUSH_MAX_BYTES) -> list[str]:
    """Split at line breaks so every part stays under the ntfy size limit (never mid-line)."""
    parts, cur = [], ""
    for line in body.split("\n"):
        cand = f"{cur}\n{line}" if cur else line
        if cur and len(cand.encode("utf-8")) > limit:
            parts.append(cur)
            cur = line
        else:
            cur = cand
    if cur:
        parts.append(cur)
    return parts


def push(topic: str, title: str, body: str) -> None:
    """One push, or several numbered pushes (1/3, 2/3 …) when the scan is too long for one."""
    parts = _chunks(body)
    for n, part in enumerate(parts, 1):
        t = title if len(parts) == 1 else f"({n}/{len(parts)}) {title}"
        try:
            requests.post(f"https://ntfy.sh/{topic}", data=part.encode("utf-8"),
                          headers={"Title": t.encode("utf-8"), "Tags": "chart_with_upwards_trend"},
                          timeout=10)
        except Exception as e:
            print(f"  ntfy error: {e}")
        if n < len(parts):
            time.sleep(1)       # keep the parts in order on the phone


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
    retr = {k: [(r, r["retr"][k]) for r in res if not r.get("error") and k in r.get("retr", {})]
            for k in (("bull", 20), ("bull", 50), ("bull", "cpr"), ("bear", 20), ("bear", 50), ("bear", "cpr"))}
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
        rt = "".join(f" | {'BULL' if k[0] == 'bull' else 'BEAR'} RETR {line_name(k)} (touch {v['touch']:%H:%M})"
                     for k, v in r.get("retr", {}).items())
        print(f"  {r['symbol']:12s} {tag} close {fmt(r['close']):>10} | "
              f"CPR TC {fmt(r['tc'])} CC {fmt(r['cc'])} BC {fmt(r['bc'])} | EMA20 {fmt(r['ema20'])} "
              f"EMA50 {fmt(r['ema50'])}{why}{rt}")
    cand = next((r["candle"] for r in res if r.get("candle") is not None), None)
    when = f"{cand:%d %b %H:%M}" if cand is not None else f"{now:%H:%M}"
    errs = [r["symbol"] for r in res if r.get("error")]
    n = len(res) - len(errs)
    print("  → retracements: " + ", ".join(f"{k[0]} {line_name(k)} {len(v)}" for k, v in retr.items()))
    print(f"  → {len(good)} bullish, {len(bad)} bearish, {n - len(good) - len(bad)} neither, of {n}"
          + (f" ({len(errs)} errors: {', '.join(errs)})" if errs else ""))
    if not do_push:
        return
    nr_b = sum(len(v) for k, v in retr.items() if k[0] == "bull")
    nr_s = sum(len(v) for k, v in retr.items() if k[0] == "bear")
    title = (f"Chandu: {len(good)} bull · {len(bad)} bear · retr {nr_b}↑ {nr_s}↓ of {n} "
             f"({when} 10-min candle)")
    up = [f"{r['symbol']}  {fmt(r['close'])}  CPR+{r['vs_cpr_pct']:.2f}%  "
          f"EMA20 {fmt(r['ema20'])} > 50 {fmt(r['ema50'])}" for r in good]
    dn = [f"{r['symbol']}  {fmt(r['close'])}  CPR−{r['below_cpr_pct']:.2f}%  "
          f"EMA20 {fmt(r['ema20'])} < 50 {fmt(r['ema50'])}" for r in bad]
    def rlines(k):
        word = "low" if k[0] == "bull" else "high"
        return "\n".join(f"{r['symbol']}  {fmt(r['close'])}  touched {v['touch']:%H:%M} "
                         f"({word} {fmt(v['extreme'])} vs {line_name(k)} {fmt(v['ema_at_touch'])})"
                         for r, v in retr[k]) or "none"
    body = (f"🟢 BULLISH ({len(good)})\n" + ("\n".join(up) or "none")
            + f"\n\n🔴 BEARISH ({len(bad)})\n" + ("\n".join(dn) or "none")
            + f"\n\n🟡 BULL RETRACEMENT — 20 EMA ({len(retr[('bull', 20)])})\n" + rlines(("bull", 20))
            + f"\n🟡 BULL RETRACEMENT — 50 EMA ({len(retr[('bull', 50)])})\n" + rlines(("bull", 50))
            + f"\n🟣 BULL RETRACEMENT — CPR ({len(retr[('bull', 'cpr')])})\n" + rlines(("bull", "cpr"))
            + f"\n\n🟠 BEAR RETRACEMENT — 20 EMA ({len(retr[('bear', 20)])})\n" + rlines(("bear", 20))
            + f"\n🟠 BEAR RETRACEMENT — 50 EMA ({len(retr[('bear', 50)])})\n" + rlines(("bear", 50))
            + f"\n🟤 BEAR RETRACEMENT — CPR ({len(retr[('bear', 'cpr')])})\n" + rlines(("bear", "cpr"))
            + "\n\nBull: close > CPR (TC, CC, BC) · close > EMA20 · EMA20 > EMA50"
            + "\nBear: close < CPR (TC, CC, BC) · close < EMA20 · EMA20 < EMA50")
    if errs:
        body += f"\nNot found: {', '.join(errs)}"
    push(env("CHANDU_NTFY_TOPIC", "chandu-logic"), title, body)
    print(f"  pushed to ntfy topic {env('CHANDU_NTFY_TOPIC', 'chandu-logic')}")


def main() -> None:
    global MIN_CPR_PCT
    try:
        MIN_CPR_PCT = float(env("CHANDU_MIN_CPR_PCT") or MIN_CPR_PCT * 100) / 100
    except ValueError:
        pass
    ap = argparse.ArgumentParser(description="chandu-logic 10-min bullish scan")
    ap.add_argument("--loop", action="store_true", help="scan after every 10-min close, 09:25–15:35")
    ap.add_argument("--cron", action="store_true",
                    help="for a 10-min cron: one scan, only on weekdays 09:25–15:35 IST (else exit quietly)")
    ap.add_argument("--no-push", action="store_true", help="log only, no ntfy push")
    ap.add_argument("--symbols", help="comma list instead of symbols.txt")
    a = ap.parse_args()
    symbols = load_symbols(a.symbols)
    if a.cron:
        now = datetime.datetime.now(IST)
        mins = now.hour * 60 + now.minute
        # the 09:15 candle closes at 09:25 — earlier runs would read yesterday's candles
        if now.weekday() >= 5 or not (9 * 60 + 25 <= mins <= 15 * 60 + 35):
            return
        if now.second < 15:
            time.sleep(15 - now.second)    # let Kite finish the candle that just closed
        scan(symbols, not a.no_push)
        return
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
