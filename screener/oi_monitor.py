"""
OI change by strike for F&O stocks → STRONG CE / STRONG PE (alerts only).

Every snapshot is one row per option contract (ATM ±10 strikes, nearest expiry):
    name, strike, type (CE/PE), oi, ltp, bid, ask, spot, spot_close, fut_ltp, fut_close, fut_oi
Changes are measured two ways, like Sensibull's OI-change chart:
    day     — now vs the base snapshot (yesterday's last one, else today's first)
    recent  — now vs the snapshot ~20 min ago (is it still happening?)

The four cases, read near the money (writers' view):
    call OI addition  above price → ceiling (bearish)   call unwinding → ceiling breaking (bullish)
    put  OI addition  below price → support (bullish)   put  unwinding → support breaking (bearish)

STRONG CE needs the three core checks plus at least two of the other three (score ≥ 5 of 6):
    1 calls unwinding near ATM        (core)    4 futures price up (long buildup / short covering)
    2 puts added near ATM             (core)    5 fresh: last ~20 min agree
    3 price up past call strikes      (core)    6 room: next call wall ≥ ROOM_PCT above
      that are unwinding
STRONG PE is the mirror. The ATM option to buy must have a tight bid/ask spread.
"""

from __future__ import annotations

import pandas as pd

NEAR = 3              # strikes either side of ATM that decide the signal
MIN_CHG_PCT = 5.0     # near-ATM OI change must be ≥ 5% of the base OI to count
MIN_PRICE_PCT = 0.5   # stock move vs yesterday's close
MIN_FUT_PCT = 0.3     # futures move vs yesterday's close
ROOM_PCT = 1.5        # distance to the next wall
MAX_SPREAD_PCT = 3.0  # ATM option bid/ask spread, % of its price


def _near(df: pd.DataFrame, atm_i: int, lo: int, hi: int) -> pd.DataFrame:
    strikes = sorted(df["strike"].unique())
    keep = strikes[max(0, atm_i + lo): atm_i + hi + 1]
    return df[df["strike"].isin(keep)]


def fut_label(price_chg: float, oi_chg: float) -> str:
    if price_chg >= 0:
        return "long buildup" if oi_chg > 0 else "short covering"
    return "short buildup" if oi_chg > 0 else "long unwinding"


def fmt_oi(x: float) -> str:
    a = abs(x)
    s = f"{a / 1e7:.2f}Cr" if a >= 1e7 else f"{a / 1e5:.1f}L" if a >= 1e5 else f"{a / 1e3:.0f}K"
    return ("+" if x >= 0 else "−") + s


