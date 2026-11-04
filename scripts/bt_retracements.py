"""
bt_retracements.py — backtest the 6 chandu retracement scenarios with the proposed exits.
Read-only (no orders). Pulls 10-min + daily candles from Kite and uses chandu_bot's own
retracement code for the signals.

Exits (index / stock points; an ATM option moves roughly half):
  1 Bull 20 EMA : stop touch low − 0.1 % (also a close below the 50 EMA);
                  T1 = today's high so far (or 1.5 × risk if already at the high)
  2 Bull 50 EMA : stop touch low − 0.1 %; T1 = the 20 EMA (today's high if the 20 EMA is not
                  ahead), T2 = today's high
  3 Bull CPR    : stop = a close > 0.1 % back inside the CPR, hard stop CPR bottom − 0.1 %;
                  T1 = today's high so far, T2 = R1 (2 × pivot − yesterday's low)
  4–6 Bear      : the mirrors (touch high + 0.1 %, today's low, S1 = 2 × pivot − yesterday's high)
  All           : at T1 book half and move the stop to cost; the rest goes to T2 or exits on a
                  close back past the 20 EMA; anything open is closed at 15:12.
  A stop and a target in the same candle count as the stop (conservative).

Usage (from /root/chandu):
  /root/venv/bin/python scripts/bt_retracements.py --symbol NIFTY --days 120
  /root/venv/bin/python scripts/bt_retracements.py --symbol RELIANCE --days 120 --trades
"""
from __future__ import annotations

import argparse
import datetime
import importlib.util
import pathlib
import sys

import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core import kite_data as _kd  # noqa: E402

_spec = importlib.util.spec_from_file_location("chandu_bot", ROOT / "scripts" / "chandu_bot.py")
cb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cb)

NAMES = {("bull", 20): "1 Bull 20 EMA", ("bull", 50): "2 Bull 50 EMA", ("bull", "cpr"): "3 Bull CPR",
         ("bear", 20): "4 Bear 20 EMA", ("bear", 50): "5 Bear 50 EMA", ("bear", "cpr"): "6 Bear CPR"}


def fetch(symbol: str, interval: str, days: int, block: int) -> pd.DataFrame:
    """Kite caps intraday history per request (10-min: 100 days) — fetch in blocks."""
    end = datetime.datetime.now().replace(microsecond=0, tzinfo=None)
    start = end - datetime.timedelta(days=days)
    parts, a = [], start
    while a < end:
        b = min(a + datetime.timedelta(days=block), end)
        df = _kd.fetch_ohlcv(symbol, interval, from_date=a, to_date=b)
        if df.empty and _kd.LAST_ERROR:
            raise SystemExit(f"Kite: {_kd.LAST_ERROR}")
        parts.append(df)
        a = b + datetime.timedelta(seconds=1)
        cb.time.sleep(0.4)
    out = pd.concat(parts)
    return out[~out.index.duplicated()].sort_index()


def signals(m10, e20, e50, daily):
    sig = []
    for day in sorted(set(m10.index.date)):
        prev = daily[[d.date() < day for d in daily.index]]
        if prev.empty:
            continue
        pv = prev.iloc[-1]
        tc, cc, bc = cb.cpr(pv)
        top, bot = max(tc, cc, bc), min(tc, cc, bc)
        pos = [i for i in range(len(m10)) if m10.index[i].date() == day]
        if not pos or pos[0] < 60:
            continue                                   # EMAs need history first
        a = pos[0]
        seen = {}
        for i in pos:
            act = cb.retracements(m10.iloc[a:i + 1], e20.iloc[a:i + 1], e50.iloc[a:i + 1], top, bot, day)
            for k, v in act.items():
                if seen.get(k) != v["touch"] and v["touch"] == m10.index[i]:
                    sig.append(dict(day=day, i=i, k=k, top=top, bot=bot,
                                    r1=2 * cc - float(pv["Low"]), s1=2 * cc - float(pv["High"])))
                seen[k] = v["touch"]
    return sig


