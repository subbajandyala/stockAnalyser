"""
Tradetron Signal Bridge sender — mirrors the index bot's trades to Sharekhan (or any broker connected in
Tradetron). Standard library only.

The Tradetron template (Signal Bridge, built with the Tradetron MCP) has one variable per basket item:
NIFTY50_CE_ATM, NIFTY50_PE_ATM, SENSEX_CE_ATM, SENSEX_PE_ATM. Sending  <item>_qty = units, <item> = BUY
makes Tradetron buy that ATM option (strike pinned at that moment); <item> = BUY EXIT sells that contract.

.env:  TT_AUTO=1 · TT_TOKEN_INDEX=<webhook token of the template> · TT_LOTS=1 (lots per Sharekhan trade)
Tradetron's limits: 3 calls / s, 30 / min, 250 / h — the bot sends at most a few a day.
"""

from __future__ import annotations

import json
import urllib.request

WEBHOOK_URL = "https://api.tradetron.tech/api?"
ITEM = {"NIFTY": "NIFTY50", "SENSEX": "SENSEX"}       # index bot symbol → basket prefix
LOT = {"NIFTY": 65, "SENSEX": 20}


def item(sym: str, side: str) -> str | None:
    p = ITEM.get(sym)
    return f"{p}_{side}_ATM" if p else None


def send_pairs(token: str, pairs: list[tuple[str, object]]) -> tuple[bool, str]:
    body = {"auth-token": token}
    for i, (k, v) in enumerate(pairs):
        sfx = "" if i == 0 else str(i)
        body["key" + sfx], body["value" + sfx] = str(k), str(v)
    req = urllib.request.Request(WEBHOOK_URL, data=json.dumps(body).encode(), method="POST",
                                 # a User-Agent is required: the default Python one is refused (403)
                                 headers={"Content-Type": "application/json", "User-Agent": "tt-signal-bridge/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return 200 <= r.status < 300, f"HTTP {r.status} {r.read().decode(errors='replace')[:120]}"
    except Exception as ex:
        return False, str(ex)[:160]


def signal(env: dict, sym: str, side: str, action: str, lots: int | None = None) -> str | None:
    """Send BUY / BUY EXIT for the index trade. → a line for the push, or None when the mirror is off / n/a."""
    if env.get("TT_AUTO", "0") != "1":
        return None
    token = env.get("TT_TOKEN_INDEX", "").strip()
    it = item(sym, side)
    if not token or not it:
        return f"Tradetron: skipped ({'no TT_TOKEN_INDEX' if not token else sym + ' not in the bridge'})"
    pairs: list[tuple[str, object]] = []
    if action == "BUY":
        n = max(1, int(lots or env.get("TT_LOTS", "1") or 1))
        pairs += [(f"{it}_qm", 1), (f"{it}_qty", n * LOT[sym])]
    pairs.append((it, action))
    ok, txt = send_pairs(token, pairs)
    return f"Tradetron {it} {action}: {'sent' if ok else 'FAILED'} ({txt})"
