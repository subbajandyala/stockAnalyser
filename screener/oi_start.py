"""
Move-START trigger for the OI monitor — buy when a move begins, not after it is done.

The old trigger (screener/oi_monitor.py, STRONG) measures OI and price against YESTERDAY's close,
so it only fires once a move is mostly over (DIXON 12750 PE, 13 Nov: bought at the top at 14:40).
This one looks at the LAST ~10 MINUTES, all from the saved snapshots (no extra Kite calls):

CE (PE is the mirror) — all must hold:
  1 breakout   the stock's price now is ≥ BREAK_PCT above the highest price of the previous
               30–60 min (snapshots from RANGE_MIN to WINDOW_MIN ago), and ~10 min ago it was not
  2 early      no more than EARLY_PCT beyond that level, above VWAP but no more than VWAP_PCT above
               it (VWAP = Kite's average traded price today; skipped when the snapshot has none)
  3 OI flip    near-ATM OI over the same ~10 min: calls covering ≥ FLIP_PCT or puts added ≥ FLIP_PCT,
               and neither side moving against (calls not added / puts not unwinding > AGAINST_PCT)
  4 volume     the stock's volume in those ~10 min ≥ VOL_X × its average per 10 min today
               (skipped when the snapshot has no stock volume)
  5 context    yesterday's OI picture is not strongly against (not calls added AND puts unwinding
               ≥ 5 % near ATM for a CE)
  6 option     ATM option spread ≤ om.MAX_SPREAD_PCT
Stop (index of the move, for the alert): back inside the broken level by STOP_PCT.
"""

from __future__ import annotations

import datetime

import pandas as pd

from screener import oi_monitor as om

WINDOW_MIN = 10        # "last ~10 min": compare with the snapshot at least this old (−1 min slack)
RANGE_MIN = 60         # the range the breakout must leave: snapshots from 60 … 10 min ago
BREAK_PCT = 0.10       # close at least this % beyond the range
EARLY_PCT = 0.60       # … and at most this % beyond it (else the move has already run)
VWAP_PCT = 1.0         # at most this % away from VWAP, on the right side of it
FLIP_PCT = 1.5         # near-ATM OI change over ~10 min that counts
AGAINST_PCT = 0.5      # the other side may not move against by more than this
VOL_X = 1.5            # volume of the last ~10 min vs the average 10 min today
STOP_PCT = 0.30        # stop: this far back inside the broken level
FROM = "0930"          # first start the trigger can call (needs a range first)


def snap_ago(files: list, now_hm: str, minutes: int):
    """The latest of today's snapshot files at least `minutes` older than now_hm (name HHMM.csv.gz)."""
    cut = (datetime.datetime.strptime(now_hm, "%H%M") - datetime.timedelta(minutes=minutes - 1)).strftime("%H%M")
    old = [f for f in files if f.name[:4] <= cut and f.name[:4] != now_hm]
    return old[-1] if old else None


def range_files(files: list, now_hm: str) -> list:
    t = datetime.datetime.strptime(now_hm, "%H%M")
    lo = (t - datetime.timedelta(minutes=RANGE_MIN)).strftime("%H%M")
    hi = (t - datetime.timedelta(minutes=WINDOW_MIN - 1)).strftime("%H%M")
    return [f for f in files if lo <= f.name[:4] <= hi and f.name[:4] != now_hm]


def _near_change(now: pd.DataFrame, prev: pd.DataFrame, typ: str, keep: set) -> float | None:
    a = now[(now["type"] == typ) & now["strike"].isin(keep)].groupby("strike")["oi"].sum()
    b = prev[(prev["type"] == typ) & prev["strike"].isin(keep)].groupby("strike")["oi"].sum()
    j = pd.concat([a, b], axis=1, keys=["a", "b"]).dropna()
    if j.empty or not j["b"].sum():
        return None
    return float((j["a"].sum() - j["b"].sum()) / j["b"].sum() * 100)


