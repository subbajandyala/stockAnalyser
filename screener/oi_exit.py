"""
Exits for the move-START trades (option trades; tr["mode"] == "equity": the stock itself, % on its price) (screener/oi_start.py) — checked every scan (5 min).

A trade is the option bought at a start. Each scan reads that stock's rows in the newest snapshot and
in the one ~10 min older, and the trade's own option (sold at its bid). In order:

  1 time          EXIT_BY (15:15) → out; nothing is carried overnight
  2 move failed   the stock is back past the level it broke (+ STOP_PCT, the stop in the entry alert)
  3 hard stop     premium HARD_STOP_PCT below the entry
  4 take profit   premium at least MIN_PROFIT_PCT up and the move is TURNING — 2 of these 3:
                    a  OI turns: near-ATM over the last ~10 min the writers on the trade's side are
                       building again (PE trade: puts added ≥ FLIP_PCT or calls covering ≥ FLIP_PCT)
                    b  bounce: the stock has given back ≥ RETRACE_PCT of its move since the entry
                    c  momentum: the stock is back against the trade vs ~10 min ago
  5 lock profit   once the premium was LOCK_AT_PCT up, out if half of that best gain is given back
"""

from __future__ import annotations

import pandas as pd

EXIT_BY = "1515"
STOP_PCT = 0.30
HARD_STOP_PCT = 25.0
MIN_PROFIT_PCT = 5.0
FLIP_PCT = 1.5
RETRACE_PCT = 35.0
LOCK_AT_PCT = 20.0
# equity trades (oi-monitor-equity-signals: the stock itself, MIS) — the same rules on the STOCK price, in %
EQ_MIN_PROFIT_PCT = 0.30     # take profit only once the stock has moved at least this much our way
EQ_HARD_STOP_PCT = 1.0       # stock this far against the entry
EQ_LOCK_AT_PCT = 1.0         # once this far our way, out if half of the best move is given back


def _near(now: pd.DataFrame, prev: pd.DataFrame, typ: str, keep: set) -> float | None:
    a = now[(now["type"] == typ) & now["strike"].isin(keep)].groupby("strike")["oi"].sum()
    b = prev[(prev["type"] == typ) & prev["strike"].isin(keep)].groupby("strike")["oi"].sum()
    j = pd.concat([a, b], axis=1, keys=["a", "b"]).dropna()
    return float((j["a"].sum() - j["b"].sum()) / j["b"].sum() * 100) if len(j) and j["b"].sum() else None


def check(tr: dict, g: pd.DataFrame, gp: pd.DataFrame | None, hm: str) -> tuple[str | None, str, float | None]:
    """→ (None | "profit" | "stop" | "time", reason, exit premium). Updates best / spot fields in tr."""
    if g is None or g.empty:
        return None, "", None
    ce = tr["side"] == "CE"
    spot = float(g["spot"].iloc[0])
    e = tr["entry"]
    tr["best_spot"] = max(tr.get("best_spot", spot), spot) if ce else min(tr.get("best_spot", spot), spot)
    if tr.get("mode") == "equity":                      # the stock itself: CE = long, PE = short
        d = 1 if ce else -1
        prem = spot
        gain = d * (spot / e - 1) * 100
        tr["best_gain"] = max(tr.get("best_gain", 0.0), gain)
        min_p, hard, lock_at = EQ_MIN_PROFIT_PCT, EQ_HARD_STOP_PCT, EQ_LOCK_AT_PCT
    else:
        o = g[g["ts"] == tr["ts"]]
        if o.empty:
            return None, "", None
        o = o.iloc[0]
        prem = float(o["bid"] or 0) or float(o["ltp"] or 0)
        if prem <= 0:
            return None, "", None
        tr["best_prem"] = max(tr.get("best_prem", e), prem)
        gain = (prem / e - 1) * 100
        tr["best_gain"] = (tr["best_prem"] / e - 1) * 100
        min_p, hard, lock_at = MIN_PROFIT_PCT, HARD_STOP_PCT, LOCK_AT_PCT
    tr["last_prem"], tr["last_spot"] = prem, spot
    if hm >= EXIT_BY:
        return "time", f"{EXIT_BY[:2]}:{EXIT_BY[2:]} square-off", prem
    if (spot < tr["stop"]) if ce else (spot > tr["stop"]):
        return "stop", f"move failed — stock {spot:g} back {'below' if ce else 'above'} {tr['stop']:g}", prem
    if gain <= -hard:
        return "stop", f"{gain:+.1f}% (hard stop −{hard:g}%)", prem
    best_gain = tr["best_gain"]
    locked = best_gain >= lock_at and gain <= best_gain / 2
    if gain < min_p or gp is None or gp.empty:
        if locked:
            return "profit", f"gave back half of the best +{best_gain:.1f}% (now {gain:+.1f}%)", prem
        return None, "", None
    strikes = sorted(g["strike"].unique())
    i = min(range(len(strikes)), key=lambda j: abs(strikes[j] - spot))
    c = _near(g, gp, "CE", set(strikes[max(0, i - 1): i + 4]))
    p = _near(g, gp, "PE", set(strikes[max(0, i - 3): i + 2]))
    signs = []
    if c is not None and p is not None:
        if ce and (c >= FLIP_PCT or p <= -FLIP_PCT):
            signs.append(f"OI turning: calls {c:+.1f}% · puts {p:+.1f}% in 10 min")
        if not ce and (p >= FLIP_PCT or c <= -FLIP_PCT):
            signs.append(f"OI turning: puts {p:+.1f}% · calls {c:+.1f}% in 10 min")
    move = abs(tr["best_spot"] - tr["entry_spot"])
    back = abs(tr["best_spot"] - spot)
    if move > 0 and move / tr["entry_spot"] * 100 >= 0.3 and back / move * 100 >= RETRACE_PCT:
        signs.append(f"bounced {back / move * 100:.0f}% of the move")
    sp = float(gp["spot"].iloc[0])
    if (spot < sp) if ce else (spot > sp):
        signs.append(f"stock turned ({sp:g} → {spot:g} in 10 min)")
    if len(signs) >= 2:
        return "profit", f"take profit {gain:+.1f}% — the move is turning: " + " · ".join(signs), prem
    if locked:
        return "profit", f"gave back half of the best +{best_gain:.1f}% (now {gain:+.1f}%)", prem
    return None, "", None
