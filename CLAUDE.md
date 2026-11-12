# CLAUDE.md — branch `indexoptions-oi-marketstructure`

(Started from `praveen-new-changes`; praveen_bot is still here unchanged.)

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
| `core/pnc_strategy.py` | Strategy maths: CPR / R1 / S1 / PDH / PDL, Supertrend, VWAP, ADX, IV / delta, breakout-retest, OI chain read, 100-pt score, mandatory filters | new |
| `scripts/praveen_bot.py` | Daemon: scans NIFTY + SENSEX 20 s after every 5-min close, enters on score ≥ 80, manages exits | new |
| `pnc_events.txt` | Event dates — no new trades | new |
| `core/structure.py` | Swings, HH-HL / LH-LL trend, change of character, base-candle zones (RBR / DBR / RBD / DBD) | new |
| `core/ios_strategy.py` | Setups A (pullback into fresh zone) / B (breakout + retest), structure stop, target = next obstacle (≥ 1.5R), OI check, trade manager | new |
| `scripts/index_bot.py` | Daemon: NIFTY / BANKNIFTY / SENSEX every 5 min — structure + option OI → alert / order, manages exits | new |
| `scripts/ios_backtest.py` | Structure-only backtest on Kite history (no OI history exists for expired contracts) | new |
| `start_droplet.sh` / `stop_droplet.sh` | Start / stop the live feed and the bot | new |

## .env (droplet)
```
KITE_API_KEY=...  KITE_API_SECRET=...  KITE_ACCESS_TOKEN=...
KITE_USER_ID=...  KITE_PASSWORD=...    KITE_TOTP_SECRET=...
KITE_AUTO_TRADE=0   # 1 = place real orders
NTFY_TOPIC=...
PNC_AUTO_TRADE=0                          # 1 = praveen_bot places real orders (separate from KITE_AUTO_TRADE)
PNC_NTFY_TOPIC=praveen-intraday-options
PNC_MAX_TRADES=3   PNC_MAX_LOSSES=2   PNC_PUSH_SKIPS=1
```

## praveen_bot rules (confirmed with the user)
- NIFTY + SENSEX, ATM option, nearest weekly (next weekly on expiry day), 1 lot, NRML LIMIT + OCO GTT.
- Entry: 5m breakout of CPR top / R1 / PDH (PE: CPR bottom / S1 / PDL), retest within 12 candles,
  score ≥ 80, 09:25–14:30, 15m Supertrend with the trade, CPR not wide, risk NIFTY 8–40 / SENSEX
  25–130 pts, room ≥ 1.5R, ATM spread ≤ 1.5 %, max 3 trades / 2 losses a day, no event days.
- Stop = retest candle low / high (index). T1 = entry ± 1.5R (index) — whole lot booked there.
  GTT: option stop clamped 20–25 % below entry, target ≥ +15 %.
- Exits by the bot: 5m close beyond the stop, T1, 5m Supertrend flip, 2 futures closes on the
  wrong side of VWAP, 15:15 square-off. GTT fired → detected and closed in state.
- Data: Kite only. State / chain snapshots in `data/pnc/<date>/`.
Cron: `30 2 * * * cd /root/stockAnalyser && /root/venv/bin/python auto_token.py >> token_refresh.log 2>&1`

## index_bot (indexoptions-oi-marketstructure) — defaults chosen 12 Nov, alerts only
- Bias: 15-min structure. Setups on the last closed 5-min candle: A pullback into a fresh demand /
  supply zone in the 15-min trend (rejection candle); B breakout of 5-min swing / PDH-PDL / opening
  range, retest + rejection. Entry 09:30–14:30.
- Stop: beyond the zone / retest candle + buffer (NIFTY 10, BANKNIFTY 30, SENSEX 40). Risk NIFTY
  10–45, BANKNIFTY 30–130, SENSEX 35–150 pts. Target: nearest obstacle (supply/demand zone, PDH/R1 or
  PDL/S1, swing, OI wall), ≥ 1.5R, else skip; none ahead → 2R.
- OI (last ~15 min, near ATM): losing side covering · winning side adding · futures buildup · PCR
  moving — need 2 of 4 and no "OI against".
- Manage: +1R → stop to entry · T1 → all out (1 lot) or half + trail under 5-min swings (2 lots) ·
  5-min change of character → out · OI against → out · 15:15 out.
- .env: IOS_AUTO_TRADE=0 · IOS_NTFY_TOPIC=index-oi-structure · IOS_LOTS=1 · IOS_SYMBOLS=NIFTY,BANKNIFTY,SENSEX
  · IOS_MAX_TRADES=3 · IOS_MAX_LOSSES=2 · IOS_OI_NEED=2 · IOS_PUSH_SKIPS=1. State in data/ios/<date>/.