def simulate(m10, e20, e50, sig) -> pd.DataFrame:
    C, H, L = (m10[c].to_numpy(dtype=float) for c in ("Close", "High", "Low"))
    E20, E50 = e20.to_numpy(dtype=float), e50.to_numpy(dtype=float)
    days = m10.index.date
    rows, busy_until = [], {}
    for s in sig:
        i, k = s["i"], s["k"]
        if busy_until.get(k, -1) >= i:
            continue                                   # one trade per scenario at a time
        side = 1 if k[0] == "bull" else -1
        px = C[i]
        today = [q for q in range(max(0, i - 40), i + 1) if days[q] == s["day"]]
        dhi, dlo = max(H[q] for q in today), min(L[q] for q in today)
        close_stop = None
        if k[1] == "cpr":
            hard = s["bot"] * 0.999 if side > 0 else s["top"] * 1.001
            close_stop = s["top"] * 0.999 if side > 0 else s["bot"] * 1.001
        else:
            hard = L[i] * 0.999 if side > 0 else H[i] * 1.001
        R = abs(px - (close_stop if close_stop else hard))
        lvl = dhi if side > 0 else dlo
        if k[1] == 20:
            t1 = lvl if (lvl - px) * side > 0 else px + side * 1.5 * R
            t2 = None
        elif k[1] == 50:
            t1, t2 = E20[i], lvl
            if (t1 - px) * side <= 0:
                t1, t2 = t2, None
        else:
            t1, t2 = lvl, (s["r1"] if side > 0 else s["s1"])
        if (t1 - px) * side <= 0:
            t1 = px + side * 1.5 * R
        half, stop, out, why, q = False, hard, [], "", i
        t1_first = t1
        for q in range(i + 1, len(m10)):
            if days[q] != s["day"]:
                q -= 1
                break
            hm = m10.index[q].strftime("%H:%M")
            hit = (lambda lv: H[q] >= lv) if side > 0 else (lambda lv: L[q] <= lv)
            if (side > 0 and L[q] <= stop) or (side < 0 and H[q] >= stop):
                out.append((stop - px) * side); why = "stop" if not half else "T1+cost"; break
            if close_stop and not half and ((side > 0 and C[q] < close_stop) or (side < 0 and C[q] > close_stop)):
                out.append((C[q] - px) * side); why = "back inside CPR"; break
            if k[1] == 20 and not half and ((side > 0 and C[q] < E50[q]) or (side < 0 and C[q] > E50[q])):
                out.append((C[q] - px) * side); why = "close past 50 EMA"; break
            if not half and hit(t1):
                out.append((t1 - px) * side); half = True; stop = px
                if t2 is not None and (t2 - px) * side <= (t1 - px) * side:
                    t2 = None
            if half:
                if t2 is not None and hit(t2):
                    out.append((t2 - px) * side); why = "T1+T2"; break
                if (side > 0 and C[q] < E20[q]) or (side < 0 and C[q] > E20[q]):
                    out.append((C[q] - px) * side); why = "T1+trail"; break
            if hm >= "15:12":
                out.append((C[q] - px) * side); why = ("T1+" if half else "") + "15:12"; break
        if not out:
            continue
        busy_until[k] = q
        pts = out[0] if len(out) == 1 else (out[0] + out[1]) / 2
        rows.append(dict(k=k, day=s["day"], t=m10.index[i], entry=px, risk=R, pts=pts, why=why,
                         xt=m10.index[q], xp=px + side * out[-1], stop=hard, t1=t1_first, side=side))
    return pd.DataFrame(rows)


COLORS = {("bull", 20): "#2e7d32", ("bull", 50): "#1565c0", ("bull", "cpr"): "#8e24aa",
          ("bear", 20): "#c62828", ("bear", 50): "#ef6c00", ("bear", "cpr"): "#6d4c41"}


