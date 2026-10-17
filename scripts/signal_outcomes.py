"""
Signal outcome tracker — what the market did after every chart_bot signal.

For each logged CE/PE signal (live and shadow logs), using 1-minute spot candles:
  MFE / MAE at 15, 30 and 60 minutes (points)  — best / worst move after entry
  first_hit  — whether the signal's spot target or stop loss was touched first
  eod_move   — move from entry to 15:25 in the signal's direction
  mfe60_r    — 60-min MFE as a multiple of the signal's risk (entry→SL distance)

Rows are appended (deduplicated) to data/signal_outcomes.csv so evidence builds
up over weeks; --report summarises it by score, symbol, hour, noise state, etc.

Cron (after close):
  45 15 * * 1-5 cd /root/stockAnalyser && /root/venv/bin/python scripts/signal_outcomes.py >> outcomes.log 2>&1

Usage:
  python scripts/signal_outcomes.py                 # process today
  python scripts/signal_outcomes.py --date 2026-10-16
  python scripts/signal_outcomes.py --report        # summary of everything collected
"""

import argparse
import datetime
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import pandas as pd
import requests

from screener.chart_analysis import _INDEX_TOKENS, _KITE_BASE, _hdrs

_ROOT = pathlib.Path(__file__).parent.parent
_CSV  = _ROOT / "data" / "signal_outcomes.csv"
_IST  = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
HORIZONS = (15, 30, 60)
SESSION_END = "15:25"


def _creds() -> tuple[str, str]:
    from scripts.chart_bot import _kite_creds
    return _kite_creds()


def _load_logs(date_str: str) -> list[dict]:
    rows = []
    for source, prefix in (("live", "chart_bot_log"), ("shadow", "chart_bot_shadow_log")):
        p = pathlib.Path(f"/tmp/{prefix}_{date_str}.json")
        if not p.exists():
            continue
        try:
            for s in json.loads(p.read_text()).get("signals", []):
                if s.get("direction") in ("CE", "PE"):
                    rows.append({**s, "source": source, "date": date_str})
        except Exception as e:
            print(f"  could not read {p}: {e}")
    return rows


def _fetch_1m(symbol: str, date_str: str, api_key: str, access_token: str) -> pd.DataFrame:
    token = _INDEX_TOKENS[symbol]
    r = requests.get(
        f"{_KITE_BASE}/instruments/historical/{token}/minute",
        headers=_hdrs(api_key, access_token),
        params={"from": f"{date_str} 09:15:00", "to": f"{date_str} 15:30:00", "continuous": 0, "oi": 0},
        timeout=30,
    )
    r.raise_for_status()
    candles = r.json().get("data", {}).get("candles", [])
    df = pd.DataFrame(candles, columns=["datetime", "Open", "High", "Low", "Close", "Volume"])
    df["datetime"] = pd.to_datetime(df["datetime"])
    if df["datetime"].dt.tz is not None:
        df["datetime"] = df["datetime"].dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    return df.set_index("datetime").sort_index()


def _first_target(s: dict) -> float | None:
    t = str(s.get("targets", "") or "").split("/")[0].strip()
    try:
        return float(t) if t else None
    except ValueError:
        return None


def evaluate(signal: dict, bars: pd.DataFrame) -> dict | None:
    """Pure calculation: outcome metrics for one signal given that day's 1m bars."""
    hhmm = str(signal.get("time", "")).split()[0]
    if not hhmm or bars.empty:
        return None
    t0 = pd.Timestamp(f"{signal['date']} {hhmm}")
    after = bars[bars.index >= t0]
    after = after[after.index.strftime("%H:%M") <= SESSION_END]
    if after.empty:
        return None
    d     = 1 if signal["direction"] == "CE" else -1
    entry = float(after["Open"].iloc[0])

    out = {"entry_spot": round(entry, 2)}
    for h in HORIZONS:
        w = after[after.index < t0 + pd.Timedelta(minutes=h)]
        fav = (w["High"].max() - entry) if d > 0 else (entry - w["Low"].min())
        adv = (entry - w["Low"].min()) if d > 0 else (w["High"].max() - entry)
        out[f"mfe{h}"] = round(float(fav), 1)
        out[f"mae{h}"] = round(float(adv), 1)
    out["eod_move"] = round(float((after["Close"].iloc[-1] - entry) * d), 1)

    sl  = signal.get("stop_loss")
    tgt = _first_target(signal)
    try:
        sl = float(sl) if sl not in ("", None) else None
    except ValueError:
        sl = None
    valid_sl  = sl is not None and (sl - entry) * d < 0
    valid_tgt = tgt is not None and (tgt - entry) * d > 0
    hit = "none"
    for _, b in after.iterrows():
        sl_hit  = valid_sl and ((b["Low"] <= sl) if d > 0 else (b["High"] >= sl))
        tgt_hit = valid_tgt and ((b["High"] >= tgt) if d > 0 else (b["Low"] <= tgt))
        if sl_hit:          # same-bar ties count as a loss (conservative)
            hit = "sl"
            break
        if tgt_hit:
            hit = "target"
            break
    out["first_hit"] = hit
    out["risk_pts"]  = round(abs(entry - sl), 1) if valid_sl else ""
    out["mfe60_r"]   = round(out["mfe60"] / abs(entry - sl), 2) if valid_sl and abs(entry - sl) > 0 else ""
    return out


