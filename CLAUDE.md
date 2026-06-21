# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the app

```bash
# Install dependencies
pip install -r requirements.txt

# Run locally
streamlit run app.py

# On Windows (Microsoft Store Python / when 'streamlit' isn't in PATH)
python -m streamlit run app.py
```

There are no tests or linters configured. The `test` file in the root is empty.

## Architecture

**MarketPulse** is a single-page Streamlit app (`app.py`) for screening NIFTY 500 stocks using live NSE/yfinance data. All screener logic lives in `screener/`.

### Data flow

1. `screener/stocks.py` — fetches the NIFTY 500 symbol list from NSE archives CSV. Falls back to a hardcoded top-100 list if the fetch fails. Returns a DataFrame with columns `Symbol`, `Company`, `NSE_Symbol` (e.g. `RELIANCE.NS`).
2. Each screener module pulls OHLCV data via `yfinance` using the `.NS`-suffixed symbol, runs its own signal logic, and returns a dict or DataFrame.
3. `app.py` imports all screeners, renders a 5-tab Streamlit UI, and calls screeners on button click. Results are cached in `st.session_state` so they survive Streamlit reruns without re-fetching.

### Screener modules (`screener/`)

| File | What it finds |
|---|---|
| `technical.py` | `analyze_stock()` — uptrend + breakout readiness for a single symbol; signals: STRONG BUY / BUY / WATCH / SKIP |
| `news.py` | `get_trending_stocks()` — matches RSS headlines against NIFTY 500 company names; calls `analyze_stock()` on matched stocks |
| `ma_retracement.py` | `run_ma_retracement_scan()` — stocks retesting 20 EMA in an uptrend with CPR confluence |
| `ma_crossover.py` | `run_crossover_scan()` — 20 EMA crossing above 50 EMA within the last 5 candles, confirmed over 2+ days |
| `ma50_support.py` | `run_ma50_support_scan()` — stocks bouncing off 50 EMA in EMA20 > EMA50 > EMA200 alignment above monthly CPR |
| `option_chain.py` | `fetch_option_chain()` — live NSE option chain; tries `undetected-chromedriver` first (requires local Chrome install), then `curl_cffi`. **Blocked on Streamlit Cloud** — only works locally. |
| `fundamental.py` | `fetch_fundamental_stocks()` — scrapes Screener.in with a 16-condition quality filter (ROCE, ROE, D/E, OPM, Piotroski, promoter holding, etc.). Requires a free Screener.in account on first use. Returns a DataFrame with Screener.in columns + `NSE_Symbol` for charting. |

### `app.py` key sections

- **`TF_CONFIG`** — dict mapping timeframe keys (`"5m"`, `"15m"`, `"1H"`, `"1D"`, `"1W"`) to `interval`, `period`, and `market_hours` flag. All screener calls thread this through.
- **`chart_modal`** — `@st.dialog` that renders a candlestick + volume + EMA 20/50 Plotly chart for any symbol. Triggered by clicking a row in any screener's `st.dataframe` (`on_select="rerun"`, `selection_mode="single-row"`).
- **`_filter_market_hours()`** — filters intraday DataFrames to 09:15–15:30 IST for the 5m/15m/1H timeframes.
- **Sidebar** — purely cosmetic HTML (`st.markdown(unsafe_allow_html=True)`); shows screener result counts from `st.session_state`.
- **Scrolling ticker** — `@st.cache_data(ttl=300)` fetches NIFTY 50, BANKNIFTY, FINNIFTY, INDIA VIX daily closes and renders a CSS marquee.

### Styling

All CSS is injected once at the top of `app.py` via `st.markdown("""<style>...</style>""", unsafe_allow_html=True)`. Theme: `#0a0e1a` background, `#00d4aa` teal accent. The option chain table uses custom HTML (`st.markdown`) with `.oc-tbl` CSS classes rather than `st.dataframe`.

### NSE option chain — known constraint

NSE's API is protected by Akamai bot detection. `requests` and `curl_cffi` are blocked with HTTP 403/404. `undetected-chromedriver` (which patches Chrome's `navigator.webdriver` flag) works locally but requires Google Chrome installed. **Cloud deployments cannot fetch live option chain data.**

### Deployment

- `packages.txt` is intentionally empty — Streamlit Cloud runs Debian Trixie where `libglib2.0-0` (bullseye) conflicts with the system's `libglib2.0-0t64`. Adding system packages breaks the deploy.
- The GitHub Actions workflow (`static.yml`) deploys the repo to GitHub Pages (not the Streamlit app).
- The Streamlit app is deployed separately via Streamlit Cloud, which reads `requirements.txt` and `packages.txt`.

### Development branch

Active development happens on `claude/website-auth-setup-fz7adz`. Push changes there, not to `main` directly.
