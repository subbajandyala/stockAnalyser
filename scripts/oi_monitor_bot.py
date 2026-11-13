"""
OI monitor — all F&O stocks, every 10 minutes, ntfy push for NEW strong CE / PE signals.
Orders: with OI_AUTO_TRADE=1 in .env each NEW signal also BUYs the ATM CE / PE of the
current monthly expiry — OI_LOTS lots (default 1), LIMIT just above the ask, tag "oimon",
at most OI_MAX_ORDERS (default 5) a day, none after 15:00; unfilled after 8 s → cancelled.
  OI_PRODUCT=NRML (default): carried overnight. No stop / target is placed — set them in
      Kite by hand. OI_GTT=1 adds an OCO GTT on each fill instead: stop OI_SL_PCT (20) %
      below the fill, target OI_TARGET_PCT (40) % above.
      No order within 4 days of expiry (stock options are physically settled).
  OI_PRODUCT=MIS: intraday, no GTT; Zerodha squares it off ~15:20.
The result (filled + GTT / Kite's rejection reason) is added to the push.

Order rules (screener/oi_rules.py, OI_NEW_RULES=1 default; 0 = buy every new signal as before):
  before 09:40      EARLY — decided at the first scan from 09:40 where the stock is still STRONG
  NIFTY             up ≥ 0.5 % and above its open → no PE; down ≥ 0.5 % and below → no CE
  futures           current + next month OI together vs yesterday: CE long buildup, PE short buildup
                    (OI_FUT_MODE=expiry7: current month only until the last 7 trading days to expiry)
  levels            CPR today / day −1 / day −2, last 2 days' highs / lows, opening range, 15-min zones,
                    OI walls — one within 1 % → WAITING; each scan: a 10-min close beyond it → BUY,
                    held (1 % the wrong way) / OI turned (score ≤ 2) / 15:00 → CANCELLED
  15:00 or later    no order
Liquidity: ATM option spread ≤ OI_MAX_SPREAD % (default 3). OI_LIQUID_TOP=N: only the top N stocks by
  the previous day's traded value of their ATM ±2 options (CE + PE) can signal (0 = all, default).
Pushes: BUY, WAITING and their follow-ups (BREAK → BUY, CANCELLED). WATCH / EARLY only in the
log and the evening report (OI_PUSH_ALL=1 pushes those too).

Each run:
  1. F&O stock list + nearest-expiry options / futures from Kite (cached per day)
  2. Quotes: stock, futures, and CE + PE for ATM ±10 strikes (≈ 18 quote calls, ~20 s)
  3. Snapshot saved to data/oi/<date>/<HHMM>.csv.gz (the last one of the day is the next
     day's "change since yesterday" base, like Sensibull's OI-change chart)
  4. screener/oi_monitor scores every stock; a stock is pushed once per side per day

Cron (Subba's machine, separate worktree /root/oimon — the trading bot is untouched):
  */10 3-10 * * 1-5 cd /root/oimon && /root/venv/bin/python scripts/oi_monitor_bot.py >> oi_monitor.log 2>&1
  (UTC; the script itself only runs 09:20–15:30 IST)

Manual:
  python scripts/oi_monitor_bot.py --force --dry-run      # scan now, print, no push
  python scripts/oi_monitor_bot.py --force --show SBIN    # one stock's strike table and checks
  python scripts/oi_monitor_bot.py --force --dry-run --all  # every F&O stock: OI change, scores, walls
  python scripts/oi_monitor_bot.py --test-order CRUDEOIL    # test ATM CE order (half price) + push
"""

import argparse
import datetime
import gzip
import json
import pathlib
import shutil
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import pandas as pd
import requests
from kiteconnect import KiteConnect

from screener import levels
from screener import oi_monitor as om
from screener import oi_rules as rules

ROOT = pathlib.Path(__file__).parent.parent
DATA = ROOT / "data" / "oi"
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
STRIKES_EACH_SIDE = 10
INDEXES = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}
RUN_FROM, RUN_TO = datetime.time(9, 20), datetime.time(15, 31)
RECENT_MIN = 18          # "recent" = the snapshot at least this many minutes old
KEEP_DAYS = 30
ORDER_UNTIL = datetime.time(15, 0)   # no new orders after this
MIN_DAYS_TO_EXPIRY = 4               # NRML: no order this close to expiry (physical settlement)
PUSH_MAX_BYTES = 3500


