"""
Order rules for the OI monitor's STRONG signals — shared by the live monitor
(scripts/oi_monitor_bot.py) and the evening report (scripts/oi_signals_report.py).

For a STRONG CE / PE at time `upto`:
  1. after 15:00                     → no order
  2. before 09:40                    → EARLY: re-checked from 09:40 while still STRONG
  3. NIFTY up ≥ 0.5 % and above its open → no PE (WATCH) · down ≥ 0.5 % and below → no CE
  4. futures buildup from current + next month OI together (rollover-proof), vs yesterday's
     close: CE needs long buildup, PE short buildup — else WATCH
  5. support / resistance of the last 2 days (screener/levels.py) within 1 %:
       opened beyond yesterday's range straight into it → WATCH
       else → WAIT for a 10-min close beyond it (BREAK → BUY), cancelled when it holds,
       OI turns or it is 15:00
     no level within 1 % → BUY
Candles come from Kite history for the signalled stocks only (a few calls per signal).
"""

from __future__ import annotations

import datetime
import time

import pandas as pd

from screener import levels
from screener import oi_monitor as om

FIRST_ORDER = "0940"
ORDER_UNTIL = "1500"          # from 15:00 on: no orders
FRESH = {"CE": "long buildup", "PE": "short buildup"}
# OI_FUT_WEAK=1 (default): the futures rule also accepts the "weak" label that still moves the
# signal's way — short covering for a CE, long unwinding for a PE (those WATCH signals did well,
# 12 Nov). 0 = only long / short buildup, as before.
WEAK = {"CE": "short covering", "PE": "long unwinding"}
WEAK_OK = True
NIFTY_TOKEN = 256265
MARKET_PCT = 0.5


class KiteData:
    """Kite history with a per-run cache. `tokens` (NSE symbol → token) and `futs`
    (name → [{instrument_token, expiry}, …]) are loaded from Kite when not given."""

    def __init__(self, kite, tokens: dict | None = None, futs: dict | None = None):
        self.k = kite
        self._tokens = tokens
        self._futs = futs
        self.cache: dict = {}

    @property
    def tokens(self) -> dict:
        if self._tokens is None:
            self._tokens = {r["tradingsymbol"]: r["instrument_token"]
                            for r in self.k.instruments("NSE") if r["segment"] == "NSE"}
        return self._tokens

    def futures(self, name: str, day: datetime.date) -> list[dict]:
        """Current and next month futures on `day`."""
        if self._futs is None:
            self._futs = {}
            for r in self.k.instruments("NFO"):
                if r["instrument_type"] == "FUT":
                    self._futs.setdefault(r["name"], []).append(
                        {"instrument_token": r["instrument_token"], "expiry": r["expiry"]})
        rows = []
        for r in self._futs.get(name, []):
            exp = r["expiry"] if isinstance(r["expiry"], datetime.date) else datetime.date.fromisoformat(str(r["expiry"]))
            if exp >= day:
                rows.append({**r, "expiry": exp})
        return sorted(rows, key=lambda r: r["expiry"])[:2]

    def candles(self, name: str, interval: str, start: datetime.date, end: datetime.date,
                token: int | None = None, oi: bool = False) -> pd.DataFrame:
        key = (name, interval, start, end, token, oi)
        if key not in self.cache:
            tok = token if token is not None else (NIFTY_TOKEN if name == "NIFTY" else self.tokens[name])
            rows = self.k.historical_data(tok, datetime.datetime.combine(start, datetime.time(9, 15)),
                                          datetime.datetime.combine(end, datetime.time(15, 30)), interval, oi=oi)
            time.sleep(0.35)                     # Kite historical: 3 requests / second
            df = pd.DataFrame(rows)
            if len(df):
                df["date"] = pd.to_datetime(df["date"])
                if df["date"].dt.tz is not None:
                    df["date"] = df["date"].dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
                df = df.set_index("date").sort_index()
            self.cache[key] = df
        return self.cache[key]


def last_closed(df: pd.DataFrame | None, upto: datetime.datetime, minutes: int = 10) -> pd.Series | None:
    if df is None or df.empty:
        return None
    done = df[df.index + datetime.timedelta(minutes=minutes) <= upto]
    return done.iloc[-1] if len(done) else None


def market(day: datetime.date, upto: datetime.datetime, kd: KiteData) -> tuple[str | None, str]:
    """NIFTY's direction at `upto`: 'up' / 'down' / None, plus a short text."""
    m10 = kd.candles("NIFTY", "10minute", day, day)
    d = kd.candles("NIFTY", "day", day - datetime.timedelta(days=10), day - datetime.timedelta(days=1))
    c = last_closed(m10, upto)
    if c is None or d.empty or m10.empty:
        return None, "NIFTY n/a"
    prev_close, day_open, now = float(d["close"].iloc[-1]), float(m10["open"].iloc[0]), float(c["close"])
    pct = (now / prev_close - 1) * 100
    txt = f"NIFTY {pct:+.2f}% ({'above' if now > day_open else 'below'} its open)"
    if pct >= MARKET_PCT and now > day_open:
        return "up", txt
    if pct <= -MARKET_PCT and now < day_open:
        return "down", txt
    return None, txt