def starts(now: pd.DataFrame, prev: pd.DataFrame | None, window: list[pd.DataFrame], context: dict,
           now_hm: str) -> list[dict]:
    """Move starts in this snapshot. now / prev: snapshots (all stocks); window: the range snapshots;
    context: name → om.analyse_stock() result vs yesterday (for check 5)."""
    if prev is None or prev.empty or len(window) < 2 or now_hm < FROM:
        return []
    rng = pd.concat([w[["name", "spot"]].drop_duplicates("name") for w in window])
    hi_lo = rng.groupby("name")["spot"].agg(["max", "min", "count"])
    out = []
    p_by = {n: g for n, g in prev.groupby("name")}
    for name, g in now.groupby("name"):
        if name not in hi_lo.index or name not in p_by or hi_lo.loc[name, "count"] < 2:
            continue
        pg = p_by[name]
        r0, p0 = g.iloc[0], pg.iloc[0]
        spot, spot_prev = float(r0["spot"]), float(p0["spot"])
        top, bot = float(hi_lo.loc[name, "max"]), float(hi_lo.loc[name, "min"])
        side = None
        if spot >= top * (1 + BREAK_PCT / 100) and spot_prev <= top:
            side, level = "CE", top
        elif spot <= bot * (1 - BREAK_PCT / 100) and spot_prev >= bot:
            side, level = "PE", bot
        if not side:
            continue
        ce = side == "CE"
        why = []
        beyond = (spot / level - 1) * 100 if ce else (1 - spot / level) * 100
        if beyond > EARLY_PCT:
            continue                                            # already ran
        vwap = float(r0.get("vwap") or 0) if "vwap" in g.columns else 0.0
        if vwap:
            off = (spot / vwap - 1) * 100
            if (ce and not 0 <= off <= VWAP_PCT) or (not ce and not -VWAP_PCT <= off <= 0):
                continue
            why.append(f"VWAP {vwap:g} ({off:+.2f}%)")
        strikes = sorted(g["strike"].unique())
        atm_i = min(range(len(strikes)), key=lambda j: abs(strikes[j] - spot))
        c = _near_change(g, pg, "CE", set(strikes[max(0, atm_i - 1): atm_i + 4]))
        p = _near_change(g, pg, "PE", set(strikes[max(0, atm_i - 3): atm_i + 2]))
        if c is None or p is None:
            continue
        if ce:
            flip = c <= -FLIP_PCT or p >= FLIP_PCT
            against = c > AGAINST_PCT or p < -AGAINST_PCT
        else:
            flip = p <= -FLIP_PCT or c >= FLIP_PCT
            against = p > AGAINST_PCT or c < -AGAINST_PCT
        if not flip or against:
            continue
        why.append(f"10-min OI calls {c:+.1f}% · puts {p:+.1f}%")
        if "stock_volume" in g.columns and "stock_volume" in pg.columns and float(p0.get("stock_volume") or 0) > 0:
            v_now, v_prev = float(r0["stock_volume"]), float(p0["stock_volume"])
            mins = max(10.0, _mins_since_open(context.get("_prev_hm", now_hm)))   # minutes up to `prev`
            avg10 = v_prev / (mins / 10)
            ratio = (v_now - v_prev) / avg10 if avg10 else 0
            if ratio < VOL_X:
                continue
            why.append(f"volume {ratio:.1f}× normal")
        a = context.get(name)
        if a:
            chk = a.get("pe_checks" if ce else "ce_checks", {})
            vals = list(chk.values())
            if len(vals) >= 2 and vals[0] and vals[1]:
                continue                                        # yesterday's OI strongly against
        row = g[(g["strike"] == strikes[atm_i]) & (g["type"] == side)]
        if row.empty:
            continue
        o = row.iloc[0]
        ltp, bid, ask = float(o["ltp"] or 0), float(o["bid"] or 0), float(o["ask"] or 0)
        spread = (ask - bid) / ltp * 100 if ltp and bid and ask else 999.0
        if spread > om.MAX_SPREAD_PCT:
            continue
        stop = level * (1 - STOP_PCT / 100) if ce else level * (1 + STOP_PCT / 100)
        out.append({"name": name, "signal": side, "spot": spot, "level": level, "stop": round(stop, 2),
                    "beyond_pct": round(beyond, 2), "price_pct": a.get("price_pct") if a else None,
                    "option": {"strike": strikes[atm_i], "ltp": ltp, "spread_pct": round(spread, 1)},
                    "entry": ask or ltp, "ts": o.get("ts"), "why": why,
                    "call_wall": a.get("call_wall") if a else None, "put_wall": a.get("put_wall") if a else None})
    return out


def _mins_since_open(hm: str) -> float:
    t = datetime.datetime.strptime(hm, "%H%M")
    return (t - t.replace(hour=9, minute=15)).total_seconds() / 60


def line(s: dict) -> str:
    up = s["signal"] == "CE"
    pct = f" ({s['price_pct']:+.1f}% today)" if s.get("price_pct") is not None else ""
    return (f"{'🟢' if up else '🔴'} {s['name']} {s['spot']:g}{pct} — START {s['signal']}: "
            f"{'broke above' if up else 'broke below'} the last hour's {'high' if up else 'low'} {s['level']:g} "
            f"(+{s['beyond_pct']:.2f}% beyond) — BUY {s['option']['strike']:g} {s['signal']} @ ₹{s['entry']:g}\n"
            f"   {' · '.join(s['why'])} · stop: stock back {'below' if up else 'above'} {s['stop']:g}")
