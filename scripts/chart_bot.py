"""
Chart Analysis Signal Bot
=========================
Runs analyze_chart() for each symbol/timeframe and sends a push notification
via ntfy.sh when a tradeable signal fires (STRONG BUY / BUY / STRONG SELL / SELL).

Signal → Option direction:
  STRONG BUY / BUY  → BUY CE
  STRONG SELL / SELL → BUY PE

Multi-timeframe: scans 15m first, confirms with 5m entry timing.
A signal only fires when BOTH timeframes agree in direction.

Set KITE_AUTO_TRADE=1 to enable auto order execution.

Cron (every 5 min, market hours):
  */5 9-15 * * 1-5 cd /root/stockAnalyser && /root/venv/bin/python -u scripts/chart_bot.py --once >> chart_bot.log 2>&1

Single test run:
  /root/venv/bin/python -u scripts/chart_bot.py --once
  /root/venv/bin/python -u scripts/chart_bot.py --test-notify
"""

import argparse
import datetime
import json
import logging
import os
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import requests

from kiteconnect import KiteConnect

from screener.chart_analysis import analyze_chart
from screener.kite_order import (
    has_open_position,
    place_order_for_signal as _place_order,
    position_pnl_pct,
)
from screener.sharekhan_order import place_sk_order as _place_sk_order

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

_IST         = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
MARKET_OPEN  = datetime.time(9, 20)   # give 5 min after open to avoid noise
MARKET_CLOSE = datetime.time(15, 25)  # stop 5 min before close

SYMBOLS    = ["NIFTY", "BANKNIFTY", "SENSEX"]
_TRADE_SYMBOLS = {"NIFTY", "SENSEX"}   # BANKNIFTY excluded from auto-trade

# Primary + confirmation timeframe
PRIMARY_TF = "15m"
CONFIRM_TF = "5m"

# Minimum score to send a notification (BUY=3, STRONG BUY=5)
MIN_NOTIFY_SCORE = 3
# Minimum score to auto-execute an order (4 = at least 4/7 conditions must agree)
MIN_TRADE_SCORE  = 4
# Minimum R:R ratio on the underlying to fire a signal
MIN_RR = 1.5
# Clean signal window: no new signals outside 9:30–15:00 IST
SIGNAL_OPEN  = datetime.time(9, 30)
SIGNAL_CLOSE = datetime.time(15, 0)

def _auto_trade_enabled() -> bool:
    """Check KITE_AUTO_TRADE in env vars first, then .env file."""
    for key in ("CHART_AUTO_TRADE", "KITE_AUTO_TRADE"):
        if os.environ.get(key) == "1":
            return True
    env_path = pathlib.Path(__file__).parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                if k.strip() in ("KITE_AUTO_TRADE", "CHART_AUTO_TRADE"):
                    if v.strip().strip('"').strip("'") == "1":
                        return True
    return False

_AUTO_TRADE = _auto_trade_enabled()
print(f"[chart_bot] AUTO_TRADE={'ON' if _AUTO_TRADE else 'OFF (set KITE_AUTO_TRADE=1 in .env to enable)'}", flush=True)


# ── Credentials ───────────────────────────────────────────────────────────────

def _kite_creds() -> tuple[str, str]:
    """Priority: env vars → .env → ~/.kite_creds.json."""
    api_key = os.environ.get("KITE_API_KEY", "")
    token   = os.environ.get("KITE_ACCESS_TOKEN", "")
    # .env first — auto_token.py always writes the fresh token here
    if not (api_key and token):
        env_path = pathlib.Path(__file__).parent.parent / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    v = v.strip().strip('"').strip("'")
                    if k.strip() == "KITE_API_KEY":
                        api_key = api_key or v
                    elif k.strip() == "KITE_ACCESS_TOKEN":
                        token = token or v
    # ~/.kite_creds.json as fallback
    if not (api_key and token):
        p = pathlib.Path.home() / ".kite_creds.json"
        if p.exists():
            try:
                c = json.loads(p.read_text())
                api_key = api_key or c.get("api_key", "")
                token   = token   or c.get("access_token", "")
            except Exception:
                pass
    return api_key, token


def _ntfy_topic() -> str:
    t = os.environ.get("NTFY_TOPIC", "")
    if not t:
        for src in [
            pathlib.Path.home() / ".kite_creds.json",
            pathlib.Path(__file__).parent.parent / ".env",
        ]:
            if src.exists():
                try:
                    if src.suffix == ".json":
                        t = json.loads(src.read_text()).get("ntfy_agent_topic", "")
                    else:
                        for line in src.read_text().splitlines():
                            if "NTFY_TOPIC" in line and "=" in line:
                                t = line.split("=", 1)[1].strip().strip('"').strip("'")
                except Exception:
                    pass
            if t:
                break
    return t


# ── Notification ──────────────────────────────────────────────────────────────

def send_ntfy(title: str, body: str, topic: str, priority: str = "high") -> bool:
    if not topic:
        return False
    try:
        r = requests.post(
            f"https://ntfy.sh/{topic}",
            data=body.encode("utf-8"),
            headers={
                "Title":        title.encode("utf-8"),
                "Priority":     priority,
                "Tags":         "chart,robot",
                "Content-Type": "text/plain; charset=utf-8",
            },
            timeout=10,
        )
        return r.ok
    except Exception as e:
        print(f"  ntfy error: {e}")
        return False


