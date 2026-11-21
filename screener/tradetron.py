"""
Tradetron Signal Bridge sender for the equity bot — mirrors each stock trade to the broker connected in Tradetron
(Sharekhan). Standard library only.

The bridge (built with the Tradetron MCP) has one variable per stock, named like the NSE symbol with anything but
letters and digits removed (M&M → MM). Entry: <VAR>_qm = 1 (fixed qty), <VAR>_qty = shares, <VAR> = BUY (CE start,
long) or SELL (PE start, short). Exit: <VAR> = BUY EXIT / SELL EXIT. A stock that is not in the bridge's basket
is skipped (the push says so).

.env (or .env.local): TT_AUTO=1 · TT_TOKEN_EQ=<webhook token> · TT_EQ_BASKET=SYM,SYM,… (the basket; default below)
Tradetron's limits: 3 calls / s, 30 / min, 250 / h.
"""

from __future__ import annotations

import json
import re
import time
import urllib.request

WEBHOOK_URL = "https://api.tradetron.tech/api?"
BASKET = ("RELIANCE,HDFCBANK,ICICIBANK,SBIN,INFY,TCS,AXISBANK,KOTAKBANK,BAJFINANCE,TATASTEEL,ITC,LT,BHARTIARTL,"
          "MARUTI,M&M,HINDALCO,JSWSTEEL,TATAPOWER,ADANIENT,ADANIPORTS,SUNPHARMA,HCLTECH,WIPRO,BEL,HAL,ONGC,NTPC,"
          "COALINDIA,BANKBARODA,PNB,VEDL,DLF,TRENT,ETERNAL")
_sent: list[float] = []


def var(sym: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", sym)


def basket(env: dict) -> set:
    return {s.strip().upper() for s in (env.get("TT_EQ_BASKET") or BASKET).split(",") if s.strip()}


def _throttle() -> None:
    now = time.time()
    _sent[:] = [t for t in _sent if now - t < 60]
    recent = [t for t in _sent if now - t < 1]
    if len(recent) >= 3:
        time.sleep(1.05 - (now - recent[0]))
    if len(_sent) >= 30:
        time.sleep(max(0.0, 60.5 - (now - _sent[0])))
    _sent.append(time.time())


def send_pairs(token: str, pairs: list[tuple[str, object]]) -> tuple[bool, str]:
    body = {"auth-token": token}
    for i, (k, v) in enumerate(pairs):
        sfx = "" if i == 0 else str(i)
        body["key" + sfx], body["value" + sfx] = str(k), str(v)
    _throttle()
    req = urllib.request.Request(WEBHOOK_URL, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "tt-signal-bridge/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return 200 <= r.status < 300, f"HTTP {r.status} {r.read().decode(errors='replace')[:100]}"
    except Exception as ex:
        return False, str(ex)[:160]


def signal(env: dict, sym: str, side: str, action: str, qty: int = 0) -> tuple[str | None, str | None]:
    """action 'ENTER' (CE → BUY, PE → SELL with qty shares) or 'EXIT' (BUY EXIT / SELL EXIT).
    → (bridge variable if the signal was sent, line for the push) — (None, None) when the mirror is off."""
    if env.get("TT_AUTO", "0") != "1":
        return None, None
    token = env.get("TT_TOKEN_EQ", "").strip()
    if not token:
        return None, "Tradetron: skipped (no TT_TOKEN_EQ)"
    if sym.upper() not in basket(env):
        return None, f"Tradetron: skipped ({sym} is not in the bridge's basket)"
    v = var(sym)
    long = side == "CE"
    if action == "ENTER":
        if qty <= 0:
            return None, "Tradetron: skipped (qty 0)"
        sig = "BUY" if long else "SELL"
        pairs = [(f"{v}_qm", 1), (f"{v}_qty", int(qty)), (v, sig)]
    else:
        sig = "BUY EXIT" if long else "SELL EXIT"
        pairs = [(v, sig)]
    ok, txt = send_pairs(token, pairs)
    return (v if ok else None), f"Tradetron {v} {sig}{' ' + str(qty) if action == 'ENTER' else ''}: " \
                                 f"{'sent' if ok else 'FAILED'} ({txt})"
