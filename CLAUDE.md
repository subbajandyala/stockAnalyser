# CLAUDE.md

This file provides guidance to Claude Code when working with this repository.

## Running the app

```bash
# Install dependencies
pip install -r requirements.txt

# Run locally
streamlit run app.py

# Start everything on the droplet
bash start_droplet.sh

# Stop everything on the droplet
bash stop_droplet.sh
```

No tests or linters are configured.

---

## Architecture

**MarketPulse** is a Streamlit trading dashboard running on a DigitalOcean droplet. It combines a live WebSocket tick feed, Kite historical OHLCV data, screener modules, and automated signal bots.

### Three-layer stack

```
1. ticker_daemon.py   — KiteTicker WebSocket → Redis (live ticks + 1m candles)
2. screener/          — OHLCV fetchers + signal logic (all Kite, no yfinance)
3. app.py / scripts/  — Streamlit UI + background signal bots (ntfy + auto-order)
```

---

## Credentials & `.env`

All credentials live ONLY in `/root/stockAnalyser/.env` on the droplet. Never commit to git.

```
KITE_API_KEY=...
KITE_API_SECRET=...
KITE_ACCESS_TOKEN=...       # refreshed daily by auto_token.py
KITE_USER_ID=...
KITE_PASSWORD=...
KITE_TOTP_SECRET=...        # base32 TOTP secret from Kite Account Security page
KITE_AUTO_TRADE=1           # set to 1 to enable live order execution
NTFY_TOPIC=...              # ntfy.sh topic for push notifications
```

`~/.kite_creds.json` on the droplet holds `api_key`, `access_token`, and optionally `ntfy_agent_topic` / `telegram_bot_token` / `telegram_chat_id`.

---

## Daily automation (fully hands-free)

### Token refresh — `auto_token.py`

Runs at **8:00 AM IST** via cron (`/root/venv/bin/python`):
1. Logs in via Kite `/api/login` with password
2. Generates TOTP from `KITE_TOTP_SECRET` using pyotp
3. Completes OAuth → `request_token` → `generate_session` → new `access_token`
4. Updates `KITE_ACCESS_TOKEN` in `.env` via regex
5. Restarts `ticker_daemon` and Streamlit via `start_droplet.sh`

Cron entry:
```
30 2 * * * cd /root/stockAnalyser && /root/venv/bin/python auto_token.py >> token_refresh.log 2>&1
```

### Signal bots — `scripts/`

| Script | Trigger | Notification |
|---|---|---|
| `agent_flow_bot.py` | cron every 5 min, 9:15–15:30, Mon–Fri | ntfy.sh push |
| `elder_ray_bot.py` | cron (configurable) | Telegram |
| `cpr_scan_bot.py` | cron | ntfy / Telegram |
| `gamma_blast_bot.py` | cron | ntfy |
| `red_flag_bot.py` | cron | ntfy |
| `eod_report.py` | cron, 15:35 | ntfy |
| `chart_bot.py --loop --interval 30` | start_droplet.sh, every 30 s | ntfy; only Kite order executor (candles engine: book at +20%, big strong candles ride with a trailing stop, exit if not working in 6 min) |
| `chart_bot.py --preflight` | before 9:15 (no orders) | ntfy READY / NOT READY |
| `replay_bot.py` | manual, before deploying a rule change | console + data/replay_*.csv |

All bots support `--once` flag (single scan, exit) and `--state-file` for dedup.

---

## `ticker_daemon.py` — WebSocket supervisor

- **Subprocess supervisor pattern** (Twisted reactor cannot restart in-process)
- `main()` spawns itself as `--worker`; on crash, restarts after 5s
- Subscribes tokens: `NIFTY=256265`, `BANKNIFTY=260105`, `SENSEX=265`
- Writes to Redis: `tick:{token}` (TTL 600s), `candles:1m:{token}` (list, max 750), `ticker:status` (TTL 120s)
- Credentials loaded from `.env` on the droplet

---

## `screener/kite_data.py` — Central OHLCV fetcher

Replaces yfinance across all screeners. **Zero yfinance imports remain.**

```python
fetch_ohlcv(symbol, interval="day", days=365)         # → DataFrame[OHLCV]
fetch_ohlcv_yf_compat(symbol, period="1y", interval="1d")  # drop-in for yf.download
```

- Instruments cached at `/tmp/kite_instr_nse.json` (refreshed every 24h)
- `get_token(symbol)` checks `INDEX_TOKENS` first, then instruments cache
- INDEX_TOKENS: NIFTY=256265, BANKNIFTY=260105, SENSEX=265, FINNIFTY=257801, MIDCPNIFTY=288009, INDIAVIX=264969
- Kite interval names: `minute`, `3minute`, `5minute`, `10minute`, `15minute`, `30minute`, `60minute`, `day`, `week`, `month`
- Returns IST-naive DatetimeIndex (already in IST — do NOT tz_localize UTC)

---

## `screener/kite_order.py` — Auto-execution

