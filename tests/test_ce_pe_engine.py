"""Tests for screener/ce_pe_engine.py  —  run:  python -m pytest tests -q"""

import copy
import pathlib
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from screener import ce_pe_engine as eng  # noqa: E402


# ── Handcrafted snapshot (modelled on the 29-Oct 14:00 breakout) ──────────────

def _tf(**kw):
    base = {
        "bar_time": "2026-10-29T13:55:00+05:30",
        "open": 22870.0, "high": 22892.0, "low": 22868.0, "close": 22890.0,
        "ema20": 22865.0, "ema50": 22850.0, "ema200": 22790.0, "vwap": 22840.0,
        "rsi14": 64.0, "adx14": 38.0, "adx14_prev": 30.0,
        "di_plus": 32.0, "di_minus": 14.0, "trend_label": "Bullish",
        "last_structure": {"type": "BOS", "direction": "UP", "level": 22878.0,
                           "bars_ago": 0, "time": "2026-10-29T13:55:00+05:30"},
        "recent_events": [],
        "swing_highs": [{"price": 22870.0, "rsi": 60.0, "time": "2026-10-29T11:55:00+05:30"},
                        {"price": 22878.0, "rsi": 62.0, "time": "2026-10-29T12:20:00+05:30"}],
        "swing_lows": [{"price": 22808.0, "rsi": 38.0, "time": "2026-10-29T12:55:00+05:30"},
                       {"price": 22860.0, "rsi": 48.0, "time": "2026-10-29T13:35:00+05:30"}],
        "order_blocks": [{"side": "bullish", "top": 22880.0, "bottom": 22872.0,
                          "time": "2026-10-29T13:45:00+05:30", "broken": False}],
        "fvgs": [],
    }
    base.update(kw)
    return base


def _snap(**kw):
    s = {
        "symbol": "NIFTY",
        "timestamp_ist": "2026-10-29T14:00:10+05:30",
        "tf_5m": _tf(),
        "tf_15m": _tf(rsi14=66.0, trend_label="Bullish"),
        "tf_1H": _tf(trend_label="Mildly Bullish"),
        "tf_1D": _tf(trend_label="Mildly Bearish"),
        "levels": {"tc": 22672.0, "pivot": 22655.0, "bc": 22640.0, "r1": 22990.0,
                   "r2": 23080.0, "r3": None, "s1": 22560.0, "s2": None, "s3": None,
                   "cpr_width_pct": 0.14},
        "session": {"time_ist": "14:00", "bars_since_open": 57, "expiry_day": False},
    }
    s.update(kw)
    return s


def test_strong_ce_on_bos_breakout():
    v, st = eng.evaluate(_snap())
    assert v["action"] == "STRONG_CE", v
    assert all(v["gates"].values())
    assert v["plan"]["entry_type"] == "PULLBACK"          # bullish OB just below price
    assert v["plan"]["stop"] < v["plan"]["entry"] < v["plan"]["target"]
    assert v["plan"]["rr"] >= eng.CFG["min_rr"]
    assert st["active"]["side"] == "CE"
    assert v["plan"]["strike_hint"] == "22900 CE (ATM)"


def test_wick_through_stop_holds_but_close_exits():
    _, st = eng.evaluate(_snap())
    st["active"].update(filled=True)
    s = _snap(timestamp_ist="2026-10-29T14:05:10+05:30")
    s["tf_5m"] = _tf(bar_time="2026-10-29T14:00:00+05:30", low=22840.0, close=22875.0)
    v, st2 = eng.evaluate(s, st)
    assert v["action"] == "HOLD"                        # wick below stop, close above
    s["tf_5m"]["close"] = 22855.0
    v, _ = eng.evaluate(s, st)
    assert v["action"] == "EXIT" and v["exit_price"] == 22855.0


def test_score_is_null_when_a_gate_fails():
    v, _ = eng.evaluate(_snap(tf_15m=_tf(rsi14=78.0)))
    assert v["score"] is None and v["checks"]["CE"]["score"] > 0


