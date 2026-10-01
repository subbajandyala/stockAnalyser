"""
KiteTicker WebSocket daemon — streams live ticks for NIFTY / BANKNIFTY / SENSEX
and writes minute candles to Redis so Streamlit can read them in near-real-time.

Run on the DigitalOcean droplet:
    python ticker_daemon.py

Reads credentials from .env (same file used by the Streamlit app):
    KITE_API_KEY=xxx
    KITE_ACCESS_TOKEN=xxx

Redis keys written:
    tick:256265          → latest NIFTY tick  (JSON, TTL 10 min)
    tick:260105          → latest BANKNIFTY tick
    tick:265             → latest SENSEX tick
    candles:1m:256265    → Redis list of closed 1m candles (newest first, max 750)
    candles:1m:260105
    candles:1m:265
    ticker:status        → JSON health blob updated every minute
"""

import datetime
import json
import os
import signal
import sys
import time
from pathlib import Path

_IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

# Instruments to watch
WATCH_TOKENS = {
    256265: "NIFTY",
    260105: "BANKNIFTY",
    265:    "SENSEX",
}


# ── credentials ───────────────────────────────────────────────────────────────

def _load_creds() -> tuple[str, str]:
    """Read API key + access token from .env then environment."""
    env: dict[str, str] = {}
    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    for k in ("KITE_API_KEY", "KITE_ACCESS_TOKEN"):
        if k in os.environ:
            env[k] = os.environ[k]
    return env.get("KITE_API_KEY", ""), env.get("KITE_ACCESS_TOKEN", "")


# ── daemon ────────────────────────────────────────────────────────────────────

class TickerDaemon:
    def __init__(self, api_key: str, access_token: str):
        import redis
        from kiteconnect import KiteTicker

        self.api_key = api_key
        self.access_token = access_token
        self.redis = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)

        # In-memory candle accumulator: token → {minute, open, high, low, close, volume}
        self._candle: dict[int, dict] = {}
        self._tick_count = 0
        self._start = datetime.datetime.now(_IST)

        self.kws = KiteTicker(api_key, access_token)
        self.kws.on_ticks     = self._on_ticks
        self.kws.on_connect   = self._on_connect
        self.kws.on_close     = self._on_close
        self.kws.on_error     = self._on_error
        self.kws.on_reconnect = self._on_reconnect

    # ── callbacks ─────────────────────────────────────────────────────────────

    def _on_connect(self, ws, response):
        tokens = list(WATCH_TOKENS.keys())
        self.kws.subscribe(tokens)
        self.kws.set_mode(self.kws.MODE_FULL, tokens)
        _log(f"Connected → subscribed {list(WATCH_TOKENS.values())}")

    def _on_ticks(self, ws, ticks):
        for tick in ticks:
            token = tick.get("instrument_token")
            if token not in WATCH_TOKENS:
                continue

            ltp  = float(tick.get("last_price") or 0)
            vol  = int(tick.get("volume_traded") or 0)
            ts   = tick.get("exchange_timestamp")
            if ts is None:
                ts = datetime.datetime.now(_IST)
            elif isinstance(ts, datetime.datetime) and ts.tzinfo is None:
                ts = ts.replace(tzinfo=_IST)

            self._tick_count += 1

            # Latest tick (TTL 10 min — expires if daemon stops)
            self.redis.setex(
                f"tick:{token}", 600,
                json.dumps({
                    "ltp":    ltp,
                    "volume": vol,
                    "ts":     ts.isoformat(),
                    "symbol": WATCH_TOKENS[token],
                }),
            )

            self._accumulate(token, ltp, vol, ts)

        # Update health status every ~60 ticks
        if self._tick_count % 60 == 0:
            self._write_status()

    def _accumulate(self, token: int, ltp: float, vol: int, ts: datetime.datetime):
        minute_key = ts.replace(second=0, microsecond=0)
        c = self._candle.get(token)

        if c is None or c["minute"] != minute_key:
            if c is not None:
                self._flush(token, c)
            self._candle[token] = {
                "minute": minute_key,
                "open": ltp, "high": ltp, "low": ltp, "close": ltp,
                "volume": vol,
            }
        else:
            c["high"]   = max(c["high"], ltp)
            c["low"]    = min(c["low"],  ltp)
            c["close"]  = ltp
            c["volume"] = vol

    def _flush(self, token: int, c: dict):
        key   = f"candles:1m:{token}"
        entry = json.dumps({
            "datetime": c["minute"].isoformat(),
            "Open":  c["open"],
            "High":  c["high"],
            "Low":   c["low"],
            "Close": c["close"],
            "Volume": c["volume"],
        })
        self.redis.lpush(key, entry)
        self.redis.ltrim(key, 0, 749)   # keep 750 candles (12.5 hours of 1m data)
        self.redis.expire(key, 86400)   # auto-expire after 24h

    def _write_status(self):
        self.redis.setex(
            "ticker:status", 120,
            json.dumps({
                "running":    True,
                "ticks":      self._tick_count,
                "started":    self._start.isoformat(),
                "last_ping":  datetime.datetime.now(_IST).isoformat(),
                "symbols":    list(WATCH_TOKENS.values()),
            }),
        )

    # ── connection events ──────────────────────────────────────────────────────

    def _on_close(self, ws, code, reason):
        _log(f"Disconnected: {code} {reason}")

    def _on_error(self, ws, code, reason):
        _log(f"Error: {code} {reason}")

    def _on_reconnect(self, ws, attempts):
        _log(f"Reconnecting (attempt {attempts})…")

    def run(self):
        _log("Starting KiteTicker…")
        self.kws.connect(threaded=False)


# ── helpers ───────────────────────────────────────────────────────────────────

def _log(msg: str):
    ts = datetime.datetime.now(_IST).strftime("%Y-%m-%d %H:%M:%S IST")
    print(f"[{ts}] {msg}", flush=True)


def main():
    api_key, access_token = _load_creds()
    if not api_key or not access_token:
        print("ERROR: Set KITE_API_KEY and KITE_ACCESS_TOKEN in .env or environment.")
        sys.exit(1)

    daemon = TickerDaemon(api_key, access_token)

    def _shutdown(sig, frame):
        _log("Shutting down…")
        try:
            daemon.kws.close()
        except Exception:
            pass
        sys.exit(0)

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    while True:
        try:
            daemon.run()
        except Exception as e:
            _log(f"Crash: {e}. Restarting in 5s…")
            time.sleep(5)


if __name__ == "__main__":
    main()
