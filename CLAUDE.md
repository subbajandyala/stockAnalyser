# CLAUDE.md — branch `chandu-logic`

A fresh, small trading bot. Only proven plumbing was copied in from the other
branches; the trading logic is written here step by step with the user.

## Rules
- Credentials live ONLY in `/root/stockAnalyser/.env` on the droplet. Never commit
  KITE_* keys, tokens, passwords, TOTP secrets or any other secret.
- The user works from a phone over SSH: give single-line commands.
- Confirm each rule with the user (and check it on data) before coding it.
- No model identifiers in commits.

## Layout
| Path | What | Copied from |
|---|---|---|
| `auto_token.py` | 08:00 cron: Kite login + TOTP → new access token in `.env`, then restarts | ma-cpr-scalp |
| `ticker_daemon.py` | KiteTicker WebSocket → Redis (`tick:{token}`, `candles:1m:{token}`) | ma-cpr-scalp |
| `core/kite_data.py` | Historical OHLCV from Kite (`fetch_ohlcv`) | ma-cpr-scalp `screener/` |
| `core/live_ticks.py` | Reads live ticks / 1m candles from Redis | ma-cpr-scalp `screener/` |
| `core/kite_order.py` | ATM option LIMIT buy + OCO GTT, safe sells (GTT id fix of 2 Nov included) | ma-cpr-scalp `screener/` |
| `scripts/chandu_bot.py` | Step 2: 10-min bullish scan — close above CPR (TC, CC, BC) + close above EMA20 + EMA20 > EMA50 → ntfy topic `chandu-logic` (`CHANDU_NTFY_TOPIC`). `--loop` = after every 10-min close 09:25–15:35, default = one scan | new |
| `symbols.txt` | Symbols to scan (indices + NIFTY 50), edit with nano | new |
| `start_droplet.sh` / `stop_droplet.sh` | Start / stop the live feed and the bot | new |

## .env (droplet)
```
KITE_API_KEY=...  KITE_API_SECRET=...  KITE_ACCESS_TOKEN=...
KITE_USER_ID=...  KITE_PASSWORD=...    KITE_TOTP_SECRET=...
KITE_AUTO_TRADE=0   # 1 = place real orders
NTFY_TOPIC=...
```
Cron: `30 2 * * * cd /root/stockAnalyser && /root/venv/bin/python auto_token.py >> token_refresh.log 2>&1`