def analyse_stock(now: pd.DataFrame, base: pd.DataFrame, recent: pd.DataFrame | None) -> dict:
    """Score one stock. Each frame holds that stock's rows of one snapshot."""
    r0 = now.iloc[0]
    spot, close = float(r0["spot"]), float(r0["spot_close"] or 0)
    price_pct = (spot / close - 1) * 100 if close else 0.0
    fut_close = float(r0.get("fut_close") or 0)
    fut_pct = (float(r0.get("fut_ltp") or 0) / fut_close - 1) * 100 if fut_close else 0.0
    b0 = base.iloc[0] if len(base) else r0
    fut_oi_chg = float(r0.get("fut_oi") or 0) - float(b0.get("fut_oi") or 0)

    strikes = sorted(now["strike"].unique())
    atm = min(strikes, key=lambda s: abs(s - spot))
    atm_i = strikes.index(atm)

    m = now.merge(base[["strike", "type", "oi"]], on=["strike", "type"], how="left", suffixes=("", "_base"))
    m["oi_base"] = m["oi_base"].fillna(m["oi"])
    m["d_day"] = m["oi"] - m["oi_base"]
    if recent is not None and len(recent):
        m = m.merge(recent[["strike", "type", "oi"]].rename(columns={"oi": "oi_rec"}),
                    on=["strike", "type"], how="left")
        m["d_rec"] = m["oi"] - m["oi_rec"].fillna(m["oi"])
        rec_spot = float(recent.iloc[0]["spot"])
    else:
        m["d_rec"], rec_spot = 0.0, spot

    calls, puts = m[m["type"] == "CE"], m[m["type"] == "PE"]

    def band(df, lo, hi):
        b = _near(df, atm_i, lo, hi)
        base_oi = b["oi_base"].sum()
        return b, (b["d_day"].sum() / base_oi * 100 if base_oi else 0.0)

    # Calls decide at/above the price, puts at/below (one strike of overlap)
    c_band, c_pct = band(calls, -1, NEAR)
    p_band, p_pct = band(puts, -NEAR, 1)
    c_rec, p_rec = c_band["d_rec"].sum(), p_band["d_rec"].sum()

    above = calls[calls["strike"] > spot]
    below = puts[puts["strike"] < spot]
    call_wall = float(above.loc[above["oi"].idxmax(), "strike"]) if len(above) else None
    put_wall = float(below.loc[below["oi"].idxmax(), "strike"]) if len(below) else None
    # writers whose strike the price has already crossed are covering
    c_crossed = bool(((c_band["strike"] <= spot) & (c_band["d_day"] < 0)).any())
    p_crossed = bool(((p_band["strike"] >= spot) & (p_band["d_day"] < 0)).any())

    ce = {
        "calls unwinding": c_pct <= -MIN_CHG_PCT,
        "puts added": p_pct >= MIN_CHG_PCT,
        "price up past unwinding call strikes": price_pct >= MIN_PRICE_PCT and c_crossed,
        "futures up": fut_pct >= MIN_FUT_PCT,
        "fresh (last 20 min)": c_rec <= 0 and p_rec >= 0 and spot >= rec_spot and (c_rec < 0 or p_rec > 0),
        "room to next call wall": call_wall is None or (call_wall / spot - 1) * 100 >= ROOM_PCT,
    }
    pe = {
        "puts unwinding": p_pct <= -MIN_CHG_PCT,
        "calls added": c_pct >= MIN_CHG_PCT,
        "price down past unwinding put strikes": price_pct <= -MIN_PRICE_PCT and p_crossed,
        "futures down": fut_pct <= -MIN_FUT_PCT,
        "fresh (last 20 min)": p_rec <= 0 and c_rec >= 0 and spot <= rec_spot and (p_rec < 0 or c_rec > 0),
        "room to next put wall": put_wall is None or (1 - put_wall / spot) * 100 >= ROOM_PCT,
    }

    def verdict(checks: dict) -> tuple[int, bool]:
        v = list(checks.values())
        score = sum(v)
        return score, all(v[:3]) and score >= 5

    ce_score, ce_strong = verdict(ce)
    pe_score, pe_strong = verdict(pe)
    side = "CE" if ce_strong else "PE" if pe_strong else None

    opt = None
    if side:   # the ATM option to buy, and whether it is liquid enough
        row = m[(m["strike"] == atm) & (m["type"] == side)]
        if len(row):
            o = row.iloc[0]
            ltp, bid, ask = float(o["ltp"] or 0), float(o["bid"] or 0), float(o["ask"] or 0)
            spread = (ask - bid) / ltp * 100 if ltp and bid and ask else 999.0
            opt = {"strike": atm, "ltp": ltp, "spread_pct": round(spread, 1),
                   "liquid": spread <= MAX_SPREAD_PCT}
        if not (opt and opt["liquid"]):
            side = None

    return {
        "name": r0["name"], "spot": spot, "price_pct": round(price_pct, 2), "atm": atm,
        "fut_pct": round(fut_pct, 2), "fut": fut_label(fut_pct, fut_oi_chg),
        "call_chg": float(c_band["d_day"].sum()), "put_chg": float(p_band["d_day"].sum()),
        "call_pct": round(c_pct, 1), "put_pct": round(p_pct, 1),
        "call_total": float(calls["d_day"].sum()), "put_total": float(puts["d_day"].sum()),
        "call_wall": call_wall, "put_wall": put_wall,
        "ce_score": ce_score, "pe_score": pe_score, "ce_checks": ce, "pe_checks": pe,
        "signal": side, "option": opt,
    }


def analyse_all(now: pd.DataFrame, base: pd.DataFrame | None, recent: pd.DataFrame | None) -> list[dict]:
    out = []
    base = base if base is not None else now
    for name, g in now.groupby("name"):
        if g["spot"].iloc[0] <= 0 or g["strike"].nunique() < 2 * NEAR + 1:
            continue
        b = base[base["name"] == name]
        r = recent[recent["name"] == name] if recent is not None else None
        try:
            out.append(analyse_stock(g, b, r))
        except Exception as e:      # one bad stock must not stop the scan
            out.append({"name": name, "error": str(e), "signal": None})
    return out


def signal_line(a: dict) -> str:
    side, o = a["signal"], a["option"]
    up = side == "CE"
    wall = a["call_wall"] if up else a["put_wall"]
    wall_txt = f"next wall {wall:g} ({(wall / a['spot'] - 1) * 100:+.1f}%)" if wall else "no wall in range"
    stop = a["put_wall"] if up else a["call_wall"]
    stop_txt = f"{'support' if up else 'resistance'} {stop:g} · " if stop else ""
    score = a["ce_score"] if up else a["pe_score"]
    return (f"{'🟢' if up else '🔴'} {a['name']} {a['spot']:g} {'▲' if a['price_pct'] >= 0 else '▼'}"
            f"{abs(a['price_pct']):.1f}% — BUY {o['strike']:g} {side} @ ₹{o['ltp']:g}\n"
            f"   calls {fmt_oi(a['call_chg'])} ({a['call_pct']:+.0f}%) · puts {fmt_oi(a['put_chg'])} "
            f"({a['put_pct']:+.0f}%) near ATM · fut {a['fut']} {a['fut_pct']:+.1f}%\n"
            f"   {stop_txt}{wall_txt} · score {score}/6")
