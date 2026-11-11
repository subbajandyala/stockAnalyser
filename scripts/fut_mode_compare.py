"""
Futures-buildup modes compared — read-only, no orders, no pushes.

  combined  current + next month OI together, every day (what the monitor uses today)
  expiry7   current month only; both months in the last 7 trading days to expiry (incl. expiry day)

Part A — every F&O stock, every trading day in the range, at 10:00 / 12:30 / 14:50: the label each
         mode gives (long buildup / short covering / short buildup / long unwinding) and where
         they differ.
Part B — the signals in oi_monitor.log: whether rule 4 (CE needs long buildup, PE short buildup)
         passes under each mode, and how the option went (same tracking as oi_signals_report.py).

Kite only lists live contracts, so days whose current-month future has already expired (October
before its expiry, seen from November) cannot be rebuilt and are skipped.

Usage (Subba's machine):
  /root/venv/bin/python scripts/fut_mode_compare.py                    # last 30 days
  /root/venv/bin/python scripts/fut_mode_compare.py --days 45 --limit 50   # quick: 50 stocks
"""

import argparse
import collections
import datetime
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).parent))

import pandas as pd

from screener import oi_monitor as om
from screener import oi_rules as rules

CHECKS = [datetime.time(10, 0), datetime.time(12, 30), datetime.time(14, 50)]