def combined_fut(name: str, day: datetime.date, upto: datetime.datetime, kd: KiteData) -> tuple[str | None, str]:
    """Futures buildup from current + next month OI together (rollover moves OI between them)."""
    futs = kd.futures(name, day)
    if not futs:
        return None, "no futures"
    oi_now = oi_prev = 0.0
    px_now = px_prev = None
    for k, f in enumerate(futs):
        h = kd.candles(name, "10minute", day - datetime.timedelta(days=6), day, token=f["instrument_token"], oi=True)
        if h is None or h.empty or "oi" not in h:
            continue
        c = last_closed(h, upto)
        prev = h[h.index.date < day]
        if c is None or prev.empty:
            continue
        oi_now += float(c["oi"])
        oi_prev += float(prev["oi"].iloc[-1])
        if k == 0:
            px_now, px_prev = float(c["close"]), float(prev["close"].iloc[-1])
    if px_now is None or not oi_prev:
        return None, "futures n/a"
    pct, oi_pct = (px_now / px_prev - 1) * 100, (oi_now / oi_prev - 1) * 100
    lab = om.fut_label(pct, oi_now - oi_prev)
    return lab, f"fut {lab} (price {pct:+.1f}%, OI {oi_pct:+.1f}% both months)"


def decide(name: str, side: str, day: datetime.date, upto: datetime.datetime, price: float,
           call_wall: float | None, put_wall: float | None, kd: KiteData,
           fallback_fut: str | None = None) -> dict:
    """Rules 3–5 for a signal at `upto` (callers handle 1 and 2).
    → {"kind": "BUY" | "WAIT" | "WATCH", "text": …, "level": levels.Level | None}"""
    mk, mk_txt = market(day, upto, kd)
    if (side == "PE" and mk == "up") or (side == "CE" and mk == "down"):
        return {"kind": "WATCH", "text": f"{mk_txt}: no {side} against the market", "level": None}
    lab, fut_txt = combined_fut(name, day, upto, kd)
    fut = lab or fallback_fut
    ok = (FRESH[side], WEAK[side]) if WEAK_OK else (FRESH[side],)
    if fut not in ok:
        return {"kind": "WATCH", "level": None,
                "text": f"{fut_txt if lab else 'fut ' + str(fut or 'n/a')} (needs {' or '.join(ok)})"}
    if fut == WEAK[side]:
        fut_txt += " (accepted, OI_FUT_WEAK=1)"
    note = f"{mk_txt} · {fut_txt}"
    daily = kd.candles(name, "day", day - datetime.timedelta(days=12), day - datetime.timedelta(days=1))
    m15 = kd.candles(name, "15minute", day - datetime.timedelta(days=6), day)
    if daily.empty or len(daily) < 2:
        return {"kind": "BUY", "text": f"{note} · no daily candles for levels", "level": None}
    daily = daily.copy()
    daily.index = [pd.Timestamp(d).date() for d in daily.index]
    lv = levels.build(daily, m15, day, upto, call_wall, put_wall)
    z = levels.check(side, price, lv)
    if z is None:
        return {"kind": "BUY", "text": f"{note} · no level within {levels.LEVEL_PCT:g}%", "level": None}
    today15 = m15[m15.index.date == day] if len(m15) else m15
    prev = daily.iloc[-1]
    if len(today15) and levels.gap_into(side, float(today15["open"].iloc[0]), float(prev["high"]),
                                        float(prev["low"]), z):
        return {"kind": "WATCH", "text": f"gap into {z.text()}", "level": z}
    return {"kind": "WAIT", "level": z, "note": note,
            "text": f"{note} · {'support' if side == 'PE' else 'resistance'} {z.text()} — "
                    f"buys only on a 10-min close {'below' if side == 'PE' else 'above'} "
                    f"{z.lo * (1 - levels.BREAK_PCT / 100) if side == 'PE' else z.hi * (1 + levels.BREAK_PCT / 100):.2f}"}


def watch_break(name: str, side: str, z: levels.Level, day: datetime.date, since: datetime.datetime,
                upto: datetime.datetime, kd: KiteData) -> tuple[str | None, str, datetime.datetime | None]:
    """10-min candles that closed after `since` and by `upto`: ('BREAK' | 'HELD' | None, text, close time)."""
    m10 = kd.candles(name, "10minute", day, day)
    if m10 is None or m10.empty:
        return None, "", None
    for ts, c in m10.iterrows():
        end = ts + datetime.timedelta(minutes=10)
        if end <= since or end > upto:
            continue
        if levels.broke(side, z, float(c["close"])):
            return "BREAK", f"{z.text()} broken — 10-min close {float(c['close']):g} at {end:%H:%M}", end
        if levels.held(side, z, float(c["close"])):
            return "HELD", f"{z.text()} held — price {float(c['close']):g} at {end:%H:%M}", end
    return None, "", None
