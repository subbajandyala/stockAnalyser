#!/usr/bin/env python3
"""
pulse_bot.py — NIFTY Pulse daemon (strategy: core/pulse.py, spec NIFTY_CE_PE_Intraday_Bot_Spec.md).

Two loops in one process (start ~09:05, it stops itself at 15:20):
  SCAN   8 s after every 5-min close (09:20:08, 09:25:08 …): closed NIFTY spot + futures candles → indicators,
         levels → ARM a signal (trigger / stop / T1 / T2, valid 2 candles); with a position open → trail /
         profit lock / time stop on the closed candle.
  WATCH  every 15 s: spot LTP crosses the armed trigger (not overshot > 0.3R) → pick the 1-ITM weekly option,
         size it, BUY; position open → stop / T1 half / T2 / premium stop on LTP; 15:10 square-off.

PAPER is the default: real Kite data, no orders; fills at ask + ₹0.5 / LTP − ₹0.5, written to
data/pulse/paper_trades.csv. Real orders only with BOTH PULSE_MODE=live and PULSE_CONFIRM_LIVE=1.
Kill switch: create the file STOP_BOT in this folder → no new entries and the open position is squared off.

Settings: .env (Kite keys, shared) + .env.local (PULSE_* — any core/pulse.py Config field, e.g.
  PULSE_CAPITAL=200000 · PULSE_RISK_PCT=0.01 · PULSE_MODE=paper · PULSE_CONFIRM_LIVE=0 ·
  PULSE_NTFY_TOPIC=nifty-pulse · PULSE_PUSH_SKIPS=0 (1 = also push why each candle was skipped)).
Log / journal / state: pulse_bot.log, data/pulse/<date>/journal.txt, data/pulse/<date>/state.json (a restart
picks up the armed signal and the open position).

Usage: python scripts/pulse_bot.py [--once] [--dry-run] [--force]
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

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import kite_order as ko   # noqa: E402
from core import pulse as P         # noqa: E402

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
DATA = ROOT / "data" / "pulse"
SPOT_TOKEN, SPOT_KEY, VIX_KEY = 256265, "NSE:NIFTY 50", "NSE:INDIA VIX"
HOLIDAYS_FILE = ROOT / "pulse_holidays.txt"
OI_REFRESH_MIN, OI_STRIKES = 15, 10

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("pulse_bot")


def load_env() -> dict:
    env: dict = {}
    for name in (".env", ".env.local"):                 # .env.local overrides .env
        p = ROOT / name
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.split(" #")[0].strip().strip('"').strip("'")
    return env


def holidays() -> set:
    if not HOLIDAYS_FILE.exists():
        return set()
    out = set()
    for line in HOLIDAYS_FILE.read_text().splitlines():
        s = line.split("#")[0].strip()
        if s:
            try:
                out.add(datetime.date.fromisoformat(s))
            except ValueError:
                pass
    return out


def tick(x: float) -> float:
    return max(0.05, round(round(x / 0.05) * 0.05, 2))


def now_ist() -> datetime.datetime:
    return datetime.datetime.now(IST).replace(tzinfo=None)      # IST-naive, like Kite's candles


class Bot:
    def __init__(self, env: dict, dry: bool = False):
        self.env, self.dry = env, dry
        self.cfg = P.Config.from_env(env)
        self.live = self.cfg.mode == "live" and self.cfg.confirm_live and not dry
        if self.cfg.mode == "live" and not self.cfg.confirm_live:
            LOG.warning("PULSE_MODE=live without PULSE_CONFIRM_LIVE=1 — staying in PAPER")
        self.topic = env.get("PULSE_NTFY_TOPIC", "nifty-pulse")
        self.push_skips = env.get("PULSE_PUSH_SKIPS", "0") == "1"
        self.kite = ko.kite_for(env.get("KITE_API_KEY", ""), env.get("KITE_ACCESS_TOKEN", ""))
        self.strat = P.Strategy(self.cfg)
        self.hol = holidays()
        self._nfo: list = []
        self._nfo_day = None
        self.day = now_ist().date()
        self.load()

    # ------------------------------------------------------------------ state
    def ddir(self) -> Path:
        d = DATA / self.day.isoformat()
        d.mkdir(parents=True, exist_ok=True)
        return d

    def load(self) -> None:
        p = self.ddir() / "state.json"
        s = json.loads(p.read_text()) if p.exists() else {}
        self.st = P.DayState(**s["day"]) if s.get("day") else P.DayState(self.day.isoformat())
        self.armed = P.Signal(**s["armed"]) if s.get("armed") else None
        self.pos = P.Position.from_dict(s["pos"]) if s.get("pos") else None
        self.oi = s.get("oi", {"at": "", "levels": []})
        self.last_scan = s.get("last_scan", "")
        self.last_why = ""

    def save(self) -> None:
        p = self.ddir() / "state.json"
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"day": self.st.__dict__, "armed": self.armed.__dict__ if self.armed else None,
                                   "pos": self.pos.to_dict() if self.pos else None, "oi": self.oi,
                                   "last_scan": self.last_scan}, indent=1, default=str))
        tmp.replace(p)

    def journal(self, event: str, text: str) -> None:
        line = f"{now_ist():%H:%M:%S} {event:<9} {text}"
        LOG.info(line)
        with open(self.ddir() / "journal.txt", "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def push(self, title: str, body: str, prio: str = "default", tags: str = "") -> None:
        mode = "LIVE" if self.live else "PAPER"
        LOG.info(f"PUSH {title} | {body}")
        if self.dry or not self.topic:
            return
        try:
            requests.post(f"https://ntfy.sh/{self.topic}", data=f"[{mode}] {body}".encode("utf-8"), timeout=10,
                          headers={"Title": title.encode("ascii", "ignore").decode(), "Priority": prio, "Tags": tags})
        except Exception as e:
            LOG.warning(f"ntfy failed: {e}")

    # ------------------------------------------------------------------ Kite data
    def nfo(self) -> list:
        if self._nfo_day == self.day and self._nfo:
            return self._nfo
        f = self.ddir() / "nfo_nifty.json"
        if f.exists():
            rows = json.loads(f.read_text())
        else:
            rows = []
            for r in self.kite.instruments("NFO"):
                if r.get("name") == "NIFTY" and r.get("instrument_type") in ("FUT", "CE", "PE"):
                    rows.append({k: (str(r[k]) if k == "expiry" else r[k]) for k in
                                 ("instrument_token", "tradingsymbol", "instrument_type", "strike", "expiry", "lot_size")})
            f.write_text(json.dumps(rows))
        for r in rows:
            r["expiry"] = datetime.date.fromisoformat(str(r["expiry"])[:10])
        self._nfo, self._nfo_day = rows, self.day
        return rows

    def future(self) -> dict:
        futs = sorted((r for r in self.nfo() if r["instrument_type"] == "FUT" and r["expiry"] >= self.day),
                      key=lambda r: r["expiry"])
        # roll a day early so VWAP / volume come from the active contract
        if len(futs) > 1 and (futs[0]["expiry"] - self.day).days <= 1:
            return futs[1]
        return futs[0]

    def bars(self, token: int, days: int = 10) -> list:
        now = now_ist()
        frm = (now - datetime.timedelta(days=days)).replace(hour=9, minute=0, second=0, microsecond=0)
        rows = self.kite.historical_data(token, frm, now, self.cfg.candle_interval)
        time.sleep(0.35)
        out = []
        for r in rows:
            ts = r["date"]
            if getattr(ts, "tzinfo", None) is not None:
                ts = ts.astimezone(IST).replace(tzinfo=None)
            b = P.Bar(ts, float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]),
                      float(r.get("volume") or 0))
            if b.ts + datetime.timedelta(minutes=self.cfg.candle_minutes) <= now:      # closed candles only
                out.append(b)
        return out

    def ltp(self, keys: list) -> dict:
        q = self.kite.ltp(keys)
        return {k: float(v["last_price"]) for k, v in q.items()}

    def vix(self) -> tuple:
        try:
            q = self.kite.quote([VIX_KEY])[VIX_KEY]
            last, prev = float(q["last_price"]), float(q.get("ohlc", {}).get("close") or 0)
            return last, ((last / prev - 1) * 100 if prev else 0.0)
        except Exception as e:
            LOG.warning(f"VIX read failed: {e}")
            return None, 0.0

    def option_rows(self, expiry: datetime.date) -> list:
        return [r for r in self.nfo() if r["instrument_type"] in ("CE", "PE") and r["expiry"] == expiry]

    def expiry(self):
        exps = sorted({r["expiry"] for r in self.nfo() if r["instrument_type"] in ("CE", "PE")})
        return P.choose_expiry(exps, self.day, self.cfg, self.hol)

    def oi_levels(self, spot: float) -> list:
        """Max call-OI strike (resistance) and max put-OI strike (support), ATM ± 10, refreshed every 15 min."""
        now = now_ist()
        if self.oi.get("at") and now - datetime.datetime.fromisoformat(self.oi["at"]) < \
                datetime.timedelta(minutes=OI_REFRESH_MIN):
            return self.oi.get("levels", [])
        exp = self.expiry()
        if not exp:
            return []
        step = self.cfg.strike_step
        atm = round(spot / step) * step
        rows = [r for r in self.option_rows(exp) if abs(float(r["strike"]) - atm) <= OI_STRIKES * step]
        try:
            q = self.kite.quote([f"NFO:{r['tradingsymbol']}" for r in rows]) if rows else {}
        except Exception as e:
            LOG.warning(f"OI quote failed: {e}")
            return self.oi.get("levels", [])
        best = {"CE": (0, None), "PE": (0, None)}
        for r in rows:
            oi = (q.get(f"NFO:{r['tradingsymbol']}") or {}).get("oi", 0) or 0
            if oi > best[r["instrument_type"]][0]:
                best[r["instrument_type"]] = (oi, float(r["strike"]))
        self.oi = {"at": now.isoformat(), "levels": [v[1] for v in best.values() if v[1] is not None],
                   "call_wall": best["CE"][1], "put_wall": best["PE"][1]}
        return self.oi["levels"]

    # ------------------------------------------------------------------ orders
    def _wait(self, oid: str, timeout: int = 20) -> tuple:
        """→ (avg_price, filled_qty, status_text); an order still open at `timeout` is cancelled (not chased)."""
        t0, last = time.time(), {}
        while time.time() - t0 < timeout:
            time.sleep(1)
            try:
                hist = self.kite.order_history(oid)
            except Exception:
                continue
            last = hist[-1] if hist else {}
            st = last.get("status", "")
            if st == "COMPLETE":
                return float(last.get("average_price") or 0), int(last.get("filled_quantity") or 0), "filled"
            if st in ("REJECTED", "CANCELLED"):
                return (float(last.get("average_price") or 0), int(last.get("filled_quantity") or 0),
                        f"{st}: {last.get('status_message') or ''}".strip())
        try:
            self.kite.cancel_order(variety=self.kite.VARIETY_REGULAR, order_id=oid)
        except Exception as e:
            LOG.warning(f"cancel {oid} failed: {e}")
        time.sleep(1)
        try:
            last = self.kite.order_history(oid)[-1]
        except Exception:
            pass
        return (float(last.get("average_price") or 0), int(last.get("filled_quantity") or 0),
                f"not filled in {timeout}s — cancelled")

    def _order(self, side: str, symbol: str, qty: int, price: float, **kw) -> str:
        return self.kite.place_order(
            variety=self.kite.VARIETY_REGULAR, exchange="NFO", tradingsymbol=symbol, transaction_type=side,
            quantity=qty, product=self.kite.PRODUCT_MIS, order_type=kw.pop("order_type", self.kite.ORDER_TYPE_LIMIT),
            price=tick(price), validity=self.kite.VALIDITY_DAY, tag="niftypulse", **kw)

    def buy(self, symbol: str, qty: int, ask: float) -> tuple:
        """→ (fill price, filled qty, text)."""
        if not self.live:
            px = round(ask + 0.5, 2)
            self.paper_log("BUY", symbol, qty, px)
            return px, qty, "paper fill"
        try:
            oid = self._order("BUY", symbol, qty, ask * (1 + self.cfg.limit_slip_pct))
        except Exception as e:
            return 0.0, 0, f"order refused: {e}"
        px, filled, txt = self._wait(oid)
        return px, filled, f"order {oid} {txt}"

    def sell(self, symbol: str, qty: int, ltp: float) -> tuple:
        if not self.live:
            px = round(max(0.05, ltp - 0.5), 2)
            self.paper_log("SELL", symbol, qty, px)
            return px, qty, "paper fill"
        left, got, value, txt = qty, 0, 0.0, ""
        for slip in (self.cfg.limit_slip_pct, 0.03, 0.06):          # exits must happen: re-price twice
            try:
                q = self.kite.quote([f"NFO:{symbol}"])[f"NFO:{symbol}"]
                bid = q["depth"]["buy"][0]["price"] or q["last_price"]
                oid = self._order("SELL", symbol, left, bid * (1 - slip))
            except Exception as e:
                txt = f"sell refused: {e}"
                continue
            px, filled, txt = self._wait(oid)
            got += filled
            value += px * filled
            left -= filled
            if left <= 0:
                break
        return (value / got if got else 0.0), got, txt

    def place_sl(self, pos: P.Position) -> None:
        """Broker-side disaster stop (SL limit) in case the bot or the connection dies."""
        if not self.live:
            return
        trig = tick(pos.entry_prem - pos.delta * pos.sig.risk * self.cfg.disaster_sl_x)
        if trig <= 0.5:
            return
        try:
            pos.sl_order_id = str(self._order("SELL", pos.symbol, pos.qty_open, trig * 0.9,
                                              order_type=self.kite.ORDER_TYPE_SL, trigger_price=trig))
            self.journal("SL ORDER", f"{pos.symbol} {pos.qty_open} trigger ₹{trig} (id {pos.sl_order_id})")
        except Exception as e:
            self.push("NIFTY Pulse: SL order failed", f"{pos.symbol}: disaster SL not placed ({e}) — bot still "
                      f"watches the stop every 15 s", "high", "warning")

    def sl_fired(self, pos: P.Position):
        if not (self.live and pos.sl_order_id):
            return None
        try:
            h = self.kite.order_history(pos.sl_order_id)[-1]
        except Exception:
            return None
        if h.get("status") == "COMPLETE":
            return float(h.get("average_price") or 0)
        return None

    def cancel_sl(self, pos: P.Position) -> None:
        if self.live and pos.sl_order_id:
            try:
                self.kite.cancel_order(variety=self.kite.VARIETY_REGULAR, order_id=pos.sl_order_id)
            except Exception as e:
                LOG.warning(f"cancel SL {pos.sl_order_id}: {e}")
            pos.sl_order_id = ""

    def paper_log(self, side: str, symbol: str, qty: int, px: float) -> None:
        DATA.mkdir(parents=True, exist_ok=True)
        f = DATA / "paper_trades.csv"
        new = not f.exists()
        with open(f, "a") as fh:
            if new:
                fh.write("time,side,symbol,qty,price\n")
            fh.write(f"{now_ist():%Y-%m-%d %H:%M:%S},{side},{symbol},{qty},{px:.2f}\n")

    # ------------------------------------------------------------------ loops
    def new_day(self) -> None:
        d = now_ist().date()
        if d != self.day:
            self.day = d
            self.load()

    def scan(self, force: bool = False) -> None:
        self.new_day()
        now = now_ist()
        fut_row = self.future()
        spot, fut = P.align(self.bars(SPOT_TOKEN), self.bars(int(fut_row["instrument_token"])))
        if len(spot) < self.cfg.ema_trend + 5:
            self.journal("WAIT", f"only {len(spot)} candles — EMA200 needs {self.cfg.ema_trend + 5}")
            return
        x = self.strat.context(spot, fut)
        i = len(spot) - 1
        b = spot[i]
        stale = now - b.ts > datetime.timedelta(minutes=self.cfg.candle_minutes * 3)
        levels = P.build_levels(spot[-400:], self.cfg, x["atr"][i], self.oi_levels(b.close))
        blk = self.strat.update_flow_blocks(i, spot, fut, x, self.st)
        if blk:
            self.journal("FLOW", blk)
        head = (f"{b.ts:%H:%M} close {b.close:.1f} · EMA8 {x['e8'][i]:.0f} / 21 {x['e21'][i]:.0f} / 200 "
                f"{x['e200'][i]:.0f} · ATR {x['atr'][i]:.1f} · fut {fut[i].close:.0f} vs VWAP {x['vwap'][i]:.0f}")

        if self.pos:
            p = self.pos
            px = self.sl_fired(p)
            if px is not None:
                p.sl_order_id = ""
                self.finish(px, "broker SL order filled")
                return
            p.atr_now = x["atr"][i]
            prem = self.ltp([f"NFO:{p.symbol}"])[f"NFO:{p.symbol}"]
            self.apply(P.manage_exit(p, b, prem, self.cfg, closed_bar=True), prem)
            if self.pos:
                self.journal("MANAGE", f"{head} · stop {self.pos.stop_spot:.1f} · peak {self.pos.peak_r:.2f}R · "
                                       f"{self.pos.bars_held} candles · prem ₹{prem:.1f}")
            self.save()
            return
        if now.time() >= self.cfg.t(self.cfg.square_off) and not force:
            return
        if self.armed and now > datetime.datetime.fromisoformat(self.armed.expires_at):
            self.journal("CANCEL", f"{self.armed.side} signal of {self.armed.bar_ts[11:16]} not triggered in "
                                   f"{self.cfg.trigger_valid_bars} candles")
            self.push("NIFTY Pulse: signal expired", f"{self.armed.side} {self.armed.pattern} — trigger "
                      f"{self.armed.trigger:.1f} not hit in {self.cfg.trigger_valid_bars} candles", "low")
            self.armed = None
        if stale:
            self.journal("STALE", f"last candle {b.ts:%H:%M} is old — no new entries")
            self.save()
            return
        vix, vix_chg = self.vix()
        why = P.can_trade(now if not force else now.replace(hour=10, minute=0), self.st, self.cfg, vix, False,
                          str(ROOT))
        if not why and self.armed:
            why = f"{self.armed.side} signal already armed (trigger {self.armed.trigger:.1f})"
        if why:
            self.journal("SKIP", f"{head} — {why}")
            self.save()
            return
        sig, why = self.strat.evaluate(i, spot, fut, x, levels, self.st)
        if not sig:
            self.journal("SKIP", f"{head} — {why}")
            if self.push_skips and why != self.last_why:
                self.push("NIFTY Pulse: no trade", f"{b.ts:%H:%M} {why}", "min")
            self.last_why = why
            self.save()
            return
        self.armed = sig
        self.st.used_bars.append(sig.bar_ts)
        self.journal("ARMED", sig.text())
        self.push(f"NIFTY Pulse: {sig.side} armed", f"{sig.text()} · valid until {sig.expires_at[11:16]} · "
                  f"VIX {vix if vix is not None else '?'}", "default", "hourglass")
        self.save()

    def watch(self) -> None:
        self.new_day()
        now = now_ist()
        sq = now.time() >= self.cfg.t(self.cfg.square_off)
        kill = (ROOT / self.cfg.kill_switch_file).exists()
        if self.pos and (sq or kill):
            prem = self.ltp([f"NFO:{self.pos.symbol}"])[f"NFO:{self.pos.symbol}"]
            self.apply([("exit", "15:10 square-off" if sq else "kill switch (STOP_BOT)")], prem)
            self.save()
            return
        if not self.pos and not self.armed:
            return
        keys = [SPOT_KEY] + ([f"NFO:{self.pos.symbol}"] if self.pos else [])
        q = self.ltp(keys)
        spot = q[SPOT_KEY]
        if self.pos:
            prem = q[f"NFO:{self.pos.symbol}"]
            acts = P.manage_exit(self.pos, P.Bar(now, spot, spot, spot, spot), prem, self.cfg, closed_bar=False)
            if acts:
                self.apply(acts, prem)
                self.save()
            return
        s = self.armed
        if kill or sq or now.time() > self.cfg.last_entry:
            self.journal("CANCEL", f"{s.side} signal dropped ({'kill switch' if kill else 'past the last entry time'})")
            self.armed = None
            self.save()
            return
        if (s.side == "CE" and spot <= s.stop) or (s.side == "PE" and spot >= s.stop):
            self.journal("CANCEL", f"{s.side}: spot {spot:.1f} hit the stop {s.stop:.1f} before the trigger")
            self.push("NIFTY Pulse: signal cancelled", f"{s.side}: spot {spot:.1f} reached the stop before the "
                      f"trigger {s.trigger:.1f}", "low")
            self.armed = None
            self.save()
            return
        hit = spot >= s.trigger if s.side == "CE" else spot <= s.trigger
        if not hit:
            return
        if abs(spot - s.trigger) > self.cfg.overshoot_r * s.risk:
            self.journal("CANCEL", f"{s.side}: spot {spot:.1f} already {abs(spot - s.trigger):.1f} pts past the "
                                   f"trigger (> {self.cfg.overshoot_r:g}R) — not chasing")
            self.push("NIFTY Pulse: no chase", f"{s.side}: spot {spot:.1f} jumped past the trigger "
                      f"{s.trigger:.1f} by > {self.cfg.overshoot_r:g}R", "low")
            self.armed = None
            self.save()
            return
        self.enter(s, spot)
        self.save()

    # ------------------------------------------------------------------ entry / exit
    def enter(self, s: P.Signal, spot: float) -> None:
        self.armed = None
        c = self.cfg
        exp = self.expiry()
        if not exp:
            self.journal("NO BUY", "no expiry with enough days left")
            return
        strike = P.choose_strike(spot, s.side, c)
        inst = next((r for r in self.option_rows(exp) if float(r["strike"]) == strike
                     and r["instrument_type"] == s.side), None)
        if not inst:
            self.journal("NO BUY", f"{strike} {s.side} {exp} not in the instrument list")
            return
        key = f"NFO:{inst['tradingsymbol']}"
        q = self.kite.quote([key])[key]
        bid, ask = q["depth"]["buy"][0]["price"], q["depth"]["sell"][0]["price"]
        mid = (bid + ask) / 2 if bid and ask else q["last_price"]
        name = f"{inst['tradingsymbol']} (bid {bid} / ask {ask}, OI {q.get('oi', 0):,.0f})"
        if not (c.min_premium <= mid <= c.max_premium):
            self.journal("NO BUY", f"{name}: premium {mid:.1f} outside ₹{c.min_premium:g}–{c.max_premium:g}")
            self.push("NIFTY Pulse: no buy", f"{s.side} triggered but {name} premium {mid:.1f} outside the band", "low")
            return
        if not bid or not ask or (ask - bid) / mid > c.max_spread_pct or (q.get("oi") or 0) < c.min_oi:
            self.journal("NO BUY", f"{name}: illiquid (spread > {c.max_spread_pct:.0%} or OI < {c.min_oi:,})")
            self.push("NIFTY Pulse: no buy", f"{s.side} triggered but {name} is illiquid", "low")
            return
        delta = c.delta_itm if c.itm_steps else c.delta_atm
        lot = int(inst["lot_size"])
        _, vix_chg = self.vix()
        half = self.st.pnl_r >= c.daily_profit_lock_r or vix_chg > c.vix_spike_pct
        lots = P.size_lots(s.risk, ask, delta, lot, c, half=half)
        if lots < 1:
            self.journal("NO BUY", f"{name}: risk budget ₹{c.capital * c.risk_pct:,.0f} too small for 1 lot "
                                   f"(R {s.risk:.0f} pts × delta {delta} × {lot})")
            self.push("NIFTY Pulse: no buy", f"{s.side} triggered but 1 lot risks more than "
                      f"₹{c.capital * c.risk_pct:,.0f} (PULSE_CAPITAL / PULSE_RISK_PCT)", "low")
            return
        px, filled, txt = self.buy(inst["tradingsymbol"], lots * lot, ask)
        if filled <= 0 or px <= 0:
            self.journal("NO FILL", f"{name} {lots * lot}: {txt}")
            self.push("NIFTY Pulse: buy not filled", f"{inst['tradingsymbol']} {lots * lot} — {txt}", "high", "x")
            return
        lots_f = max(1, filled // lot)
        self.pos = P.Position(s, inst["tradingsymbol"], lots_f, lot, px, spot, s.stop, delta, now_ist().isoformat(),
                              peak_prem=px, best_spot=spot, qty_open=filled, atr_now=s.atr)
        self.st.trades += 1
        self.place_sl(self.pos)
        msg = (f"BUY {inst['tradingsymbol']} × {filled} ({lots_f} lot{'s' if lots_f > 1 else ''}{', half size' if half else ''}) "
               f"@ ₹{px:.2f} · spot {spot:.1f} · stop {s.stop:.1f} · T1 {s.t1:.1f} · T2 {s.t2:.1f} · "
               f"risk ≈ ₹{delta * s.risk * filled:,.0f} · {txt}")
        self.journal("ENTER", msg)
        self.push(f"NIFTY Pulse: BUY {s.side}", f"{msg}\n{s.kind} · {s.pattern} · {s.reason}", "high", "rocket")

    def apply(self, actions: list, prem: float) -> None:
        p = self.pos
        for act in actions:
            if act[0] == "partial":
                px, got, txt = self.sell(p.symbol, act[1], prem)
                if got <= 0:
                    self.push("NIFTY Pulse: partial sell FAILED", f"{p.symbol} {act[1]} — {txt}", "urgent", "warning")
                    continue
                p.realised += (px - p.entry_prem) * got
                p.qty_open -= got
                if self.live and p.sl_order_id:
                    try:
                        self.kite.modify_order(variety=self.kite.VARIETY_REGULAR, order_id=p.sl_order_id,
                                               quantity=p.qty_open)
                    except Exception as e:
                        LOG.warning(f"SL qty modify failed: {e}")
                msg = (f"booked {got} of {p.symbol} @ ₹{px:.2f} (+₹{(px - p.entry_prem) * got:,.0f}) at 1R · stop → "
                       f"{p.stop_spot:.1f} (breakeven + {self.cfg.be_plus_pts:g})")
                self.journal("PARTIAL", msg)
                self.push("NIFTY Pulse: half booked", msg, "default", "moneybag")
            elif act[0] == "exit":
                self.cancel_sl(p)
                px, got, txt = self.sell(p.symbol, p.qty_open, prem)
                if got < p.qty_open:
                    p.qty_open -= got
                    p.realised += (px - p.entry_prem) * got
                    self.push("NIFTY Pulse: EXIT FAILED", f"{p.symbol}: {p.qty_open} still open ({act[1]}) — {txt}. "
                              f"Sell it in Kite.", "urgent", "warning")
                    self.place_sl(p)
                    return
                p.realised += (px - p.entry_prem) * got
                p.qty_open = 0
                self.finish(px, act[1])
                return

    def finish(self, px: float, why: str) -> None:
        p = self.pos
        if p.qty_open:
            p.realised += (px - p.entry_prem) * p.qty_open
            p.qty_open = 0
        p.realised -= self.cfg.cost_per_lot_rt * p.lots
        risk_rs = self.cfg.capital * self.cfg.risk_pct
        r = p.realised / risk_rs if risk_rs else 0
        self.st.pnl += p.realised
        self.st.pnl_r += r
        if p.realised < 0:
            self.st.losses += 1
            self.st.last_loss_at = now_ist().isoformat()
        if self.st.pnl <= -self.cfg.daily_loss_limit_pct * self.cfg.capital:
            self.st.halted = True
        msg = (f"EXIT {p.symbol} @ ₹{px:.2f} — {why} · trade ₹{p.realised:+,.0f} ({r:+.2f}R, after costs) · "
               f"day ₹{self.st.pnl:+,.0f} · {self.st.trades} trades / {self.st.losses} losses")
        self.journal("EXIT", msg)
        self.push(f"NIFTY Pulse: EXIT {'+' if p.realised >= 0 else '-'}", msg, "high",
                  "white_check_mark" if p.realised >= 0 else "red_circle")
        with open(DATA / "trades.csv", "a") as f:
            f.write(f"{self.day},{p.opened_at[11:16]},{now_ist():%H:%M},{p.sig.side},{p.sig.kind},{p.symbol},"
                    f"{p.lots * p.lot_size},{p.entry_prem:.2f},{px:.2f},{p.realised:.0f},{r:.2f},"
                    f"{'live' if self.live else 'paper'},{why}\n")
        self.pos = None

    def adopt(self) -> None:
        """Live restart: check the saved position is still open in Kite."""
        if not (self.live and self.pos):
            return
        try:
            net = {r["tradingsymbol"]: int(r["quantity"]) for r in self.kite.positions()["net"]
                   if r.get("exchange") == "NFO"}
        except Exception as e:
            LOG.warning(f"positions read failed: {e}")
            return
        q = net.get(self.pos.symbol, 0)
        if q <= 0:
            prem = self.ltp([f"NFO:{self.pos.symbol}"])[f"NFO:{self.pos.symbol}"]
            self.pos.sl_order_id = ""
            self.finish(prem, "closed outside the bot (not in Kite positions after restart)")
        elif q != self.pos.qty_open:
            self.pos.qty_open = q
        self.save()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="one scan now and exit")
    ap.add_argument("--dry-run", action="store_true", help="paper, no pushes")
    ap.add_argument("--force", action="store_true", help="scan outside market hours (with --once)")
    a = ap.parse_args()
    bot = Bot(load_env(), a.dry_run)
    c = bot.cfg
    LOG.info(f"NIFTY Pulse — {'LIVE' if bot.live else 'PAPER'} · capital ₹{c.capital:,.0f} · risk {c.risk_pct:.1%} · "
             f"{c.candle_interval} · windows {c.win1_from}-{c.win1_to}, {c.win2_from}-{c.win2_to}")
    if a.once:
        bot.scan(force=a.force)
        return
    bot.adopt()
    bot.push("NIFTY Pulse started", f"capital ₹{c.capital:,.0f} · risk {c.risk_pct:.1%}/trade · max "
             f"{c.max_trades_per_day} trades / {c.max_losses_per_day} losses · "
             f"{'open position adopted: ' + bot.pos.symbol if bot.pos else 'no open position'}", "low")
    while True:
        now = now_ist()
        if now.weekday() >= 5 or now.time() >= datetime.time(15, 20):
            LOG.info("market closed — NIFTY Pulse stops")
            break
        if now.time() < datetime.time(9, 15):
            time.sleep(30)
            continue
        slot = now.replace(second=0, microsecond=0)
        slot -= datetime.timedelta(minutes=slot.minute % c.candle_minutes)
        if slot.isoformat() != bot.last_scan and now >= slot + datetime.timedelta(seconds=c.scan_delay_sec) \
                and now.time() >= datetime.time(9, 20):
            bot.last_scan = slot.isoformat()
            try:
                bot.scan()
            except Exception as e:
                LOG.error(f"scan failed: {e}\n{traceback.format_exc()}")
        try:
            bot.watch()
        except Exception as e:
            LOG.error(f"watch failed: {e}\n{traceback.format_exc()}")
        time.sleep(c.watch_every_sec)


if __name__ == "__main__":
    main()