# ── Signal helpers ────────────────────────────────────────────────────────────

def _verdict_direction(verdict: str) -> str:
    """STRONG BUY/BUY → CE, STRONG SELL/SELL → PE, else WAIT."""
    if "BUY" in verdict:
        return "CE"
    if "SELL" in verdict:
        return "PE"
    return "WAIT"


def _stars(score: int) -> str:
    abs_s = abs(score)
    if abs_s >= 5: return "★★★★★"
    if abs_s >= 4: return "★★★★"
    if abs_s >= 3: return "★★★"
    return "★★"


# ── Alert formatter ───────────────────────────────────────────────────────────

def _fmt_alert(symbol: str, p_res: dict, c_res: dict) -> tuple[str, str]:
    verdict = p_res["verdict"]["verdict"]
    score   = p_res["verdict"]["score"]
    conf    = p_res["verdict"]["confidence"]
    entry   = p_res["verdict"].get("entry_zone")
    sl      = p_res["verdict"].get("stop_loss")
    targets = p_res["verdict"].get("targets", [])
    rr      = p_res["verdict"].get("rr")
    reasons = p_res["verdict"].get("reasons", [])
    summary = p_res["summary"]
    cpr     = p_res.get("cpr", {})
    direction = _verdict_direction(verdict)
    now_ist   = datetime.datetime.now(_IST)
    stars     = _stars(score)

    title = f"Chart Signal: {verdict} {symbol} | {stars} ({conf}%)"

    lines = [
        f"{'🚀' if abs(score) >= 5 else '⚡'} CHART SIGNAL — {symbol}",
        f"Time     : {now_ist.strftime('%H:%M IST')}",
        f"",
        f"Verdict  : {verdict}  {stars}",
        f"Direction: BUY {'CALL (CE)' if direction == 'CE' else 'PUT (PE)'}",
        f"Score    : {score:+d} / 7   Confidence: {conf}%",
        f"",
        f"Spot     : {summary['close']:,.1f}",
        f"Trend    : {summary['trend']}",
        f"Structure: {summary['structure']}",
        f"RSI      : {summary['rsi']}   ADX: {summary['adx']}",
    ]

    if cpr:
        lines.append(f"CPR      : {cpr.get('pos_label', '')}")

    if entry:
        lines.append(f"")
        lines.append(f"Entry    : {entry[0]:,.1f} – {entry[1]:,.1f}")
    if sl:
        lines.append(f"Stop Loss: {sl:,.1f}")
    if targets:
        t_str = " / ".join(f"{t:,.1f}" for t in targets)
        lines.append(f"Targets  : {t_str}")
    if rr:
        lines.append(f"R:R      : {rr}x")

    # Confirmation timeframe summary
    c_verdict = c_res.get("verdict", {}).get("verdict", "WAIT")
    c_score   = c_res.get("verdict", {}).get("score", 0)
    lines += [
        f"",
        f"15m : {verdict} ({score:+d})",
        f"5m  : {c_verdict} ({c_score:+d})",
        f"",
    ]

    ok_reasons  = [r["text"] for r in reasons if r.get("ok") is True][:4]
    bad_reasons = [r["text"] for r in reasons if r.get("ok") is False][:2]
    if ok_reasons:
        lines.append("Signals:")
        for r in ok_reasons:
            lines.append(f"  ✓ {r}")
    if bad_reasons:
        lines.append("Risks:")
        for r in bad_reasons:
            lines.append(f"  ✗ {r}")

    lines += ["", "Verify before executing. Not financial advice."]
    return title, "\n".join(lines)


# ── State management ──────────────────────────────────────────────────────────

def _load_state(path: str) -> dict:
    try:
        return json.loads(pathlib.Path(path).read_text())
    except Exception:
        return {}


def _save_state(path: str, state: dict) -> None:
    pathlib.Path(path).write_text(json.dumps(state, indent=2))


# ── Signal log (for EOD report) ───────────────────────────────────────────────

def _chart_log_path(date_str: str) -> pathlib.Path:
    return pathlib.Path(f"/tmp/chart_bot_log_{date_str}.json")


def _append_chart_log(date_str: str, entry: dict) -> None:
    path = _chart_log_path(date_str)
    try:
        log = json.loads(path.read_text()) if path.exists() else {"date": date_str, "signals": []}
    except Exception:
        log = {"date": date_str, "signals": []}
    # Update existing entry for same symbol+time if re-processed (e.g. trade details added)
    existing = next((s for s in log["signals"] if s.get("_key") == entry.get("_key")), None)
    if existing:
        existing.update(entry)
    else:
        log["signals"].append(entry)
    try:
        path.write_text(json.dumps(log, indent=2))
    except Exception as e:
        print(f"  [log] Failed to write chart log: {e}")


# ── Trailing GTT adjuster ────────────────────────────────────────────────────

_BREAKEVEN_THRESHOLD = 0.40   # 40% premium gain → move SL to entry (breakeven)
_TRAIL1_THRESHOLD    = 0.65   # 65% premium gain → move SL to entry + 20%
_TRAIL1_SL_BUFFER    = 0.20   # locks in 20% of entry premium as profit
_EXCHANGE_MAP = {"NIFTY": "NFO", "SENSEX": "BFO", "BANKNIFTY": "NFO"}


