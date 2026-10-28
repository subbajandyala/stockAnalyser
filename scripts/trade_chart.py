"""
Today's chart_bot trades drawn on the NIFTY / SENSEX 3-minute chart.

Each trade is a marker at its entry (▲ CE bought / ▼ PE bought) and at its exit
(green ● won / red ● lost), joined by a line, placed on the index price at those
minutes. Entry and exit times are the real Kite fill times of this machine's
account (FIFO-matched like the journal), so run it on the day — Kite only
returns today's fills.

Writes data/trade_chart_<date>.html (open it in a browser) and
data/trade_chart_<date>.csv (one row per trade with the index levels), and
prints the trade list.

Usage (on the droplet, after 15:30):
  /root/venv/bin/python scripts/trade_chart.py
  /root/venv/bin/python scripts/trade_chart.py --interval minute    # 1-minute candles
"""

import argparse
import datetime
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from kiteconnect import KiteConnect

from scripts.chart_bot import _kite_creds
import scripts.notion_daily_log as nd

_IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
_TOKENS = {"NIFTY": 256265, "SENSEX": 265}
_ROOT = pathlib.Path(__file__).parent.parent


def _candles(kite, symbol: str, day: datetime.date, interval: str) -> pd.DataFrame:
    rows = kite.historical_data(_TOKENS[symbol], datetime.datetime.combine(day, datetime.time(9, 15)),
                                datetime.datetime.combine(day, datetime.time(15, 30)), interval)
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
    return df.set_index("date")


def _level(df: pd.DataFrame, day: datetime.date, hms: str) -> tuple:
    """(timestamp, index close of the candle that holds that time)."""
    if not hms or df.empty:
        return None, None
    t = datetime.datetime.combine(day, datetime.time.fromisoformat(hms))
    upto = df[df.index <= t]
    return t, round(float(upto["close"].iloc[-1]) if len(upto) else float(df["open"].iloc[0]), 1)


def collect(kite, day: datetime.date) -> pd.DataFrame:
    log = nd._load_log(day.isoformat())
    bot = [s for s in log if str(s.get("auto_trade", "")).startswith("placed") and s.get("order_id")]
    matched = nd.match_fills(bot, kite.trades())
    rows = []
    for s in bot:
        m = matched.get(str(s["order_id"]))
        if not m or not m.get("entry_time"):
            continue                     # order never filled
        rows.append({"symbol": s.get("symbol", ""), "side": s.get("direction", ""),
                     "candle": s.get("verdict", ""), "contract": s.get("tradingsymbol", ""),
                     "qty": m["qty"], "entry_time": m["entry_time"], "exit_time": m["exit_time"],
                     "entry": round(m["entry"], 2), "exit": round(m["exit"], 2) if m["exit"] else None,
                     "points": round(m["exit"] - m["entry"], 2) if m["exit"] else None,
                     "pnl": round(m["pnl"]) if m["closed_qty"] else None})
    return pd.DataFrame(rows)


def main() -> None:
    ap = argparse.ArgumentParser(description="Draw today's bot trades on the index chart")
    ap.add_argument("--interval", default="3minute", help="Kite candle interval (default 3minute)")
    args = ap.parse_args()

    day = datetime.datetime.now(_IST).date()
    api_key, token = _kite_creds()
    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(token)

    trades = collect(kite, day)
    if trades.empty:
        sys.exit(f"No filled bot trades today ({day}) on this account")
    symbols = [s for s in ("NIFTY", "SENSEX") if s in set(trades["symbol"])]

    fig = make_subplots(rows=len(symbols), cols=1, shared_xaxes=False, vertical_spacing=0.08,
                        subplot_titles=[f"{s} · {nd.account_name()} · {day:%d %b %Y}" for s in symbols])
    extra = []
    for i, sym in enumerate(symbols, start=1):
        df = _candles(kite, sym, day, args.interval)
        fig.add_trace(go.Candlestick(x=df.index, open=df["open"], high=df["high"], low=df["low"], close=df["close"],
                                     name=sym, showlegend=False,
                                     increasing_line_color="#9aa5b1", decreasing_line_color="#52606d"), row=i, col=1)
        for _, r in trades[trades["symbol"] == sym].iterrows():
            t0, p0 = _level(df, day, r["entry_time"])
            t1, p1 = _level(df, day, r["exit_time"])
            won = (r["points"] or 0) > 0
            col = "#1a7f37" if won else "#cf222e"
            info = (f"{r['side']} · {r['candle']}<br>in {r['entry_time']} ₹{r['entry']} → "
                    f"out {r['exit_time'] or 'open'} ₹{r['exit']}<br>{r['points']:+.2f} pts · ₹{r['pnl']:,}"
                    if r["exit"] else f"{r['side']} · {r['candle']}<br>in {r['entry_time']} ₹{r['entry']} · still open")
            fig.add_trace(go.Scatter(x=[t0], y=[p0], mode="markers", showlegend=False, hovertext=info, hoverinfo="text",
                                     marker=dict(symbol="triangle-up" if r["side"] == "CE" else "triangle-down",
                                                 size=13, color="#0969da" if r["side"] == "CE" else "#8250df",
                                                 line=dict(width=1, color="white"))), row=i, col=1)
            if t1 is not None:
                fig.add_trace(go.Scatter(x=[t0, t1], y=[p0, p1], mode="lines", showlegend=False, hoverinfo="skip",
                                         line=dict(color=col, width=2, dash="dot")), row=i, col=1)
                fig.add_trace(go.Scatter(x=[t1], y=[p1], mode="markers", showlegend=False, hovertext=info,
                                         hoverinfo="text", marker=dict(symbol="circle", size=9, color=col)), row=i, col=1)
            extra.append({"index_at_entry": p0, "index_at_exit": p1,
                          "index_move": round(p1 - p0, 1) if p1 is not None else None, "_k": _})
        fig.update_xaxes(rangeslider_visible=False, row=i, col=1)
    for name, sym_, col in (("CE bought", "triangle-up", "#0969da"), ("PE bought", "triangle-down", "#8250df"),
                            ("Exit, won", "circle", "#1a7f37"), ("Exit, lost", "circle", "#cf222e")):
        fig.add_trace(go.Scatter(x=[None], y=[None], mode="markers", name=name,
                                 marker=dict(symbol=sym_, size=11, color=col)))
    fig.update_layout(height=520 * len(symbols), template="plotly_white", hovermode="closest",
                      legend=dict(orientation="h", y=1.04, x=0), margin=dict(l=50, r=20, t=70, b=30))

    lv = pd.DataFrame(extra).set_index("_k")
    trades = trades.join(lv)
    out = _ROOT / "data"
    out.mkdir(exist_ok=True)
    html, csv = out / f"trade_chart_{day}.html", out / f"trade_chart_{day}.csv"
    fig.write_html(html, include_plotlyjs=True)   # self-contained, opens offline
    trades.to_csv(csv, index=False)

    cols = ["entry_time", "exit_time", "symbol", "side", "candle", "index_at_entry", "index_at_exit",
            "index_move", "entry", "exit", "points", "pnl"]
    print(trades[cols].to_string(index=False))
    print(f"\nChart → {html}\nTable → {csv}")


if __name__ == "__main__":
    main()