def label_at(h_by_tok: dict, futs: list, day: datetime.date, upto: datetime.datetime) -> tuple[str | None, float]:
    oi_now = oi_prev = 0.0
    px_now = px_prev = None
    for k, f in enumerate(futs):
        h = h_by_tok.get(f["instrument_token"])
        if h is None or h.empty:
            continue
        c = rules.last_closed(h[h.index.date == day], upto)
        prev = h[h.index.date < day]
        if c is None or prev.empty:
            continue
        oi_now += float(c["oi"])
        oi_prev += float(prev["oi"].iloc[-1])
        if k == 0:
            px_now, px_prev = float(c["close"]), float(prev["close"].iloc[-1])
    if px_now is None or not oi_prev:
        return None, 0.0
    return om.fut_label((px_now / px_prev - 1) * 100, oi_now - oi_prev), (oi_now / oi_prev - 1) * 100


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30, help="calendar days back (default 30)")
    ap.add_argument("--limit", type=int, default=0, help="only the first N stocks (quick test)")
    ap.add_argument("--no-signals", action="store_true", help="skip part B")
    a = ap.parse_args()

    import oi_signals_report as rep
    kd = rep.Kite()
    today = datetime.date.today()
    start = today - datetime.timedelta(days=a.days)
    names = sorted({r["name"] for r in kd.k.instruments("NFO") if r["instrument_type"] == "FUT"
                    and r["segment"] == "NFO-FUT"} - {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"})
    if a.limit:
        names = names[:a.limit]
    print(f"Part A: {len(names)} stocks, {start} → {today} (two 10-min OI histories per stock) …", flush=True)

    hist: dict = {}
    for n, name in enumerate(names, 1):
        futs = kd.futures(name, start)
        hist[name] = {}
        for f in futs:
            try:
                hist[name][f["instrument_token"]] = kd.candles(name, "10minute", start - datetime.timedelta(days=5),
                                                               today, token=f["instrument_token"], oi=True)
            except Exception as ex:
                print(f"  {name}: {ex}")
        if n % 25 == 0:
            print(f"  {n}/{len(names)} stocks loaded", flush=True)

    days = sorted({d for h in hist.values() for df in h.values() if len(df) for d in set(df.index.date)
                   if start <= d <= today})
    skipped, rows = [], []
    for day in days:
        any_f = kd.futures(names[0], day)
        if not rules.current_known(any_f, day):
            skipped.append(day)
            continue
        left = rules.trading_days_to(day, any_f[0]["expiry"])
        for name in names:
            futs = kd.futures(name, day)
            if len(futs) < 2:
                continue
            use, how = rules.fut_months(futs, day, "expiry7")
            for t in CHECKS:
                upto = datetime.datetime.combine(day, t)
                comb, comb_oi = label_at(hist[name], futs, day, upto)
                e7, e7_oi = label_at(hist[name], use, day, upto)
                cur, cur_oi = label_at(hist[name], futs[:1], day, upto)
                if comb is None:
                    continue
                rows.append({"day": day, "time": f"{t:%H:%M}", "name": name, "to_expiry": left,
                             "expiry7_uses": "both" if len(use) == 2 else "current",
                             "combined": comb, "expiry7": e7, "current_only": cur,
                             "comb_oi_pct": round(comb_oi, 2), "cur_oi_pct": round(cur_oi, 2)})
    df = pd.DataFrame(rows)
    out_dir = rep.mon.ROOT / "data"
    out_dir.mkdir(exist_ok=True)
    if skipped:
        print(f"\nskipped {len(skipped)} day(s) {skipped[0]} … {skipped[-1]}: their current-month future has "
              f"expired and Kite no longer lists it")
    if df.empty:
        print("no rows")
        return
    df.to_csv(out_dir / f"fut_mode_compare_{today}.csv", index=False)
    print("\nday         to expiry  expiry7 uses  readings  combined≠current-only  combined≠expiry7")
    for day, g in df.groupby("day"):
        d1 = int((g["combined"] != g["current_only"]).sum())
        d2 = int((g["combined"] != g["expiry7"]).sum())
        print(f"{day}  {int(g['to_expiry'].iloc[0]):>6}d     {g['expiry7_uses'].iloc[0]:<10}  {len(g):>8}  "
              f"{d1:>6} ({100 * d1 / len(g):4.1f}%)          {d2:>6} ({100 * d2 / len(g):4.1f}%)")
    diff = df[df["combined"] != df["current_only"]]
    print(f"\nAll days: {len(df)} readings · combined and current-month-only disagree on {len(diff)} "
          f"({100 * len(diff) / len(df):.1f}%)")
    for (x, y), n in collections.Counter(zip(diff["combined"], diff["current_only"])).most_common(8):
        print(f"  combined {x:<15} → current only {y:<15} {n}")
    flips = diff[diff["combined"].isin(rules.FRESH.values()) | diff["current_only"].isin(rules.FRESH.values())]
    print(f"  of these, {len(flips)} change a long/short buildup (what rule 4 checks)")

    if a.no_signals:
        return
    log = rep.mon.ROOT / "oi_monitor.log"
    sigs = [s for s in rep.from_log(log) if s.get("ts")] if log.exists() else []
    print(f"\nPart B: {len(sigs)} signals from oi_monitor.log")
    all_days = rep.day_dirs()
    changed = 0
    for s in sigs:
        day, side = s["day"], s["side"]
        hh, mm = map(int, s["time"].split(":"))
        upto = datetime.datetime.combine(day, datetime.time(hh, mm))
        futs = kd.futures(s["name"], day)
        if not rules.current_known(futs, day):
            continue
        res = {}
        for mode in ("combined", "expiry7"):
            try:
                res[mode] = rules.combined_fut(s["name"], day, upto, kd, mode)[0]
            except Exception as ex:
                res[mode] = f"error {ex}"
        need = rules.FRESH[side]
        ok_c, ok_e = res["combined"] == need, res["expiry7"] == need
        later = [x for x in all_days if x > day and rep.market_files(x)]
        f = rep.follow(s, 20.0, 20.0, later[0] if later else None)
        mark = "" if ok_c == ok_e else "  ◀ differs"
        changed += ok_c != ok_e
        close = f"close {f['close_pct']:+.0f}%" if f["close_pct"] is not None else ""
        print(f"  {day} {s['time']} {s['name']:<11} {side}  combined: {res['combined']} ({'pass' if ok_c else 'WATCH'})"
              f" · expiry7: {res['expiry7']} ({'pass' if ok_e else 'WATCH'}) → {f['result']} {close}{mark}")
    print(f"\n{changed} signal(s) where the two modes decide rule 4 differently")


if __name__ == "__main__":
    main()