def test_g6_fails_when_sitting_on_resistance():
    s = _snap()
    s["levels"]["r1"] = 22900.0                            # 0.04% above close
    v, _ = eng.evaluate(s)
    assert v["action"] == "WAIT"
    assert v["gates"]["G6"] is False


def test_g5_fails_when_15m_overbought():
    s = _snap(tf_15m=_tf(rsi14=78.0))
    v, _ = eng.evaluate(s)
    assert v["action"] == "WAIT" and v["gates"]["G5"] is False


def test_stale_data_is_no_data():
    v, _ = eng.evaluate(_snap(timestamp_ist="2026-10-29T14:20:00+05:30"))
    assert v["action"] == "NO_DATA"


def test_session_gates():
    s = _snap(timestamp_ist="2026-10-29T09:25:00+05:30")
    s["tf_5m"]["bar_time"] = "2026-10-29T09:20:00+05:30"
    assert eng.evaluate(s)[0]["action"] == "NO_TRADE"
    s = _snap(timestamp_ist="2026-10-29T15:05:00+05:30")
    s["tf_5m"]["bar_time"] = "2026-10-29T15:00:00+05:30"
    assert eng.evaluate(s)[0]["action"] == "NO_TRADE"
    s = _snap()
    s["session"]["expiry_day"] = True
    assert eng.evaluate(s)[0]["action"] == "NO_TRADE"


def _mirror(snap):
    """Reflect every price around 22800 → bearish twin of the bullish snapshot."""
    m = copy.deepcopy(snap)
    P = 2 * 22800.0
    def flip(x): return None if x is None else round(P - x, 2)
    for k in ("tf_5m", "tf_15m", "tf_1H", "tf_1D"):
        t = m[k]
        t["open"], t["close"] = flip(t["open"]), flip(t["close"])
        t["high"], t["low"] = flip(t["low"]), flip(t["high"])
        for e in ("ema20", "ema50", "ema200", "vwap"):
            t[e] = flip(t[e])
        t["rsi14"] = 100 - t["rsi14"]
        t["di_plus"], t["di_minus"] = t["di_minus"], t["di_plus"]
        t["trend_label"] = {"Bullish": "Bearish", "Mildly Bullish": "Mildly Bearish",
                            "Mildly Bearish": "Mildly Bullish", "Bearish": "Bullish"}[t["trend_label"]]
        if t["last_structure"]:
            t["last_structure"] = {**t["last_structure"], "direction": "DOWN",
                                   "level": flip(t["last_structure"]["level"])}
        t["swing_highs"], t["swing_lows"] = (
            [{**s, "price": flip(s["price"]), "rsi": 100 - s["rsi"]} for s in t["swing_lows"]],
            [{**s, "price": flip(s["price"]), "rsi": 100 - s["rsi"]} for s in t["swing_highs"]])
        t["order_blocks"] = [{**o, "side": "bearish", "top": flip(o["bottom"]),
                              "bottom": flip(o["top"])} for o in t["order_blocks"]]
    lv = m["levels"]
    m["levels"] = {"tc": flip(lv["bc"]), "pivot": flip(lv["pivot"]), "bc": flip(lv["tc"]),
                   "r1": flip(lv["s1"]), "r2": None, "r3": None,
                   "s1": flip(lv["r1"]), "s2": flip(lv["r2"]), "s3": None,
                   "cpr_width_pct": lv["cpr_width_pct"]}
    return m


def test_pe_is_exact_mirror_of_ce():
    ce, _ = eng.evaluate(_snap())
    pe, _ = eng.evaluate(_mirror(_snap()))
    assert pe["action"] == "STRONG_PE", pe
    assert pe["score"] == ce["score"]
    assert pe["plan"]["rr"] == pytest.approx(ce["plan"]["rr"], rel=0.02)