def _adjust_trailing_gtt(
    kite,
    symbol: str,
    bot_ts: str,
    gtt_id: int | str,
    entry_price: float,
    target_price: float,
    qty: int,
    sl_step: int,
    pnl_pct: float,
    state: dict,
    trade_key: str,
    ntfy_topic: str,
) -> None:
    """Cancel old GTT and place a new one with a better SL when trade is profitable.

    sl_step 0→1 : SL moves to breakeven (entry price) once premium is +30%.
    sl_step 1→2 : SL moves to entry+20% (locks profit) once premium is +55%.
    """
    exchange = _EXCHANGE_MAP.get(symbol, "NFO")

    if sl_step < 1 and pnl_pct >= _BREAKEVEN_THRESHOLD:
        new_sl_step = 1
        new_sl      = round(round(entry_price / 0.05) * 0.05, 2)
        step_label  = "BREAKEVEN"
    elif sl_step < 2 and pnl_pct >= _TRAIL1_THRESHOLD:
        new_sl_step = 2
        new_sl      = round(round(entry_price * (1 + _TRAIL1_SL_BUFFER) / 0.05) * 0.05, 2)
        step_label  = "TRAIL +20%"
    else:
        return

    if new_sl >= target_price:
        return  # degenerate — don't adjust

    try:
        ltp_data    = kite.ltp([f"{exchange}:{bot_ts}"])
        current_ltp = float(ltp_data[f"{exchange}:{bot_ts}"]["last_price"])
    except Exception as e:
        _LOG.warning(f"[TRAIL] LTP fetch failed for {bot_ts}: {e}")
        return

    if current_ltp <= new_sl:
        return  # already at/below the new SL — skip

    # Cancel the old GTT
    if gtt_id:
        try:
            kite.delete_gtt(int(gtt_id))
            _LOG.info(f"[TRAIL] Deleted old GTT {gtt_id}")
        except Exception as e:
            _LOG.warning(f"[TRAIL] GTT delete failed ({e}) — placing new GTT anyway")

    # Place new GTT with adjusted SL
    new_gtt_id = None
    try:
        new_gtt_id = kite.place_gtt(
            trigger_type=kite.GTT_TYPE_OCO,
            tradingsymbol=bot_ts,
            exchange=exchange,
            trigger_values=[new_sl, target_price],
            last_price=current_ltp,
            orders=[
                {
                    "transaction_type": kite.TRANSACTION_TYPE_SELL,
                    "quantity":         qty,
                    "product":          kite.PRODUCT_NRML,
                    "order_type":       kite.ORDER_TYPE_LIMIT,
                    "price":            new_sl,
                },
                {
                    "transaction_type": kite.TRANSACTION_TYPE_SELL,
                    "quantity":         qty,
                    "product":          kite.PRODUCT_NRML,
                    "order_type":       kite.ORDER_TYPE_LIMIT,
                    "price":            target_price,
                },
            ],
        )
    except Exception as e:
        _LOG.error(f"[TRAIL] New GTT placement failed: {e}")
        return

    state[f"{trade_key}:gtt_id"]  = new_gtt_id
    state[f"{trade_key}:sl_step"] = new_sl_step

    _LOG.info(
        f"[TRAIL] {symbol} SL → {step_label}: entry={entry_price:.2f}"
        f"  new_sl={new_sl:.2f}  target={target_price:.2f}  pnl={pnl_pct:+.1%}"
    )

    if ntfy_topic:
        send_ntfy(
            f"🛡 Chart Bot: {symbol} SL → {step_label}",
            (
                f"Trade is in profit ({pnl_pct:+.1%}) — SL adjusted.\n"
                f"Contract : {bot_ts}\n"
                f"Entry    : ₹{entry_price:.2f}\n"
                f"New SL   : ₹{new_sl:.2f}\n"
                f"Target   : ₹{target_price:.2f}\n"
                f"GTT ID   : {new_gtt_id}"
            ),
            ntfy_topic,
            priority="default",
        )


# ── Position monitor ─────────────────────────────────────────────────────────