def env() -> dict:
    e = {}
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                e[k.strip()] = v.strip().strip('"').strip("'")
    return e


def _quote_part(kite: KiteConnect, keys: list[str], bad: list) -> dict:
    """One quote call; when Kite refuses it (a 400 on one batch every scan, 12 Nov), split it in
    halves down to the single contract that breaks it, so the rest of the batch is kept."""
    err = None
    for _ in range(2):
        try:
            r = kite.quote(keys)
            time.sleep(1.05)                     # Kite quote limit: 1 request / second
            return r
        except Exception as ex:
            err = ex
            time.sleep(1.05)
    if len(keys) == 1:
        bad.append(keys[0])
        print(f"  quote failed for {keys[0]}: {str(err)[:90]} — skipped today")
        return {}
    mid = len(keys) // 2
    return {**_quote_part(kite, keys[:mid], bad), **_quote_part(kite, keys[mid:], bad)}


def quotes(kite: KiteConnect, keys: list[str]) -> dict:
    skip_p = DATA / f"bad_quotes_{datetime.date.today()}.json"
    skip = set(json.loads(skip_p.read_text())) if skip_p.exists() else set()
    keys = [k for k in keys if k not in skip]
    out, bad = {}, []
    for i in range(0, len(keys), 500):
        out.update(_quote_part(kite, keys[i:i + 500], bad))
    if bad:
        DATA.mkdir(parents=True, exist_ok=True)
        for old in DATA.glob("bad_quotes_*.json"):
            if old != skip_p:
                old.unlink()
        skip_p.write_text(json.dumps(sorted(skip | set(bad))))
    return out


def universe(kite: KiteConnect, today: datetime.date) -> dict:
    """{name: {"fut": ts, "expiry": "YYYY-MM-DD", "opts": [[strike, CE ts, PE ts], ...]}}"""
    cache = DATA / f"universe_{today}.json"
    if cache.exists():
        uni = json.loads(cache.read_text())
        if all("lot" in v and "futs" in v for v in uni.values()):
            return uni
    rows = [r for r in kite.instruments("NFO") if r["name"] not in INDEXES and r["expiry"] >= today]
    uni: dict = {}
    by_name: dict = {}
    for r in rows:
        by_name.setdefault(r["name"], []).append(r)
    for name, rs in by_name.items():
        opt_exp = sorted({r["expiry"] for r in rs if r["instrument_type"] in ("CE", "PE")})
        fut = sorted((r for r in rs if r["instrument_type"] == "FUT"), key=lambda r: r["expiry"])
        if not opt_exp or not fut:
            continue
        exp = opt_exp[0]
        legs: dict = {}
        for r in rs:
            if r["expiry"] == exp and r["instrument_type"] in ("CE", "PE"):
                legs.setdefault(float(r["strike"]), {})[r["instrument_type"]] = r["tradingsymbol"]
        opts = [[k, v["CE"], v["PE"]] for k, v in sorted(legs.items()) if "CE" in v and "PE" in v]
        if len(opts) >= 2 * om.NEAR + 1:
            uni[name] = {"fut": fut[0]["tradingsymbol"], "expiry": str(exp), "opts": opts,
                         "lot": int(fut[0].get("lot_size") or 1),
                         "futs": [[int(f.get("instrument_token") or 0), str(f["expiry"])] for f in fut[:2]]}
    DATA.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(uni))
    for old in DATA.glob("universe_*.json"):
        if old != cache:
            old.unlink()
    return uni


def nse_tokens(kite: KiteConnect, today: datetime.date) -> dict:
    """NSE symbol → instrument token, cached per day (only read when a signal needs candles)."""
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


