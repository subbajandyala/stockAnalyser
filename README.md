# CLAUDE.md — branch `srikanth-trendlyne-logic`

Srikanth's Trendlyne screener rebuilt on Kite daily data. Fresh branch: only proven
plumbing (from `chandu-logic`'s clean base) plus one screener script.

## Rules
- Credentials live ONLY in `.env` on the droplet (here a symlink to
  `/root/stockAnalyser/.env`). Never commit keys, tokens, passwords or TOTP secrets.
- The user works from a phone over SSH: give single-line commands.
- Confirm rules with the user before coding them. No model identifiers in commits.

## The screener — `scripts/trendlyne_screen.py`
Nifty 500 (NSE list, cached a week in `data/nifty500.csv`), daily candles from Kite:
- price within ±2 % of the daily 20 EMA
- price 0–5 % below the 1-year high
- price above the daily 50 EMA, 20 EMA above 50 EMA
- market cap > ₹1,000 cr → met by the Nifty 500 universe
- Trendlyne Momentum Score > 60 → OUR OWN 0–100 score > 60 (`TRENDLYNE_MIN_SCORE`):
  25 1-month + 25 3-month + 20 6-month return percentile rank across the Nifty 500,
  15 RSI(14) (40 → 0, 70+ → full), 15 nearness to the 1-year high (0 % → full, 20 % → 0)

Output: ntfy topic `srikanth-trendlyne` (`TRENDLYNE_NTFY_TOPIC`) and
`data/trendlyne_<date>_<time>.csv` with every stock. Run time / schedule: not set yet.

## Other files
`auto_token.py`, `ticker_daemon.py`, `core/` (Kite data, live ticks, orders) — copied
plumbing, not used by the screener except `core/kite_data.py`.
