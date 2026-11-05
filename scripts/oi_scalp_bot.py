"""
OI + MA pullback scalp — F&O stocks, every 5 minutes. Rules: screener/oi_scalp.py.

Each run (just after a 5-min candle closes):
  1. Watchlist from the OI monitor's latest scan (data/oi/<date>/latest.json, ≤ 25 min old)
  2. Open trades: exit checks (stop / target 1 → trail / OI flip / hard stop / 15:15)
  3. Watchlist stocks without a trade today: 5-min chart → 20 / 50 EMA pullback entry
  4. Entry: BUY the ATM CE / PE (current monthly expiry) with OIS_AUTO_TRADE=1, else alert only
  Every entry and exit is pushed to OI_NTFY_TOPIC (falls back to NTFY_TOPIC).

Settings (.env): OIS_AUTO_TRADE (0), OIS_LOTS (1), OIS_PRODUCT (MIS), OIS_MAX_TRADES (5 a day),
OIS_MAX_WATCH (30 stocks read per run). One trade per stock per day.
When OIS_AUTO_TRADE=1 the OI monitor stops buying on its own signals (no double orders).

Cron (UTC; the script only works 09:20–15:30 IST):
  1-59/5 3-10 * * 1-5 cd /root/stockAnalyser && /root/venv/bin/python scripts/oi_scalp_bot.py >> oi_scalp.log 2>&1
Manual:  python scripts/oi_scalp_bot.py --force --dry-run     # read and print, no orders / pushes
"""

import argparse
import datetime
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).parent))

import pandas as pd
from kiteconnect import KiteConnect

import oi_monitor_bot as mon
from screener import oi_scalp as sc

DATA, IST = mon.DATA, mon.IST
RUN_FROM, RUN_TO = datetime.time(9, 20), datetime.time(15, 31)
LATEST_MAX_AGE_MIN = 25


def nse_tokens(kite: KiteConnect, today: datetime.date) -> dict:
    cache = DATA / f"nse_tokens_{today}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    toks = {r["tradingsymbol"]: r["instrument_token"] for r in kite.instruments("NSE") if r["segment"] == "NSE"}
    DATA.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(toks))
    for old in DATA.glob("nse_tokens_*.json"):
        if old != cache:
            old.unlink()
    return toks


def candles(kite: KiteConnect, token: int, now: datetime.datetime) -> pd.DataFrame | None:
    """Closed 5-min candles of the last ~7 days (IST-naive index)."""
    try:
        n = now.replace(tzinfo=None)
        rows = kite.historical_data(token, n - datetime.timedelta(days=7), n, "5minute")
    except Exception as ex:
        print(f"  candles failed for {token}: {ex}")
        return None
    time.sleep(0.35)                     # Kite historical limit: 3 requests / second
    if not rows:
        return None
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    if df["date"].dt.tz is not None:
        df["date"] = df["date"].dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    df = df.set_index("date").sort_index()
    cutoff = now.replace(tzinfo=None) - datetime.timedelta(minutes=5)
    return sc.add_indicators(df[df.index <= cutoff])       # drop the forming candle


def option_row(kite: KiteConnect, uni: dict, name: str, side: str, spot: float) -> dict | None:
    opts = uni.get(name, {}).get("opts") or []
    if not opts:
        return None
    k, ce, pe = min(opts, key=lambda o: abs(o[0] - spot))
    ts = ce if side == "CE" else pe
    try:
        q = kite.quote([f"NFO:{ts}"])[f"NFO:{ts}"]
    except Exception:
        return None
    d = q.get("depth") or {}
    return {"name": name, "strike": k, "type": side, "ts": ts, "ltp": float(q.get("last_price") or 0),
            "ask": float((d.get("sell") or [{}])[0].get("price") or 0),
            "bid": float((d.get("buy") or [{}])[0].get("price") or 0)}


def ltp(kite: KiteConnect, ts: str) -> float | None:
    try:
        return float(kite.ltp([f"NFO:{ts}"])[f"NFO:{ts}"]["last_price"])
    except Exception:
        return None