def test_active_ce_stop_exit_and_no_repeat():
    v, st = eng.evaluate(_snap())
    assert st["active"]
    nxt = _snap(timestamp_ist="2026-10-29T14:05:10+05:30")
    nxt["tf_5m"] = _tf(bar_time="2026-10-29T14:00:00+05:30", low=22850.0, close=22852.0)
    v2, st2 = eng.evaluate(nxt, st)
    assert v2["action"] == "EXIT" and v2["points"] < 0
    # same structure break must not re-fire after the exit
    again = _snap(timestamp_ist="2026-10-29T14:10:10+05:30")
    again["tf_5m"]["bar_time"] = "2026-10-29T14:05:00+05:30"
    v3, _ = eng.evaluate(again, st2)
    assert v3["action"] == "WAIT" and "already signalled" in v3["failed_reason"]


def test_pending_pullback_cancels_on_opposite_choch():
    s = _snap()
    s["tf_5m"]["order_blocks"][0].update(top=22886.0, bottom=22878.0)
    s["tf_5m"]["close"] = 22895.0                           # entry 22886 → pending
    v, st = eng.evaluate(s)
    assert st["active"] and st["active"]["filled"] is False
    nxt = _snap(timestamp_ist="2026-10-29T14:05:10+05:30")
    nxt["tf_5m"] = _tf(bar_time="2026-10-29T14:00:00+05:30", low=22890.0, high=22899.0,
                       recent_events=[{"type": "CHoCH", "direction": "DOWN", "level": 22889.0,
                                       "bars_ago": 0, "time": "2026-10-29T14:00:00+05:30"}])
    v2, st2 = eng.evaluate(nxt, st)
    assert v2["action"] == "EXIT" and v2["points"] is None and v2["unfilled"]
    assert st2["active"] is None


# ── End-to-end replay on synthetic history (+ look-ahead check) ───────────────

def _synthetic_frames(days=45, seed=7):
    rng = np.random.default_rng(seed)
    sessions = pd.bdate_range("2026-08-03", periods=days)
    rows = []
    price = 22000.0
    for d in sessions:
        idx = pd.date_range(f"{d.date()} 09:15", f"{d.date()} 15:29", freq="1min", tz="Asia/Kolkata")
        drift = rng.normal(0, 0.15)
        steps = rng.normal(drift, 3.0, len(idx))
        path = price + np.cumsum(steps)
        price = path[-1]
        for t, p in zip(idx, path):
            rows.append((t, p))
    m1 = pd.DataFrame(rows, columns=["dt", "p"]).set_index("dt")["p"]

    def ohlc(rule, origin="start_day"):
        o = m1.resample(rule, origin=origin, offset="15min" if rule == "60min" else None).ohlc().dropna()
        o.columns = ["Open", "High", "Low", "Close"]
        o["Volume"] = 0
        return o

    daily = m1.groupby(m1.index.date).agg(["first", "max", "min", "last"])
    daily.index = pd.to_datetime(daily.index).tz_localize("Asia/Kolkata")
    daily.columns = ["Open", "High", "Low", "Close"]
    daily["Volume"] = 0
    return {"5m": ohlc("5min"), "15m": ohlc("15min"), "1H": ohlc("60min"), "1D": daily}


def test_replay_runs_and_has_no_lookahead():
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "scripts"))
    import ce_pe_bot  # noqa: E402

    frames = _synthetic_frames()
    day = sorted(set(frames["5m"].index.date))[-2]
    trades = ce_pe_bot.replay("NIFTY", frames, day, day, log_path=None, verbose=False)
    for t in trades:
        assert t["result"] in ("EXIT", "UNFILLED")

    # verdict at a mid-session bar must be identical with and without future candles
    now = pd.Timestamp(f"{day} 12:00", tz="Asia/Kolkata")
    full = eng.evaluate(eng.build_snapshot("NIFTY", eng.analyze_frames("NIFTY", frames, now), now))[0]
    cut = {tf: f[f.index < now] for tf, f in frames.items()}
    past = eng.evaluate(eng.build_snapshot("NIFTY", eng.analyze_frames("NIFTY", cut, now), now))[0]
    assert full == past
