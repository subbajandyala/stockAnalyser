"""
OI monitor — all F&O stocks, every 10 minutes, ntfy push for NEW strong CE / PE signals.
Alerts only: places no orders.

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

from screener import oi_monitor as om

ROOT = pathlib.Path(__file__).parent.parent
DATA = ROOT / "data" / "oi"
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
STRIKES_EACH_SIDE = 10
INDEXES = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}
RUN_FROM, RUN_TO = datetime.time(9, 20), datetime.time(15, 31)
RECENT_MIN = 18          # "recent" = the snapshot at least this many minutes old
KEEP_DAYS = 30
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


def quotes(kite: KiteConnect, keys: list[str]) -> dict:
    out = {}
    for i in range(0, len(keys), 500):
        for attempt in range(3):
            try:
                out.update(kite.quote(keys[i:i + 500]))
                break
            except Exception as ex:
                if attempt == 2:
                    print(f"  quote batch {i // 500 + 1} failed: {ex}")
                time.sleep(2)
        time.sleep(1.05)          # Kite quote limit: 1 request / second
    return out


def universe(kite: KiteConnect, today: datetime.date) -> dict:
    """{name: {"fut": ts, "expiry": "YYYY-MM-DD", "opts": [[strike, CE ts, PE ts], ...]}}"""
    cache = DATA / f"universe_{today}.json"
    if cache.exists():
        return json.loads(cache.read_text())
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
            uni[name] = {"fut": fut[0]["tradingsymbol"], "expiry": str(exp), "opts": opts}
    DATA.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(uni))
    for old in DATA.glob("universe_*.json"):
        if old != cache:
            old.unlink()
    return uni


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
    a = ap.parse_args()

    now = datetime.datetime.now(IST)
    today, hm = now.date(), now.strftime("%H%M")
    if not a.force and (now.weekday() >= 5 or not RUN_FROM <= now.time() <= RUN_TO):
        return
    e = env()
    kite = KiteConnect(e.get("KITE_API_KEY", ""))
    kite.set_access_token(e.get("KITE_ACCESS_TOKEN", ""))

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

    res = om.analyse_all(snap, base, recent)
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
    new = [r for r in strong if sent.get(r["name"]) != r["signal"]]
    for r in new:
        print(om.signal_line(r))
    if new and not a.dry_run:
        topic = e.get("OI_NTFY_TOPIC") or e.get("NTFY_TOPIC", "")
        if topic:
            push(topic, f"OI monitor {now:%H:%M}: {len(new)} new signal(s)",
                 "\n\n".join(om.signal_line(r) for r in new))
        for r in new:
            sent[r["name"]] = r["signal"]
        state_p.write_text(json.dumps(sent))

    for d in DATA.iterdir():   # keep the last KEEP_DAYS days of snapshots
        if d.is_dir() and d.name < str(today - datetime.timedelta(days=KEEP_DAYS)):
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    main()