def plot(sym: str, m10, e20, e50, daily, T, path: pathlib.Path) -> None:
    """Interactive chart (plotly.js from its CDN — no extra Python package needed)."""
    import json
    ts = [t.strftime("%Y-%m-%d %H:%M") for t in m10.index]
    tr = [dict(type="candlestick", x=ts, open=m10.Open.round(2).tolist(), high=m10.High.round(2).tolist(),
               low=m10.Low.round(2).tolist(), close=m10.Close.round(2).tolist(), name=sym,
               increasing=dict(line=dict(color="#26a69a")), decreasing=dict(line=dict(color="#ef5350"))),
          dict(type="scatter", mode="lines", x=ts, y=e20.round(2).tolist(), name="EMA20",
               line=dict(color="#ffb300", width=1.3)),
          dict(type="scatter", mode="lines", x=ts, y=e50.round(2).tolist(), name="EMA50",
               line=dict(color="#7e57c2", width=1.3))]
    # CPR top / bottom per day (gaps between days)
    cx, ctop, cbot = [], [], []
    for day in sorted(set(m10.index.date)):
        prev = daily[[d.date() < day for d in daily.index]]
        if prev.empty:
            continue
        tc, cc, bc = cb.cpr(prev.iloc[-1])
        dts = [t for t in ts if t.startswith(str(day))]
        cx += [dts[0], dts[-1], None]
        ctop += [max(tc, cc, bc)] * 2 + [None]
        cbot += [min(tc, cc, bc)] * 2 + [None]
    tr += [dict(type="scatter", mode="lines", x=cx, y=ctop, name="CPR top", line=dict(color="#90a4ae", width=1, dash="dot")),
           dict(type="scatter", mode="lines", x=cx, y=cbot, name="CPR bottom", line=dict(color="#90a4ae", width=1, dash="dot"))]
    for k, nm in NAMES.items():
        g = T[T.k == k] if not T.empty else T
        if g.empty:
            continue
        col = COLORS[k]
        txt = [f"{nm}<br>entry {r.entry:,.1f} · stop {r.stop:,.1f} · T1 {r.t1:,.1f}<br>"
               f"exit {r.xp:,.1f} ({r.why}) · <b>{r.pts:+.1f} pts</b>" for r in g.itertuples()]
        tr.append(dict(type="scatter", mode="markers", name=nm, legendgroup=nm, text=txt, hoverinfo="text",
                       x=[t.strftime("%Y-%m-%d %H:%M") for t in g.t], y=g.entry.round(2).tolist(),
                       marker=dict(color=col, size=11, symbol="triangle-up" if k[0] == "bull" else "triangle-down",
                                   line=dict(color="#000", width=1))))
        tr.append(dict(type="scatter", mode="markers", name=nm + " exit", legendgroup=nm, showlegend=False,
                       x=[t.strftime("%Y-%m-%d %H:%M") for t in g.xt], y=g.xp.round(2).tolist(), text=txt,
                       hoverinfo="text", marker=dict(color=["#2e7d32" if v > 0 else "#c62828" for v in g.pts],
                                                     size=9, symbol="x")))
        sx, sy, tx, ty = [], [], [], []
        for r in g.itertuples():
            a, b = r.t.strftime("%Y-%m-%d %H:%M"), r.xt.strftime("%Y-%m-%d %H:%M")
            sx += [a, b, None]; sy += [r.stop, r.stop, None]
            tx += [a, b, None]; ty += [r.t1, r.t1, None]
        tr.append(dict(type="scatter", mode="lines", x=sx, y=sy, legendgroup=nm, showlegend=False, hoverinfo="skip",
                       line=dict(color="#c62828", width=1, dash="dash")))
        tr.append(dict(type="scatter", mode="lines", x=tx, y=ty, legendgroup=nm, showlegend=False, hoverinfo="skip",
                       line=dict(color="#2e7d32", width=1, dash="dash")))
    last = m10.index[-1]
    first = m10.index[m10.index >= last - pd.Timedelta(days=6)][0]
    layout = dict(title=dict(text=f"{sym} 10-min — retracement backtest<br><sup>▲ bull entry · ▼ bear entry · "
                                  f"× exit (green profit / red loss) · red dash = stop · green dash = T1</sup>",
                             x=0.01, y=0.985, yanchor="top", font=dict(size=15)),
                  dragmode="pan", hovermode="closest", height=760, margin=dict(l=45, r=10, t=120, b=30),
                  legend=dict(orientation="h", x=0, y=1.0, yanchor="bottom", font=dict(size=11)),
                  xaxis=dict(rangeslider=dict(visible=True), type="date",
                             range=[first.strftime("%Y-%m-%d %H:%M"), last.strftime("%Y-%m-%d %H:%M")],
                             rangebreaks=[dict(bounds=["sat", "mon"]), dict(bounds=[15.5, 9.25], pattern="hour")]),
                  yaxis=dict(fixedrange=False))
    html = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{sym} retracement backtest</title>