def snapshot(kite: KiteConnect, uni: dict) -> tuple[pd.DataFrame, datetime.date | None]:
    names = sorted(uni)
    under = quotes(kite, [f"NSE:{n}" for n in names] + [f"NFO:{uni[n]['fut']}" for n in names])
    last_dates = [q.get("last_trade_time") for q in under.values() if q.get("last_trade_time")]
    traded = max(last_dates).date() if last_dates else None

    want, meta = [], {}
    for n in names:
        s = under.get(f"NSE:{n}", {})
        f = under.get(f"NFO:{uni[n]['fut']}", {})
        spot = float(s.get("last_price") or f.get("last_price") or 0)
        if spot <= 0:
            continue
        opts = uni[n]["opts"]
        i = min(range(len(opts)), key=lambda j: abs(opts[j][0] - spot))
        pick = opts[max(0, i - STRIKES_EACH_SIDE): i + STRIKES_EACH_SIDE + 1]
        meta[n] = {"spot": spot,
                   "spot_close": float((s.get("ohlc") or {}).get("close") or (f.get("ohlc") or {}).get("close") or 0),
                   "fut_ltp": float(f.get("last_price") or 0),
                   "fut_close": float((f.get("ohlc") or {}).get("close") or 0),
                   "fut_oi": float(f.get("oi") or 0), "pick": pick}
        for k, ce, pe in pick:
            want += [f"NFO:{ce}", f"NFO:{pe}"]

    oq = quotes(kite, want)
    rows = []
    for n, mt in meta.items():
        for k, ce, pe in mt["pick"]:
            for typ, ts in (("CE", ce), ("PE", pe)):
                q = oq.get(f"NFO:{ts}")
                if not q:
                    continue
                d = q.get("depth") or {}
                bid = (d.get("buy") or [{}])[0].get("price") or 0
                ask = (d.get("sell") or [{}])[0].get("price") or 0
                rows.append({"name": n, "strike": k, "type": typ, "ts": ts,
                             "oi": float(q.get("oi") or 0), "ltp": float(q.get("last_price") or 0),
                             "bid": float(bid), "ask": float(ask), "volume": float(q.get("volume") or 0),
                             "spot": mt["spot"], "spot_close": mt["spot_close"], "fut_ltp": mt["fut_ltp"],
                             "fut_close": mt["fut_close"], "fut_oi": mt["fut_oi"]})
    return pd.DataFrame(rows), traded


def snap_files(day: datetime.date) -> list[pathlib.Path]:
    return sorted((DATA / str(day)).glob("*.csv.gz"))


def load(p: pathlib.Path | None) -> pd.DataFrame | None:
    return pd.read_csv(p) if p else None


def base_and_recent(today: datetime.date, now_hm: str) -> tuple[pathlib.Path | None, pathlib.Path | None]:
    days = sorted(d for d in DATA.iterdir() if d.is_dir() and d.name < str(today)) if DATA.exists() else []
    prev = [f for d in reversed(days) for f in [snap_files(datetime.date.fromisoformat(d.name))] if f]
    todays = [f for f in snap_files(today) if f.name[:4] != now_hm]
    base = prev[0][-1] if prev else (todays[0] if todays else None)
    cut = datetime.datetime.strptime(now_hm, "%H%M") - datetime.timedelta(minutes=RECENT_MIN)
    old = [f for f in todays if f.name[:4] <= cut.strftime("%H%M")]
    recent = old[-1] if old else (todays[0] if todays else None)
    return base, recent


def tick(x: float, size: float = 0.05) -> float:
    return round(round(x / size) * size, 2)