def held_qty(kite: KiteConnect, ts: str) -> int | None:
    try:
        return sum(int(p.get("quantity", 0)) for p in kite.positions().get("net", []) if p.get("tradingsymbol") == ts)
    except Exception:
        return None


def sell(kite: KiteConnect, pos: dict) -> str:
    """LIMIT SELL what is held (never more) 2 % under the LTP; cancelled if not filled in 8 s."""
    held = held_qty(kite, pos["ts"])
    if held is None:
        return "position check failed — retry next run"
    qty = min(held, int(pos["qty"]))
    if qty <= 0:
        return "already flat"
    px = mon.tick(max(0.05, (ltp(kite, pos["ts"]) or pos["fill"]) * 0.98))
    prod = kite.PRODUCT_MIS if pos.get("product") == "MIS" else kite.PRODUCT_NRML
    try:
        oid = kite.place_order(variety=kite.VARIETY_REGULAR, exchange="NFO", tradingsymbol=pos["ts"],
                               transaction_type=kite.TRANSACTION_TYPE_SELL, quantity=qty, product=prod,
                               order_type=kite.ORDER_TYPE_LIMIT, price=px, tag="oiscalp")
    except Exception as ex:
        return f"sell failed: {ex}"
    for _ in range(4):
        time.sleep(2)
        try:
            h = kite.order_history(oid)[-1]
            if h.get("status") == "COMPLETE":
                return f"sold {qty} @ ₹{float(h.get('average_price') or px):g}"
            if h.get("status") in ("REJECTED", "CANCELLED"):
                return f"sell {h['status']}: {h.get('status_message') or ''}"
        except Exception:
            pass
    try:
        kite.cancel_order(variety=kite.VARIETY_REGULAR, order_id=oid)
    except Exception:
        pass
    return "sell not filled in 8 s — cancelled, retry next run"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="run outside 09:20–15:30 IST")
    ap.add_argument("--dry-run", action="store_true", help="print only: no orders, pushes or state")
    a = ap.parse_args()

    now = datetime.datetime.now(IST)
    today, hm = now.date(), now.strftime("%H:%M")
    if not a.force and (now.weekday() >= 5 or not RUN_FROM <= now.time() <= RUN_TO):
        return
    e = mon.env()
    kite = KiteConnect(e.get("KITE_API_KEY", ""))
    kite.set_access_token(e.get("KITE_ACCESS_TOKEN", ""))
    auto = e.get("OIS_AUTO_TRADE", "0") == "1" and not a.dry_run
    lots = max(1, int(e.get("OIS_LOTS") or 1))
    product = "NRML" if e.get("OIS_PRODUCT", "MIS").upper() == "NRML" else "MIS"
    max_trades = int(e.get("OIS_MAX_TRADES") or 5)
    max_watch = int(e.get("OIS_MAX_WATCH") or 30)
    topic = e.get("OI_NTFY_TOPIC") or e.get("NTFY_TOPIC", "")

    day_dir = DATA / str(today)
    state_p = day_dir / "scalp_state.json"
    state = json.loads(state_p.read_text()) if state_p.exists() else {"trades": {}, "n": 0}
    lp = day_dir / "latest.json"
    latest = json.loads(lp.read_text()) if lp.exists() else {"time": None, "stocks": {}}
    stocks = latest["stocks"]
    fresh = False
    if latest.get("time"):
        t = datetime.datetime.combine(today, datetime.datetime.strptime(latest["time"], "%H%M").time())
        fresh = (now.replace(tzinfo=None) - t).total_seconds() <= LATEST_MAX_AGE_MIN * 60
    uni_p = DATA / f"universe_{today}.json"
    uni = json.loads(uni_p.read_text()) if uni_p.exists() else {}
    toks = nse_tokens(kite, today)
    msgs: list[str] = []

    # 1. open trades → exits
    for name, pos in state["trades"].items():
        if pos.get("closed"):
            continue
        df = candles(kite, toks[name], now) if name in toks else None
        opt = ltp(kite, pos["ts"])
        reason, upd = sc.exit_signal(pos, df, stocks.get(name), now, opt)
        if upd.get("trailing") and not pos.get("trailing"):
            pos["trailing"] = True
            msgs.append(f"💰 {name} {pos['side']} reached target 1 ({pos['target']:g}) — trailing on the 10 SMA")
        if not reason:
            continue
        res = sell(kite, pos) if (pos.get("ordered") and not a.dry_run) else "alert only (no order)"
        pnl = f" · premium ₹{pos['fill']:g} → ₹{opt:g} ({(opt / pos['fill'] - 1) * 100:+.0f}%)" if opt and pos.get("fill") else ""
        msgs.append(f"⏹ {name} {pos['side']} EXIT — {reason}{pnl}\n   {res}")
        if res.startswith(("sold", "already flat", "alert only")):
            pos["closed"] = hm

    # 2. watchlist → entries
    watch = [(n, sc.watch_side(r), r) for n, r in stocks.items()] if fresh else []
    watch = [w for w in watch if w[1] and w[0] not in state["trades"] and w[0] in toks][:max_watch]
    if not (sc.ENTRY_FROM <= now.time() <= sc.ENTRY_UNTIL):
        watch = []
    print(f"[{hm}] OI read {latest.get('time')} ({'fresh' if fresh else 'stale / none'}) · "
          f"watchlist {len(watch)} · open {sum(1 for p in state['trades'].values() if not p.get('closed'))} · "
          f"trades today {state['n']}/{max_trades} · {'AUTO' if auto else 'alerts only'}")
    for name, side, row in watch:
        if state["n"] >= max_trades:
            break
        df = candles(kite, toks[name], now)
        if df is None:
            continue
        lv = sc.day_levels(df, today)
        sig = sc.entry_signal(df, side, lv, row.get("call_wall" if side == "CE" else "put_wall"), today)
        if not sig:
            continue
        opt = option_row(kite, uni, name, side, sig["close"])
        if not opt:
            continue
        snap = pd.DataFrame([opt])
        r = {"name": name, "signal": side, "option": {"strike": opt["strike"]}}
        lot = int(uni.get(name, {}).get("lot") or 1)
        if auto:
            res = mon.buy_atm(kite, r, snap, lot, lots, product)
            res = res.split(" (no stop")[0].split(" — set your")[0]    # this bot manages the exit
            ordered = res.startswith("order ✓")
            fill = opt["ask"] or opt["ltp"]
            try:
                fill = float(res.split("@ ₹")[1].split()[0]) if ordered else fill
            except Exception:
                pass
        else:
            res, ordered, fill = "alert only (OIS_AUTO_TRADE=0)", False, opt["ask"] or opt["ltp"]
        up = side == "CE"
        msgs.append(f"{'🟢' if up else '🔴'} {name} {sig['close']:g} — {side} pullback to the {sig['ema']} "
                    f"({sig['ema_value']:.2f}) · OI {'STRONG' if row.get('signal') else 'watch'} "
                    f"(score {row['ce_score' if up else 'pe_score']}/6, calls {row['call_pct']:+.0f}% "
                    f"puts {row['put_pct']:+.0f}%)\n"
                    f"   BUY {opt['strike']:g} {side} @ ₹{opt['ltp']:g} · stop: close {'below' if up else 'above'} "
                    f"the {sig['ema']} · target 1 {sig['target']:g} then trail\n   {res}")
        if ordered or not auto:
            state["trades"][name] = {"side": side, "ts": opt["ts"], "qty": lot * lots, "fill": fill,
                                     "ema": sig["ema"], "target": sig["target"], "entry_candle": sig["candle"],
                                     "entry_time": hm, "product": product, "ordered": ordered}
            state["n"] += 1 if ordered else 0

    for m in msgs:
        print(m)
    if a.dry_run:
        return
    if msgs and topic:
        mon.push(topic, f"OI scalp {hm}", "\n\n".join(msgs))
    day_dir.mkdir(parents=True, exist_ok=True)
    state_p.write_text(json.dumps(state))


if __name__ == "__main__":
    main()
