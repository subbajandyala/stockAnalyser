#!/usr/bin/env python3
"""
index_bot.py — NIFTY / BANKNIFTY / SENSEX options: market structure + option OI
(core/ios_strategy.py, core/structure.py).

Every 5 minutes, ~20 s after a 5-min candle closes (09:20–15:30 IST, Mon–Fri):
  1. Kite data per index: 5-min / 15-min / daily candles, nearest future (5-min with OI), the
     option chain of the nearest expiry (next one on expiry day; BANKNIFTY monthly), ATM ±10.
     Each chain is saved (data/ios/<date>/<SYM>_<HHMM>.csv.gz); "recent" = the one ~15 min ago.
  2. Open trade → manage_step on the last closed candle (stop / breakeven / T1 / trail /
     change of character / OI against / 15:15).
  3. No open trade → setups A (pullback into a fresh zone) / B (breakout + retest) with a
     structure stop and the next obstacle as target (≥ 1.5R), then the OI check (≥ IOS_OI_NEED
     of 4, nothing against). Pass → alert, and with IOS_AUTO_TRADE=1 a BUY of the ATM CE / PE
     (IOS_LOTS lots, LIMIT + OCO GTT as the emergency stop; T1 in the GTT for 1 lot, far target
     for 2 lots where the bot books half at T1 and trails the rest).

.env: IOS_AUTO_TRADE=0 (1 = real orders) · IOS_NTFY_TOPIC=index-oi-structure · IOS_LOTS=1 ·
      IOS_SYMBOLS=NIFTY,BANKNIFTY,SENSEX · IOS_MAX_TRADES=3 · IOS_MAX_LOSSES=2 · IOS_OI_NEED=2 ·
      IOS_PUSH_SKIPS=1 (also push setups that the OI check blocked)
Usage: python scripts/index_bot.py [--once] [--dry-run] [--force]
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

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import ios_strategy as ios      # noqa: E402
from core import kite_order as ko         # noqa: E402

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
DATA = ROOT / "data" / "ios"
EVENTS_FILE = ROOT / "pnc_events.txt"
INDEX = {
    "NIFTY":     {"token": 256265, "ltp": "NSE:NIFTY 50",   "exch": "NFO", "step": 50},
    "BANKNIFTY": {"token": 260105, "ltp": "NSE:NIFTY BANK", "exch": "NFO", "step": 100},
    "SENSEX":    {"token": 265,    "ltp": "BSE:SENSEX",     "exch": "BFO", "step": 100},
}
CHAIN_STRIKES = 10
RECENT_MIN = 15
SCAN_FROM, SCAN_UNTIL = datetime.time(9, 20), datetime.time(15, 30)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("index_bot")

ko._EXCHANGE.setdefault("BANKNIFTY", "NFO")
ko._STRIKE_STEP.setdefault("BANKNIFTY", 100)


def load_env() -> dict:
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
        self.dry = dry
        self.auto = env.get("IOS_AUTO_TRADE", "0") == "1" and not dry
        self.topic = env.get("IOS_NTFY_TOPIC", "index-oi-structure")
        self.lots = max(1, int(env.get("IOS_LOTS", "1")))
        self.syms = [s.strip().upper() for s in env.get("IOS_SYMBOLS", "NIFTY,BANKNIFTY,SENSEX").split(",")
                     if s.strip().upper() in INDEX]
        self.max_trades = int(env.get("IOS_MAX_TRADES", "3"))
        self.max_losses = int(env.get("IOS_MAX_LOSSES", "2"))
        self.oi_need = int(env.get("IOS_OI_NEED", str(ios.OI_NEED)))
        self.push_skips = env.get("IOS_PUSH_SKIPS", "1") == "1"
        self.key = env.get("KITE_API_KEY", "")
        self.token = env.get("KITE_ACCESS_TOKEN", "")


def push(ctx: Ctx, title: str, body: str, prio: str = "default", tags: str = "") -> None:
    LOG.info(f"PUSH {title}\n{body}")
    if ctx.dry or not ctx.topic:
        return
    try:
        requests.post(f"https://ntfy.sh/{ctx.topic}", data=body.encode("utf-8"), timeout=10,
                      headers={"Title": title.encode("ascii", "ignore").decode(), "Priority": prio, "Tags": tags})
    except Exception as e:
        LOG.warning(f"ntfy failed: {e}")


def day_dir(day: datetime.date) -> Path:
    d = DATA / day.isoformat()
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_state(day: datetime.date) -> dict:
    p = day_dir(day) / "state.json"
    return json.loads(p.read_text()) if p.exists() else {"trades": 0, "losses": 0, "open": {}, "done": [], "seen": []}


def save_state(day: datetime.date, st: dict) -> None:
    p = day_dir(day) / "state.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1, default=str))
    tmp.replace(p)


def event_day(day: datetime.date) -> bool:
    if not EVENTS_FILE.exists():
        return False
    return any(line.split("#")[0].strip() == day.isoformat() for line in EVENTS_FILE.read_text().splitlines())


# ── Kite data ──────────────────────────────────────────────────────────────────────────────

def candles(kite, token: int, days: int, interval: str, now: datetime.datetime, oi: bool = False) -> pd.DataFrame:
    frm = (now - datetime.timedelta(days=days)).replace(hour=9, minute=0, second=0, microsecond=0)
    rows = kite.historical_data(token, frm.replace(tzinfo=None), now.replace(tzinfo=None), interval, oi=oi)
    time.sleep(0.35)
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    idx = pd.to_datetime(df.pop("date"))
    if getattr(idx.dt, "tz", None) is not None:
        idx = idx.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    df.index = pd.DatetimeIndex(idx)
    if interval != "day":
        mins = {"5minute": 5, "15minute": 15}[interval]
        df = df[df.index + pd.Timedelta(minutes=mins) <= pd.Timestamp(now.replace(tzinfo=None))]
    return df


_INSTR: dict = {}


def instruments(kite, exch: str, day: datetime.date) -> pd.DataFrame:
    if _INSTR.get(exch, (None,))[0] == day:
        return _INSTR[exch][1]
    f = day_dir(day) / f"instr_{exch}.pkl"
    if f.exists():
        df = pd.read_pickle(f)
    else:
        df = pd.DataFrame(kite.instruments(exch))
        df = df[df["name"].isin(list(INDEX)) & df["instrument_type"].isin(["FUT", "CE", "PE"])]
        df = df[["instrument_token", "tradingsymbol", "name", "instrument_type", "strike", "expiry", "lot_size"]].copy()
        df["expiry"] = pd.to_datetime(df["expiry"]).dt.date
        df.to_pickle(f)
    _INSTR[exch] = (day, df)
    return df


def nearest(ins: pd.DataFrame, sym: str, types: list, day: datetime.date):
    rows = ins[(ins["name"] == sym) & ins["instrument_type"].isin(types) & (ins["expiry"] >= day)]
    ex = sorted(set(rows["expiry"]))
    if not ex:
        return None
    return ex[1] if ex[0] == day and len(ex) > 1 else ex[0]


def chain_now(kite, ins: pd.DataFrame, sym: str, expiry, spot: float) -> pd.DataFrame:
    step = INDEX[sym]["step"]
    atm = round(spot / step) * step
    want = {float(atm + i * step) for i in range(-CHAIN_STRIKES, CHAIN_STRIKES + 1)}
    rows = ins[(ins["name"] == sym) & (ins["expiry"] == expiry) & ins["instrument_type"].isin(["CE", "PE"])
               & ins["strike"].astype(float).isin(want)]
    exch = INDEX[sym]["exch"]
    keys = [f"{exch}:{t}" for t in rows["tradingsymbol"]]
    q = kite.quote(keys) if keys else {}
    time.sleep(1.05)
    out = []
    for _, r in rows.iterrows():
        d = q.get(f"{exch}:{r['tradingsymbol']}")
        if not d:
            continue
        dp = d.get("depth") or {}
        out.append({"strike": float(r["strike"]), "type": r["instrument_type"], "ts": r["tradingsymbol"],
                    "token": int(r["instrument_token"]), "oi": float(d.get("oi") or 0),
                    "ltp": float(d.get("last_price") or 0),
                    "bid": float((dp.get("buy") or [{}])[0].get("price") or 0),
                    "ask": float((dp.get("sell") or [{}])[0].get("price") or 0), "spot": spot})
    return pd.DataFrame(out)


def base_chain(kite, sym: str, day: datetime.date, now_chain: pd.DataFrame) -> pd.DataFrame:
    """Previous day's closing OI / premium per contract (day candles with OI), cached per day."""
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
        new.append({"strike": float(r["strike"]), "type": r["type"],
                    "oi": float(rows[-1].get("oi") or 0) if rows else r["oi"],
                    "ltp": float(rows[-1]["close"]) if rows else r["ltp"]})
    if new:
        base = pd.concat([base, pd.DataFrame(new)], ignore_index=True)
        base.to_csv(f, index=False)
    return base


