#!/usr/bin/env python3
"""
praveen_bot.py — NIFTY / SENSEX intraday option buying (confluence strategy, core/pnc_strategy.py).

Every 5 minutes, ~20 s after a 5-min candle closes (09:20–15:30 IST, Mon–Fri):
  1. Kite data per symbol: index 5m / 15m / daily, nearest futures 5m (VWAP, volume), India VIX,
     option chain (nearest weekly, next weekly on expiry day, ATM ±10 strikes).
  2. Open trade → exit checks: GTT already fired, 5m close beyond the structure stop, index at T1,
     5m Supertrend flip, 2 futures closes on the wrong side of VWAP, 15:15 square-off.
  3. No open trade → breakout-retest on the last closed 5m candle, score (≥ 80) and the mandatory
     filters → BUY ATM CE / PE, 1 lot, LIMIT + OCO GTT (option stop 20–25 % below entry, target T1).

Orders only with PNC_AUTO_TRADE=1 in .env (separate from KITE_AUTO_TRADE); otherwise alerts and
paper trades. Pushes go to ntfy topic PNC_NTFY_TOPIC (default praveen-intraday-options).

Usage:
  python scripts/praveen_bot.py               # daemon (what start_droplet.sh runs)
  python scripts/praveen_bot.py --once        # one scan now
  python scripts/praveen_bot.py --once --dry-run --force   # one scan, no orders / pushes, any time
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import sys
import time
import traceback
from pathlib import Path

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import kite_order as ko          # noqa: E402
from core import pnc_strategy as ps        # noqa: E402

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
DATA = ROOT / "data" / "pnc"
EVENTS_FILE = ROOT / "pnc_events.txt"
SYMBOLS = {
    "NIFTY":  {"token": 256265, "ltp": "NSE:NIFTY 50", "exch": "NFO", "step": 50},
    "SENSEX": {"token": 265,    "ltp": "BSE:SENSEX",   "exch": "BFO", "step": 100},
}
VIX_KEY = "NSE:INDIA VIX"
CHAIN_STRIKES = 10            # ATM ± 10
EARLIER_MIN = 25              # "earlier" chain snapshot for PCR / wall movement
SQUARE_OFF = datetime.time(15, 15)
SCAN_FROM, SCAN_UNTIL = datetime.time(9, 20), datetime.time(15, 30)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("praveen_bot")


# ── env / pushes / state ───────────────────────────────────────────────────────────────────

def load_env() -> dict:
    """.env re-read every scan, so the 08:00 token refresh is picked up without a restart."""
    env = {}
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.split(" #")[0].strip().strip('"').strip("'")
    return env


class Ctx:
    def __init__(self, env: dict, dry: bool):
        self.env = env
        self.dry = dry
        self.auto = env.get("PNC_AUTO_TRADE", "0") == "1" and not dry
        self.topic = env.get("PNC_NTFY_TOPIC", "praveen-intraday-options")
        self.max_trades = int(env.get("PNC_MAX_TRADES", "3"))
        self.max_losses = int(env.get("PNC_MAX_LOSSES", "2"))
        self.push_skips = env.get("PNC_PUSH_SKIPS", "1") == "1"
        self.key = env.get("KITE_API_KEY", "")
        self.token = env.get("KITE_ACCESS_TOKEN", "")


def push(ctx: Ctx, title: str, body: str, prio: str = "default", tags: str = "") -> None:
    LOG.info(f"PUSH {title}\n{body}")
    if ctx.dry or not ctx.topic:
        return
    try:
        requests.post(f"https://ntfy.sh/{ctx.topic}", data=body.encode("utf-8"), timeout=10,
                      headers={"Title": title.encode("ascii", "ignore").decode(), "Priority": prio,
                               "Tags": tags})
    except Exception as e:
        LOG.warning(f"ntfy failed: {e}")


def day_dir(day: datetime.date) -> Path:
    d = DATA / day.isoformat()
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_state(day: datetime.date) -> dict:
    p = day_dir(day) / "state.json"
    if p.exists():
        return json.loads(p.read_text())
    return {"trades": 0, "losses": 0, "open": {}, "done": [], "seen": []}


def save_state(day: datetime.date, st: dict) -> None:
    p = day_dir(day) / "state.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1, default=str))
    tmp.replace(p)


def event_day(day: datetime.date) -> bool:
    if not EVENTS_FILE.exists():
        return False
    for line in EVENTS_FILE.read_text().splitlines():
        if line.split("#")[0].strip() == day.isoformat():
            return True
    return False


# ── Kite data ──────────────────────────────────────────────────────────────────────────────

def candles(kite, token: int, days: int, interval: str, now: datetime.datetime,
            closed_only: bool = True, oi: bool = False) -> pd.DataFrame:
    frm = (now - datetime.timedelta(days=days)).replace(hour=9, minute=0, second=0, microsecond=0)
    rows = kite.historical_data(token, frm.replace(tzinfo=None), now.replace(tzinfo=None), interval, oi=oi)
    time.sleep(0.35)                                   # Kite: 3 historical calls / s
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    idx = pd.to_datetime(df.pop("date"))
    if getattr(idx.dt, "tz", None) is not None:
        idx = idx.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    df.index = pd.DatetimeIndex(idx)
    if closed_only and interval != "day":
        mins = {"5minute": 5, "15minute": 15, "minute": 1}[interval]
        cut = now.replace(tzinfo=None) - datetime.timedelta(minutes=mins)
        df = df[df.index <= cut]                        # drop the candle still forming
    return df


_INSTR: dict = {}


def instruments(kite, exch: str, day: datetime.date) -> pd.DataFrame:
    """Futures + options rows of NIFTY / SENSEX, downloaded once a day (cached on disk)."""
    if _INSTR.get(exch, (None,))[0] == day:
        return _INSTR[exch][1]
    f = day_dir(day) / f"instr_{exch}.pkl"
    if f.exists():
        df = pd.read_pickle(f)
    else:
        df = pd.DataFrame(kite.instruments(exch))
        df = df[df["name"].isin(list(SYMBOLS)) & df["instrument_type"].isin(["FUT", "CE", "PE"])]
        df = df[["instrument_token", "tradingsymbol", "name", "instrument_type", "strike", "expiry"]].copy()
        df["expiry"] = pd.to_datetime(df["expiry"]).dt.date
        df.to_pickle(f)
    _INSTR[exch] = (day, df)
    return df


def option_expiry(ins: pd.DataFrame, sym: str, day: datetime.date) -> datetime.date | None:
    """Nearest weekly; on its expiry day the next one (same rule as kite_order)."""
    ex = sorted(set(ins[(ins["name"] == sym) & ins["instrument_type"].isin(["CE", "PE"])
                        & (ins["expiry"] >= day)]["expiry"]))
    if not ex:
        return None
    return ex[1] if ex[0] == day and len(ex) > 1 else ex[0]


def future_row(ins: pd.DataFrame, sym: str, day: datetime.date):
    f = ins[(ins["name"] == sym) & (ins["instrument_type"] == "FUT") & (ins["expiry"] >= day)].sort_values("expiry")
    if f.empty:
        return None
    return f.iloc[1] if f.iloc[0]["expiry"] == day and len(f) > 1 else f.iloc[0]


def chain_now(kite, ins: pd.DataFrame, sym: str, expiry, spot: float) -> pd.DataFrame:
    step = SYMBOLS[sym]["step"]
    atm = round(spot / step) * step
    want = {atm + i * step for i in range(-CHAIN_STRIKES, CHAIN_STRIKES + 1)}
    rows = ins[(ins["name"] == sym) & (ins["expiry"] == expiry) & ins["instrument_type"].isin(["CE", "PE"])
               & ins["strike"].astype(float).isin(want)]
    exch = SYMBOLS[sym]["exch"]
    keys = [f"{exch}:{t}" for t in rows["tradingsymbol"]]
    q = kite.quote(keys) if keys else {}
    out = []
    for _, r in rows.iterrows():
        d = q.get(f"{exch}:{r['tradingsymbol']}")
        if not d:
            continue
        depth = d.get("depth") or {}
        bid = (depth.get("buy") or [{}])[0].get("price") or 0
        ask = (depth.get("sell") or [{}])[0].get("price") or 0
        out.append({"strike": float(r["strike"]), "type": r["instrument_type"], "ts": r["tradingsymbol"],
                    "token": int(r["instrument_token"]), "oi": float(d.get("oi") or 0),
                    "ltp": float(d.get("last_price") or 0), "bid": float(bid), "ask": float(ask),
                    "volume": float(d.get("volume") or 0)})
    return pd.DataFrame(out)


def base_chain(kite, sym: str, day: datetime.date, now_chain: pd.DataFrame) -> pd.DataFrame:
    """Previous day's closing OI / premium per contract (Kite day candles with OI), cached in the
    day folder; strikes that come into the ATM ±10 window later are fetched when first seen."""
    f = day_dir(day) / f"{sym}_base.csv"
    base = pd.read_csv(f) if f.exists() else pd.DataFrame(columns=["strike", "type", "oi", "ltp"])
    have = set(zip(base["strike"].astype(float), base["type"]))
    new = []
    for _, r in now_chain.iterrows():
        if (float(r["strike"]), r["type"]) in have:
            continue
        try:
            rows = kite.historical_data(int(r["token"]), day - datetime.timedelta(days=10),
                                        day - datetime.timedelta(days=1), "day", oi=True)
            time.sleep(0.35)
        except Exception as e:
            LOG.warning(f"base {r['ts']}: {e}")
            continue
        if rows:
            new.append({"strike": float(r["strike"]), "type": r["type"],
                        "oi": float(rows[-1].get("oi") or 0), "ltp": float(rows[-1]["close"])})
        else:                                           # new contract → no history, use now
            new.append({"strike": float(r["strike"]), "type": r["type"], "oi": r["oi"], "ltp": r["ltp"]})
    if new:
        base = pd.concat([base, pd.DataFrame(new)], ignore_index=True)
        base.to_csv(f, index=False)
    return base


def earlier_chain(sym: str, day: datetime.date, now: datetime.datetime) -> pd.DataFrame | None:
    cut = (now - datetime.timedelta(minutes=EARLIER_MIN)).strftime("%H%M")
    snaps = sorted(day_dir(day).glob(f"{sym}_[0-9][0-9][0-9][0-9].csv.gz"))
    older = [p for p in snaps if p.name[len(sym) + 1:len(sym) + 5] <= cut]
    pick = older[-1] if older else (snaps[0] if snaps else None)
    return pd.read_csv(pick) if pick is not None else None


# ── one symbol ─────────────────────────────────────────────────────────────────────────────

def gather(kite, sym: str, now: datetime.datetime, vix: tuple, daily_cache: dict) -> dict | None:
    day = now.date()
    meta = SYMBOLS[sym]
    m5 = candles(kite, meta["token"], 7, "5minute", now)
    m15 = candles(kite, meta["token"], 12, "15minute", now)
    if m5.empty or m15.empty or not (m5.index.date == day).any():
        LOG.info(f"{sym}: no 5m candles today yet")
        return None
    if sym not in daily_cache:
        d = candles(kite, meta["token"], 200, "day", now, closed_only=False)
        d.index = [x.date() for x in d.index]
        daily_cache[sym] = d
    lv = ps.levels(daily_cache[sym], day)
    if lv is None:
        return None
    ins = instruments(kite, meta["exch"], day)
    fut = future_row(ins, sym, day)
    f5 = candles(kite, int(fut["instrument_token"]), 7, "5minute", now) if fut is not None else pd.DataFrame()
    fut_close = fut_vwap = slope = vol = vol_avg = None
    vw_series = None
    if not f5.empty and (f5.index.date == day).any():
        vw_series = ps.vwap(f5)
        ft = f5[f5.index.date == day]
        fv = vw_series[f5.index.date == day]
        fut_close, fut_vwap = float(ft["close"].iloc[-1]), float(fv.iloc[-1])
        slope = float(fv.iloc[-1] - fv.iloc[-4]) if len(fv) >= 4 else None
        vol = float(f5["volume"].iloc[-1])
        prev = f5["volume"].iloc[-21:-1]
        vol_avg = float(prev.mean()) if len(prev) else None
    spot = float(kite.ltp([meta["ltp"]])[meta["ltp"]]["last_price"])
    expiry = option_expiry(ins, sym, day)
    ch_now = chain_now(kite, ins, sym, expiry, spot) if expiry else pd.DataFrame()
    chain = {}
    if not ch_now.empty:
        base = base_chain(kite, sym, day, ch_now)
        chain = ps.chain_read(ch_now, base, earlier_chain(sym, day, now), spot)
        ch_now.to_csv(day_dir(day) / f"{sym}_{now:%H%M}.csv.gz", index=False)
    st5 = ps.supertrend(m5)
    st15 = ps.supertrend(m15)
    return {"sym": sym, "now": now, "m5": m5, "lv": lv, "spot": spot, "chain": chain, "ch_now": ch_now,
            "expiry": expiry, "st5": float(st5.iloc[-1]), "st5_prev": float(st5.iloc[-2]),
            "st15": float(st15.iloc[-1]), "f5": f5, "vw": vw_series, "fut_close": fut_close,
            "fut_vwap": fut_vwap, "slope": slope, "vol": vol, "vol_avg": vol_avg,
            "vix": vix[0], "vix_chg": vix[1], "adx": float(ps.adx(m5).iloc[-1])}


def atm_option(g: dict, side: str) -> dict:
    """ATM contract of `side`: premium, spread %, IV, delta."""
    ch = g["ch_now"]
    if ch.empty:
        return {}
    step = SYMBOLS[g["sym"]]["step"]
    atm = round(g["spot"] / step) * step
    r = ch[(ch["strike"] == atm) & (ch["type"] == side)]
    if r.empty:
        return {}
    r = r.iloc[0]
    bid, ask, ltp = r["bid"], r["ask"], r["ltp"]
    mid = (bid + ask) / 2 if bid and ask else ltp
    spread = (ask - bid) / mid * 100 if bid and ask and mid else None
    exp = datetime.datetime.combine(g["expiry"], datetime.time(15, 30), IST)
    t = max((exp - g["now"]).total_seconds(), 600) / (365 * 86400)
    iv = ps.implied_vol(mid, g["spot"], atm, t, side == "CE")
    dl = ps.delta(g["spot"], atm, t, iv, side == "CE") if iv else None
    return {"strike": int(atm), "ts": r["ts"], "ltp": ltp, "spread": spread, "iv": iv, "delta": dl}


def build_inputs(g: dict, side: str, trig: dict | None, opt: dict) -> ps.Inputs:
    price = trig["close"] if trig else g["spot"]
    return ps.Inputs(
        symbol=g["sym"], side=side, time=g["now"].time().replace(tzinfo=None), spot=price, lv=g["lv"],
        trigger=trig, st15=g["st15"], st5=g["st5"], fut_close=g["fut_close"], fut_vwap=g["fut_vwap"],
        vwap_slope=g["slope"], vol=g["vol"], vol_avg=g["vol_avg"], chain=g["chain"], vix=g["vix"],
        vix_chg_pct=g["vix_chg"], atm_iv=opt.get("iv"), spread_pct=opt.get("spread"), adx=g["adx"],
        next_level=ps.next_level(side, price, g["lv"], g["chain"]))


def describe(g: dict, side: str, x: ps.Inputs, sc: int, parts: dict, opt: dict) -> str:
    lv, ch = g["lv"], g["chain"]
    f = lambda v, n=0: "-" if v is None else f"{v:,.{n}f}"
    st = lambda v: "green" if v == 1 else "red" if v == -1 else "-"
    lines = [
        f"{g['sym']} {side}  score {sc}/100  ({', '.join(f'{k} {v}' for k, v in parts.items())})",
        f"spot {f(g['spot'], 1)}  CPR {f(lv.bottom, 1)}–{f(lv.top, 1)} ({lv.width_class} {lv.width_pct:.2f}%)"
        f"  R1 {f(lv.r1)}  S1 {f(lv.s1)}  PDH {f(lv.pdh)}  PDL {f(lv.pdl)}",
        f"Supertrend 15m {st(g['st15'])} / 5m {st(g['st5'])}   ADX {f(g['adx'], 0)}",
        f"fut {f(g['fut_close'], 1)} vs VWAP {f(g['fut_vwap'], 1)} (slope {f(g['slope'], 1)})"
        f"  vol {f(g['vol'])} / avg {f(g['vol_avg'])}",
        f"PCR {ch.get('pcr', '-')} (earlier {ch.get('pcr_earlier', '-')})  chg-OI PCR {ch.get('coi_pcr', '-')}"
        f"  call wall {f(ch.get('call_wall'))}  put wall {f(ch.get('put_wall'))}",
        f"OI vs prev close (ATM±3): calls {f(ch.get('call_doi'))} (prem {ch.get('call_dprem', '-')})"
        f"  puts {f(ch.get('put_doi'))} (prem {ch.get('put_dprem', '-')})",
        f"VIX {f(g['vix'], 2)} ({f(g['vix_chg'], 1)}%)  ATM IV {f(opt.get('iv') and opt['iv'] * 100, 1)}%"
        f"  delta {f(opt.get('delta'), 2)}  spread {f(opt.get('spread'), 2)}%",
    ]
    if x.trigger:
        t = x.trigger
        risk = abs(t["close"] - t["stop"])
        t1 = t["close"] + 1.5 * risk if side == "CE" else t["close"] - 1.5 * risk
        lines.append(f"trigger: retest of {t['level']} {t['price']:,.1f} (breakout {t['breakout_time'][11:16]})"
                     f"  entry {t['close']:,.1f}  stop {t['stop']:,.1f}  risk {risk:.0f} pts  T1 {t1:,.1f}"
                     f"  next level {f(x.next_level)}")
    return "\n".join(lines)


# ── exits ──────────────────────────────────────────────────────────────────────────────────

def close_trade(ctx: Ctx, st: dict, sym: str, why: str, exit_spot: float, kite=None) -> None:
    tr = st["open"].pop(sym)
    ce = tr["side"] == "CE"
    pts = (exit_spot - tr["entry_spot"]) if ce else (tr["entry_spot"] - exit_spot)
    pnl = None
    info = ""
    if not tr.get("paper") and kite is not None:
        if why != "GTT closed":
            ko.delete_gtts_for(kite, tr["ts"])
            q = ko.open_qty(kite, tr["ts"])
            if q and q > 0:
                ok, info = ko.sell_and_confirm(kite, sym, tr["ts"], q)
                if not ok:                               # keep it protected, retry next scan
                    try:
                        tr["gtt_id"] = ko.place_oco_gtt(kite, sym, tr["ts"], q, tr["sl"], tr["target"],
                                                        tr["entry_price"])
                    except Exception as e:
                        info += f" | GTT restore failed: {e}"
                    st["open"][sym] = tr
                    push(ctx, f"PNC {sym} EXIT FAILED", f"{why}: {info} — will retry next scan", "high", "warning")
                    return
        pnl = ko.contract_pnl(kite, tr["ts"])
    loss = (pnl < 0) if pnl is not None else (pts < 0)
    if loss:
        st["losses"] += 1
    tr.update({"exit_time": datetime.datetime.now(IST).strftime("%H:%M"), "exit_spot": exit_spot,
               "exit_why": why, "pts": round(pts, 1), "pnl": pnl})
    st["done"].append(tr)
    body = (f"{tr.get('ts') or sym + ' ' + tr['side']}: {why}\n"
            f"index {tr['entry_spot']:,.1f} → {exit_spot:,.1f} ({pts:+.1f} pts)"
            + (f"  P&L ₹{pnl:,.0f}" if pnl is not None else "") + (f"\n{info}" if info else "")
            + f"\ntoday: {st['trades']} trades, {st['losses']} losses")
    push(ctx, f"PNC {sym} EXIT {'LOSS' if loss else 'WIN'}{' (paper)' if tr.get('paper') else ''}", body,
         "high", "x" if loss else "white_check_mark")


def manage(ctx: Ctx, kite, st: dict, g: dict) -> None:
    sym = g["sym"]
    tr = st["open"].get(sym)
    if not tr:
        return
    ce = tr["side"] == "CE"
    if not tr.get("paper"):
        q = ko.open_qty(kite, tr["ts"])
        if q == 0:
            ko.delete_gtts_for(kite, tr["ts"])
            close_trade(ctx, st, sym, "GTT closed", g["spot"], kite)
            return
    last = g["m5"].iloc[-1]
    c = float(last["close"])
    t = g["now"].time().replace(tzinfo=None)
    why = None
    if t >= SQUARE_OFF:
        why = "15:15 square-off"
    elif (c < tr["stop"]) if ce else (c > tr["stop"]):
        why = f"5m close {c:,.1f} beyond structure stop {tr['stop']:,.1f}"
    elif (g["spot"] >= tr["t1"]) if ce else (g["spot"] <= tr["t1"]):
        why = f"T1 {tr['t1']:,.1f} reached"
    elif g["st5"] == (-1 if ce else 1) and g["m5"].index[-1] > pd.Timestamp(tr["entry_candle"]):
        why = "5m Supertrend flipped"
    elif g["vw"] is not None:
        f5 = g["f5"][g["f5"].index.date == g["now"].date()]
        vw = g["vw"][g["f5"].index.date == g["now"].date()]
        if len(f5) >= 2:
            wrong = ((f5["close"] < vw) if ce else (f5["close"] > vw)).iloc[-2:]
            after = f5.index[-2] > pd.Timestamp(tr["entry_candle"])
            if wrong.all() and after:
                why = "2 futures closes on the wrong side of VWAP"
    if why:
        close_trade(ctx, st, sym, why, c if "stop" in why else g["spot"], kite)


# ── entries ────────────────────────────────────────────────────────────────────────────────

def enter(ctx: Ctx, kite, st: dict, g: dict) -> None:
    sym, day = g["sym"], g["now"].date()
    if sym in st["open"]:
        return
    best = None
    for side in ("CE", "PE"):
        trig = ps.breakout_retest(g["m5"], g["lv"], side, day)
        if not trig:
            continue
        key = f"{sym}|{side}|{trig['level']}|{trig['breakout_time']}|{g['m5'].index[-1]}"
        if key in st["seen"]:
            continue
        st["seen"].append(key)
        opt = atm_option(g, side)
        x = build_inputs(g, side, trig, opt)
        sc, parts = ps.score(x)
        why = ps.mandatory(x, st["trades"], st["losses"], ctx.max_trades, ctx.max_losses, event_day(day))
        if sc < ps.MIN_SCORE:
            why.insert(0, f"score {sc} < {ps.MIN_SCORE}")
        cand = (sc, side, trig, opt, x, parts, why)
        if not why and (best is None or sc > best[0]):
            best = cand
        elif why:
            LOG.info(f"{sym} {side} SKIP: {'; '.join(why)}")
            if ctx.push_skips:
                push(ctx, f"PNC {sym} {side} skipped (score {sc})",
                     "not taken: " + "; ".join(why) + "\n\n" + describe(g, side, x, sc, parts, opt), "low", "pause_button")
    if not best:
        return
    sc, side, trig, opt, x, parts, _ = best
    risk = abs(trig["close"] - trig["stop"])
    t1 = trig["close"] + 1.5 * risk if side == "CE" else trig["close"] - 1.5 * risk
    tr = {"sym": sym, "side": side, "time": g["now"].strftime("%H:%M"), "entry_spot": trig["close"],
          "stop": trig["stop"], "t1": t1, "score": sc, "level": trig["level"],
          "entry_candle": str(g["m5"].index[-1]), "paper": not ctx.auto}
    text = describe(g, side, x, sc, parts, opt)
    if ctx.auto:
        ko.MAX_LOTS.update({"NIFTY": 1, "SENSEX": 1})
        res = ko.place_order_for_signal(sym, side, g["spot"], ctx.key, ctx.token,
                                        chart_sl=trig["stop"], chart_target=t1)
        if res.get("error"):
            push(ctx, f"PNC {sym} {side} ORDER FAILED", f"{res['error']}\n\n{text}", "high", "warning")
            if not res.get("sent"):
                return
            st["trades"] += 1                    # a sent-but-unfilled order still counts
            return
        tr.update({k: res.get(k) for k in ("qty", "entry_price", "sl", "target", "gtt_id", "order_id")})
        tr["ts"] = res["tradingsymbol"]
        head = (f"BUY {res['tradingsymbol']} × {res['qty']} @ ₹{res['entry_price']:.2f}"
                f"  GTT stop ₹{res['sl']} / target ₹{res['target']}")
    else:
        tr["ts"] = opt.get("ts")
        head = (f"ALERT (no order, PNC_AUTO_TRADE=0): BUY {opt.get('ts', sym + ' ATM ' + side)}"
                f" @ ~₹{opt.get('ltp', 0):.2f}")
    st["trades"] += 1
    st["open"][sym] = tr
    push(ctx, f"PNC {sym} BUY {side} score {sc}", head + "\n\n" + text, "urgent" if ctx.auto else "high",
         "chart_with_upwards_trend" if side == "CE" else "chart_with_downwards_trend")


# ── scan loop ──────────────────────────────────────────────────────────────────────────────

def scan(dry: bool = False, force: bool = False, daily_cache: dict | None = None) -> None:
    now = datetime.datetime.now(IST)
    day = now.date()
    if not force and (now.weekday() >= 5 or not (SCAN_FROM <= now.time().replace(tzinfo=None) <= SCAN_UNTIL)):
        LOG.info("outside market hours — use --force to scan anyway")
        return
    ctx = Ctx(load_env(), dry)
    if not ctx.key or not ctx.token:
        LOG.error("KITE_API_KEY / KITE_ACCESS_TOKEN missing in .env")
        return
    kite = ko.kite_for(ctx.key, ctx.token)
    daily_cache = daily_cache if daily_cache is not None else {}
    if daily_cache.get("_day") != day:
        daily_cache.clear()
        daily_cache["_day"] = day
    st = load_state(day)
    try:
        q = kite.quote([VIX_KEY])[VIX_KEY]
        v, pc = float(q["last_price"]), float(q.get("ohlc", {}).get("close") or 0)
        vix = (v, (v / pc - 1) * 100 if pc else None)
    except Exception as e:
        LOG.warning(f"VIX: {e}")
        vix = (None, None)
    for sym in SYMBOLS:
        try:
            g = gather(kite, sym, now, vix, daily_cache)
            if not g:
                continue
            manage(ctx, kite, st, g)
            if now.time().replace(tzinfo=None) < SQUARE_OFF:
                enter(ctx, kite, st, g)
            LOG.info(f"{sym} spot {g['spot']:,.1f} ST15 {g['st15']:+.0f} ST5 {g['st5']:+.0f}"
                     f" PCR {g['chain'].get('pcr')} open={sym in st['open']}"
                     f" trades {st['trades']} losses {st['losses']} auto={ctx.auto}")
        except Exception as e:
            LOG.error(f"{sym}: {e}\n{traceback.format_exc()}")
        finally:
            save_state(day, st)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="one scan, then exit")
    ap.add_argument("--dry-run", action="store_true", help="no orders, no pushes")
    ap.add_argument("--force", action="store_true", help="scan outside market hours / weekends")
    a = ap.parse_args()
    if a.once:
        scan(a.dry_run, a.force)
        return
    LOG.info("praveen_bot daemon started — scans 20 s after every 5-min candle close")
    cache: dict = {}
    while True:
        now = datetime.datetime.now(IST)
        nxt = (now.replace(second=0, microsecond=0) + datetime.timedelta(minutes=5 - now.minute % 5)
               + datetime.timedelta(seconds=20))
        time.sleep(max(1.0, (nxt - now).total_seconds()))
        try:
            scan(a.dry_run, a.force, cache)
        except Exception as e:
            LOG.error(f"scan failed: {e}\n{traceback.format_exc()}")


if __name__ == "__main__":
    main()