def process(date_str: str) -> int:
    signals = _load_logs(date_str)
    if not signals:
        print(f"No CE/PE signals logged for {date_str}.")
        return 0
    api_key, access_token = _creds()
    bars = {}
    rows = []
    for s in signals:
        sym = s.get("symbol")
        if sym not in _INDEX_TOKENS:
            continue
        if sym not in bars:
            try:
                bars[sym] = _fetch_1m(sym, date_str, api_key, access_token)
            except Exception as e:
                print(f"  {sym}: 1m fetch failed — {e}")
                bars[sym] = pd.DataFrame()
        res = evaluate(s, bars[sym])
        if not res:
            continue
        g = s.get("groups") or {}
        rows.append({
            "key": f"{s['source']}:{s.get('_key', '')}", "date": date_str, "time": s.get("time", ""),
            "source": s["source"], "symbol": sym, "direction": s["direction"], "verdict": s.get("verdict", ""),
            "score": s.get("score", ""), "tf_5m": s.get("tf_5m", ""), "adx": s.get("adx", ""),
            "noise": s.get("noise", ""),
            "g_trend": g.get("trend", ""), "g_momentum": g.get("momentum", ""),
            "g_location": g.get("location", ""), "g_flow": g.get("flow", ""),
            "auto_trade": s.get("auto_trade", ""), "stop_loss": s.get("stop_loss", ""),
            "target1": _first_target(s) or "", **res,
        })
    if not rows:
        print("Nothing to evaluate.")
        return 0

    new = pd.DataFrame(rows)
    _CSV.parent.mkdir(parents=True, exist_ok=True)
    if _CSV.exists():
        old = pd.read_csv(_CSV, dtype=str)
        new = pd.concat([old[~old["key"].isin(new["key"])], new.astype(str)], ignore_index=True)
    new.to_csv(_CSV, index=False)
    print(f"{len(rows)} signal outcomes for {date_str} saved → {_CSV}")
    return len(rows)


def report() -> None:
    if not _CSV.exists():
        print(f"No data yet at {_CSV}. Run this script after market close for a few days.")
        return
    df = pd.read_csv(_CSV)
    df = df[df["first_hit"].notna()]
    df["score_abs"] = pd.to_numeric(df["score"], errors="coerce").abs()
    df["hour"] = df["time"].astype(str).str[:2]
    df["placed"] = df["auto_trade"].astype(str).str.startswith(("placed", "shadow"))

    def summary(g: pd.DataFrame) -> pd.Series:
        n = len(g)
        decided = g[g["first_hit"].isin(["target", "sl"])]
        return pd.Series({
            "signals":    n,
            "target_1st": f"{(decided['first_hit'] == 'target').mean() * 100:.0f}%" if len(decided) else "—",
            "sl_1st":     f"{(decided['first_hit'] == 'sl').mean() * 100:.0f}%" if len(decided) else "—",
            "mfe30":      round(g["mfe30"].mean(), 1),
            "mae30":      round(g["mae30"].mean(), 1),
            "eod_move":   round(g["eod_move"].mean(), 1),
            "mfe60_R":    round(pd.to_numeric(g["mfe60_r"], errors="coerce").mean(), 2),
        })

    print(f"\n{len(df)} signals from {df['date'].min()} to {df['date'].max()}\n")
    for col, title in (("score_abs", "By |score|"), ("symbol", "By symbol"), ("direction", "By direction"),
                       ("hour", "By hour"), ("noise", "By noise state"), ("placed", "Trade placed (live/shadow)?"),
                       ("source", "By source")):
        if col in df and df[col].notna().any():
            print(f"── {title} ──")
            print(df.groupby(col, dropna=False).apply(summary, include_groups=False).to_string())
            print()
    print("mfe/mae/eod in index points (favourable direction positive). "
          "target_1st/sl_1st exclude signals where neither level was touched.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Measure what happened after chart_bot signals")
    ap.add_argument("--date", default=datetime.datetime.now(_IST).strftime("%Y-%m-%d"))
    ap.add_argument("--report", action="store_true", help="Print summary of all collected outcomes")
    args = ap.parse_args()
    if args.report:
        report()
    else:
        process(args.date)
        report()