def recent_chain(sym: str, day: datetime.date, now: datetime.datetime) -> pd.DataFrame | None:
    cut = (now - datetime.timedelta(minutes=RECENT_MIN)).strftime("%H%M")
    snaps = sorted(day_dir(day).glob(f"{sym}_[0-9][0-9][0-9][0-9].csv.gz"))
    older = [p for p in snaps if p.name[len(sym) + 1:len(sym) + 5] <= cut]
    return pd.read_csv(older[-1]) if older else None


def gather(kite, sym: str, now: datetime.datetime, cache: dict) -> dict | None:
    day, meta = now.date(), INDEX[sym]
    m5 = candles(kite, meta["token"], 7, "5minute", now)
    m15 = candles(kite, meta["token"], 12, "15minute", now)
    if m5.empty or m15.empty or not (m5.index.date == day).any():
        LOG.info(f"{sym}: no 5-min candles today yet")
        return None
    if sym not in cache:
        d = candles(kite, meta["token"], 200, "day", now)
        d.index = [x.date() for x in d.index]
        cache[sym] = d
    ins = instruments(kite, meta["exch"], day)
    opt = ins[(ins["name"] == sym) & ins["instrument_type"].isin(["CE", "PE"])]
    if len(opt):
        ko.LOT_SIZE[sym] = int(opt["lot_size"].iloc[0])
    fut_exp = nearest(ins, sym, ["FUT"], day)
    fut = None
    if fut_exp:
        f = ins[(ins["name"] == sym) & (ins["instrument_type"] == "FUT") & (ins["expiry"] == fut_exp)].iloc[0]
        f5 = candles(kite, int(f["instrument_token"]), 5, "5minute", now, oi=True)
        if len(f5) and "oi" in f5 and (f5.index.date < day).any() and (f5.index.date == day).any():
            prev, cur = f5[f5.index.date < day].iloc[-1], f5.iloc[-1]
            fut = ios.fut_label(float(cur["close"]) - float(prev["close"]), float(cur["oi"]) - float(prev["oi"]))
    spot = float(kite.ltp([meta["ltp"]])[meta["ltp"]]["last_price"])
    exp = nearest(ins, sym, ["CE", "PE"], day)
    ch = chain_now(kite, ins, sym, exp, spot) if exp else pd.DataFrame()
    cv = {}
    if not ch.empty:
        base = base_chain(kite, sym, day, ch)
        rec = recent_chain(sym, day, now)
        rec_spot = float(rec["spot"].iloc[0]) if rec is not None and "spot" in rec and len(rec) else None
        cv = ios.chain_view(ch, base, rec, spot, rec_spot)
        ch.to_csv(day_dir(day) / f"{sym}_{now:%H%M}.csv.gz", index=False)
    return {"sym": sym, "now": now, "m5": m5, "m15": m15, "daily": cache[sym], "spot": spot,
            "fut": fut, "cv": cv, "chain": ch, "expiry": exp}