def buy_atm(kite: KiteConnect, r: dict, snap: pd.DataFrame, lot: int, lots: int,
            product: str = "NRML", sl_pct: float = 20.0, tgt_pct: float = 40.0,
            gtt: bool = False, exchange: str = "NFO", tick_size: float = 0.05,
            price_factor: float = 1.005) -> str:
    """BUY lots × lot of the signal's ATM option: LIMIT just above the ask.

    NRML (default): held overnight. gtt=True adds an OCO GTT once filled — stop sl_pct
    below the fill, target tgt_pct above; off by default (stop / target managed by hand).
    MIS: intraday, Zerodha squares it off ~15:20.
    An order not filled within ~8 s is cancelled. Returns a result line for the push."""
    row = snap[(snap["name"] == r["name"]) & (snap["strike"] == r["option"]["strike"])
               & (snap["type"] == r["signal"])]
    if not len(row):
        return "order: contract not found"
    o = row.iloc[0]
    ts = o["ts"]
    ref = max(float(o["ask"] or 0), float(o["ltp"] or 0))
    price, qty = max(tick_size, tick(ref * price_factor, tick_size)), lot * lots
    prod = kite.PRODUCT_MIS if product == "MIS" else kite.PRODUCT_NRML
    try:
        oid = kite.place_order(variety=kite.VARIETY_REGULAR, exchange=exchange,
                               tradingsymbol=ts, transaction_type=kite.TRANSACTION_TYPE_BUY,
                               quantity=qty, product=prod, order_type=kite.ORDER_TYPE_LIMIT,
                               price=price, tag="oimon")
    except Exception as ex:
        return f"order ✗ {ts} {qty} @ ₹{price:g} {product} — {ex}"
    status, why, fill = "PLACED", "", 0.0
    for _ in range(4):
        time.sleep(2)
        try:
            h = kite.order_history(oid)[-1]
            status, why = h.get("status", status), h.get("status_message") or ""
            fill = float(h.get("average_price") or 0)
            if status in ("COMPLETE", "REJECTED", "CANCELLED"):
                break
        except Exception:
            pass
    if status not in ("COMPLETE", "REJECTED", "CANCELLED"):
        try:
            kite.cancel_order(variety=kite.VARIETY_REGULAR, order_id=oid)
            status, why = "CANCELLED", "not filled in 8 s"
        except Exception as ex:
            why = f"still open, cancel failed: {ex}"
    if status != "COMPLETE":
        return f"order ✗ {ts} {qty} @ ₹{price:g} {product} — {status}" + (f": {why}" if why else "")
    fill = fill or price
    txt = f"order ✓ {ts} {qty} @ ₹{fill:g} {product} — COMPLETE"
    if product == "MIS":
        return txt + " (no stop: MIS square-off ~15:20)"
    if not gtt:
        return txt + " — set your stop loss / target in Kite"
    sl, tgt = tick(fill * (1 - sl_pct / 100), tick_size), tick(fill * (1 + tgt_pct / 100), tick_size)
    leg = lambda px: {"transaction_type": kite.TRANSACTION_TYPE_SELL, "quantity": qty,
                      "product": kite.PRODUCT_NRML, "order_type": kite.ORDER_TYPE_LIMIT, "price": px}
    try:
        g = kite.place_gtt(trigger_type=kite.GTT_TYPE_OCO, tradingsymbol=ts, exchange=exchange,
                           trigger_values=[sl, tgt], last_price=fill,
                           orders=[leg(max(tick_size, tick(sl * 0.95, tick_size))), leg(tick(tgt * 0.99, tick_size))])
        gid = g.get("trigger_id") if isinstance(g, dict) else g
        return txt + f"\n   GTT {gid}: stop ₹{sl:g} (−{sl_pct:g}%) · target ₹{tgt:g} (+{tgt_pct:g}%)"
    except Exception as ex:
        return txt + f"\n   ⚠️ GTT FAILED ({ex}) — SET A STOP LOSS IN KITE NOW"


def test_order(kite: KiteConnect, e: dict, name: str, side: str, fill: bool, today: datetime.date) -> None:
    """Run the real order + push path on one instrument's ATM option (CRUDEOIL → MCX, else NFO).
    Default price is half the market so it never fills (it shows in Kite, then is cancelled
    after 8 s); --fill places it at the real price."""
    name = name.upper()
    exch = "MCX" if name in ("CRUDEOIL", "CRUDEOILM", "NATURALGAS", "GOLD", "GOLDM", "SILVER", "SILVERM") else "NFO"
    rows = [r for r in kite.instruments(exch) if r["name"] == name and r["expiry"] >= today]
    opts = [r for r in rows if r["instrument_type"] in ("CE", "PE")]
    futs = sorted((r for r in rows if r["instrument_type"] == "FUT"), key=lambda r: r["expiry"])
    if not opts or not futs:
        print(f"{name}: no options / futures on {exch}")
        return
    exp = min(r["expiry"] for r in opts)
    opts = [r for r in opts if r["expiry"] == exp]
    fq = kite.quote([f"{exch}:{futs[0]['tradingsymbol']}"])[f"{exch}:{futs[0]['tradingsymbol']}"]
    spot = float(fq["last_price"])
    atm = min({float(r["strike"]) for r in opts}, key=lambda k: abs(k - spot))
    leg = next(r for r in opts if float(r["strike"]) == atm and r["instrument_type"] == side)
    q = kite.quote([f"{exch}:{leg['tradingsymbol']}"])[f"{exch}:{leg['tradingsymbol']}"]
    d = q.get("depth") or {}
    snap = pd.DataFrame([{"name": name, "strike": atm, "type": side, "ts": leg["tradingsymbol"],
                          "ltp": float(q.get("last_price") or 0),
                          "ask": float((d.get("sell") or [{}])[0].get("price") or 0)}])
    r = {"name": name, "signal": side, "option": {"strike": atm}}
    lot = int(leg.get("lot_size") or 1)
    print(f"TEST {name} fut {futs[0]['tradingsymbol']} {spot:g} → ATM {atm:g} {side} {leg['tradingsymbol']} "
          f"LTP ₹{snap['ltp'][0]:g} ask ₹{snap['ask'][0]:g} lot {lot} ({exch}, expiry {exp})")
    res = buy_atm(kite, r, snap, lot, 1, "NRML", exchange=exch,
                  tick_size=float(leg.get("tick_size") or 0.05), price_factor=1.005 if fill else 0.5)
    body = (f"🧪 TEST — {'🟢' if side == 'CE' else '🔴'} {name} {spot:g} — BUY {atm:g} {side} "
            f"@ ₹{snap['ltp'][0]:g}\n   {res}"
            + ("" if fill else "\n   (test price: half the market — not meant to fill)"))
    print(body)
    topic = e.get("OI_NTFY_TOPIC") or e.get("NTFY_TOPIC", "")
    if topic:
        push(topic, f"OI monitor TEST {name}", body)
        print("pushed to ntfy")


