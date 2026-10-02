# MarketPulse — Project Reference

## What is this?

A self-hosted stock and options trading dashboard running on a DigitalOcean droplet.

- **Live dashboard** at `http://<droplet-ip>:8501` — screener tabs, candlestick charts, option chain
- **Signal bots** — automated scans every 5 min during market hours, push notifications to phone
- **Auto-orders** — when `KITE_AUTO_TRADE=1`, strong signals place orders directly in Kite
- **Fully hands-free** — token refreshes daily at 8 AM, no manual steps needed

---

## Daily routine (nothing to do)

| Time | What happens automatically |
|---|---|
| 8:00 AM IST | `auto_token.py` runs via cron — refreshes Kite token, restarts services |
| 9:15 AM onwards | Signal bots scan every 5 min |
| Strong signal | Push notification on phone (ntfy app) |
| `KITE_AUTO_TRADE=1` | Order placed in Kite (fails without funds — that's fine for testing) |
| 3:30 PM | Market closes, scans stop |

---

## Droplet — daily operations

```bash
# Check if everything is running
ps aux | grep -E "ticker_daemon|streamlit"
tail -20 ticker.log
tail -20 agent_bot.log

# Start / stop
bash start_droplet.sh
bash stop_droplet.sh

# If processes get stuck, force-kill and restart
pkill -9 -f ticker_daemon.py
pkill -9 -f "streamlit run"
bash start_droplet.sh

# Manual token refresh (if auto_token.py ever fails)
/root/venv/bin/python auto_token.py

# Test push notification
/root/venv/bin/python scripts/agent_flow_bot.py --test-notify

# Manual signal scan
/root/venv/bin/python scripts/agent_flow_bot.py NIFTY BANKNIFTY SENSEX --once
```

---

## `.env` file (on droplet, never commit to git)

Located at `/root/stockAnalyser/.env`

```
KITE_API_KEY=...
KITE_API_SECRET=...
KITE_ACCESS_TOKEN=...       # auto-updated daily — do not edit manually
KITE_USER_ID=...
KITE_PASSWORD=...
KITE_TOTP_SECRET=...        # from Kite Account Security → Enable external TOTP
KITE_AUTO_TRADE=1           # 1 = live orders, 0 = signal-only
NTFY_TOPIC=...              # your ntfy.sh topic name
```

**TOTP secret location:** Kite app → Account → Security → Enable external TOTP → shows base32 secret once.

---

## Crontab (on droplet)

```
# Daily token refresh at 8:00 AM IST
30 2 * * * cd /root/stockAnalyser && /root/venv/bin/python auto_token.py >> token_refresh.log 2>&1

# Agent flow bot every 5 min during market hours, Mon–Fri
*/5 9-15 * * 1-5 cd /root/stockAnalyser && /root/venv/bin/python scripts/agent_flow_bot.py NIFTY BANKNIFTY SENSEX --once >> agent_bot.log 2>&1
```

Edit with: `crontab -e`

---

## Architecture

```
Kite WebSocket ──► ticker_daemon.py ──► Redis
                                           └──► live_ticks.py ──► app.py (live tab)

Kite REST API ───► kite_data.py (central fetcher, replaces yfinance)
                       └──► screener/* (all 14 screener modules)
                                └──► app.py (screener tabs + charts)
                                └──► scripts/*_bot.py
                                           └──► ntfy.sh push notification
                                           └──► kite_order.py (auto BUY + GTT)
```

### Key components

| File | Purpose |
|---|---|
| `ticker_daemon.py` | WebSocket supervisor → Redis ticks |
| `auto_token.py` | Daily Kite token refresh via TOTP |
| `screener/kite_data.py` | Central OHLCV fetcher (all Kite, no yfinance) |
| `screener/kite_order.py` | ATM option order placement + GTT stop-loss |
| `scripts/agent_flow_bot.py` | Main signal bot — ntfy + auto-order |
| `scripts/elder_ray_bot.py` | Elder Ray signal bot — Telegram |
| `app.py` | Streamlit dashboard |
| `start_droplet.sh` | Start ticker + Streamlit |
| `stop_droplet.sh` | Stop ticker + Streamlit |

---

## Signal bot — how orders work

When a **3★+ signal** fires on NIFTY or SENSEX:

1. Push notification sent to phone via ntfy app
2. If `KITE_AUTO_TRADE=1`:
   - Finds nearest-expiry ATM option (CE or PE)
   - Places LIMIT BUY at LTP+1% (NRML product)
   - Places GTT OCO: Stop-loss and Target auto-set
   - Sends order confirmation to phone
3. If no funds → Kite rejects order (expected during testing)

Lot sizes: NIFTY = 65 units, SENSEX = 20 units

---

## Screeners in the app

| Tab / Screener | What it finds |
|---|---|
| MA Crossover | EMA20 crossing above EMA50 |
| MA Retracement | Pullback to 20 EMA in an uptrend |
| MA50 Support | Bounce off 50 EMA in full alignment |
| CPR Retracement | CPR-level retracements |
| Fundamental | Stocks >20% below 52-week high (buy-the-dip filter) |
| RPCI | 11-condition quality score for NIFTY 500 |
| Agent Flow | Multi-timeframe OI + momentum → CALL/PUT signal |
| Elder Ray | Bull/Bear power multi-timeframe |
| Open=Low | NIFTY100 intraday momentum (Open = Low setups) |
| Late Session | Last-hour direction signal for NIFTY/BANKNIFTY/SENSEX |
| Smart Alerts | Smart money flow alerts |
| Option Chain | Live NSE option chain (local only) |

---

## Log files (on droplet)

| File | Contents |
|---|---|
| `ticker.log` | WebSocket connect/disconnect, tick subscription |
| `streamlit.log` | Streamlit startup and errors |
| `token_refresh.log` | Daily auto_token.py output |
| `agent_bot.log` | Agent flow bot scan results |

---

## Tech stack

- **Python 3.11** in `/root/venv`
- **Streamlit** — dashboard UI
- **Kite Connect** — data + orders (no yfinance)
- **Redis** — tick/candle store for WebSocket data
- **KiteTicker** (Twisted WebSocket) — live tick feed
- **ntfy.sh** — push notifications (free, no account needed)
- **pyotp** — TOTP generation for token automation
- **DigitalOcean droplet** — 512 MB RAM, Singapore region

---

## Troubleshooting

**Token expired / auth errors**
```bash
/root/venv/bin/python auto_token.py
```

**Multiple processes stuck**
```bash
pkill -9 -f ticker_daemon.py
pkill -9 -f "streamlit run"
bash start_droplet.sh
```

**No notifications on phone**
```bash
# Check ntfy topic is set
grep NTFY_TOPIC /root/stockAnalyser/.env
# Send test
/root/venv/bin/python scripts/agent_flow_bot.py --test-notify
```

**Order placed but no fill**
- Check Kite order book — order may be rejected (no funds during testing)
- If `KITE_AUTO_TRADE=0`, orders are suppressed — check .env

**Redis not running**
```bash
systemctl start redis
# or
redis-server --daemonize yes
```