# ── trades ─────────────────────────────────────────────────────────────────────────────────

def describe(g: dict, side: str | None = None) -> str:
    cv = g["cv"]
    f = lambda v: "-" if v is None else f"{v:,.0f}"
    pct = lambda k: "-" if cv.get(k) is None else f"{cv[k]:+.1f}%"
    return (f"spot {g['spot']:,.1f} · fut {g['fut'] or '-'} · PCR {cv.get('pcr', '-')} (15 min ago "
            f"{cv.get('pcr_earlier', '-')}) · walls call {f(cv.get('call_wall'))} / put {f(cv.get('put_wall'))}\n"
            f"near-ATM OI last 15 min: calls {pct('c_rec_pct')} · puts {pct('p_rec_pct')} · expiry {g['expiry']}")


def close_trade(ctx: Ctx, kite, st: dict, sym: str, why: str, px: float) -> None:
    tr = st["open"].pop(sym)
    ce = tr["side"] == "CE"
    if why.startswith("T1"):
        px = tr["target"]
    pts = (px - tr["entry"]) * (1 if ce else -1)
    if tr.get("half_pts") is not None:
        pts = (tr["half_pts"] + pts) / 2
    pnl, info = None, ""
    if not tr.get("paper"):
        if why != "GTT closed":
            ko.delete_gtts_for(kite, tr["ts"])
            q = ko.open_qty(kite, tr["ts"])
            if q and q > 0:
                ok, info = ko.sell_and_confirm(kite, sym, tr["ts"], q)
                if not ok:
                    try:
                        tr["gtt_id"] = ko.place_oco_gtt(kite, sym, tr["ts"], q, tr["sl"], tr["gtt_target"], tr["entry_price"])
                    except Exception as e:
                        info += f" | GTT restore failed: {e}"
                    st["open"][sym] = tr
                    push(ctx, f"IOS {sym} EXIT FAILED", f"{why}: {info} — retry next scan", "high", "warning")
                    return
        pnl = ko.contract_pnl(kite, tr["ts"])
    loss = (pnl < 0) if pnl is not None else pts < 0
    st["losses"] += loss
    tr.update({"exit": why, "pts": round(pts, 1), "R": round(pts / tr["risk"], 2), "pnl": pnl,
               "exit_time": datetime.datetime.now(IST).strftime("%H:%M")})
    st["done"].append(tr)
    push(ctx, f"IOS {sym} EXIT {'LOSS' if loss else 'WIN'}{' (paper)' if tr.get('paper') else ''}",
         f"{tr.get('ts') or sym} {tr['side']} · {tr['kind']}: {why}\nindex {tr['entry']:,.1f} → {px:,.1f} "
         f"({pts:+.1f} pts, {pts / tr['risk']:+.2f}R)" + (f" · P&L ₹{pnl:,.0f}" if pnl is not None else "")
         + (f"\n{info}" if info else "") + f"\ntoday: {st['trades']} trades, {st['losses']} losses",
         "high", "x" if loss else "white_check_mark")