def _monitor_positions(
    symbols: list[str],
    state: dict,
    api_key: str,
    access_token: str,
    ntfy_topic: str,
    now_ist: datetime.datetime,
) -> None:
    """Check every symbol that has a recorded trade today.

    For each open-trade state key:
    - If the Kite position is gone (GTT hit SL or Target) → clear the trade_key
      so the next strong signal can re-enter.
    - If the position is still open AND the current 15m signal has reversed →
      send an ntfy alert prompting the user to review the position.
    """
    today = now_ist.strftime("%Y-%m-%d")

    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)

    for symbol in symbols:
        trade_key = f"{today}:{symbol}:trade"
        order_id  = state.get(trade_key)
        if not order_id:
            continue  # no recorded trade for this symbol today

        # Use the exact contract the bot placed — never touch manual positions
        bot_ts = state.get(f"{trade_key}:ts")  # e.g. "NIFTY2541524950CE"

        # Look up original direction from the alerted state key
        # state[symbol] = "YYYYMMDD:symbol:direction:verdict"
        state_val = state.get(symbol, "")
        parts = state_val.split(":")
        recorded_direction = parts[2] if len(parts) >= 4 else None

        position_open = has_open_position(kite, symbol, only_ts=bot_ts)

        if not position_open:
            print(f"  [MONITOR] {symbol}: position closed (GTT hit)")
            for _k in (trade_key, f"{trade_key}:ts", f"{trade_key}:gtt_id",
                       f"{trade_key}:entry_price", f"{trade_key}:target_price",
                       f"{trade_key}:qty", f"{trade_key}:sl_step"):
                state.pop(_k, None)
            # Clear the alerted key too — prevents falling through to immediate re-entry
            state.pop(symbol, None)
            # Set a 30-min cooldown so the bot doesn't re-enter right away
            state[f"{today}:{symbol}:sl_cooldown"] = now_ist.strftime("%H:%M")
            if ntfy_topic:
                send_ntfy(
                    f"Chart Bot: {symbol} position closed",
                    (
                        f"The GTT was triggered (SL or Target hit).\n"
                        f"30-min cooldown active — next re-entry earliest in 30 min."
                    ),
                    ntfy_topic,
                    priority="default",
                )
            continue

        # Position open — attempt trailing GTT adjustment
        gtt_id       = state.get(f"{trade_key}:gtt_id", "")
        entry_price  = float(state.get(f"{trade_key}:entry_price") or 0)
        target_price = float(state.get(f"{trade_key}:target_price") or 0)
        qty          = int(state.get(f"{trade_key}:qty") or 0)
        sl_step      = int(state.get(f"{trade_key}:sl_step") or 0)

        if gtt_id and entry_price > 0 and target_price > 0 and qty > 0 and bot_ts:
            pnl_pct = position_pnl_pct(kite, symbol)
            if pnl_pct is not None:
                print(f"  [MONITOR] {symbol}: pnl={pnl_pct:+.1%}  sl_step={sl_step}")
                _adjust_trailing_gtt(
                    kite, symbol, bot_ts, gtt_id, entry_price, target_price,
                    qty, sl_step, pnl_pct, state, trade_key, ntfy_topic,
                )

        # Position still open — check if 15m signal has reversed
        if not recorded_direction:
            continue
        try:
            p_res = analyze_chart(symbol, PRIMARY_TF, api_key, access_token)
        except Exception as e:
            print(f"  [MONITOR] {symbol}: chart fetch failed — {e}")
            continue

        if p_res.get("error"):
            continue

        current_verdict   = p_res["verdict"]["verdict"]
        current_direction = _verdict_direction(current_verdict)
        current_score     = p_res["verdict"]["score"]

        if current_direction == "WAIT":
            continue

        if current_direction != recorded_direction:
            print(
                f"  [MONITOR] {symbol}: SIGNAL REVERSED — was {recorded_direction}, "
                f"now {current_verdict} ({current_score:+d}) — alerting"
            )
            if ntfy_topic:
                send_ntfy(
                    f"⚠️ Chart Bot: {symbol} signal REVERSED",
                    (
                        f"You have an open {recorded_direction} position.\n"
                        f"15m chart now shows: {current_verdict} ({current_score:+d})\n\n"
                        f"Consider reviewing / exiting your position."
                    ),
                    ntfy_topic,
                    priority="urgent",
                )


# ── Profit target helpers ─────────────────────────────────────────────────────

def _profit_target_pct() -> float:
    """Read CHART_PROFIT_TARGET_PCT from env / .env. Default 100 (= 100% of opening balance)."""
    v = os.environ.get("CHART_PROFIT_TARGET_PCT", "")
    if not v:
        env_path = pathlib.Path(__file__).parent.parent / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, val = line.split("=", 1)
                    if k.strip() == "CHART_PROFIT_TARGET_PCT":
                        v = val.strip().strip('"').strip("'")
                        break
    try:
        return float(v) if v else 100.0
    except ValueError:
        return 100.0


def _day_pnl(api_key: str, access_token: str) -> float:
    """Return total day P&L (realised + unrealised) across all net positions."""
    try:
        _k = KiteConnect(api_key=api_key)
        _k.set_access_token(access_token)
        net = _k.positions().get("net", [])
        return sum(float(p.get("pnl", 0)) for p in net)
    except Exception as e:
        print(f"  [PNL] Could not fetch day P&L: {e}")
        return 0.0


# ── Single scan ───────────────────────────────────────────────────────────────