<script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script></head>
<body style="margin:0;font-family:sans-serif"><div id="c"></div>
<p style="padding:6px 10px;font-size:13px">Drag to move, pinch / scroll to zoom, the slider below picks the period.
Tap a ▲ / ▼ / × for the trade. Index points; ATM option ≈ half.</p>
<script>Plotly.newPlot('c',{json.dumps(tr)},{json.dumps(layout)},{{responsive:true,scrollZoom:true}});</script>
</body></html>"""
    path.write_text(html, encoding="utf-8")


def push_file(path: pathlib.Path, title: str) -> None:
    import requests
    topic = cb.env("CHANDU_NTFY_TOPIC", "chandu-logic")
    try:
        r = requests.put(f"https://ntfy.sh/{topic}", data=path.read_bytes(),
                         headers={"Filename": path.name, "Title": title.encode("utf-8")}, timeout=60)
        print(f"  chart sent to ntfy topic {topic}" if r.ok else f"  ntfy upload failed: {r.status_code} {r.text[:120]}")
    except Exception as e:
        print(f"  ntfy upload failed: {e}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="NIFTY")
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--trades", action="store_true", help="also list every trade")
    ap.add_argument("--plot", action="store_true", help="write an interactive chart to data/")
    ap.add_argument("--push", action="store_true", help="with --plot: send the chart file to ntfy")
    a = ap.parse_args()
    sym = a.symbol.upper()
    m10 = fetch(sym, "10minute", a.days + 10, 95)
    m10 = m10.between_time("09:15", "15:29")
    daily = fetch(sym, "day", a.days + 20, 1900)
    e20 = m10["Close"].ewm(span=20, adjust=False).mean()
    e50 = m10["Close"].ewm(span=50, adjust=False).mean()
    sig = signals(m10, e20, e50, daily)
    T = simulate(m10, e20, e50, sig)
    dates = sorted(set(m10.index.date))
    print(f"{sym} 10-min, {len(dates)} days {dates[0]} → {dates[-1]} — {len(sig)} signals\n")
    if T.empty:
        print("no trades")
        return
    print(f"{'scenario':14s} {'trades':>6s} {'won':>5s} {'total':>8s} {'avg':>7s} {'avg risk':>9s}   exits")
    for k, nm in NAMES.items():
        g = T[T.k == k]
        if g.empty:
            print(f"{nm:14s} {0:6d}")
            continue
        w = int((g.pts > 0).sum())
        print(f"{nm:14s} {len(g):6d} {w / len(g) * 100:4.0f}% {g.pts.sum():+8.0f} {g.pts.mean():+7.1f} "
              f"{g.risk.mean():9.1f}   {g.why.value_counts().to_dict()}")
    m = T.assign(month=T.t.dt.strftime("%Y-%m")).pivot_table(index="month", columns="k", values="pts", aggfunc="sum")
    m.columns = [NAMES[c][:13] for c in m.columns]
    print("\nPoints per month:\n" + m.round(0).fillna(0).astype(int).to_string())
    if a.trades:
        print("\n" + T.assign(t=T.t.dt.strftime("%d %b %H:%M"), k=T.k.map(NAMES))[
            ["k", "t", "entry", "risk", "pts", "why"]].round(1).to_string(index=False))
    print("\nIndex points; ATM option ≈ half. No slippage, theta or charges.")
    if a.plot:
        (ROOT / "data").mkdir(exist_ok=True)
        path = ROOT / "data" / f"bt_{sym}_{dates[-1]:%Y%m%d}.html"
        plot(sym, m10, e20, e50, daily, T, path)
        print(f"  chart saved: {path} ({path.stat().st_size // 1024} KB)")
        if a.push:
            push_file(path, f"{sym} retracement backtest chart ({dates[0]:%d %b} → {dates[-1]:%d %b})")


if __name__ == "__main__":
    main()
