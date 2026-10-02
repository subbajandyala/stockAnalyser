# MarketPulse

A self-hosted NSE/BSE trading dashboard and signal bot built on **Kite Connect** and **Streamlit**.

Live OHLCV data, multi-strategy stock screeners, option chain analysis, and automated signal notifications — all running on a single DigitalOcean droplet with zero daily manual steps.

---

## Features

- **Live tick feed** — KiteTicker WebSocket streams NIFTY, BANKNIFTY, SENSEX ticks into Redis
- **10+ screeners** — MA crossover, MA retracement, Elder Ray, RPCI, open=low, late session, fundamental, and more
- **Option chain** — live NSE option chain with OI, volume, and PCR (local only)
- **Agent Flow** — multi-timeframe OI + momentum signal engine → CALL / PUT / NO TRADE
- **Push notifications** — ntfy.sh push to phone on strong signals (3★+)
- **Auto-orders** — ATM LIMIT BUY + GTT OCO placed in Kite on confirmed signals
- **Fully automated** — daily Kite token refresh via TOTP, no manual steps after initial setup

---

## Stack

| Layer | Technology |
|---|---|
| Data | Kite Connect REST + WebSocket |
| Tick store | Redis |
| Dashboard | Streamlit |
| Charts | Plotly |
| Notifications | ntfy.sh |
| Hosting | DigitalOcean droplet |
| Token automation | pyotp + cron |

---

## Screeners

| Screener | Signal |
|---|---|
| MA Crossover | EMA20 crossing above EMA50 (last 5 days) |
| MA Retracement | Pullback to 20 EMA in an uptrend with CPR confluence |
| MA50 Support | Bounce off 50 EMA in EMA20 > EMA50 > EMA200 alignment |
| CPR Retracement | Price retesting CPR levels |
| Fundamental | Stocks >20% below 52-week high (buy-the-dip quality filter) |
| RPCI | 11-condition score: valuation, momentum, stage, Dow Theory, and more |
| Agent Flow | Multi-timeframe OI + momentum → institutional CALL/PUT signal |
| Elder Ray | Bull/Bear power across 1m, 5m, 15m timeframes |
| Open = Low | NIFTY 100 intraday setup — open equals low (momentum signal) |
| Late Session | Last-hour direction signal for NIFTY / BANKNIFTY / SENSEX |
| Smart Alerts | Smart money flow detection |
| Option Chain | Live NSE option chain with OI walls and max pain |

---

## Architecture

```
Kite WebSocket
    └─ ticker_daemon.py  (subprocess supervisor)
           └─ Redis: tick:{token}, candles:1m:{token}
                  └─ app.py live tab

Kite REST API
    └─ screener/kite_data.py  (central OHLCV fetcher)
           └─ 14 screener modules
                  └─ app.py  (Streamlit dashboard)
                  └─ scripts/*_bot.py  (background signal bots)
                         └─ ntfy.sh (push notification)
                         └─ screener/kite_order.py (ATM option + GTT)

auto_token.py  (cron 8 AM IST)
    └─ TOTP login → new access_token → restart services
```

---

## Setup

### Prerequisites

- Python 3.10+
- Redis (`redis-server`)
- Kite Connect app (api_key + api_secret from [kite.trade/connect](https://kite.trade/connect))
- TOTP enabled on your Kite account (Account → Security → External TOTP)
- ntfy app on your phone (optional, for push notifications)

### Install

```bash
git clone https://github.com/subbajandyala/stockAnalyser
cd stockAnalyser
pip install -r requirements.txt
```

### Configure `.env`

Create `/root/stockAnalyser/.env` (never commit this file):

```
KITE_API_KEY=your_api_key
KITE_API_SECRET=your_api_secret
KITE_ACCESS_TOKEN=your_access_token
KITE_USER_ID=your_user_id
KITE_PASSWORD=your_password
KITE_TOTP_SECRET=your_base32_totp_secret
KITE_AUTO_TRADE=0           # set to 1 to enable live orders
NTFY_TOPIC=your-ntfy-topic  # any unique string
```

### Run locally

```bash
streamlit run app.py
```

### Deploy on droplet

```bash
bash start_droplet.sh
```

App available at `http://<droplet-ip>:8501`

---

## Daily token automation

Kite access tokens expire at midnight IST every day. `auto_token.py` automates the refresh:

```bash
# Test it once
/root/venv/bin/python auto_token.py

# Add to crontab (runs at 8:00 AM IST = 2:30 AM UTC)
30 2 * * * cd /root/stockAnalyser && /root/venv/bin/python auto_token.py >> token_refresh.log 2>&1
```

### Signal bot cron (every 5 min during market hours)

```bash
*/5 9-15 * * 1-5 cd /root/stockAnalyser && /root/venv/bin/python scripts/agent_flow_bot.py NIFTY BANKNIFTY SENSEX --once >> agent_bot.log 2>&1
```

---

## Signal bot

`scripts/agent_flow_bot.py` scans NIFTY, BANKNIFTY, SENSEX on each run:

- Sends a push notification when signal rating ≥ 3★
- With `KITE_AUTO_TRADE=1`: places ATM LIMIT BUY + GTT OCO in Kite
- Deduplicates — only alerts when direction or strike changes

```bash
# Single scan
python scripts/agent_flow_bot.py NIFTY BANKNIFTY SENSEX --once

# Test notification
python scripts/agent_flow_bot.py --test-notify
```

---

## Auto-order details

When `KITE_AUTO_TRADE=1` and a 3★+ signal fires on NIFTY or SENSEX:

1. Nearest-expiry ATM option resolved from Kite instruments
2. LIMIT BUY at LTP + 1% (NRML product)
3. GTT OCO placed:  SL = entry − stop_pts, Target = entry + target_pts
4. Order confirmation pushed to phone

Lot sizes: NIFTY = 65 units · SENSEX = 20 units

Order will be rejected if account has insufficient funds — this is expected behaviour during testing.

---

## Logs

```bash
tail -f ticker.log          # WebSocket connection and ticks
tail -f streamlit.log       # Streamlit startup and errors
tail -f token_refresh.log   # Daily token refresh output
tail -f agent_bot.log       # Signal bot scan results
```

---

## Troubleshooting

**Auth errors / token expired**
```bash
/root/venv/bin/python auto_token.py
```

**Stale processes after crash**
```bash
pkill -9 -f ticker_daemon.py
pkill -9 -f "streamlit run"
bash start_droplet.sh
```

**No push notifications**
```bash
grep NTFY_TOPIC .env
/root/venv/bin/python scripts/agent_flow_bot.py --test-notify
```

---

## Notes

- Kite does not expose fundamental ratios (P/E, ROE, D/E) — the Fundamental screener uses 52-week high pullback as a quality proxy
- The NSE option chain scraper requires `undetected-chromedriver` with Chrome installed; it is blocked on cloud deployments
- All historical data comes from Kite Connect — yfinance has been fully removed

---

## License

Private repository. Not for public distribution.