- `place_order_for_signal(symbol, direction, spot, api_key, access_token)` — places ATM LIMIT BUY + GTT OCO
- Only fires when `KITE_AUTO_TRADE=1`
- Supported symbols: `NIFTY` (NFO, lot=65), `SENSEX` (BFO, lot=20)
- Falls back to 1 lot if margin fetch fails (order always attempts to fire)
- GTT: SL = entry − scalp_stop_pts, Target = entry + scalp_target_pts

---

## Screener modules (`screener/`)

| File | What it finds |
|---|---|
| `kite_data.py` | Central Kite OHLCV fetcher (replaces yfinance) |
| `kite_hist.py` | `batch_quote_nse()` — live Kite /quote batch |
| `kite_order.py` | ATM option order placement + GTT |
| `technical.py` | `analyze_stock()` — uptrend + breakout; STRONG BUY/BUY/WATCH/SKIP |
| `ma_crossover.py` | EMA20 crossing EMA50, confirmed 2+ days |
| `ma_retracement.py` | 20 EMA retest in uptrend with CPR confluence |
| `ma50_support.py` | Bounce off 50 EMA in EMA20>EMA50>EMA200 alignment |
| `cpr_retracement.py` | CPR-level retracements |
| `technical.py` | Breakout readiness |
| `agent_flow.py` | Multi-timeframe OI + momentum → CALL/PUT/NO TRADE |
| `sensex_option_moves.py` | SENSEX intraday option move signals |
| `candles.py` | chart_bot default engine: reads the forming 3m candle (strong / engulfing / hammer / shooting star, range ≥ 1.2× avg) → CE / PE |
| `setups.py` | chart_bot alternative engine: regime + setups on 3m (trend pullback, ORB, VWAP reclaim/reject, range fade) |
| `open_low.py` | NIFTY100 Open=Low intraday momentum setups |
| `elder_ray.py` | Elder Ray Bull/Bear Power multi-timeframe |
| `smart_alerts.py` | Smart money alerts |
| `smart_alerts_v2.py` | Smart money alerts v2 |
| `fundamental.py` | 52W-high pullback >20% filter via batch_quote_nse |
| `rpci.py` | RPCI 11-condition score; batch scan via ThreadPoolExecutor |
| `late_session.py` | Last-hour signal engine (2:30–3:30 PM); NIFTY/BANKNIFTY/SENSEX |
| `option_chain.py` | Live NSE option chain (blocked on cloud, works locally) |
| `news.py` | RSS → NIFTY 500 company name match → analyze_stock |
| `live_ticks.py` | Redis tick reader for WebSocket data |

---

## `app.py` key sections

- **`TF_CONFIG`** — maps timeframe keys (`"5m"`, `"15m"`, `"1H"`, `"1D"`, `"1W"`) to Kite interval + days
- **`chart_modal`** — `@st.dialog` candlestick + EMA chart; triggered by row click
- **`_filter_market_hours()`** — filters intraday DataFrames to 09:15–15:30 IST
- **Sidebar** — cosmetic HTML; shows screener result counts from `st.session_state`
- **Scrolling ticker** — `@st.cache_data(ttl=300)` CSS marquee with NIFTY/BANKNIFTY/VIX

---

## Droplet operations

```bash
# Start everything
bash start_droplet.sh

# Stop everything
bash stop_droplet.sh

# View live logs
tail -f ticker.log
tail -f streamlit.log
tail -f token_refresh.log
tail -f agent_bot.log

# Force-kill stray processes
pkill -9 -f ticker_daemon.py
pkill -9 -f "streamlit run"

# Manual token refresh
/root/venv/bin/python auto_token.py

# Test ntfy notification
/root/venv/bin/python scripts/agent_flow_bot.py --test-notify

# Manual single scan
/root/venv/bin/python scripts/agent_flow_bot.py NIFTY BANKNIFTY SENSEX --once
```

---

## Data flow

```
Kite WebSocket
    └─ ticker_daemon.py
           └─ Redis: tick:{token}, candles:1m:{token}
                  └─ live_ticks.py → app.py (live tab)

Kite REST API
    └─ kite_data.py (historical OHLCV)
           └─ all screener modules
                  └─ app.py (screener tabs)
                  └─ scripts/*_bot.py (signal bots)
                         └─ kite_order.py (auto-execute)
                         └─ ntfy.sh / Telegram (push notification)
```

---

## Known constraints

- Kite provides no fundamental ratios (P/E, ROE, OPM, D/E) — `fundamental.py` uses 52W-high pullback as a proxy
- NSE option chain (`option_chain.py`) needs undetected-chromedriver; blocked on cloud
- `late_session.py` VWAP: Kite data is IST-naive — use `tz_localize(_IST)` not `tz_localize("UTC")`
- Kite access token expires daily at midnight IST — `auto_token.py` handles this

---

## Development branch

Active development: `claude/sensex-option-moves-analysis-ma3tr7`