def scan_once(symbols: list[str], state_file: str, force: bool = False) -> None:
    api_key, access_token = _kite_creds()
    ntfy_topic = _ntfy_topic()

    if not (api_key and access_token):
        print("ERROR: KITE_API_KEY and KITE_ACCESS_TOKEN required.")
        sys.exit(1)

    now_ist = datetime.datetime.now(_IST)
    print(f"[{now_ist.strftime('%H:%M:%S IST')}] Chart bot scan — {', '.join(symbols)}")

    if not force and now_ist.weekday() >= 5:  # 5=Saturday, 6=Sunday
        print("Weekend — market closed, skipping.")
        return

    if not force and not (MARKET_OPEN <= now_ist.time() <= MARKET_CLOSE):
        print("Market closed — skipping.")
        return

    state = _load_state(state_file)

    today  = now_ist.strftime("%Y-%m-%d")

    # Prune state entries from previous days so the file doesn't grow unbounded.
    # Keep only keys that either have no date prefix (e.g. symbol→state_key which
    # embeds today's date in its value) or whose date prefix matches today.
    stale_keys = [
        k for k in list(state.keys())
        if k[:10].count("-") == 2 and k[:10] < today   # looks like "YYYY-MM-DD:…"
    ]
    if stale_keys:
        for k in stale_keys:
            del state[k]
        _save_state(state_file, state)
        print(f"  [STATE] Pruned {len(stale_keys)} stale entries from previous days")

    # Opening balance snapshot — taken once per day on the first scan.
    # Used only as baseline for the daily profit target; lot sizing uses live balance.
    ob_key = f"{today}:opening_balance"
    if ob_key not in state:
        try:
            _kite = KiteConnect(api_key=api_key)
            _kite.set_access_token(access_token)
            _m  = _kite.margins()
            _eq = _m.get("equity", {})
            _ob = float(_eq.get("available", {}).get("live_balance", 0))
            if _ob > 0:
                state[ob_key] = _ob
                _save_state(state_file, state)
                print(f"  [BALANCE] Opening balance snapshot: ₹{_ob:,.0f}")
        except Exception as _e:
            print(f"  [BALANCE] Could not snapshot opening balance: {_e}")
    opening_balance = float(state.get(ob_key, 0))

    # Monitor existing open positions before scanning for new signals
    _monitor_positions(symbols, state, api_key, access_token, ntfy_topic, now_ist)
    _save_state(state_file, state)  # persist any GTT-hit clearances immediately

    # Daily profit target gate — stop new trades once P&L >= target% of opening balance.
    # Existing GTTs remain active; only new entries are blocked.
    profit_paused_key = f"{today}:profit_target_hit"
    if not state.get(profit_paused_key) and opening_balance > 0:
        target_pct = _profit_target_pct()
        target_amt = opening_balance * target_pct / 100
        day_pnl    = _day_pnl(api_key, access_token)
        print(
            f"  [PROFIT] Day P&L ₹{day_pnl:,.0f} | "
            f"Target ₹{target_amt:,.0f} ({target_pct:.0f}% of ₹{opening_balance:,.0f})"
        )
        if day_pnl >= target_amt:
            state[profit_paused_key] = True
            _save_state(state_file, state)
            print(f"  [PROFIT TARGET] Hit — pausing new trades for today.")
            if ntfy_topic:
                send_ntfy(
                    "🎯 Chart Bot: Daily profit target hit!",
                    (
                        f"Day P&L  : ₹{day_pnl:,.0f}\n"
                        f"Target   : ₹{target_amt:,.0f} ({target_pct:.0f}% of ₹{opening_balance:,.0f})\n"
                        f"Status   : No new trades for the rest of the day.\n"
                        f"Existing GTTs remain active."
                    ),
                    ntfy_topic,
                    priority="high",
                )
            return
    elif state.get(profit_paused_key):
        print(f"  [PROFIT TARGET] Already hit today — skipping new signals.")
        return

    # Time filter: no new signals during opening noise or closing chop
    if not force and not (SIGNAL_OPEN <= now_ist.time() <= SIGNAL_CLOSE):
        print(f"Outside clean signal window ({SIGNAL_OPEN}–{SIGNAL_CLOSE} IST) — skipping new signals.")
        return

    for symbol in symbols:
        try:
            p_res = analyze_chart(symbol, PRIMARY_TF, api_key, access_token)
        except Exception as e:
            print(f"  {symbol} {PRIMARY_TF}: ERROR — {e}")
            continue

        if p_res.get("error"):
            print(f"  {symbol}: {p_res['error']}")
            continue

        verdict   = p_res["verdict"]["verdict"]
        score     = p_res["verdict"]["score"]
        conf      = p_res["verdict"]["confidence"]
        direction = _verdict_direction(verdict)

        marker = "🚀" if abs(score) >= 5 else ("⚡" if abs(score) >= 3 else "  ")
        print(f"  {marker} {symbol:12s} | {PRIMARY_TF} | {verdict:12s} | score {score:+d} | {conf}% conf")

        # R:R check — logged but does NOT block notification; only blocks auto-trade
        rr_val = p_res["verdict"].get("rr")
        _rr_warn = f"R:R {rr_val} < {MIN_RR} (notify-only)" if (rr_val is not None and rr_val < MIN_RR) else None
        if _rr_warn:
            print(f"         {_rr_warn}")

        if direction == "WAIT" or abs(score) < MIN_NOTIFY_SCORE:
            # Log WAIT/weak scans so the EOD Chart Bot sheet is never blank
            today = now_ist.strftime("%Y-%m-%d")
            vdict   = p_res["verdict"]
            entry_z = vdict.get("entry_zone") or []
            targets = vdict.get("targets") or []
            wait_entry: dict = {
                "_key":       f"{today}:{symbol}:{now_ist.strftime('%H%M')}",
                "time":       now_ist.strftime("%H:%M IST"),
                "symbol":     symbol,
                "direction":  direction,
                "verdict":    verdict,
                "score":      score,
                "conf":       conf,
                "spot":       p_res["summary"]["close"],
                "tf_15m":     f"{verdict} ({score:+d})",
                "tf_5m":      "",
                "entry_low":  round(entry_z[0], 1) if len(entry_z) >= 1 else "",
                "entry_high": round(entry_z[1], 1) if len(entry_z) >= 2 else "",
                "stop_loss":  vdict.get("stop_loss", ""),
                "targets":    " / ".join(str(round(t, 1)) for t in targets),
                "rr":         vdict.get("rr", ""),
                "auto_trade": "no signal",
                "tradingsymbol": "", "strike": "", "qty": "",
                "entry_premium": "", "sl_premium": "", "target_premium": "",
                "order_id": "",
            }
            _append_chart_log(today, wait_entry)
            continue

        # Confirm on 5m
        try:
            c_res = analyze_chart(symbol, CONFIRM_TF, api_key, access_token)
        except Exception as e:
            print(f"       5m confirm failed: {e}")
            c_res = {}

        c_verdict   = c_res.get("verdict", {}).get("verdict", "WAIT") if c_res else "WAIT"
        c_direction = _verdict_direction(c_verdict)
        c_score     = c_res.get("verdict", {}).get("score", 0) if c_res else 0

        print(f"         {CONFIRM_TF} confirm: {c_verdict} ({c_score:+d})")

        # 5m confirmation gate: only block if the 5m shows a genuine opposing
        # SIGNAL (score ≥ MIN_NOTIFY_SCORE in the opposite direction).
        # A mild bounce (c_score +1/+2) after a sell-off should NOT block a PE
        # entry — that is a retracement, which is actually a better entry point.
        _ce_blocked = direction == "CE" and c_score <= -MIN_NOTIFY_SCORE
        _pe_blocked = direction == "PE" and c_score >= MIN_NOTIFY_SCORE
        if _ce_blocked or _pe_blocked:
            _block_dir = "CE" if _ce_blocked else "PE"
            print(f"         5m score {c_score:+d} is an actual opposing signal — skipping {_block_dir}")
            vdict2 = p_res["verdict"]
            entry_z2 = vdict2.get("entry_zone") or []
            today2 = now_ist.strftime("%Y-%m-%d")
            _append_chart_log(today2, {
                "_key": f"{today2}:{symbol}:{now_ist.strftime('%H%M')}",
                "time": now_ist.strftime("%H:%M IST"), "symbol": symbol,
                "direction": direction, "verdict": verdict, "score": score, "conf": conf,
                "spot": p_res["summary"]["close"],
                "tf_15m": f"{verdict} ({score:+d})", "tf_5m": f"{c_verdict} ({c_score:+d})",
                "entry_low": round(entry_z2[0], 1) if len(entry_z2) >= 1 else "",
                "entry_high": round(entry_z2[1], 1) if len(entry_z2) >= 2 else "",
                "stop_loss": vdict2.get("stop_loss", ""),
                "targets": " / ".join(str(round(t, 1)) for t in (vdict2.get("targets") or [])),
                "rr": vdict2.get("rr", ""),
                "auto_trade": f"blocked: 5m score {c_score:+d} is actual opposing signal",
                "tradingsymbol": "", "strike": "", "qty": "",
                "entry_premium": "", "sl_premium": "", "target_premium": "", "order_id": "",
            })
            continue

        # Cooldown gate: 30-min wait after an SL or target hit before re-entering.
        today = now_ist.strftime("%Y-%m-%d")
        cooldown_key = f"{today}:{symbol}:sl_cooldown"
        cooldown_str = state.get(cooldown_key)
        if cooldown_str:
            try:
                cd_time = datetime.datetime.strptime(
                    f"{today} {cooldown_str}", "%Y-%m-%d %H:%M"
                ).replace(tzinfo=_IST)
                cd_end = cd_time + datetime.timedelta(minutes=30)
                if now_ist < cd_end:
                    remaining = int((cd_end - now_ist).total_seconds() / 60)
                    print(
                        f"         [{symbol}] SL cooldown active — "
                        f"{remaining}min remaining (re-entry after {cd_end.strftime('%H:%M')})"
                    )
                    continue
                else:
                    state.pop(cooldown_key, None)  # cooldown expired
            except Exception:
                state.pop(cooldown_key, None)

        # Dedup: skip if same date+direction+verdict already alerted AND order was placed.
        # Prefix with today's date so stale state from yesterday never blocks today.
        state_key = f"{today}:{symbol}:{direction}:{verdict}"
        trade_key = f"{today}:{symbol}:trade"
        already_alerted = state.get(symbol) == state_key
        already_traded  = bool(state.get(trade_key))

        if already_alerted and already_traded:
            print(f"         Already alerted+traded today — no change")
            continue
        elif already_alerted:
            print(f"         Already alerted (no trade yet) — will retry order if conditions met")

        # If a trade was placed for this symbol today (even for a different direction),
        # block a new order until _monitor_positions clears the trade_key.
        # This prevents double-entry when the signal flips CE→PE while a position is open.
        if already_traded:
            bot_ts_check = state.get(f"{trade_key}:ts")
            try:
                _chk_kite = KiteConnect(api_key=api_key)
                _chk_kite.set_access_token(access_token)
                _pos_open = has_open_position(_chk_kite, symbol, only_ts=bot_ts_check)
            except Exception:
                _pos_open = True  # assume open on error — safer than double-entering
            if _pos_open:
                print(f"         [{symbol}] Position still open from earlier trade — waiting for GTT")
                continue

        # Format and send notification
        title, message = _fmt_alert(symbol, p_res, c_res)
        print(f"\n{'='*55}\n{message}\n{'='*55}\n")

        if ntfy_topic:
            ok = send_ntfy(title, message, ntfy_topic)
            print(f"  ntfy.sh: {'OK' if ok else 'FAILED'}")
        else:
            print("  No ntfy topic configured.")

        # Build log entry for EOD report
        vdict   = p_res["verdict"]
        targets = vdict.get("targets") or []
        entry_z = vdict.get("entry_zone") or []
        log_entry: dict = {
            "_key":      f"{today}:{symbol}:{now_ist.strftime('%H%M')}",
            "time":      now_ist.strftime("%H:%M IST"),
            "symbol":    symbol,
            "direction": direction,
            "verdict":   verdict,
            "score":     score,
            "conf":      conf,
            "spot":      p_res["summary"]["close"],
            "tf_15m":    f"{verdict} ({score:+d})",
            "tf_5m":     f"{c_verdict} ({c_score:+d})",
            "entry_low":  round(entry_z[0], 1) if len(entry_z) >= 1 else "",
            "entry_high": round(entry_z[1], 1) if len(entry_z) >= 2 else "",
            "stop_loss":  vdict.get("stop_loss", ""),
            "targets":    " / ".join(str(round(t, 1)) for t in targets),
            "rr":         vdict.get("rr", ""),
            "auto_trade": f"skipped: {_rr_warn}" if _rr_warn else "off",
            "tradingsymbol": "",
            "strike": "",
            "qty": "",
            "entry_premium": "",
            "sl_premium": "",
            "target_premium": "",
            "order_id": "",
        }

        # Auto-execute order
        if _AUTO_TRADE and symbol in _TRADE_SYMBOLS and abs(score) >= MIN_TRADE_SCORE:
            # Hard gates — must all pass before placing an order
            adx_val    = p_res["summary"].get("adx", 0)
            cpr_pos    = p_res.get("cpr", {}).get("price_position", "")
            # 5m must confirm direction: CE needs 5m score >= +1, PE needs 5m score <= -1
            c_confirms = (direction == "CE" and c_score >= 1) or (direction == "PE" and c_score <= -1)

            if adx_val < 20:
                _skip = f"ADX {adx_val:.0f} < 20 — ranging market, no trend"
                print(f"  [AUTO-TRADE] SKIPPED — {_skip}")
                log_entry["auto_trade"] = f"skipped: {_skip}"
            elif cpr_pos == "inside":
                _skip = "price inside CPR — indecision/choppy zone"
                print(f"  [AUTO-TRADE] SKIPPED — {_skip}")
                log_entry["auto_trade"] = f"skipped: {_skip}"
            elif not c_confirms:
                _skip = f"5m score {c_score:+d} not confirming {direction} direction"
                print(f"  [AUTO-TRADE] SKIPPED — {_skip}")
                log_entry["auto_trade"] = f"skipped: {_skip}"
            else:
                spot      = p_res["summary"]["close"]
                chart_sl  = p_res["verdict"].get("stop_loss")
                chart_tgt = (p_res["verdict"].get("targets") or [None])[0]
                print(
                    f"  [AUTO-TRADE] {direction} order for {symbol} @ {spot:.0f}"
                    f"  (chart SL={chart_sl}  T={chart_tgt})"
                )
                trade = _place_order(
                    symbol, direction, spot, api_key, access_token,
                    chart_sl=chart_sl, chart_target=chart_tgt,
                )
                if "error" in trade:
                    print(f"  [AUTO-TRADE] SKIPPED — {trade['error']}")
                    log_entry["auto_trade"] = f"skipped: {trade['error']}"
                    if ntfy_topic:
                        send_ntfy(
                            f"Chart Auto-Trade SKIPPED: {symbol}",
                            f"Signal: {verdict}\nReason: {trade['error']}",
                            ntfy_topic,
                            priority="default",
                        )
                else:
                    print(
                        f"  [AUTO-TRADE] ORDER — {trade['tradingsymbol']}"
                        f" | Entry ₹{trade['entry_price']}"
                        f" | SL ₹{trade['sl']}"
                        f" | Target ₹{trade['target']}"
                    )
                    log_entry.update({
                        "auto_trade":    "placed",
                        "tradingsymbol": trade["tradingsymbol"],
                        "strike":        trade.get("strike", ""),
                        "qty":           trade.get("qty", ""),
                        "entry_premium": trade.get("entry_price", ""),
                        "sl_premium":    trade.get("sl", ""),
                        "target_premium":trade.get("target", ""),
                        "order_id":      trade.get("order_id", ""),
                    })
                    if ntfy_topic:
                        send_ntfy(
                            f"Chart ORDER PLACED: {trade['tradingsymbol']}",
                            (
                                f"Symbol  : {symbol} {trade['opt_type']}\n"
                                f"Strike  : {trade['strike']}\n"
                                f"Qty     : {trade['qty']}\n"
                                f"Entry   : ₹{trade['entry_price']}\n"
                                f"SL      : ₹{trade['sl']}\n"
                                f"Target  : ₹{trade['target']}\n"
                                f"Order ID: {trade['order_id']}"
                            ),
                            ntfy_topic,
                        )
                    state[trade_key]                    = trade["order_id"]
                    state[f"{trade_key}:ts"]           = trade["tradingsymbol"]
                    state[f"{trade_key}:gtt_id"]       = trade.get("gtt_id") or ""
                    state[f"{trade_key}:entry_price"]  = trade.get("entry_price", 0)
                    state[f"{trade_key}:target_price"] = trade.get("target", 0)
                    state[f"{trade_key}:qty"]          = trade.get("qty", 0)
                    state[f"{trade_key}:sl_step"]      = 0

                    # Mirror on Sharekhan
                    try:
                        sk_result = _place_sk_order(
                            symbol, direction, spot,
                            option_ltp=trade.get("entry_price", 0),
                            option_sl=trade.get("sl", 0),
                            option_target=trade.get("target", 0),
                        )
                        if "error" in sk_result:
                            print(f"  [SHAREKHAN] SKIPPED — {sk_result['error']}")
                            if ntfy_topic:
                                send_ntfy(
                                    f"Sharekhan SKIPPED: {symbol}",
                                    f"Kite order placed but Sharekhan failed.\nReason: {sk_result['error']}",
                                    ntfy_topic,
                                    priority="default",
                                )
                        else:
                            print(
                                f"  [SHAREKHAN] ORDER — {sk_result['tradingsymbol']}"
                                f" | ID: {sk_result['order_id']}"
                            )
                            log_entry["sk_order_id"]      = sk_result["order_id"]
                            log_entry["sk_tradingsymbol"] = sk_result["tradingsymbol"]
                            if ntfy_topic:
                                send_ntfy(
                                    f"Sharekhan ORDER: {sk_result['tradingsymbol']}",
                                    (
                                        f"Symbol : {symbol} {sk_result['opt_type']}\n"
                                        f"Strike : {sk_result['strike']}\n"
                                        f"Qty    : {sk_result['qty']}\n"
                                        f"Price  : ₹{sk_result['price']}\n"
                                        f"Order  : {sk_result['order_id']}"
                                    ),
                                    ntfy_topic,
                                )
                    except Exception as _sk_err:
                        print(f"  [SHAREKHAN] ERROR — {_sk_err}")

        elif _AUTO_TRADE and symbol not in _TRADE_SYMBOLS:
            log_entry["auto_trade"] = "signal only (no auto-trade for this symbol)"
        elif _AUTO_TRADE and abs(score) < MIN_TRADE_SCORE:
            log_entry["auto_trade"] = f"signal only (score {score:+d} < {MIN_TRADE_SCORE})"

        _append_chart_log(today, log_entry)
        state[symbol] = state_key  # mark as alerted (date-prefixed)

    _save_state(state_file, state)
    print("Done.")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Chart Analysis signal bot")
    ap.add_argument("symbols", nargs="*", default=SYMBOLS)
    ap.add_argument("--once", action="store_true", help="Single scan then exit")
    ap.add_argument("--state-file", default="/root/stockAnalyser/chart_bot_state.json")
    ap.add_argument("--loop",        action="store_true", help="Run as daemon, scanning every --interval seconds")
    ap.add_argument("--interval",    type=int, default=300, help="Loop interval in seconds (default: 300)")
    ap.add_argument("--test-notify", action="store_true", help="Send test notification and exit")
    ap.add_argument("--force", action="store_true", help="Run scan even outside market hours")
    args = ap.parse_args()

    symbols = [s.upper() for s in args.symbols]

    if args.test_notify:
        ntfy_topic = _ntfy_topic()
        now = datetime.datetime.now(_IST).strftime("%H:%M IST")
        title = "Chart Bot — Test Notification"
        body  = (
            f"Chart Analysis Bot is running!\n"
            f"Time     : {now}\n"
            f"Watching : {', '.join(symbols)}\n"
            f"Strategy : 15m primary + 5m confirmation\n"
            f"Signals  : SMC structure + EMA + VWAP + RSI + ADX + OB + CPR\n\n"
            f"You will receive alerts when STRONG BUY or STRONG SELL fires on "
            f"both timeframes during market hours (09:20–15:25 IST)."
        )
        print(body)
        ok = send_ntfy(title, body, ntfy_topic)
        print(f"ntfy.sh: {'OK' if ok else 'FAILED — check NTFY_TOPIC in .env'}")
    elif args.once:
        scan_once(symbols, args.state_file, force=args.force)
    elif args.loop:
        print(f"Chart bot loop started — scanning every {args.interval}s")
        while True:
            try:
                scan_once(symbols, args.state_file, force=args.force)
            except Exception as e:
                print(f"[loop] Unhandled error: {e}")
            time.sleep(args.interval)
    else:
        print("Use --once for cron mode, --loop for daemon mode, or --test-notify.")
        sys.exit(1)