def manage(ctx: Ctx, kite, st: dict, g: dict) -> None:
    sym = g["sym"]
    tr = st["open"].get(sym)
    if not tr:
        return
    if not tr.get("paper") and ko.open_qty(kite, tr["ts"]) == 0:
        ko.delete_gtts_for(kite, tr["ts"])
        close_trade(ctx, kite, st, sym, "GTT closed", g["spot"])
        return
    against = ios.oi_check(tr["side"], g["cv"], g["fut"])[2]
    was_be = tr["be"]
    if g["now"].time().replace(tzinfo=None) >= ios.SQUARE_OFF:          # clock, not only the candle time
        act, why = "exit", "15:15 square-off"
    else:
        act, why = ios.manage_step(tr, g["m5"], against)
    c = float(g["m5"]["close"].iloc[-1])
    if act == "book_half":
        tr["half_pts"] = (tr["target"] - tr["entry"]) * (1 if tr["side"] == "CE" else -1)
        msg = "half booked"
        if not tr.get("paper"):
            half = (tr["qty"] // ko.LOT_SIZE[sym] // 2) * ko.LOT_SIZE[sym]
            try:
                ko.modify_oco_gtt(kite, sym, tr["ts"], tr["gtt_id"], tr["qty"] - half, tr["sl"], tr["gtt_target"],
                                  tr["entry_price"])
                oid = ko.sell_partial(kite, sym, tr["ts"], half)
                px, reason = ko._fill_price(kite, oid, ko._EXCHANGE[sym], tr["ts"]) if oid else (0.0, "not placed")
            except Exception as e:
                px, reason = 0.0, str(e)
            if px:
                tr["qty"] -= half
                msg = f"sold {half} @ ₹{px:.2f}, {tr['qty']} trailing"
            else:
                tr["booked"], tr["half_pts"] = False, None
                try:
                    ko.modify_oco_gtt(kite, sym, tr["ts"], tr["gtt_id"], tr["qty"], tr["sl"], tr["gtt_target"],
                                      tr["entry_price"])
                except Exception:
                    pass
                msg = f"half NOT sold ({reason}) — retry next scan"
        push(ctx, f"IOS {sym} T1 — {msg}", f"{why}\nstop now {tr['stop']:,.1f} (trailing under 5-min swings)",
             "high", "moneybag")
    elif act == "exit":
        close_trade(ctx, kite, st, sym, why, c)
    elif tr["be"] and not was_be:
        push(ctx, f"IOS {sym} +1R — stop to entry", f"{tr['side']} {tr['kind']} · stop {tr['stop']:,.1f}", "low")


def enter(ctx: Ctx, kite, st: dict, g: dict) -> None:
    sym, day = g["sym"], g["now"].date()
    if sym in st["open"] or st["trades"] >= ctx.max_trades or st["losses"] >= ctx.max_losses or event_day(day):
        return
    cv = g["cv"]
    setups, why = ios.find_setups(sym, g["m5"], g["m15"], g["daily"], day, (cv.get("call_wall"), cv.get("put_wall")))
    for w in why:
        LOG.info(f"{sym} dropped: {w}")
    for s in sorted(setups, key=lambda x: -x.rr):
        key = f"{sym}|{s.side}|{s.kind}|{s.level}|{s.t}"
        if key in st["seen"]:
            continue
        st["seen"].append(key)
        pts, reasons, against = ios.oi_check(s.side, cv, g["fut"])
        head = (f"{s.side} {s.kind} at {s.level}\nentry {s.entry:,.1f} · stop {s.stop:,.1f} (risk {s.risk:.0f}) · "
                f"T1 {s.target:,.1f} = {s.target_label} ({s.rr:.1f}R) · {', '.join(s.notes)}\n"
                f"OI {pts}/4: {'; '.join(reasons) or 'none'}{' · OI AGAINST' if against else ''}\n{describe(g)}")
        if pts < ctx.oi_need or against:
            LOG.info(f"{sym} {s.side} setup blocked by OI")
            if ctx.push_skips:
                push(ctx, f"IOS {sym} {s.side} setup - OI says no", head, "low", "pause_button")
            continue
        tr = ios.new_trade(s, ctx.lots)
        tr["paper"] = not ctx.auto
        if ctx.auto:
            ko.MAX_LOTS[sym] = ctx.lots
            res = ko.place_order_for_signal(sym, s.side, g["spot"], ctx.key, ctx.token, chart_sl=s.stop,
                                            chart_target=s.target, trailing=ctx.lots >= 2)
            st["trades"] += 1
            if res.get("error"):
                push(ctx, f"IOS {sym} {s.side} ORDER FAILED", f"{res['error']}\n\n{head}", "high", "warning")
                return
            tr.update({"ts": res["tradingsymbol"], "qty": res["qty"], "entry_price": res["entry_price"],
                       "sl": res["sl"], "gtt_target": res["target"], "gtt_id": res["gtt_id"]})
            top = (f"BUY {res['tradingsymbol']} × {res['qty']} @ ₹{res['entry_price']:.2f} · GTT stop ₹{res['sl']}"
                   f" / target ₹{res['target']}")
        else:
            st["trades"] += 1
            row = g["chain"]
            atm = round(g["spot"] / INDEX[sym]["step"]) * INDEX[sym]["step"]
            o = row[(row["strike"] == atm) & (row["type"] == s.side)] if len(row) else row
            tr["ts"] = o["ts"].iloc[0] if len(o) else None
            top = f"ALERT (IOS_AUTO_TRADE=0, no order): BUY {tr['ts'] or sym + ' ATM ' + s.side}" + \
                  (f" ~₹{o['ltp'].iloc[0]:.2f}" if len(o) else "")
        st["open"][sym] = tr
        push(ctx, f"IOS {sym} BUY {s.side} - {s.kind}", top + "\n\n" + head, "urgent" if ctx.auto else "high",
             "chart_with_upwards_trend" if s.side == "CE" else "chart_with_downwards_trend")
        return


def scan(dry: bool = False, force: bool = False, cache: dict | None = None) -> None:
    now = datetime.datetime.now(IST)
    day = now.date()
    if not force and (now.weekday() >= 5 or not SCAN_FROM <= now.time().replace(tzinfo=None) <= SCAN_UNTIL):
        LOG.info("outside market hours — --force to scan anyway")
        return
    ctx = Ctx(load_env(), dry)
    if not ctx.key or not ctx.token:
        LOG.error("KITE_API_KEY / KITE_ACCESS_TOKEN missing in .env")
        return
    kite = ko.kite_for(ctx.key, ctx.token)
    cache = cache if cache is not None else {}
    if cache.get("_day") != day:
        cache.clear()
        cache["_day"] = day
    st = load_state(day)
    for sym in ctx.syms:
        try:
            g = gather(kite, sym, now, cache)
            if not g:
                continue
            manage(ctx, kite, st, g)
            enter(ctx, kite, st, g)
            LOG.info(f"{sym} {g['spot']:,.1f} fut {g['fut']} PCR {g['cv'].get('pcr')} open={sym in st['open']} "
                     f"trades {st['trades']} losses {st['losses']} auto={ctx.auto}")
        except Exception as e:
            LOG.error(f"{sym}: {e}\n{traceback.format_exc()}")
        finally:
            save_state(day, st)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="no orders, no pushes")
    ap.add_argument("--force", action="store_true", help="scan outside market hours")
    a = ap.parse_args()
    if a.once:
        scan(a.dry_run, a.force)
        return
    LOG.info("index_bot started — scans 20 s after every 5-min candle close")
    cache: dict = {}
    while True:
        now = datetime.datetime.now(IST)
        nxt = now.replace(second=0, microsecond=0) + datetime.timedelta(minutes=5 - now.minute % 5, seconds=20)
        time.sleep(max(1.0, (nxt - now).total_seconds()))
        try:
            scan(a.dry_run, a.force, cache)
        except Exception as e:
            LOG.error(f"scan failed: {e}\n{traceback.format_exc()}")


if __name__ == "__main__":
    main()
