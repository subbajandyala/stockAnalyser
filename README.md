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
| `scripts/chandu_bot.py` | Step 2: 10-min scan — bullish: close above CPR (TC, CC, BC) + close above EMA20 + EMA20 > EMA50; bearish (step 3): the mirror (below CPR, below EMA20, EMA20 < EMA50); retracements (step 4): bull/bear × EMA20/EMA50 — the touch candle itself is the signal (no confirmation candle): low within 0.1 % of the EMA or under it, close above or at par (≤ 0.1 % below), above/below the CPR, listed in every push while valid, 09:15 candle never a touch (`CHANDU_FIRST_TOUCH=1` allows it); all in one push → ntfy topic `chandu-logic` (`CHANDU_NTFY_TOPIC`). `--cron` = one scan, only weekdays 09:25–15:35 IST (droplet cron every 10 min, see below); `--loop` = same as a long-running process; default = one scan | new |
| `symbols.txt` | Symbols to scan (indices + NIFTY 50), edit with nano | new |
| `start_droplet.sh` / `stop_droplet.sh` | Start / stop the live feed and the bot | new |

## .env (droplet)
```
KITE_API_KEY=...  KITE_API_SECRET=...  KITE_ACCESS_TOKEN=...
KITE_USER_ID=...  KITE_PASSWORD=...    KITE_TOTP_SECRET=...
KITE_AUTO_TRADE=0   # 1 = place real orders
NTFY_TOPIC=...
```
Cron (droplet clock is UTC; IST = UTC + 5:30):
`5-55/10 3-10 * * 1-5 cd /root/chandu && /root/venv/bin/python scripts/chandu_bot.py --cron >> chandu_bot.log 2>&1` (IST :25 :35 … :15; the script keeps 09:25–15:35)
Token: `30 2 * * * cd /root/stockAnalyser && /root/venv/bin/python auto_token.py >> token_refresh.log 2>&1`