def push(topic: str, title: str, body: str) -> None:
    parts, cur = [], ""
    for line in body.split("\n\n"):
        if cur and len((cur + "\n\n" + line).encode()) > PUSH_MAX_BYTES:
            parts.append(cur)
            cur = line
        else:
            cur = f"{cur}\n\n{line}" if cur else line
    if cur:
        parts.append(cur)
    for i, p in enumerate(parts, 1):
        t = title + (f" ({i}/{len(parts)})" if len(parts) > 1 else "")
        try:
            requests.post(f"https://ntfy.sh/{topic}", data=p.encode(),
                          headers={"Title": t.encode(), "Priority": "high", "Tags": "chart_with_upwards_trend"},
                          timeout=10)
        except Exception as ex:
            print(f"  ntfy failed: {ex}")
        time.sleep(1)


def show(a: dict, now: pd.DataFrame, base: pd.DataFrame | None) -> None:
    g = now[now["name"] == a["name"]]
    b = base[base["name"] == a["name"]] if base is not None else g
    m = g.merge(b[["strike", "type", "oi"]], on=["strike", "type"], how="left", suffixes=("", "_b"))
    m["chg"] = m["oi"] - m["oi_b"].fillna(m["oi"])
    t = m.pivot_table(index="strike", columns="type", values=["oi", "chg"], aggfunc="sum")
    print(f"\n{a['name']} {a['spot']:g} ({a['price_pct']:+.2f}%)  ATM {a['atm']:g}  fut {a['fut']} {a['fut_pct']:+.2f}%")
    print(f"{'strike':>9} {'call OI':>10} {'call chg':>10} {'put OI':>10} {'put chg':>10}")
    for k, r in t.iterrows():
        mark = " ◀ ATM" if k == a["atm"] else ""
        print(f"{k:>9g} {r[('oi', 'CE')]:>10,.0f} {r[('chg', 'CE')]:>+10,.0f} "
              f"{r[('oi', 'PE')]:>10,.0f} {r[('chg', 'PE')]:>+10,.0f}{mark}")
    print(f"near ATM: calls {om.fmt_oi(a['call_chg'])} ({a['call_pct']:+.1f}%), puts {om.fmt_oi(a['put_chg'])} "
          f"({a['put_pct']:+.1f}%) · call wall {a['call_wall']} · put wall {a['put_wall']}")
    for side in ("ce", "pe"):
        print(f"{side.upper()} score {a[side + '_score']}/6: " +
              ", ".join(f"{'✓' if v else '✗'} {k}" for k, v in a[side + "_checks"].items()))
    print(f"signal: {a['signal'] or 'none'}  option: {a['option']}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="run outside 09:20–15:30 IST")
    ap.add_argument("--dry-run", action="store_true", help="print only: no push, no signal state")
    ap.add_argument("--show", metavar="NAME", help="print one stock's strike table and checks")
    ap.add_argument("--all", action="store_true", help="print a summary line for every F&O stock")
    ap.add_argument("--test-order", metavar="NAME", help="place a test ATM order + push (e.g. CRUDEOIL)")
    ap.add_argument("--side", default="CE", choices=["CE", "PE"], help="--test-order side")
    ap.add_argument("--fill", action="store_true", help="--test-order at the real price (it will fill)")
    a = ap.parse_args()

    now = datetime.datetime.now(IST)
    today, hm = now.date(), now.strftime("%H%M")
    if not (a.force or a.test_order) and (now.weekday() >= 5 or not RUN_FROM <= now.time() <= RUN_TO):
        return
    e = env()
    kite = KiteConnect(e.get("KITE_API_KEY", ""))
    kite.set_access_token(e.get("KITE_ACCESS_TOKEN", ""))

    if a.test_order:
        test_order(kite, e, a.test_order, a.side, a.fill, today)
        return

    t0 = time.time()
    uni = universe(kite, today)
    snap, traded = snapshot(kite, uni)
    if snap.empty:
        print(f"[{now:%H:%M}] no data")
        return
    if traded and traded != today and not a.force:
        print(f"[{now:%H:%M}] market closed today (last trade {traded}) — nothing saved")
        return
    day_dir = DATA / str(today)
    day_dir.mkdir(parents=True, exist_ok=True)
    base_p, recent_p = base_and_recent(today, hm)
    with gzip.open(day_dir / f"{hm}.csv.gz", "wt") as fh:
        snap.to_csv(fh, index=False)
    base, recent = load(base_p), load(recent_p)

    om.MAX_SPREAD_PCT = float(e.get("OI_MAX_SPREAD") or om.MAX_SPREAD_PCT)
    om.FRESH_REQUIRED = e.get("OI_FRESH", "1") == "1"     # STRONG needs the last ~20 min to agree
    res = om.analyse_all(snap, base, recent)
    liquid_top = int(e.get("OI_LIQUID_TOP") or 0)       # 0 = every F&O stock can signal
    if liquid_top:
        lq_p = day_dir / "liquid.csv"
        if lq_p.exists():
            rank = pd.read_csv(lq_p)
        else:
            prev = [f for d in sorted(DATA.iterdir()) if d.is_dir() and d.name[:2] == "20" and d.name < str(today)
                    for f in [snap_files(datetime.date.fromisoformat(d.name))] if f]
            rank = om.liquid_rank(load(prev[-1][-1])) if prev else None
            if rank is not None:
                rank.to_csv(lq_p, index=False)
        if rank is None:
            print("  liquid list: no previous-day snapshot with volume — every stock allowed today")
        else:
            ok_names = set(rank[rank["rank"] <= liquid_top]["name"])
            dropped = [r["name"] for r in res if r.get("signal") and r["name"] not in ok_names]
            for r in res:
                if r.get("signal") and r["name"] not in ok_names:
                    r["signal"] = None
            if dropped:
                print(f"  illiquid (not in yesterday's top {liquid_top} by ATM±2 option value): {', '.join(dropped)}")
    strong = [r for r in res if r.get("signal")]
    print(f"[{now:%H:%M}] {snap['name'].nunique()} stocks, {len(snap)} contracts in {time.time() - t0:.0f}s · "
          f"base {base_p.parent.name + '/' + base_p.name if base_p else 'none (first snapshot)'} · "
          f"recent {recent_p.name if recent_p else 'none'} · strong CE {sum(r['signal'] == 'CE' for r in strong)}"
          f" PE {sum(r['signal'] == 'PE' for r in strong)}")

    if a.all:
        ok = sorted((r for r in res if "error" not in r), key=lambda r: -max(r["ce_score"], r["pe_score"]))
        print(f"\n{'stock':<12}{'price':>9}{'chg%':>7}{'calls ATM':>11}{'puts ATM':>10}  {'futures':<15}"
              f"{'CE':>3}{'PE':>4}  {'call wall':>9}{'put wall':>9}  signal")
        for r in ok:
            print(f"{r['name']:<12}{r['spot']:>9g}{r['price_pct']:>+7.2f}{r['call_pct']:>+10.1f}%{r['put_pct']:>+9.1f}%"
                  f"  {r['fut']:<15}{r['ce_score']:>3}{r['pe_score']:>4}  {r['call_wall'] or 0:>9g}"
                  f"{r['put_wall'] or 0:>9g}  {('STRONG ' + r['signal']) if r['signal'] else ''}")
        bad = [r["name"] for r in res if "error" in r]
        if bad:
            print(f"errors: {', '.join(bad)}")

    if a.show:
        hit = [r for r in res if r["name"] == a.show.upper() and "error" not in r]
        if hit:
            show(hit[0], snap, base)
        else:
            print(f"{a.show.upper()} not found")

    state_p = day_dir / "sent.json"
    sent = json.loads(state_p.read_text()) if state_p.exists() else {}
    early, armed = sent.setdefault("_early", {}), sent.setdefault("_armed", {})
    use_rules = e.get("OI_NEW_RULES", "1") == "1"
    rules.WEAK_OK = e.get("OI_FUT_WEAK", "1") == "1"     # CE also on short covering, PE on long unwinding
    rules.FUT_MODE = e.get("OI_FUT_MODE", "combined")   # expiry7 = current month until 7 days to expiry
    push_all = e.get("OI_PUSH_ALL", "0") == "1"
    new = [r for r in strong if sent.get(r["name"]) != r["signal"]]
    auto = e.get("OI_AUTO_TRADE", "0") == "1" and not a.dry_run
    max_orders = int(e.get("OI_MAX_ORDERS") or 5)
    lots = max(1, int(e.get("OI_LOTS") or 1))
    n_orders = int(sent.get("_orders", 0))
    product = "MIS" if e.get("OI_PRODUCT", "NRML").upper() == "MIS" else "NRML"
    sl_pct = float(e.get("OI_SL_PCT") or 20)
    tgt_pct = float(e.get("OI_TARGET_PCT") or 40)
    use_gtt = e.get("OI_GTT", "0") == "1"
    days_left = lambda u: (datetime.date.fromisoformat(u["expiry"]) - today).days
    by_name = {r["name"]: r for r in res if "error" not in r}
    naive = now.replace(tzinfo=None, second=0, microsecond=0)
    hm4 = now.strftime("%H%M")
    pushes: list[str] = []          # what goes to ntfy this scan
    logs: list[str] = []            # log only (WATCH / EARLY, unless OI_PUSH_ALL=1)

    def order(r: dict) -> str:
        """Place the order for r (signal side, ATM strike) — or say why not."""
        nonlocal n_orders
        if not auto:
            return "no order — OI_AUTO_TRADE is off"
        if hm4 >= rules.ORDER_UNTIL:
            return "no order — 15:00 or later"
        if product == "NRML" and days_left(uni[r["name"]]) < MIN_DAYS_TO_EXPIRY:
            return (f"no order — expiry {uni[r['name']]['expiry']} is under {MIN_DAYS_TO_EXPIRY} days away "
                    f"(stock options settle physically)")
        if n_orders >= max_orders:
            return f"no order — {max_orders} orders already today (OI_MAX_ORDERS)"
        n_orders += 1
        return buy_atm(kite, r, snap, int(uni[r["name"]].get("lot") or 1), lots, product, sl_pct, tgt_pct, use_gtt)

    def atm_r(name: str, side: str) -> dict | None:
        """A minimal signal dict (ATM strike now) for an armed stock that may no longer be STRONG."""
        g = snap[snap["name"] == name]
        if g.empty:
            return None
        spot = float(g["spot"].iloc[0])
        return {"name": name, "signal": side, "spot": spot,
                "option": {"strike": min(g["strike"].unique(), key=lambda k: abs(k - spot))}}

    if not use_rules:                                   # OI_NEW_RULES=0: buy every new signal (old way)
        for r in new:
            pushes.append(om.signal_line(r) + "\n   " + order(r))
            sent[r["name"]] = r["signal"]
    else:
        kd = rules.KiteData(kite, tokens=nse_tokens(kite, today),
                            futs={n: [{"instrument_token": t, "expiry": x} for t, x in u.get("futs", [])]
                                  for n, u in uni.items()})

        def evaluate(r: dict, prefix: str = "") -> None:
            name, side = r["name"], r["signal"]
            try:
                d = rules.decide(name, side, today, naive, float(r["spot"]), r["call_wall"], r["put_wall"],
                                 kd, r.get("fut"))
            except Exception as ex:                     # no rules read → no order (conservative)
                logs.append(f"{om.signal_line(r)}\n   ⚠️ rules failed ({ex}) — no order")
                return
            head = om.signal_line(r) + (f"\n   {prefix}" if prefix else "")
            if d["kind"] != "BUY":                      # no order now — don't let the line say BUY
                head = head.replace(" — BUY ", " — signal ", 1)
            if d["kind"] == "BUY":
                pushes.append(f"{head}\n   ✅ BUY — {d['text']}\n   {order(r)}")
            elif d["kind"] == "WAIT":
                z = d["level"]
                armed[name] = {"side": side, "lo": z.lo, "hi": z.hi, "label": z.label,
                               "since": naive.isoformat(), "checked": naive.isoformat()}
                pushes.append(f"{head}\n   ⏸ WAITING — {d['text']}")
            else:
                logs.append(f"{head}\n   👀 WATCH — {d['text']}")

        # 1. WAITING signals: break → buy, held / OI turned / 15:00 → cancel
        for name, w in list(armed.items()):
            side, z = w["side"], levels.Level(w["lo"], w["hi"], w["label"])
            r_now = by_name.get(name)
            score = r_now[f"{side.lower()}_score"] if r_now else None
            if hm4 >= rules.ORDER_UNTIL:
                pushes.append(f"❌ CANCELLED — {name} {side}: {z.text()} never broke by 15:00")
                armed.pop(name)
                continue
            if score is not None and score <= 2:
                pushes.append(f"❌ CANCELLED — {name} {side}: OI turned (score now {score}/6)")
                armed.pop(name)
                continue
            try:
                kind, txt, at = rules.watch_break(name, side, z, today,
                                                  datetime.datetime.fromisoformat(w["checked"]), naive, kd)
            except Exception as ex:
                logs.append(f"{name} {side} waiting: break check failed ({ex})")
                continue
            if kind == "BREAK":
                r = atm_r(name, side)
                pushes.append(f"{'🟢' if side == 'CE' else '🔴'} {name} {side} — ✅ BREAK → BUY: {txt}\n   "
                              + (order(r) if r else "no order — stock not in this scan"))
                armed.pop(name)
            elif kind == "HELD":
                pushes.append(f"❌ CANCELLED — {name} {side}: {txt}")
                armed.pop(name)
            else:
                w["checked"] = naive.isoformat()

        # 2. EARLY signals (before 09:40): decided at the first scan from 09:40 where still STRONG
        if hm4 >= rules.FIRST_ORDER:
            for name, side in list(early.items()):
                r_now = by_name.get(name)
                if hm4 >= rules.ORDER_UNTIL:
                    logs.append(f"⏱ EARLY — {name} {side} faded (not STRONG again by 15:00)")
                    early.pop(name)
                elif r_now and r_now.get("signal") == side:
                    early.pop(name)
                    evaluate(r_now, prefix=f"early signal → still STRONG at {now:%H:%M}")

        # 3. new signals
        for r in new:
            name, side = r["name"], r["signal"]
            sent[name] = side
            if name in armed and armed[name]["side"] != side:
                logs.append(f"{name}: waiting {armed.pop(name)['side']} dropped — signal flipped to {side}")
            early.pop(name, None) if early.get(name) != side else None
            if hm4 >= rules.ORDER_UNTIL:
                logs.append(f"{om.signal_line(r)}\n   ⏰ 15:00 or later — no order")
            elif hm4 < rules.FIRST_ORDER:
                early[name] = side
                logs.append(f"{om.signal_line(r)}\n   ⏱ EARLY — decided from 09:40 if still STRONG")
            else:
                evaluate(r)

    for m in pushes:
        print(m)
    for m in logs:
        print(m)
    if not a.dry_run:
        topic = e.get("OI_NTFY_TOPIC") or e.get("NTFY_TOPIC", "")
        out = pushes + (logs if push_all else [])
        if topic and out:
            push(topic, f"OI monitor {now:%H:%M}: {len(out)} update(s)", "\n\n".join(out))
        sent["_orders"] = n_orders
        state_p.write_text(json.dumps(sent))

    for d in DATA.iterdir():   # keep the last KEEP_DAYS days of snapshots
        if d.is_dir() and d.name < str(today - datetime.timedelta(days=KEEP_DAYS)):
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:          # output piped into head / less that closed early
        import os
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
