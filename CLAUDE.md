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

# Optional second Kite account — trades the same candles at the same moment
KITE2_NAME=...              # label on its log lines and pushes
KITE2_USER_ID=...
KITE2_PASSWORD=...
KITE2_TOTP_SECRET=...
KITE2_API_KEY=...           # its own Kite Connect app
KITE2_API_SECRET=...
KITE2_ACCESS_TOKEN=...      # written by auto_token.py at 08:00
KITE2_AUTO_TRADE=1
KITE2_NTFY_TOPIC=...        # optional; defaults to NTFY_TOPIC
KITE2_PROXY=http://<ip>:8888  # Zerodha links each static IP to ONE account: account 2's
                              # calls go out through a tinyproxy relay on its whitelisted IP
```

Alternative to KITE2_*: a separate trading droplet with its own `.env` (that account's
details as `KITE_*`, `KITE_NAME=<name>` to tag its pushes, `TRADE_ONLY=1` so
`start_droplet.sh` runs only `ticker_daemon.py` + `chart_bot.py` — no duplicate alerts).

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
- Lot cap per order: NIFTY 2, SENSEX 3 (`MAX_LOTS`; `.env` `CHART_MAX_LOTS_NIFTY` / `CHART_MAX_LOTS_SENSEX`, 0 = balance only)
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
| `candles.py` | chart_bot default engine: reads the forming 3m candle (strong / engulfing / hammer / shooting star, range ≥ 1.2× avg) → CE / PE; `CHART_CANDLE_SOURCE=future` reads the current-month NIFTY / SENSEX futures chart instead of the index (order prices converted to spot with the basis) |
| `momentum_v3.py` | Branch `momentum-v3`: context around the candle trigger — day checks (expiry cutoff, event days), direction votes (future vs VWAP + 15m Supertrend, ATM±2 OI change, opening range; 2 of 3), OI walls (room check), optional VIX-crush strictness. chart_bot gates entries and adds fade / direction exits; every rule is a `.env` switch |
| (branch `scalp-points`) | Pure intraday scalping on the candle engine: index chart, forming-candle entry, strong / engulfing only, book NIFTY +10 / SENSEX +30 premium points, stop −12 / −36 points (`CHART_SL_PTS_*`), fade exit, 12-min exit, no flip for 15 min after a loss, first signal skipped, no direction / volume / OI gates, 1 lot |
| `ma_cpr.py` | Branch `ma-cpr-scalp`: the manual Kite-chart method as chart_bot's signal (`CHART_SIGNAL=macpr`, default there). Index 3m, simple MA 10/20/50 + previous-day CPR. Entry: candle closes across the 50, 20 or 10 MA and the next candle confirms (`CHART_MACPR_ENTRY_MAS=50,20,10`; stop = the crossed MA). Exit: 2 closes past the 10 MA. CPR: 2 closes above → no PE, 2 below → no CE. Line exits (default): at entry the target is the nearest MA 10/20/50 or CPR (bottom/pivot/top) line ahead of price (≥ 10 NIFTY / 30 SENSEX pts), the stop is the crossed MA on a 3m close (`CHART_MACPR_STOP_ON=touch` for intrabar); the bot watches the index and exits; 2 closes past the 10 MA also exit (`CHART_MACPR_MA10_EXIT=always`; `open` = only with no line ahead); the GTT is a backstop 1.5× the line distance (premium at delta 0.5) but at least −25% / +30% (`CHART_MACPR_GTT_MIN_SL_PCT` / `_TP_PCT`); one trade per signal candle. Profit lock: once the premium is up `CHART_LOCK_PTS_NIFTY` (10) / `_SENSEX` (30) + `CHART_LOCK_BUFFER_PTS` (1), the GTT stop moves to entry + lock (target and ride unchanged); a fall back there books the points (bot sells too as a backup). `CHART_MACPR_LINE_EXITS=0` → old +60% / −20% with the 10 MA exit. No time exit, 1 lot. NIFTY only by default (`CHART_SYMBOLS=NIFTY`; `NIFTY,SENSEX` adds SENSEX). Switches: `CHART_MACPR_EXIT_MA`, `CHART_MACPR_EXIT_CONFIRM`, `CHART_MACPR_CPR`, `CHART_MACPR_FLIPS`. Log + order push also show Smart Money levels (nearest liquidity above / below, equal highs / lows, nearest order block / FVG, last BOS / CHoCH) from `chart_analysis.py` — information only, never gates. Monitoring pushes (low priority) when a closed candle breaks / confirms / takes support / is rejected at MA 10 / 20 / 50: `CHART_MA_ALERTS`, `CHART_MA_ALERT_MAS`, `CHART_MA_ALERT_KINDS`, `CHART_MA_ALERT_GAP_MIN`, `CHART_MA_ALERT_TOPIC`, `CHART_MA_ALERT_PRIORITY`; CPR pushes too (break / confirm 2nd close / failed 1-candle trap / back inside / support / reject, `CHART_MA_ALERT_CPR`) |
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

### Reference branch: `chart-success`

`chart-success` is a frozen copy of the working trading setup (commit `662b8e9`,
23 Oct 2026): candles engine on 3m, book at +20%, ride mode, 6-min exit, SL buffer
NIFTY 15 / SENSEX 30, lot cap NIFTY 2 / SENSEX 3, fill chasing, second account
support, TRADE_ONLY droplet mode.

- Never commit to `chart-success` unless the user explicitly asks.
- Before changing trading logic on the active branch, compare against it
  (`git diff chart-success -- <file>`) and say what differs from it.
- To roll a droplet back: `git fetch origin chart-success && git checkout chart-success`
  then `bash start_droplet.sh`.
