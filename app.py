import time
import datetime
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import yfinance as yf
from screener.stocks import get_nifty500_symbols
from screener.news import get_trending_stocks
from screener.technical import analyze_stock, _ema
from screener.ma_retracement import run_ma_retracement_scan
from screener.ma_crossover import run_crossover_scan
from screener.ma50_support import run_ma50_support_scan
from screener.cpr_retracement import run_cpr_retracement_scan
from screener.option_chain import (
    fetch_option_chain, get_expiries, parse_chain,
    atm_strike, calc_pcr, calc_max_pain,
)
try:
    from screener.fundamental import fetch_fundamental_stocks as _fetch_fundamental_stocks
    _HAS_FUNDAMENTAL = True
except Exception:
    _HAS_FUNDAMENTAL = False
    _fetch_fundamental_stocks = None  # type: ignore[assignment]
from screener.fo_scanner import run_fo_scan
from screener.sensex_option_moves import (
    run_sensex_option_moves_scan,
    get_sensex_expiry_fridays,
    run_preexpiry_analysis,
    analyze_expiry_day_patterns,
    run_oi_buildup_scanner,
)
from screener.trending_oi import (
    fetch_instruments  as toi_fetch_instruments,
    get_expiries       as toi_get_expiries,
    get_spot           as toi_get_spot,
    get_atm_strikes    as toi_get_atm_strikes,
    fetch_snapshot     as toi_fetch_snapshot,
    compute_row        as toi_compute_row,
    check_alerts       as toi_check_alerts,
    send_telegram      as toi_send_telegram,
    ind_fmt            as toi_ind_fmt,
    SYMBOLS            as TOI_SYMBOLS,
    INTERVALS          as TOI_INTERVALS,
)
from screener.smart_alerts import run_smart_signal
from screener.smart_alerts_v2 import run_smart_signal_v2
from screener.gamma_blast import run_gamma_blast_scan
from screener.oi_pulse import run_oi_pulse_scan
from screener.red_flag import run_red_flag_scan as _run_red_flag, FLAG_DEFS as _RF_FLAG_DEFS
from screener.rpci import run_rpci_scan as _rpci_scan, analyse_single as _rpci_detail
from screener.elder_ray import (
    run_elder_ray_signal, fetch_atm_option,
    INDEX_CONFIG as _ER_INDEX_CONFIG,
)
from screener.late_session import (
    INDEX_CONFIG as _LS_CONFIG,
    score_signal  as _ls_score,
    raw_signal    as _ls_raw_signal,
    apply_flip_guard as _ls_flip_guard,
    fetch_data    as _ls_fetch,
    session_window as _ls_window,
    atm_strike    as _ls_atm,
)
from plotly.subplots import make_subplots

try:
    from streamlit_autorefresh import st_autorefresh as _st_autorefresh
    _HAS_AUTOREFRESH = True
except ImportError:
    _HAS_AUTOREFRESH = False

import streamlit.components.v1 as _scomp


st.set_page_config(page_title="MarketPulse", layout="wide", page_icon="🐂")

# ── Global CSS ────────────────────────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&display=swap');

/* Base font on body — cascades without !important so icon fonts survive */
body, .stApp, .main, p, h1, h2, h3, h4, h5, h6,
button, input, textarea, select, label, td, th, li, a,
[data-testid], [class*="st-"] {
  font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif !important;
}

/* Sidebar collapse toggle — hide broken ligature text, show ☰ via CSS */
[data-testid="stSidebarCollapsedControl"] span,
[data-testid="collapsedControl"] span {
  font-size: 0 !important;
  width: 22px !important;
  display: inline-block !important;
}
[data-testid="stSidebarCollapsedControl"] span::after,
[data-testid="collapsedControl"] span::after {
  content: '☰';
  font-size: 18px !important;
  font-family: sans-serif !important;
  color: #c9d1d9 !important;
  display: block !important;
  line-height: 1 !important;
}

.stApp { background: #131722; }
.main .block-container { padding-top: 1rem; }
[data-testid="stSidebar"] { background: #1e222d !important; border-right: 1px solid #2a2e39 !important; }

/* ── Dark select / input widgets (TradingView-style) ─────────────────────── */
[data-baseweb="select"] > div:first-child {
  background: #1a1e2a !important;
  border: 1px solid #2a2e39 !important;
  border-radius: 8px !important;
  color: #e6edf3 !important;
}
[data-baseweb="select"] [class*="ValueContainer"] { color: #e6edf3 !important; }
[data-baseweb="select"] svg { fill: #6e7681 !important; }
[data-baseweb="popover"] [role="option"] { background: #1e222d !important; color: #e6edf3 !important; }
[data-baseweb="popover"] [role="option"]:hover,
[data-baseweb="popover"] [aria-selected="true"] { background: rgba(0,212,170,0.12) !important; color: #00d4aa !important; }
[data-baseweb="menu"] { background: #1e222d !important; border: 1px solid #2a2e39 !important; border-radius: 8px !important; }

/* Dark text inputs */
[data-baseweb="input"] > div,
[data-baseweb="input"] input,
input[type="text"], input[type="password"], textarea {
  background: #1a1e2a !important;
  border-color: #2a2e39 !important;
  color: #e6edf3 !important;
  border-radius: 8px !important;
}
input::placeholder, textarea::placeholder { color: #4a5568 !important; }

/* Segmented control (timeframe picker) */
[data-testid="stSegmentedControl"] { background: #1a1e2a !important; border: 1px solid #2a2e39 !important; border-radius: 8px !important; }
[data-testid="stSegmentedControl"] label { color: #8b949e !important; font-weight: 600 !important; font-size: 0.78rem !important; }
[data-testid="stSegmentedControl"] [aria-checked="true"] label { color: #00d4aa !important; background: rgba(0,212,170,0.12) !important; border-radius: 6px !important; }

/* Expander */
[data-testid="stExpander"] { background: #1a1e2a !important; border: 1px solid #2a2e39 !important; border-radius: 10px !important; }
[data-testid="stExpander"] summary { color: #c9d1d9 !important; font-weight: 600 !important; }

/* Checkbox */
[data-baseweb="checkbox"] label { color: #c9d1d9 !important; }
[data-baseweb="checkbox"] [role="checkbox"] { border-color: #2a2e39 !important; background: #1a1e2a !important; }
[data-baseweb="checkbox"] [role="checkbox"][aria-checked="true"] { background: #00d4aa !important; border-color: #00d4aa !important; }

/* Labels */
[data-testid="stWidgetLabel"] p, label { color: #8b949e !important; font-size: 0.78rem !important; font-weight: 600 !important; letter-spacing: 0.3px !important; text-transform: uppercase !important; }

/* Scrollbar */
::-webkit-scrollbar { width: 5px; height: 5px; }
::-webkit-scrollbar-track { background: #131722; }
::-webkit-scrollbar-thumb { background: #2a2e39; border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: #3a3e49; }

/* Ticker */
.ticker-wrapper { background: #1e222d; border: 1px solid #2a2e39; border-radius: 8px; overflow: hidden; white-space: nowrap; padding: 8px 0; margin-bottom: 18px; }
.ticker-track { display: inline-block; animation: ticker-scroll 40s linear infinite; }
.ticker-track:hover { animation-play-state: paused; }
.ticker-item { display: inline-block; margin: 0 24px; font-size: 0.8rem; font-weight: 600; color: #c9d1d9; }
.ticker-item .tn { color: #6e7681; margin-right: 5px; font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.3px; }
.ticker-item .tu { color: #00d4aa; }
.ticker-item .td { color: #f85149; }
@keyframes ticker-scroll { 0% { transform: translateX(0); } 100% { transform: translateX(-50%); } }

/* Hero */
.hero-h1 { font-size: 2.55rem; font-weight: 900; color: #fff; letter-spacing: -1.5px; line-height: 1.12; margin: 28px 0 0; }
.hero-h1 .tl { color: #00d4aa; }
.hero-sub { font-size: 0.88rem; color: #6e7681; margin-top: 10px; }
.hero-badge { display: inline-block; background: rgba(0,212,170,0.1); border: 1px solid rgba(0,212,170,0.25); color: #00d4aa; font-size: 0.68rem; font-weight: 700; letter-spacing: 1.2px; text-transform: uppercase; padding: 2px 10px; border-radius: 20px; margin-top: 12px; }

/* Metric cards */
[data-testid="metric-container"] { background: #1e222d !important; border: 1px solid #2a2e39 !important; border-radius: 10px !important; padding: 12px 16px !important; }
[data-testid="stMetricLabel"] { color: #8b949e !important; font-size: 0.78rem !important; font-weight: 600 !important; text-transform: uppercase; }
[data-testid="stMetricValue"] { color: #e6edf3 !important; font-size: 1.4rem !important; font-weight: 700 !important; }
[data-testid="stMetricDelta"] { font-size: 0.85rem !important; }

/* Tabs */
[data-testid="stTabs"] [role="tablist"] { background: #131722; border-radius: 0; padding: 0; gap: 6px; border-bottom: 1px solid #2a2e39; }
[data-testid="stTabs"] button[role="tab"] { border-radius: 6px 6px 0 0 !important; font-size: 0.75rem !important; font-weight: 600 !important; color: #6e7681 !important; padding: 5px 14px !important; border: 1px solid transparent !important; border-bottom: none !important; background: #1e222d !important; letter-spacing: 0.3px !important; text-transform: uppercase !important; transition: all 0.15s !important; }
[data-testid="stTabs"] button[role="tab"]:hover { color: #c9d1d9 !important; background: #262b36 !important; border-color: #2a2e39 !important; }
[data-testid="stTabs"] button[role="tab"][aria-selected="true"] { background: #262b36 !important; color: #00d4aa !important; border-color: #2a2e39 !important; border-bottom: 2px solid #00d4aa !important; font-weight: 700 !important; }

/* Buttons */
button[kind="primary"] { background: linear-gradient(135deg, #00b894, #00d4aa) !important; border: none !important; border-radius: 8px !important; color: #131722 !important; font-weight: 700 !important; box-shadow: 0 2px 8px rgba(0,212,170,0.3) !important; transition: all 0.2s !important; }
button[kind="primary"]:hover { box-shadow: 0 4px 14px rgba(0,212,170,0.5) !important; transform: translateY(-1px) !important; }

/* Misc */
[data-testid="stAlert"] { border-radius: 8px !important; }
[data-testid="stDialog"] > div { max-width: 98vw !important; width: 98vw !important; }
[data-testid="stDialog"] section { max-width: 98vw !important; }
[data-testid="stDataFrame"] { border-radius: 8px; overflow: hidden; }
hr { border-color: #2a2e39 !important; margin: 10px 0 !important; }
.stCaption { color: #8b949e !important; }

/* Sidebar nav */
.sb-brand { padding: 20px 16px 14px; border-bottom: 1px solid #2a2e39; }
.sb-brand-name { font-size: 1.3rem; font-weight: 900; color: #fff; letter-spacing: -0.5px; }
.sb-brand-sub { font-size: 0.67rem; color: #8b949e; margin-top: 2px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.8px; }
.sb-sec { font-size: 0.61rem; font-weight: 700; text-transform: uppercase; letter-spacing: 1.8px; color: #8b949e; padding: 14px 16px 5px; }
.sb-item { display: flex; align-items: center; justify-content: space-between; padding: 7px 16px; border-radius: 7px; margin: 1px 6px; cursor: pointer; }
.sb-item:hover { background: rgba(0,212,170,0.08); }
.sb-item:active { background: rgba(0,212,170,0.15); }
.sb-lbl { color: #c9d1d9; font-size: 0.8rem; }
.sb-badge { background: rgba(0,212,170,0.12); border: 1px solid rgba(0,212,170,0.25); color: #00d4aa; font-size: 0.61rem; font-weight: 700; padding: 1px 6px; border-radius: 10px; }
.sb-div { height: 1px; background: #2a2e39; margin: 14px 0; }
.sb-live { display: flex; align-items: center; gap: 8px; padding: 8px 16px; font-size: 0.72rem; font-weight: 600; color: #00d4aa; }
.sb-dot { width: 7px; height: 7px; background: #00d4aa; border-radius: 50%; display: inline-block; animation: sb-pulse 1.5s ease-in-out infinite; }
@keyframes sb-pulse { 0%,100% { opacity:1; transform:scale(1); } 50% { opacity:0.3; transform:scale(0.6); } }

/* Streamlit st.navigation() link colors — force visible on dark sidebar */
[data-testid="stSidebarNavLink"] { color: #c9d1d9 !important; }
[data-testid="stSidebarNavLink"] p,
[data-testid="stSidebarNavLink"] span { color: #c9d1d9 !important; }
[data-testid="stSidebarNavLink"]:hover { background: rgba(0,212,170,0.08) !important; }
[data-testid="stSidebarNavLink"]:hover p,
[data-testid="stSidebarNavLink"]:hover span { color: #fff !important; }
[data-testid="stSidebarNavLink"][aria-current="page"],
[data-testid="stSidebarNavLink"][aria-selected="true"] { background: rgba(0,212,170,0.1) !important; border-left: 2px solid #00d4aa !important; }
[data-testid="stSidebarNavLink"][aria-current="page"] p,
[data-testid="stSidebarNavLink"][aria-current="page"] span,
[data-testid="stSidebarNavLink"][aria-selected="true"] p,
[data-testid="stSidebarNavLink"][aria-selected="true"] span { color: #00d4aa !important; font-weight: 700 !important; }
[data-testid="stSidebarNavSeparatorHeader"],
[data-testid="stSidebarNavSeparatorHeader"] p,
[data-testid="stSidebarNavSeparatorHeader"] span { color: #8b949e !important; font-size: 0.62rem !important; font-weight: 700 !important; letter-spacing: 1.4px !important; text-transform: uppercase !important; }

/* OI table */
.oc-wrap { width:100%; overflow-x:auto; margin:4px 0 16px; }
.oc-tbl { width:100%; border-collapse:collapse; font-size:0.78rem; }
.oc-tbl th { background:#1e222d; color:#6e7681; font-weight:700; text-transform:uppercase; font-size:0.64rem; letter-spacing:0.6px; padding:8px 10px; border-bottom:1px solid #2a2e39; }
.oc-tbl th.r { text-align:right; } .oc-tbl th.l { text-align:left; } .oc-tbl th.c { text-align:center; }
.oc-tbl td { padding:5px 8px; border-bottom:1px solid rgba(26,32,53,0.6); color:#c9d1d9; vertical-align:middle; white-space:nowrap; }
.oc-tbl td.r { text-align:right; font-variant-numeric:tabular-nums; }
.oc-tbl td.l { text-align:left; font-variant-numeric:tabular-nums; }
.oc-tbl td.c { text-align:center; }
.oc-tbl td.stk { text-align:center; font-weight:700; }
.oc-tbl tr.atm td { background:rgba(0,212,170,0.07) !important; }
.oc-tbl tr.atm td.stk { color:#00d4aa; font-weight:800; font-size:0.88rem; }
.oc-tbl tr:hover td { background:rgba(255,255,255,0.025); }
.oc-tbl tr.atm:hover td { background:rgba(0,212,170,0.1) !important; }
.bce { display:flex; justify-content:flex-end; align-items:center; }
.bce-i { height:9px; background:rgba(248,81,73,0.65); border-radius:2px 0 0 2px; }
.bpe { display:flex; justify-content:flex-start; align-items:center; }
.bpe-i { height:9px; background:rgba(0,212,170,0.65); border-radius:0 2px 2px 0; }
.cup { color:#00d4aa; font-weight:600; } .cdn { color:#f85149; font-weight:600; }
.atm-lbl { display:block; font-size:0.55rem; color:#00d4aa; letter-spacing:1.5px; font-weight:800; text-transform:uppercase; text-align:center; margin-top:1px; }

/* ── Hide sidebar + collapsed control ──────────────────────────────────────── */
[data-testid="stSidebar"],
[data-testid="stSidebarCollapsedControl"],
[data-testid="collapsedControl"] { display: none !important; }
.main .block-container {
  padding-top: 0 !important;
  padding-left: 0.75rem !important;
  padding-right: 0.75rem !important;
  max-width: 100% !important;
}

/* ── Top app bar ─────────────────────────────────────────────────────────── */
.mp-topbar {
  background: #13161f;
  border-bottom: 1px solid #1e2433;
  padding: 10px 16px;
  display: flex; align-items: center; justify-content: space-between; gap: 10px;
  position: sticky; top: 0; z-index: 999;
  margin: -0.5rem -0.75rem 0;
}
.mp-brand { font-size: 1.05rem; font-weight: 900; color: #fff; letter-spacing: -0.3px; display:flex; align-items:center; gap:6px; }
.mp-brand-accent { color: #00d4aa; }
.mp-topright { display: flex; align-items: center; gap: 8px; }
.mp-live { display: flex; align-items: center; gap: 4px; font-size: 0.65rem; font-weight: 700; color: #00d4aa; letter-spacing: 0.5px; }
.mp-kite-ok  { background: rgba(0,212,170,0.1); border: 1px solid rgba(0,212,170,0.3); color: #00d4aa; font-size: 0.68rem; font-weight: 700; padding: 3px 10px; border-radius: 20px; }
.mp-kite-btn { background: rgba(56,126,209,0.15); border: 1px solid rgba(56,126,209,0.35); color: #79c0ff; font-size: 0.68rem; font-weight: 700; padding: 3px 10px; border-radius: 20px; text-decoration: none !important; white-space: nowrap; }
.mp-kite-btn:hover { background: rgba(56,126,209,0.25) !important; }

/* ── Page nav tabs ─────────────────────────────────────────────────────────── */
.mp-nav {
  background: #13161f;
  border-bottom: 1px solid #1e2433;
  display: flex; flex-direction: row; gap: 0; align-items: stretch;
  overflow-x: auto; -webkit-overflow-scrolling: touch; scrollbar-width: none;
  margin: 0 -0.75rem 1rem;
  padding: 0 4px;
}
.mp-nav::-webkit-scrollbar { display: none; }
.mp-nav a {
  flex-shrink: 0; display: flex; align-items: center; gap: 5px;
  padding: 12px 15px; font-size: 0.83rem; font-weight: 600;
  color: #5c6470; text-decoration: none !important;
  border-bottom: 2px solid transparent; margin-bottom: -1px;
  white-space: nowrap; transition: color 0.15s, border-color 0.15s;
  letter-spacing: 0.1px;
}
.mp-nav a:hover { color: #c9d1d9; }
.mp-nav a.active { color: #00d4aa; border-bottom-color: #00d4aa; }

/* ── Ticker strip in topbar ─────────────────────────────────────────────── */
.mp-ticker { display: flex; gap: 16px; overflow: hidden; font-size: 0.7rem; font-weight: 600; }
.mp-ticker-item { white-space: nowrap; color: #8b949e; }
.mp-ticker-item .tu { color: #00d4aa; } .mp-ticker-item .td { color: #f85149; }

/* ── Timeframe strip ─────────────────────────────────────────────────────── */
[data-testid="stSegmentedControl"] {
  background: #1a1e2a !important; border: 1px solid #2a2e39 !important;
  border-radius: 8px !important; padding: 3px !important;
}
[data-testid="stSegmentedControl"] button {
  font-size: 0.75rem !important; font-weight: 600 !important;
  padding: 4px 10px !important; color: #6e7681 !important;
  border-radius: 6px !important;
}
[data-testid="stSegmentedControl"] button[aria-checked="true"] {
  background: rgba(0,212,170,0.14) !important; color: #00d4aa !important;
}

/* ── Mobile ──────────────────────────────────────────────────────────────── */
@media (max-width: 768px) {
  .main .block-container { padding: 0 0.6rem 2rem !important; }
  .stApp { overflow-x: hidden !important; }
  .mp-topbar { padding: 9px 12px; margin: -0.5rem -0.6rem 0; }
  .mp-brand { font-size: 0.95rem; }
  .mp-nav { margin: 0 -0.6rem 0.75rem; }
  .mp-nav a { padding: 10px 12px; font-size: 0.78rem; }

  [data-testid="stHorizontalBlock"] { flex-direction: column !important; }
  [data-testid="stHorizontalBlock"] > div { width: 100% !important; min-width: 100% !important; flex: none !important; }

  [data-testid="stTabs"] [role="tablist"] { overflow-x: auto !important; flex-wrap: nowrap !important; -webkit-overflow-scrolling: touch; scrollbar-width: none; }
  [data-testid="stTabs"] [role="tablist"]::-webkit-scrollbar { display: none; }
  [data-testid="stTabs"] button[role="tab"] { font-size: 0.7rem !important; padding: 5px 10px !important; white-space: nowrap !important; flex-shrink: 0 !important; }

  [data-testid="metric-container"] { padding: 8px 10px !important; }
  [data-testid="stMetricValue"] { font-size: 1.05rem !important; }

  [data-testid="stDataFrame"] { overflow-x: auto !important; font-size: 0.72rem !important; }
  .oc-wrap { overflow-x: auto !important; -webkit-overflow-scrolling: touch; }
  .oc-tbl { font-size: 0.7rem !important; }
  .oc-tbl td, .oc-tbl th { padding: 4px 6px !important; }

  .ticker-wrapper { display: none !important; }
  .lhb-signal-card { padding: 14px 12px !important; }
  .lhb-score-ring { width: 64px !important; height: 64px !important; font-size: 1.4rem !important; }
  .rpci-sec-card { min-width: 130px !important; }
  .er-factor-name { min-width: 80px !important; font-size: 0.72rem !important; }
  .js-plotly-plot, .plotly { max-width: 100% !important; }
  .stPlotlyChart { overflow-x: auto !important; }
  [data-testid="stDialog"] > div { max-width: 100vw !important; width: 100vw !important; margin: 0 !important; border-radius: 0 !important; }
}

/* Touch-friendly tap targets */
@media (hover: none) and (pointer: coarse) {
  button { min-height: 44px !important; }
  .mp-nav a { min-height: 44px !important; }
  [data-testid="stTabs"] button[role="tab"] { min-height: 40px !important; }
}
</style>
""", unsafe_allow_html=True)

# ── Timeframe config ──────────────────────────────────────────────────────────
TF_CONFIG = {
    "5m":  {"interval": "5m",   "screener_period": "5d",  "chart_period": "2d",  "display_period": "2d",  "label": "5 Min",  "market_hours": True},
    "15m": {"interval": "15m",  "screener_period": "5d",  "chart_period": "2d",  "display_period": "2d",  "label": "15 Min", "market_hours": True},
    "1H":  {"interval": "1h",   "screener_period": "6mo", "chart_period": "6mo", "display_period": "6mo", "label": "1 Hour", "market_hours": True},
    "1D":  {"interval": "1d",   "screener_period": "1y",  "chart_period": "1y",  "display_period": "1y",  "label": "Daily",  "market_hours": False},
    "1W":  {"interval": "1wk",  "screener_period": "5y",  "chart_period": "5y",  "display_period": "5y",  "label": "Weekly", "market_hours": False},
}

SIGNAL_COLORS       = {"STRONG BUY": "#00C853", "BUY": "#64DD17", "WATCH": "#FFD600", "SKIP": "#FF5252"}
MA_SIGNAL_COLORS    = {"STRONG (20MA + CPR)": "#00C853", "GOOD (20MA + CPR)": "#64DD17", "GOOD (20MA + Vol)": "#FFD600", "20MA Bounce": "#80CBC4"}
CROSS_SIGNAL_COLORS = {"STRONG BUY": "#00C853", "BUY": "#64DD17", "WATCH": "#FFD600"}
MA50_SIGNAL_COLORS  = {"STRONG (50MA + Monthly CPR)": "#00C853", "BUY (50MA + Monthly CPR)": "#64DD17", "WATCH": "#FFD600"}

NEWS_COLS  = ["Symbol","Company","News Mentions","Price","Change Last%","Change Prev 5%","RSI","% from High","Vol Ratio","Signal","Top Headline","Top Link"]
MA_COLS    = ["Symbol","Company","Price","Change%","EMA20","% Above EMA20","Touch%","Vol Ratio","CPR Support","CPR BC","CPR Pivot","CPR TC","Signal"]
CROSS_COLS = ["Symbol","Company","Price","Change 1D%","RSI","EMA 20","EMA 50","EMA 200","% Above EMA200","EMA Gap%","Cross Day","Confirmed Days","Vol on Cross","Vol Today","Move Since Cross%","Signal"]
MA50_COLS  = ["Symbol","Company","Price","Change 1D%","EMA 20","EMA 50","EMA 200","% Above EMA50","% Above EMA200","Touch%","Vol Ratio","Above Monthly CPR","Monthly TC","Monthly Pivot","Monthly BC","Signal"]

def _style(colors):
    def fn(val):
        c = colors.get(val, "#ffffff")
        return f"background-color:{c};color:#000;font-weight:700;border-radius:4px;padding:2px 6px;"
    return fn

def change_style(val):
    try:
        return "color:#26a641;font-weight:600;" if float(val) > 0 else "color:#f85149;font-weight:600;"
    except Exception:
        return ""

def _filter_market_hours(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    idx = df.index
    if idx.tzinfo is None:
        idx = idx.tz_localize("UTC")
    idx = idx.tz_convert("Asia/Kolkata")
    df = df.copy()
    df.index = idx
    return df.between_time("09:15", "15:30")

@st.dialog("📊 Chart", width="large")
def chart_modal(nse_symbol: str, company: str, tf_key: str, extra_levels: dict | None = None):
    cfg           = TF_CONFIG[tf_key]
    interval      = cfg["interval"]
    chart_per     = cfg["chart_period"]
    use_mkt_hours = cfg["market_hours"]

    hcol1, hcol2 = st.columns([3, 1])
    hcol1.markdown(f"**{company}** &nbsp; `{nse_symbol}` &nbsp; · &nbsp; `{TF_CONFIG[tf_key]['label']}`")
    sym = nse_symbol.replace(".NS", "")
    hcol2.link_button("🔗 TradingView", f"https://www.tradingview.com/chart/?symbol=NSE:{sym}", use_container_width=True)

    with st.spinner("Loading chart data..."):
        df = yf.download(nse_symbol, period=chart_per, interval=interval, progress=False, auto_adjust=True)

    if df is None or df.empty:
        st.error("No data available.")
        return

    if use_mkt_hours:
        df = _filter_market_hours(df)
        if df.empty:
            st.warning("No data in market hours (9:15–15:30 IST). Market may be closed.")
            return

    close  = df["Close"].squeeze()
    open_  = df["Open"].squeeze()
    high   = df["High"].squeeze()
    low    = df["Low"].squeeze()
    volume = df["Volume"].squeeze()
    ema20  = _ema(close, 20)
    ema50  = _ema(close, 50)

    latest = float(close.iloc[-1])
    prev   = float(close.iloc[-2]) if len(close) > 1 else latest
    chg    = (latest - prev) / prev * 100 if prev else 0
    chg_arrow = "▲" if chg >= 0 else "▼"

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Price",       f"₹{latest:,.2f}", f"{chg_arrow} {abs(chg):.2f}%", delta_color="normal" if chg >= 0 else "inverse")
    m2.metric("EMA 20",      f"₹{float(ema20.iloc[-1]):,.2f}")
    m3.metric("EMA 50",      f"₹{float(ema50.iloc[-1]):,.2f}")
    m4.metric("Period High", f"₹{float(close.max()):,.2f}")
    m5.metric("Period Low",  f"₹{float(close.min()):,.2f}")

    if use_mkt_hours or cfg["interval"] not in ("1d", "1wk"):
        x_labels = df.index.strftime("%d %b %H:%M")
    else:
        x_labels = df.index.strftime("%d %b '%y")

    vol_colors = ["#26a641" if float(c) >= float(o) else "#f85149" for c, o in zip(close, open_)]

    fig = go.Figure()
    fig.add_trace(go.Candlestick(
        x=x_labels, open=open_, high=high, low=low, close=close, name="Price",
        increasing_line_color="#26a641", increasing_fillcolor="#26a641",
        decreasing_line_color="#f85149", decreasing_fillcolor="#f85149",
    ))
    fig.add_trace(go.Scatter(x=x_labels, y=ema20, name="EMA 20", line=dict(color="#f0b429", width=1.6)))
    fig.add_trace(go.Scatter(x=x_labels, y=ema50, name="EMA 50", line=dict(color="#e06c75", width=1.6)))

    if extra_levels:
        level_colors = {
            "CPR TC": "#c792ea", "CPR Pivot": "#82aaff", "CPR BC": "#89ddff",
            "Monthly TC": "#c792ea", "Monthly Pivot": "#82aaff", "Monthly BC": "#89ddff",
        }
        for label, price in extra_levels.items():
            fig.add_hline(y=price, line_dash="dot",
                          line_color=level_colors.get(label, "#aaa"), line_width=1.4,
                          annotation_text=f"  {label}: ₹{price:.2f}",
                          annotation_position="right", annotation_font_size=11,
                          annotation_font_color=level_colors.get(label, "#aaa"))

    fig.add_trace(go.Bar(x=x_labels, y=volume, name="Volume", marker_color=vol_colors, marker_opacity=0.7, yaxis="y2"))

    total_bars = len(x_labels)
    tick_every = max(1, total_bars // 18)
    vis_ticks  = x_labels[::tick_every].tolist()

    fig.update_layout(
        xaxis=dict(type="category", tickvals=vis_ticks, ticktext=vis_ticks, tickangle=-40,
                   tickfont=dict(size=10, color="#8b949e"), showgrid=False, zeroline=False),
        yaxis=dict(title="Price (₹)", side="left", showgrid=True, gridcolor="#2a2e39", gridwidth=1,
                   tickfont=dict(color="#8b949e"), title_font=dict(color="#8b949e")),
        yaxis2=dict(overlaying="y", side="right", showgrid=False, title="Volume",
                    title_font=dict(color="#8b949e"), tickfont=dict(color="#8b949e")),
        legend=dict(orientation="h", yanchor="bottom", y=1.01, x=0,
                    font=dict(color="#c9d1d9"), bgcolor="rgba(0,0,0,0)"),
        xaxis_rangeslider_visible=False,
        height=560,
        margin=dict(t=10, b=55, l=10, r=110),
        template="plotly_dark",
        plot_bgcolor="#131722",
        paper_bgcolor="#131722",
    )
    st.plotly_chart(fig, use_container_width=True)

    if extra_levels:
        with st.expander("📐 CPR / Support Levels", expanded=False):
            for label, price in extra_levels.items():
                st.markdown(f"**{label}:** ₹{price:,.2f}")


def _get_secret(key: str, default: str = "") -> str:
    try:
        return st.secrets.get(key, default)
    except Exception:
        return default

# ── One-tap OAuth: auto-exchange request_token → access_token ────────────────
_qp = st.query_params
_rt = _qp.get("request_token", "")
if _rt and not st.session_state.get("kite_access_token", ""):
    import hashlib, requests as _kreq
    _ex_key    = st.session_state.get("kite_api_key", "") or _get_secret("KITE_API_KEY", "plz6ik09bgb62mey")
    _ex_secret = _get_secret("KITE_API_SECRET", "")
    if _ex_key and _ex_secret:
        try:
            _checksum = hashlib.sha256((_ex_key + _rt + _ex_secret).encode()).hexdigest()
            _resp = _kreq.post(
                "https://api.kite.trade/session/token",
                data={"api_key": _ex_key, "request_token": _rt, "checksum": _checksum},
                headers={"X-Kite-Version": "3"},
                timeout=15,
            )
            if _resp.ok:
                _tok_data = _resp.json().get("data", {})
                _new_tok  = _tok_data.get("access_token", "")
                if _new_tok:
                    st.session_state["kite_access_token"] = _new_tok
                    st.session_state["kite_api_key"] = _ex_key
                    st.session_state["_kite_auto_name"] = _tok_data.get("user_name", "")
                    st.query_params.clear()
                    st.rerun()
        except Exception:
            pass

# ── localStorage persist ──────────────────────────────────────────────────────
_ses_key = st.session_state.get("kite_api_key", "")
_ses_tok = st.session_state.get("kite_access_token", "")
_scomp.html(f"""<script>
(function(){{
  const CK={repr(_ses_key)}, CT={repr(_ses_tok)};
  if(CK) localStorage.setItem('mp_kite_api_key', CK);
  if(CT) localStorage.setItem('mp_kite_access_token', CT);
}})();
</script>""", height=0)

# ── Kite status ───────────────────────────────────────────────────────────────
_kite_ok    = bool(st.session_state.get("kite_api_key","") and st.session_state.get("kite_access_token",""))
_kite_name  = st.session_state.get("_kite_auto_name","")
_login_key  = _get_secret("KITE_API_KEY","plz6ik09bgb62mey")
_has_secret = bool(_get_secret("KITE_API_SECRET",""))
_login_url  = f"https://kite.zerodha.com/connect/login?api_key={_login_key}&v=3"

if _kite_ok:
    _kn = f" {_kite_name}" if _kite_name else ""
    _kite_topbar_badge = f'<span class="mp-kite-ok">✓{_kn}</span>'
else:
    _kite_topbar_badge = ""

# ── Top bar ───────────────────────────────────────────────────────────────────
st.markdown(f"""
<div class="mp-topbar">
  <div class="mp-brand">🐂 Market<span class="mp-brand-accent">Pulse</span></div>
  <div class="mp-topright">
    <div class="mp-live"><span class="sb-dot"></span>NSE LIVE</div>
    {_kite_topbar_badge}
  </div>
</div>""", unsafe_allow_html=True)

# ── Nav tabs (URL-driven) ─────────────────────────────────────────────────────
_NAV = [
    ("smart_alerts_pro", "⚡", "Alerts Pro"),
    ("gamma_blast",      "💥", "Gamma Blast"),
    ("elder_ray",        "🎯", "Elder Ray"),
    ("late_session",     "🌅", "Late Session"),
    ("rpci",             "📈", "RPCI"),
    ("red_flag",         "🚨", "Red Flag"),
]
_cur_page = _qp.get("page", "smart_alerts_pro")
_nav_links = "".join(
    f'<a href="?page={k}" target="_top" class="{"active" if k == _cur_page else ""}">{ic} {lbl}</a>'
    for k, ic, lbl in _NAV
)
st.markdown(f'<nav class="mp-nav">{_nav_links}</nav>', unsafe_allow_html=True)

# ── Kite login banner (shown when not connected) ──────────────────────────────
if not _kite_ok and _has_secret:
    # GET form submission replaces the action's query string with form fields,
    # so pass api_key and v as hidden inputs instead of baking them into action.
    _scomp.html(f"""
<style>
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{background:transparent;padding:0 0 4px;}}
button{{
  width:100%;padding:11px 14px;
  background:linear-gradient(135deg,rgba(56,126,209,0.18),rgba(56,126,209,0.1));
  border:1px solid rgba(56,126,209,0.45);border-radius:9px;
  color:#79c0ff;font-size:0.9rem;font-weight:700;
  cursor:pointer;font-family:Inter,-apple-system,sans-serif;
  letter-spacing:0.2px;
}}
button:hover{{background:rgba(56,126,209,0.28);border-color:rgba(56,126,209,0.7);}}
button:active{{background:rgba(56,126,209,0.4);}}
</style>
<form action="https://kite.zerodha.com/connect/login" method="GET" target="_top">
  <input type="hidden" name="api_key" value="{_login_key}">
  <input type="hidden" name="v" value="3">
  <button type="submit">🔑 Login with Zerodha</button>
</form>
""", height=52)

# ── Timeframe selector ────────────────────────────────────────────────────────
_tf_col, _kite_col = st.columns([3, 1])
with _tf_col:
    st.segmented_control(
        label="Timeframe", options=list(TF_CONFIG.keys()),
        format_func=lambda k: TF_CONFIG[k]["label"],
        default="1D", key="global_tf", label_visibility="collapsed",
    )
with _kite_col:
    if _kite_ok:
        if st.button("🚪 Disconnect", key="kite_logout_top", use_container_width=True):
            st.session_state.pop("kite_api_key", None)
            st.session_state.pop("kite_access_token", None)
            st.session_state.pop("_kite_auto_name", None)
            st.rerun()


# ── Scrolling ticker ──────────────────────────────────────────────────────────
@st.cache_data(ttl=300)
def _fetch_ticker_data():
    symbols = [
        ("NIFTY 50",  "^NSEI"),
        ("BANKNIFTY", "^NSEBANK"),
        ("FINNIFTY",  "NIFTY_FIN_SERVICE.NS"),
        ("INDIA VIX", "^INDIAVIX"),
    ]
    results = []
    for name, sym in symbols:
        try:
            df = yf.download(sym, period="5d", interval="1d", progress=False, auto_adjust=True)
            df = df.dropna()
            close = df["Close"].squeeze()
            if len(close) >= 2:
                price = float(close.iloc[-1])
                prev  = float(close.iloc[-2])
                chg   = (price - prev) / prev * 100 if prev else 0.0
            elif len(close) == 1:
                price, chg = float(close.iloc[0]), 0.0
            else:
                price, chg = 0.0, 0.0
        except Exception:
            price, chg = 0.0, 0.0
        results.append({"name": name, "price": price, "chg": chg})
    return results

_ticker_items = _fetch_ticker_data()
_ticker_parts = []
for _td in _ticker_items:
    if _td["price"] > 0:
        _cls   = "tu" if _td["chg"] >= 0 else "td"
        _arrow = "▲" if _td["chg"] >= 0 else "▼"
        _ticker_parts.append(
            f'<span class="ticker-item">'
            f'<span class="tn">{_td["name"]}</span>'
            f'{_td["price"]:,.2f}&thinsp;'
            f'<span class="{_cls}">{_arrow}&thinsp;{abs(_td["chg"]):.2f}%</span>'
            f'</span>'
        )

if _ticker_parts:
    _track = "".join(_ticker_parts)
    st.markdown(
        f'<div class="ticker-wrapper"><div class="ticker-track">{_track}{_track}</div></div>',
        unsafe_allow_html=True,
    )



# ── PAGE: News + Breakout ─────────────────────────────────────────────────────
def page_news_breakout():
    tf_key   = st.session_state.get("global_tf", "1D")
    tf       = TF_CONFIG.get(tf_key, TF_CONFIG["1D"])
    interval = tf["interval"]
    period   = tf["screener_period"]
    st.markdown("#### 📰 In-the-news stocks in uptrend — ready for breakout")

    with st.container(border=True):
        fc1, fc2, fc3, fc4, fc5 = st.columns([2, 2, 2, 1, 2])
        news_days    = fc1.slider("News lookback (days)", 7, 14, 10, key="nd")
        min_mentions = fc2.slider("Min mentions", 1, 10, 2, key="mm")
        breakout_pct = fc3.slider("Max % below High", 1, 20, 5, key="bp")
        show_all     = fc4.checkbox("Show SKIP", value=False, key="sa")
        run_btn      = fc5.button("🔍 Run News Screener", type="primary", use_container_width=True, key="run_news")

    if run_btn:
        with st.spinner("Fetching news and analysing stocks…"):
            symbols_df = get_nifty500_symbols()
            trending   = get_trending_stocks(symbols_df, days=news_days)

        if not trending:
            st.error("No trending stocks found. Try increasing the lookback period.")
        else:
            prog = st.progress(0, text="Analysing…")
            rows = []
            for i, item in enumerate(trending):
                tech = analyze_stock(item["NSE_Symbol"], breakout_pct=breakout_pct/100,
                                     interval=interval, period=period)
                if tech:
                    rows.append({"Symbol": item["Symbol"], "NSE_Symbol": item["NSE_Symbol"],
                                 "Company": item["Company"], "News Mentions": item["News_Mentions"],
                                 "Top Headline": item["Headlines"][0] if item["Headlines"] else "",
                                 "Top Link": item["Headline_Links"][0] if item.get("Headline_Links") else "",
                                 **tech})
                prog.progress((i+1)/len(trending), text=f"Analysing {item['Symbol']}…")
            prog.empty()
            st.session_state["news_results"] = pd.DataFrame(rows)
            st.session_state["news_tf"] = tf_key

    if "news_results" in st.session_state:
        full_df: pd.DataFrame = st.session_state["news_results"]
        cached_tf = st.session_state.get("news_tf", "1D")
        if cached_tf != tf_key:
            st.warning(f"⚠️ Results from **{TF_CONFIG[cached_tf]['label']}**. Re-run for **{TF_CONFIG[tf_key]['label']}**.")
        if not full_df.empty:
            sig_ord = {"STRONG BUY": 0, "BUY": 1, "WATCH": 2, "SKIP": 3}
            full_df = (full_df.assign(_r=full_df["Signal"].map(sig_ord))
                       .sort_values(["_r", "News Mentions"], ascending=[True, False])
                       .drop(columns=["_r"]))
            filtered = full_df[full_df["News Mentions"] >= min_mentions]
            if not show_all:
                filtered = filtered[filtered["Signal"] != "SKIP"]

            mc1, mc2, mc3, mc4 = st.columns(4)
            mc1.metric("Total Screened", len(full_df))
            mc2.metric("Strong Buy 🟢",  len(full_df[full_df["Signal"] == "STRONG BUY"]))
            mc3.metric("Buy 🟡",         len(full_df[full_df["Signal"] == "BUY"]))
            mc4.metric("Watch 👀",       len(full_df[full_df["Signal"] == "WATCH"]))
            st.caption("👆 Click any row to open chart · Esc to close")

            show_cols = [c for c in NEWS_COLS if c in filtered.columns]
            styled = (filtered[show_cols].copy().style
                      .map(_style(SIGNAL_COLORS), subset=["Signal"])
                      .map(change_style, subset=[c for c in ["Change Last%", "Change Prev 5%"] if c in filtered.columns])
                      .format({c: "₹{:.2f}" for c in ["Price"] if c in filtered.columns})
                      .format({c: "{:+.2f}%" for c in ["Change Last%", "Change Prev 5%", "% from High"] if c in filtered.columns})
                      .format({c: "{:.1f}" for c in ["RSI"] if c in filtered.columns})
                      .format({c: "{:.2f}x" for c in ["Vol Ratio"] if c in filtered.columns}))
            sel = st.dataframe(styled, use_container_width=True, height=500,
                               on_select="rerun", selection_mode="single-row", key="news_table",
                               column_config={
                                   "Top Link": st.column_config.LinkColumn("🔗 Link", display_text="Read →"),
                               })
            csv = filtered.to_csv(index=False).encode("utf-8")
            st.download_button("⬇️ Export CSV", csv, "news_screener.csv", "text/csv")
            rows_sel = sel.selection.get("rows", []) if sel else []
            if rows_sel:
                row = filtered.iloc[rows_sel[0]]
                chart_modal(row.get("NSE_Symbol", row["Symbol"] + ".NS"), row["Company"], cached_tf)
    else:
        st.info("🔎 Configure filters above and click **Run News Screener** to begin.")


# ── PAGE: 20 MA Retracement ───────────────────────────────────────────────────
def page_ma_retracement():
    tf_key   = st.session_state.get("global_tf", "1D")
    tf       = TF_CONFIG.get(tf_key, TF_CONFIG["1D"])
    interval = tf["interval"]
    period   = tf["screener_period"]
    st.markdown("#### 🔁 Stocks retesting 20 EMA in uptrend with bullish continuation")

    with st.container(border=True):
        rc1, rc2, rc3 = st.columns([2, 4, 2])
        touch_pct  = rc1.slider("Touch tolerance (%)", 1, 3, 1, key="tp")
        rc2.markdown(
            '<small style="color:#8b949e;">Price > EMA20 > EMA50 · Prev candle low touched EMA20 · '
            'Closed above EMA20 · Today bullish · Bonus: CPR confluence</small>',
            unsafe_allow_html=True)
        run_ma_btn = rc3.button("🔍 Run MA Screener", type="primary", use_container_width=True, key="run_ma")

    if run_ma_btn:
        with st.spinner(f"Scanning NIFTY 500 on {tf['label']} candles… (3–5 min)"):
            symbols_df = get_nifty500_symbols()
            _ma_kite_key   = st.session_state.get("kite_api_key", "")
            _ma_kite_token = st.session_state.get("kite_access_token", "")
            ma_results = run_ma_retracement_scan(
                symbols_df, touch_pct=touch_pct/100,
                interval=interval, period=period,
                api_key=_ma_kite_key, access_token=_ma_kite_token,
            )
            st.session_state["ma_results"] = ma_results
            st.session_state["ma_tf"] = tf_key

    if "ma_results" in st.session_state:
        ma_df: pd.DataFrame = st.session_state["ma_results"]
        cached_ma_tf = st.session_state.get("ma_tf", "1D")
        if cached_ma_tf != tf_key:
            st.warning(f"⚠️ Results from **{TF_CONFIG[cached_ma_tf]['label']}**. Re-run for **{TF_CONFIG[tf_key]['label']}**.")
        if ma_df.empty:
            st.warning("No stocks found. Try increasing touch tolerance or switching timeframe.")
        else:
            mc1, mc2, mc3 = st.columns(3)
            mc1.metric("Stocks Found",     len(ma_df))
            mc2.metric("With CPR Support", len(ma_df[ma_df["CPR Support"] == "✅ Yes"]))
            mc3.metric("Vol Surge >1.2x",  len(ma_df[ma_df["Vol Ratio"] > 1.2]))
            st.caption("👆 Click any row to open chart · Esc to close")

            show_cols = [c for c in MA_COLS if c in ma_df.columns]
            styled_ma = (ma_df[show_cols].copy().style
                         .map(_style(MA_SIGNAL_COLORS), subset=["Signal"])
                         .map(change_style, subset=[c for c in ["Change%"] if c in ma_df.columns])
                         .format({c: "₹{:.2f}" for c in ["Price", "EMA20", "CPR BC", "CPR Pivot", "CPR TC"] if c in ma_df.columns})
                         .format({c: "{:+.2f}%" for c in ["Change%", "% Above EMA20", "Touch%"] if c in ma_df.columns})
                         .format({c: "{:.2f}x" for c in ["Vol Ratio"] if c in ma_df.columns}))
            ma_sel = st.dataframe(styled_ma, use_container_width=True, height=500,
                                  on_select="rerun", selection_mode="single-row", key="ma_table")
            csv_ma = ma_df.to_csv(index=False).encode("utf-8")
            st.download_button("⬇️ Export CSV", csv_ma, "ma_retracement.csv", "text/csv")
            ma_rows = ma_sel.selection.get("rows", []) if ma_sel else []
            if ma_rows:
                r = ma_df.iloc[ma_rows[0]]
                cpr = {k: float(r[k]) for k in ["CPR TC", "CPR Pivot", "CPR BC"] if k in r}
                chart_modal(r["Symbol"] + ".NS", r["Company"], cached_ma_tf, extra_levels=cpr)
    else:
        st.info("🔎 Configure filters above and click **Run MA Screener** to scan.")


# ── PAGE: EMA Crossover ───────────────────────────────────────────────────────
def page_ema_crossover():
    st.markdown("#### 📈 20 EMA crossing above 50 EMA — position trading setups (Daily TF)")

    with st.container(border=True):
        xc1, xc2 = st.columns([5, 2])
        xc1.markdown(
            '<small style="color:#8b949e;">Price > 200 EMA · 20 EMA crossed above 50 EMA within last 1–5 days '
            '· Confirmed 2+ days · Volume surge on crossover · RSI > 30 · <b>Fixed: Daily timeframe</b></small>',
            unsafe_allow_html=True)
        run_cross_btn = xc2.button("🔍 Run Crossover Scan", type="primary", use_container_width=True, key="run_cross")

    if run_cross_btn:
        with st.spinner("Scanning NIFTY 500 for 20/50 EMA crossovers… (3–5 min)"):
            symbols_df    = get_nifty500_symbols()
            _cx_kite_key   = st.session_state.get("kite_api_key", "")
            _cx_kite_token = st.session_state.get("kite_access_token", "")
            cross_results = run_crossover_scan(
                symbols_df,
                api_key=_cx_kite_key, access_token=_cx_kite_token,
            )
            st.session_state["cross_results"] = cross_results

    if "cross_results" in st.session_state:
        cross_df: pd.DataFrame = st.session_state["cross_results"]
        if cross_df.empty:
            st.warning("No fresh crossovers found. Check back after market hours.")
        else:
            mc1, mc2, mc3, mc4 = st.columns(4)
            mc1.metric("Crossovers Found",   len(cross_df))
            mc2.metric("Strong Buy 🟢",      len(cross_df[cross_df["Signal"] == "STRONG BUY"]))
            mc3.metric("Buy 🟡",             len(cross_df[cross_df["Signal"] == "BUY"]))
            mc4.metric("Vol Surge on Cross", len(cross_df[cross_df["Vol on Cross"] > 1.3]))
            st.caption("👆 Click any row to open Daily chart · Esc to close")

            show_cols = [c for c in CROSS_COLS if c in cross_df.columns]
            styled_cross = (cross_df[show_cols].copy().style
                            .map(_style(CROSS_SIGNAL_COLORS), subset=["Signal"])
                            .map(change_style, subset=["Change 1D%"])
                            .format({c: "₹{:.2f}" for c in ["Price", "EMA 20", "EMA 50", "EMA 200"] if c in cross_df.columns})
                            .format({c: "{:+.2f}%" for c in ["Change 1D%", "% Above EMA200", "EMA Gap%", "Move Since Cross%"] if c in cross_df.columns})
                            .format({c: "{:.1f}" for c in ["RSI"] if c in cross_df.columns})
                            .format({c: "{:.2f}x" for c in ["Vol on Cross", "Vol Today"] if c in cross_df.columns})
                            .format({c: "{:.0f} day(s) ago" for c in ["Cross Day"] if c in cross_df.columns})
                            .format({c: "{:.0f} days" for c in ["Confirmed Days"] if c in cross_df.columns}))
            cross_sel = st.dataframe(styled_cross, use_container_width=True, height=500,
                                     on_select="rerun", selection_mode="single-row", key="cross_table")
            csv_cross = cross_df.to_csv(index=False).encode("utf-8")
            st.download_button("⬇️ Export CSV", csv_cross, "ema_crossover.csv", "text/csv")
            cross_rows = cross_sel.selection.get("rows", []) if cross_sel else []
            if cross_rows:
                cr = cross_df.iloc[cross_rows[0]]
                chart_modal(cr["Symbol"] + ".NS", cr["Company"], "1D")
    else:
        st.info("🔎 Click **Run Crossover Scan** above to identify position trading setups.")


# ── PAGE: 50 MA Support ───────────────────────────────────────────────────────
def page_ma50_support():
    tf_key   = st.session_state.get("global_tf", "1D")
    tf       = TF_CONFIG.get(tf_key, TF_CONFIG["1D"])
    interval = tf["interval"]
    period   = tf["screener_period"]
    st.markdown("#### 🛡️ Stocks bouncing off 50 EMA in strong uptrend, above Monthly CPR")

    with st.container(border=True):
        sc1, sc2, sc3 = st.columns([2, 4, 2])
        touch_pct_50 = sc1.slider("EMA50 touch tolerance (%)", 1, 3, 1, key="touch50")
        sc2.markdown(
            '<small style="color:#8b949e;">EMA20 > EMA50 > EMA200 · Prev candle low touched EMA50 '
            '· Closed above EMA50 · Today bullish · Price above Monthly CPR TC · Volume above avg</small>',
            unsafe_allow_html=True)
        run_ma50_btn = sc3.button("🔍 Run 50 MA Scan", type="primary", use_container_width=True, key="run_ma50")

    if run_ma50_btn:
        with st.spinner(f"Scanning NIFTY 500 on {tf['label']} candles… (3–6 min)"):
            symbols_df   = get_nifty500_symbols()
            _m50_kite_key   = st.session_state.get("kite_api_key", "")
            _m50_kite_token = st.session_state.get("kite_access_token", "")
            ma50_results = run_ma50_support_scan(
                symbols_df, touch_pct=touch_pct_50/100,
                interval=interval, period=period,
                api_key=_m50_kite_key, access_token=_m50_kite_token,
            )
            st.session_state["ma50_results"] = ma50_results
            st.session_state["ma50_tf"]      = tf_key

    if "ma50_results" in st.session_state:
        ma50_df: pd.DataFrame = st.session_state["ma50_results"]
        cached_ma50_tf = st.session_state.get("ma50_tf", "1D")
        if cached_ma50_tf != tf_key:
            st.warning(f"⚠️ Results from **{TF_CONFIG[cached_ma50_tf]['label']}**. Re-run for **{TF_CONFIG[tf_key]['label']}**.")
        if ma50_df.empty:
            st.warning("No stocks found. Try adjusting touch tolerance or timeframe.")
        else:
            mc1, mc2, mc3 = st.columns(3)
            mc1.metric("Stocks Found",      len(ma50_df))
            mc2.metric("Above Monthly CPR", len(ma50_df[ma50_df["Above Monthly CPR"] == "✅ Yes"]))
            mc3.metric("Vol Surge >1.3x",   len(ma50_df[ma50_df["Vol Ratio"] > 1.3]))
            st.caption("👆 Click any row to open chart · Esc to close")

            show_cols = [c for c in MA50_COLS if c in ma50_df.columns]
            styled_ma50 = (ma50_df[show_cols].copy().style
                           .map(_style(MA50_SIGNAL_COLORS), subset=["Signal"])
                           .map(change_style, subset=["Change 1D%"])
                           .format({c: "₹{:.2f}" for c in ["Price", "EMA 20", "EMA 50", "EMA 200", "Monthly TC", "Monthly Pivot", "Monthly BC"] if c in ma50_df.columns})
                           .format({c: "{:+.2f}%" for c in ["Change 1D%", "% Above EMA50", "% Above EMA200", "Touch%"] if c in ma50_df.columns})
                           .format({c: "{:.2f}x" for c in ["Vol Ratio"] if c in ma50_df.columns}))
            ma50_sel = st.dataframe(styled_ma50, use_container_width=True, height=500,
                                    on_select="rerun", selection_mode="single-row", key="ma50_table")
            csv_ma50 = ma50_df.to_csv(index=False).encode("utf-8")
            st.download_button("⬇️ Export CSV", csv_ma50, "ma50_support.csv", "text/csv")
            ma50_rows = ma50_sel.selection.get("rows", []) if ma50_sel else []
            if ma50_rows:
                r = ma50_df.iloc[ma50_rows[0]]
                cpr = {k: float(r[k]) for k in ["Monthly TC", "Monthly Pivot", "Monthly BC"] if k in r}
                chart_modal(r["Symbol"] + ".NS", r["Company"], cached_ma50_tf, extra_levels=cpr)
    else:
        st.info("🔎 Configure filters above and click **Run 50 MA Scan** to start.")


# ── TAB 5 — Option Chain Insights ─────────────────────────────────────────────
def _oc_signal(df: pd.DataFrame, spot: float, atm: float,
               pcr: float, mp: float) -> tuple:
    """
    Returns (signal, score, details, max_ce_strike, max_pe_strike).
    Analyses PCR, Max Pain, near-ATM OI buildup/unwinding, and key OI levels.
    Positive score → bullish (BUY CE), negative → bearish (BUY PE).
    """
    score = 0
    details: list[tuple] = []   # (indicator, value, verdict, explanation)

    # ── 1. PCR ────────────────────────────────────────────────────────────────
    if pcr >= 1.3:
        score += 2
        details.append(("PCR", f"{pcr:.2f}", "Bullish 🟢",
                         "Heavy put writing = strong support floor under market"))
    elif pcr >= 1.0:
        score += 1
        details.append(("PCR", f"{pcr:.2f}", "Mildly Bullish 🟡",
                         "More puts than calls = mild support"))
    elif pcr <= 0.7:
        score -= 2
        details.append(("PCR", f"{pcr:.2f}", "Bearish 🔴",
                         "Heavy call writing = strong resistance ceiling on market"))
    elif pcr < 1.0:
        score -= 1
        details.append(("PCR", f"{pcr:.2f}", "Mildly Bearish 🟡",
                         "More calls than puts = mild resistance"))
    else:
        details.append(("PCR", f"{pcr:.2f}", "Neutral ⚪",
                         "Balanced call/put OI"))

    # ── 2. Max Pain vs Spot ───────────────────────────────────────────────────
    mp_diff = (spot - mp) / mp * 100   # +ve → spot above MP (bearish pull)
    if mp_diff > 1.0:
        score -= 2
        details.append(("Max Pain", f"Spot {spot:,.0f}  MP {mp:,.0f}  (+{mp_diff:.1f}%)", "Bearish 🔴",
                         "Spot well above Max Pain — gravity pulls price down to MP"))
    elif mp_diff > 0.3:
        score -= 1
        details.append(("Max Pain", f"Spot {spot:,.0f}  MP {mp:,.0f}  (+{mp_diff:.1f}%)", "Mildly Bearish 🟡",
                         "Spot slightly above Max Pain — mild downward pull"))
    elif mp_diff < -1.0:
        score += 2
        details.append(("Max Pain", f"Spot {spot:,.0f}  MP {mp:,.0f}  ({mp_diff:.1f}%)", "Bullish 🟢",
                         "Spot well below Max Pain — gravity pulls price up to MP"))
    elif mp_diff < -0.3:
        score += 1
        details.append(("Max Pain", f"Spot {spot:,.0f}  MP {mp:,.0f}  ({mp_diff:.1f}%)", "Mildly Bullish 🟡",
                         "Spot slightly below Max Pain — mild upward pull"))
    else:
        details.append(("Max Pain", f"Spot ≈ MP {mp:,.0f}  ({mp_diff:.1f}%)", "Neutral ⚪",
                         "Spot near Max Pain — range-bound, no directional bias"))

    # ── 3. CE OI Change (above ATM — call resistance zone) ───────────────────
    atm_i     = int((df["Strike"] - atm).abs().values.argmin())
    above_atm = df.iloc[atm_i : atm_i + 8]       # ATM + 7 strikes above
    below_atm = df.iloc[max(0, atm_i - 7) : atm_i + 1]  # ATM - 7 strikes below

    ce_add  = float(above_atm[above_atm["CE Chng OI"] > 0]["CE Chng OI"].sum())
    ce_shed = float(above_atm[above_atm["CE Chng OI"] < 0]["CE Chng OI"].sum())

    if abs(ce_shed) > ce_add * 1.3 and abs(ce_shed) > 50_000:
        score += 1
        details.append(("CE OI (above ATM)", f"Unwinding {abs(ce_shed)/1000:.0f}K lots", "Bullish 🟢",
                         "Call writers covering shorts above ATM — resistance weakening"))
    elif ce_add > abs(ce_shed) * 1.3 and ce_add > 50_000:
        score -= 1
        details.append(("CE OI (above ATM)", f"Buildup +{ce_add/1000:.0f}K lots", "Bearish 🔴",
                         "Fresh call writing above ATM — strong resistance building"))
    else:
        details.append(("CE OI (above ATM)", "Mixed / Low activity", "Neutral ⚪",
                         "No clear CE OI trend above ATM"))

    # ── 4. PE OI Change (below ATM — put support zone) ───────────────────────
    pe_add  = float(below_atm[below_atm["PE Chng OI"] > 0]["PE Chng OI"].sum())
    pe_shed = float(below_atm[below_atm["PE Chng OI"] < 0]["PE Chng OI"].sum())

    if pe_add > abs(pe_shed) * 1.3 and pe_add > 50_000:
        score += 1
        details.append(("PE OI (below ATM)", f"Buildup +{pe_add/1000:.0f}K lots", "Bullish 🟢",
                         "Fresh put writing below ATM — strong support building"))
    elif abs(pe_shed) > pe_add * 1.3 and abs(pe_shed) > 50_000:
        score -= 1
        details.append(("PE OI (below ATM)", f"Unwinding {abs(pe_shed)/1000:.0f}K lots", "Bearish 🔴",
                         "Put writers covering shorts below ATM — support weakening"))
    else:
        details.append(("PE OI (below ATM)", "Mixed / Low activity", "Neutral ⚪",
                         "No clear PE OI trend below ATM"))

    # ── 5. Key OI levels (highest CE OI = resistance, highest PE OI = support) ──
    max_ce_strike = float(df.loc[df["CE OI"].idxmax(), "Strike"])
    max_pe_strike = float(df.loc[df["PE OI"].idxmax(), "Strike"])

    if spot > max_ce_strike:
        score += 1
        details.append(("Key Levels", f"Spot {spot:,.0f} > CE wall {max_ce_strike:,.0f}", "Bullish 🟢",
                         "Spot broke above highest call OI — resistance cleared"))
    elif spot < max_pe_strike:
        score -= 1
        details.append(("Key Levels", f"Spot {spot:,.0f} < PE wall {max_pe_strike:,.0f}", "Bearish 🔴",
                         "Spot below highest put OI support — support broken"))
    else:
        details.append(("Key Levels", f"Resistance ₹{max_ce_strike:,.0f}  |  Support ₹{max_pe_strike:,.0f}", "Neutral ⚪",
                         f"Spot {spot:,.0f} trading between key OI walls"))

    # ── Final verdict ─────────────────────────────────────────────────────────
    if   score >=  4: signal = "STRONG BUY CE 📈"
    elif score >=  2: signal = "BUY CE 📈"
    elif score <= -4: signal = "STRONG BUY PE 📉"
    elif score <= -2: signal = "BUY PE 📉"
    else:             signal = "NEUTRAL — WAIT ⚖️"

    return signal, score, details, max_ce_strike, max_pe_strike


def _recommend_trade(signal: str, score: int, oc_df: pd.DataFrame, atm: float, expiry: str) -> dict:
    strikes    = sorted(oc_df["Strike"].unique())
    strike_gap = min(b - a for a, b in zip(strikes, strikes[1:])) if len(strikes) > 1 else 50

    is_neutral = "NEUTRAL" in signal
    is_ce      = "CE" in signal
    is_strong  = "STRONG" in signal

    # For neutral, show both ATM CE and PE
    if is_neutral:
        atm_row = oc_df[oc_df["Strike"] == atm]
        ce_ltp  = float(atm_row.iloc[0]["CE LTP"]) if not atm_row.empty else 0
        pe_ltp  = float(atm_row.iloc[0]["PE LTP"]) if not atm_row.empty else 0
        return {
            "neutral":    True,
            "atm":        atm,
            "ce_ltp":     ce_ltp,
            "pe_ltp":     pe_ltp,
            "expiry":     expiry,
            "score":      score,
        }

    # ATM for strong, 1-OTM for regular
    rec_strike = atm if is_strong else (atm + strike_gap if is_ce else atm - strike_gap)
    row = oc_df[oc_df["Strike"] == rec_strike]
    if row.empty:
        rec_strike = atm
        row = oc_df[oc_df["Strike"] == atm]

    ltp_col = "CE LTP" if is_ce else "PE LTP"
    ltp     = float(row.iloc[0][ltp_col]) if not row.empty else 0
    # fallback: ATM if OTM has no price
    if ltp <= 0.5 and rec_strike != atm:
        rec_strike = atm
        row = oc_df[oc_df["Strike"] == atm]
        ltp = float(row.iloc[0][ltp_col]) if not row.empty else 0

    sl     = round(ltp * 0.65, 1)
    target = round(ltp * 1.65, 1)
    rr     = round((target - ltp) / (ltp - sl), 1) if ltp > sl else 0

    return {
        "neutral":  False,
        "strike":   rec_strike,
        "type":     "CE" if is_ce else "PE",
        "ltp":      ltp,
        "expiry":   expiry,
        "sl":       sl,
        "target":   target,
        "rr":       rr,
        "strong":   is_strong,
        "score":    score,
    }


def _build_oc_html(view_df: pd.DataFrame, atm: float) -> str:
    if view_df.empty:
        return "<p style='color:#6e7681;padding:12px;'>No data available.</p>"

    max_ce = max(float(view_df["CE OI"].max()), 1.0)
    max_pe = max(float(view_df["PE OI"].max()), 1.0)
    BAR = 130

    def _fk(v: int) -> str:
        if abs(v) >= 1000:
            return f"+{v/1000:.1f}K" if v > 0 else f"{v/1000:.1f}K"
        return f"+{v}" if v > 0 else str(v)

    rows_html = []
    for _, row in view_df.iterrows():
        is_atm = abs(float(row["Strike"]) - atm) < 0.5
        rc = "atm" if is_atm else ""
        atm_lbl = '<span class="atm-lbl">ATM</span>' if is_atm else ""

        ce_k  = row["CE OI"] / 1000
        pe_k  = row["PE OI"] / 1000
        ce_bw = max(2, int(row["CE OI"] / max_ce * BAR))
        pe_bw = max(2, int(row["PE OI"] / max_pe * BAR))

        cc = int(row["CE Chng OI"])
        pc = int(row["PE Chng OI"])
        cc_cls = "cup" if cc > 0 else ("cdn" if cc < 0 else "")
        pc_cls = "cup" if pc > 0 else ("cdn" if pc < 0 else "")

        rows_html.append(
            f'<tr class="{rc}">'
            f'<td class="r"><span class="{cc_cls}">{_fk(cc)}</span></td>'
            f'<td class="r">{ce_k:.1f}K</td>'
            f'<td class="c"><div class="bce" style="width:{BAR}px"><div class="bce-i" style="width:{ce_bw}px"></div></div></td>'
            f'<td class="stk">{int(row["Strike"]):,}{atm_lbl}</td>'
            f'<td class="c"><div class="bpe" style="width:{BAR}px"><div class="bpe-i" style="width:{pe_bw}px"></div></div></td>'
            f'<td class="l">{pe_k:.1f}K</td>'
            f'<td class="l"><span class="{pc_cls}">{_fk(pc)}</span></td>'
            f'</tr>'
        )

    return (
        '<div class="oc-wrap"><table class="oc-tbl">'
        '<thead><tr>'
        '<th class="r">CE Chng OI</th>'
        '<th class="r">CE OI</th>'
        '<th class="c">CALLS ▶</th>'
        '<th class="c">STRIKE</th>'
        '<th class="c">◀ PUTS</th>'
        '<th class="l">PE OI</th>'
        '<th class="l">PE Chng OI</th>'
        '</tr></thead>'
        f'<tbody>{"".join(rows_html)}</tbody>'
        '</table></div>'
    )


def page_option_chain():
    _kite_key   = st.session_state.get("kite_api_key",   _get_secret("KITE_API_KEY", ""))
    _kite_token = st.session_state.get("kite_access_token", _get_secret("KITE_ACCESS_TOKEN", ""))
    _kite_live  = bool(_kite_key and _kite_token)

    _src_badge = (
        '<span style="background:rgba(0,212,170,0.12);border:1px solid rgba(0,212,170,0.35);'
        'color:#00d4aa;font-size:0.7rem;font-weight:700;padding:2px 9px;border-radius:20px;'
        'margin-left:10px;">⚡ ZERODHA KITE — REAL-TIME</span>'
        if _kite_live else
        '<span style="background:rgba(248,81,73,0.10);border:1px solid rgba(248,81,73,0.3);'
        'color:#f85149;font-size:0.7rem;font-weight:700;padding:2px 9px;border-radius:20px;'
        'margin-left:10px;">⚠ NSE SCRAPE — LOCAL ONLY</span>'
    )
    st.markdown(
        f'<span style="font-size:1.1rem;font-weight:700;">🔗 Option Chain Insights</span>{_src_badge}',
        unsafe_allow_html=True,
    )

    oc1, oc2, oc3, oc4 = st.columns([2, 2, 2, 1])

    with oc1:
        oc_symbol = st.selectbox("Index", ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"], key="oc_symbol")
        if oc_symbol in ("SENSEX", "BANKEX") and not _kite_live:
            st.caption("⚠️ SENSEX/BANKEX require Zerodha Kite — connect in sidebar")

    cache_key = f"oc_{oc_symbol}"
    ts_key    = f"oc_{oc_symbol}_ts"
    oc_raw    = st.session_state.get(cache_key)

    expiries = get_expiries(oc_raw) if oc_raw else []
    with oc2:
        oc_expiry = st.selectbox(
            "Expiry", expiries if expiries else ["— load data first —"],
            key="oc_expiry", disabled=not expiries,
        )
    with oc3:
        n_strikes = st.slider("Strikes ± ATM", 5, 25, 10, key="oc_n")
    with oc4:
        load_oc = st.button("🔄 Refresh", type="primary", use_container_width=True, key="load_oc")

    if load_oc:
        _fetch_ok = False
        _spinner_src = "Zerodha Kite" if _kite_live else "NSE"
        with st.spinner(f"Fetching {oc_symbol} option chain from {_spinner_src}…"):
            try:
                oc_raw = fetch_option_chain(oc_symbol, api_key=_kite_key, access_token=_kite_token)
                st.session_state[cache_key] = oc_raw
                st.session_state[ts_key]    = time.time()
                _fetch_ok = True
            except Exception as _e:
                st.error(str(_e))
        if _fetch_ok:
            st.rerun()

    if not oc_raw:
        st.info("👆 Select an index and click **🔄 Refresh** to load live option chain data from NSE.")
    elif expiries and oc_expiry and oc_expiry != "— load data first —":
        ts = st.session_state.get(ts_key)
        if ts:
            elapsed = int(time.time() - ts)
            st.caption(f"🕐 Last updated: {elapsed}s ago  ·  Source: NSE India  ·  Click Refresh to update")

        oc_df, spot = parse_chain(oc_raw, oc_expiry)

        if oc_df.empty:
            st.warning("No data available for the selected expiry.")
        else:
            atm  = atm_strike(oc_df, spot)
            _pcr = calc_pcr(oc_df)
            _mp  = calc_max_pain(oc_df)
            pcr_label = "Bullish" if _pcr >= 1.2 else ("Bearish" if _pcr < 0.8 else "Neutral")

            m1, m2, m3, m4, m5, m6 = st.columns(6)
            m1.metric("Spot Price",  f"₹{spot:,.2f}")
            m2.metric("PCR",         f"{_pcr:.2f}", pcr_label)
            m3.metric("Max Pain",    f"₹{_mp:,.0f}", f"{_mp - spot:+.0f} from spot")
            m4.metric("ATM Strike",  f"₹{atm:,.0f}")
            m5.metric("Total CE OI", f"{oc_df['CE OI'].sum() / 1_000:.0f}K lots")
            m6.metric("Total PE OI", f"{oc_df['PE OI'].sum() / 1_000:.0f}K lots")

            # ── CE / PE Signal ────────────────────────────────────────────────
            sig, sig_score, sig_details, max_ce_wall, max_pe_wall = _oc_signal(
                oc_df, spot, atm, _pcr, _mp
            )

            if "STRONG BUY CE" in sig:
                sig_color, sig_bg = "#00d4aa", "rgba(0,212,170,0.12)"
            elif "BUY CE" in sig:
                sig_color, sig_bg = "#58d68d", "rgba(88,214,141,0.10)"
            elif "STRONG BUY PE" in sig:
                sig_color, sig_bg = "#f85149", "rgba(248,81,73,0.14)"
            elif "BUY PE" in sig:
                sig_color, sig_bg = "#ff7043", "rgba(255,112,67,0.10)"
            else:
                sig_color, sig_bg = "#e6b800", "rgba(230,184,0,0.10)"

            st.markdown(
                f"""<div style="background:{sig_bg};border:1.5px solid {sig_color};
                border-radius:10px;padding:18px 24px;margin:10px 0 18px 0;text-align:center;">
                <div style="font-size:1.6rem;font-weight:700;color:{sig_color};letter-spacing:1px;">
                    {sig}
                </div>
                <div style="color:#8b949e;font-size:0.82rem;margin-top:6px;">
                    Composite OI Score: <strong style="color:{sig_color};">{sig_score:+d}</strong>
                    &nbsp;·&nbsp; CE Resistance Wall: <strong style="color:#f85149;">₹{max_ce_wall:,.0f}</strong>
                    &nbsp;·&nbsp; PE Support Wall: <strong style="color:#00d4aa;">₹{max_pe_wall:,.0f}</strong>
                </div></div>""",
                unsafe_allow_html=True,
            )

            # Signal breakdown table
            sig_rows = "".join(
                f"""<tr>
                <td style="padding:6px 12px;color:#c9d1d9;white-space:nowrap;">{ind}</td>
                <td style="padding:6px 12px;color:#e6edf3;font-weight:600;">{val}</td>
                <td style="padding:6px 12px;">{verd}</td>
                <td style="padding:6px 12px;color:#8b949e;font-size:0.82rem;">{expl}</td>
                </tr>"""
                for ind, val, verd, expl in sig_details
            )
            st.markdown(
                f"""<table style="width:100%;border-collapse:collapse;background:#0d1117;
                border-radius:8px;overflow:hidden;margin-bottom:12px;">
                <thead><tr style="background:#161b22;">
                <th style="padding:8px 12px;color:#00d4aa;text-align:left;">Indicator</th>
                <th style="padding:8px 12px;color:#00d4aa;text-align:left;">Value</th>
                <th style="padding:8px 12px;color:#00d4aa;text-align:left;">Verdict</th>
                <th style="padding:8px 12px;color:#00d4aa;text-align:left;">Interpretation</th>
                </tr></thead><tbody>{sig_rows}</tbody></table>""",
                unsafe_allow_html=True,
            )

            # ── Recommended Trade ─────────────────────────────────────────────
            trade = _recommend_trade(sig, sig_score, oc_df, atm, oc_expiry)
            if trade.get("neutral"):
                st.markdown(
                    f"""<div style="background:rgba(230,184,0,0.07);border:1.5px solid rgba(230,184,0,0.35);
                    border-radius:10px;padding:16px 22px;margin:10px 0 18px 0;">
                    <div style="font-size:0.75rem;font-weight:700;color:#e6b800;letter-spacing:1.5px;
                    text-transform:uppercase;margin-bottom:8px;">⚖️ No Clear Signal — Wait for Setup</div>
                    <div style="color:#8b949e;font-size:0.88rem;margin-bottom:12px;">
                    OI signals are mixed (score {trade['score']:+d}). ATM options for reference:</div>
                    <div style="display:flex;gap:40px;flex-wrap:wrap;">
                    <div><div style="font-size:0.68rem;color:#6e7681;text-transform:uppercase;letter-spacing:1px;">
                    ATM CE ({int(trade['atm'])})</div>
                    <div style="font-size:1.1rem;font-weight:700;color:#f85149;">₹{trade['ce_ltp']:.1f}</div></div>
                    <div><div style="font-size:0.68rem;color:#6e7681;text-transform:uppercase;letter-spacing:1px;">
                    ATM PE ({int(trade['atm'])})</div>
                    <div style="font-size:1.1rem;font-weight:700;color:#00d4aa;">₹{trade['pe_ltp']:.1f}</div></div>
                    <div><div style="font-size:0.68rem;color:#6e7681;text-transform:uppercase;letter-spacing:1px;">
                    Expiry</div>
                    <div style="font-size:1.1rem;font-weight:700;color:#e6edf3;">{trade['expiry']}</div></div>
                    </div></div>""",
                    unsafe_allow_html=True,
                )
            else:
                t_color = "#00d4aa" if trade["type"] == "CE" else "#f85149"
                t_bg    = "rgba(0,212,170,0.08)" if trade["type"] == "CE" else "rgba(248,81,73,0.08)"
                badge   = "⚡ STRONG" if trade["strong"] else "📌"
                ltp_str = f"₹{trade['ltp']:.1f}" if trade["ltp"] > 0.5 else "—"
                sl_str  = f"₹{trade['sl']:.1f}"  if trade["ltp"] > 0.5 else "—"
                tgt_str = f"₹{trade['target']:.1f}" if trade["ltp"] > 0.5 else "—"
                rr_str  = f"1 : {trade['rr']}" if trade["ltp"] > 0.5 else "—"
                st.markdown(
                    f"""<div style="background:{t_bg};border:1.5px solid {t_color};
                    border-radius:10px;padding:16px 22px;margin:10px 0 18px 0;">
                    <div style="font-size:0.75rem;font-weight:700;color:{t_color};
                    letter-spacing:1.5px;text-transform:uppercase;margin-bottom:8px;">
                    {badge} Recommended Trade</div>
                    <div style="font-size:1.25rem;font-weight:800;color:#e6edf3;">
                    Buy {oc_symbol} <span style="color:{t_color};">{int(trade['strike'])} {trade['type']}</span>
                    &nbsp;<span style="font-size:0.8rem;color:#8b949e;font-weight:400;">Expiry: {trade['expiry']}</span>
                    </div>
                    <div style="display:flex;gap:32px;margin-top:12px;flex-wrap:wrap;">
                    <div><div style="font-size:0.68rem;color:#6e7681;text-transform:uppercase;letter-spacing:1px;">Entry (LTP)</div>
                    <div style="font-size:1.1rem;font-weight:700;color:#e6edf3;">{ltp_str}</div></div>
                    <div><div style="font-size:0.68rem;color:#6e7681;text-transform:uppercase;letter-spacing:1px;">Stop Loss</div>
                    <div style="font-size:1.1rem;font-weight:700;color:#f85149;">{sl_str}</div></div>
                    <div><div style="font-size:0.68rem;color:#6e7681;text-transform:uppercase;letter-spacing:1px;">Target</div>
                    <div style="font-size:1.1rem;font-weight:700;color:#00d4aa;">{tgt_str}</div></div>
                    <div><div style="font-size:0.68rem;color:#6e7681;text-transform:uppercase;letter-spacing:1px;">Risk:Reward</div>
                    <div style="font-size:1.1rem;font-weight:700;color:#e6edf3;">{rr_str}</div></div>
                    </div>
                    <div style="font-size:0.72rem;color:#6e7681;margin-top:10px;">
                    ⚠️ For educational purposes only — not financial advice. Always use proper position sizing.
                    </div></div>""",
                    unsafe_allow_html=True,
                )

            st.divider()

            # ── OI bar table ──────────────────────────────────────────────────
            if _kite_live:
                st.caption(
                    "ℹ️ Zerodha Kite does not expose daily OI change — "
                    "Change in OI columns will show 0. All OI levels, PCR, and Max Pain are live."
                )

            atm_iloc = int((oc_df["Strike"] - atm).abs().values.argmin())
            r_start  = max(0, atm_iloc - n_strikes)
            r_end    = min(len(oc_df), atm_iloc + n_strikes + 1)
            view_df  = oc_df.iloc[r_start:r_end].copy()

            st.caption("Teal row = ATM · Red bars = CE OI (resistance) · Teal bars = PE OI (support) · OI in K lots")
            st.markdown(_build_oc_html(view_df, atm), unsafe_allow_html=True)

            csv_oc = oc_df.to_csv(index=False).encode("utf-8")
            st.download_button(
                "⬇️ Export Full Chain CSV", csv_oc,
                f"{oc_symbol}_option_chain.csv", "text/csv", key="dl_oc",
            )

            st.divider()

            # ── Charts — OI Buildup & Change in OI ───────────────────────────
            chart_df = view_df.sort_values("Strike")
            str_x    = chart_df["Strike"].astype(int).astype(str).tolist()
            atm_x    = str(int(atm))

            chart_left, chart_right = st.columns(2)

            with chart_left:
                st.markdown("##### 📊 OI Buildup — CE vs PE")
                fig_oi = go.Figure()
                fig_oi.add_trace(go.Bar(x=str_x, y=chart_df["CE OI"].tolist(), name="CE OI", marker_color="#f85149", opacity=0.85))
                fig_oi.add_trace(go.Bar(x=str_x, y=chart_df["PE OI"].tolist(), name="PE OI", marker_color="#00d4aa", opacity=0.85))
                if atm_x in str_x:
                    fig_oi.add_vline(x=atm_x, line_dash="dash", line_color="#00d4aa")
                    fig_oi.add_annotation(x=atm_x, y=1, yref="paper", text="ATM",
                                          font=dict(color="#00d4aa", size=11), showarrow=False,
                                          yanchor="bottom")
                fig_oi.update_layout(
                    barmode="group", template="plotly_dark",
                    plot_bgcolor="#131722", paper_bgcolor="#131722",
                    height=340, margin=dict(t=10, b=50, l=10, r=10),
                    legend=dict(orientation="h", yanchor="bottom", y=1.01, x=0,
                                font=dict(color="#c9d1d9"), bgcolor="rgba(0,0,0,0)"),
                    xaxis=dict(tickfont=dict(color="#8b949e", size=9), showgrid=False, tickangle=-45),
                    yaxis=dict(tickfont=dict(color="#8b949e"), gridcolor="#2a2e39"),
                )
                st.plotly_chart(fig_oi, use_container_width=True)

            with chart_right:
                st.markdown("##### 📉 Change in OI — Fresh Positions")
                ce_chng = chart_df["CE Chng OI"].tolist()
                pe_chng = chart_df["PE Chng OI"].tolist()
                fig_chng = go.Figure()
                fig_chng.add_trace(go.Bar(
                    x=str_x, y=ce_chng, name="CE Chng OI",
                    marker_color=["#f85149" if v >= 0 else "#80cbc4" for v in ce_chng], opacity=0.85,
                ))
                fig_chng.add_trace(go.Bar(
                    x=str_x, y=pe_chng, name="PE Chng OI",
                    marker_color=["#00d4aa" if v >= 0 else "#ff7043" for v in pe_chng], opacity=0.85,
                ))
                if atm_x in str_x:
                    fig_chng.add_vline(x=atm_x, line_dash="dash", line_color="#00d4aa")
                    fig_chng.add_annotation(x=atm_x, y=1, yref="paper", text="ATM",
                                            font=dict(color="#00d4aa", size=11), showarrow=False,
                                            yanchor="bottom")
                fig_chng.update_layout(
                    barmode="group", template="plotly_dark",
                    plot_bgcolor="#131722", paper_bgcolor="#131722",
                    height=340, margin=dict(t=10, b=50, l=10, r=10),
                    legend=dict(orientation="h", yanchor="bottom", y=1.01, x=0,
                                font=dict(color="#c9d1d9"), bgcolor="rgba(0,0,0,0)"),
                    xaxis=dict(tickfont=dict(color="#8b949e", size=9), showgrid=False, tickangle=-45),
                    yaxis=dict(tickfont=dict(color="#8b949e"), gridcolor="#2a2e39"),
                )
                st.plotly_chart(fig_chng, use_container_width=True)


# ── PAGE: Fundamentals ────────────────────────────────────────────────────────
def page_fundamentals():
    if not _HAS_FUNDAMENTAL:
        try:
            from screener.fundamental import fetch_fundamental_stocks as _fetch_fundamental_stocks_lazy
        except Exception as _imp_err:
            st.error(f"Fundamentals screener unavailable: {_imp_err}")
            return
    else:
        _fetch_fundamental_stocks_lazy = _fetch_fundamental_stocks  # type: ignore[assignment]

    st.markdown("#### 📊 Fundamentally Strong Stocks — Quality Filter")

    with st.container(border=True):
        gc1, gc2 = st.columns([6, 1])
        gc1.markdown(
            '<small style="color:#8b949e;">'
            "Market Cap >₹1000Cr &nbsp;·&nbsp; ROE >15% &nbsp;·&nbsp; D/E &lt;0.5 &nbsp;·&nbsp; "
            "OPM >12% &nbsp;·&nbsp; Revenue & Earnings growth >8% &nbsp;·&nbsp; "
            "Down from 52W High >25% &nbsp;·&nbsp; Source: yfinance"
            "</small>",
            unsafe_allow_html=True,
        )
        run_fund_btn = gc2.button("🔍 Run", type="primary", use_container_width=True, key="run_fund")

    if run_fund_btn:
        _fund_prog = st.progress(0, text="Scanning NIFTY 500 fundamentals… (1–2 min)")
        try:
            _symbols_df = get_nifty500_symbols()
            fund_results = _fetch_fundamental_stocks_lazy(
                _symbols_df,
                progress_cb=lambda p: _fund_prog.progress(p, text=f"Scanning… {int(p*100)}% done"),
            )
            _fund_prog.empty()
            st.session_state["fund_results"] = fund_results
        except Exception as _fe:
            _fund_prog.empty()
            st.error(str(_fe))

    if "fund_results" in st.session_state:
        fund_df: pd.DataFrame = st.session_state["fund_results"]
        if fund_df.empty:
            st.warning("No stocks passed all filters today. Markets may be in a downtrend — most stocks will be below their 52W high by less than 25% or lack earnings growth.")
        else:
            fm1, fm2, fm3 = st.columns(3)
            fm1.metric("Stocks Found", len(fund_df))
            fm2.metric("Top Pick", fund_df["Company"].iloc[0])
            fm3.metric("Avg ROE %", f"{fund_df['ROE %'].mean():.1f}%")

            st.caption("👆 Click any row to open Daily chart · Source: yfinance · Esc to close")

            display_cols = [c for c in fund_df.columns if c != "NSE_Symbol"]
            styled_fund = (
                fund_df[display_cols].copy().style
                .map(change_style, subset=[c for c in ["Rev Growth %", "Earn Growth %"] if c in display_cols])
                .format({c: "₹{:.2f}" for c in ["Price"] if c in display_cols})
                .format({c: "{:.0f}" for c in ["Mkt Cap (Cr)"] if c in display_cols})
                .format({c: "{:.1f}%" for c in ["ROE %", "OPM %", "Rev Growth %", "Earn Growth %", "↓ 52W High %"] if c in display_cols})
                .format({c: "{:.2f}" for c in ["D/E"] if c in display_cols})
            )
            fund_sel = st.dataframe(
                styled_fund,
                use_container_width=True,
                height=500,
                on_select="rerun",
                selection_mode="single-row",
                key="fund_table",
            )
            csv_fund = fund_df.to_csv(index=False).encode("utf-8")
            st.download_button("⬇️ Export CSV", csv_fund, "fundamental_screener.csv", "text/csv")

            fund_rows = fund_sel.selection.get("rows", []) if fund_sel else []
            if fund_rows:
                fr = fund_df.iloc[fund_rows[0]]
                chart_modal(str(fr["NSE_Symbol"]), str(fr["Company"]), "1D")
    else:
        st.info("🔎 Click **Run** above to screen NIFTY 500 stocks by fundamental quality.")


# ── PAGE: F&O Scanner ─────────────────────────────────────────────────────────
def page_fo_scanner():
    _fo_kite_key   = st.session_state.get("kite_api_key",   _get_secret("KITE_API_KEY", ""))
    _fo_kite_token = st.session_state.get("kite_access_token", _get_secret("KITE_ACCESS_TOKEN", ""))
    _fo_kite_live  = bool(_fo_kite_key and _fo_kite_token)

    st.markdown("#### 🎯 F&O Options Scanner — CE/PE Signals Across All Stocks")

    with st.container(border=True):
        foc1, foc2 = st.columns([6, 1])
        foc1.markdown(
            '<small style="color:#8b949e;">'
            "Scans all NSE F&O stocks · Nearest expiry · Signals based on PCR, Max Pain & OI walls"
            " · Requires Zerodha Kite credentials in sidebar"
            "</small>",
            unsafe_allow_html=True,
        )
        run_fo_btn = foc2.button("🔍 Scan", type="primary",
                                 use_container_width=True, key="run_fo",
                                 disabled=not _fo_kite_live)

    if not _fo_kite_live:
        st.warning("⚡ Connect Zerodha Kite in the sidebar to enable F&O scanning.")
    else:
        if run_fo_btn:
            _fo_prog = st.progress(0, text="Starting F&O scan…")
            try:
                fo_results = run_fo_scan(
                    _fo_kite_key,
                    _fo_kite_token,
                    progress_cb=lambda p, m: _fo_prog.progress(p, text=m),
                )
                st.session_state["fo_results"] = fo_results
                _fo_prog.progress(1.0, text="Done!")
            except Exception as _e:
                st.error(f"F&O scan failed: {_e}")
                fo_results = pd.DataFrame()
        else:
            fo_results = st.session_state.get("fo_results", pd.DataFrame())

        if not fo_results.empty:
            # ── Filter ────────────────────────────────────────────────────────
            _sig_opts = ["All"] + [s for s in ["STRONG BUY CE 📈", "BUY CE 📈",
                                                "BUY PE 📉", "STRONG BUY PE 📉",
                                                "NEUTRAL ⚖️"] if s in fo_results["Signal"].values]
            _fo_filter = st.selectbox("Filter by Signal", _sig_opts, key="fo_filter")
            fo_view = fo_results if _fo_filter == "All" else fo_results[fo_results["Signal"] == _fo_filter]

            # ── Metrics ───────────────────────────────────────────────────────
            _fo_ce     = len(fo_results[fo_results["Type"] == "CE"])
            _fo_pe     = len(fo_results[fo_results["Type"] == "PE"])
            _fo_strong = len(fo_results[fo_results["Signal"].str.startswith("STRONG")])
            _fo_neu    = len(fo_results[fo_results["Signal"] == "NEUTRAL ⚖️"])
            fm1, fm2, fm3, fm4, fm5 = st.columns(5)
            fm1.metric("Stocks Scanned", len(fo_results))
            fm2.metric("BUY CE",         _fo_ce)
            fm3.metric("BUY PE",         _fo_pe)
            fm4.metric("Strong",         _fo_strong)
            fm5.metric("Neutral",        _fo_neu)

            st.caption("👆 Click any row to open chart · Green = CE · Red = PE")

            # ── Style signal column ───────────────────────────────────────────
            def _fo_signal_style(val: str) -> str:
                if "STRONG BUY CE" in val: return "color:#00d4aa;font-weight:800"
                if "BUY CE"        in val: return "color:#58d68d;font-weight:600"
                if "STRONG BUY PE" in val: return "color:#f85149;font-weight:800"
                if "BUY PE"        in val: return "color:#ff7043;font-weight:600"
                return ""

            display_cols = [c for c in fo_view.columns if c not in ("NSE_Symbol", "Type")]
            styled_fo = (
                fo_view[display_cols].style
                .map(_fo_signal_style, subset=["Signal"])
                .format({"Spot": "₹{:,.2f}", "PCR": "{:.2f}",
                         "MP Diff %": "{:+.2f}%",
                         "LTP": lambda v: f"₹{v:.1f}" if v else "—",
                         "SL":  lambda v: f"₹{v:.1f}" if v else "—",
                         "Target": lambda v: f"₹{v:.1f}" if v else "—",
                         "R:R": lambda v: f"1:{v}" if v else "—"})
            )
            fo_sel = st.dataframe(
                styled_fo,
                use_container_width=True,
                height=540,
                on_select="rerun",
                selection_mode="single-row",
                key="fo_table",
            )

            csv_fo = fo_results.to_csv(index=False).encode("utf-8")
            st.download_button("⬇️ Export CSV", csv_fo, "fo_scanner.csv", "text/csv", key="dl_fo")

            fo_rows = fo_sel.selection.get("rows", []) if fo_sel else []
            if fo_rows:
                fr = fo_view.iloc[fo_rows[0]]
                chart_modal(str(fr["NSE_Symbol"]), str(fr["Symbol"]), "1D")
        elif run_fo_btn:
            st.warning(
                "**Scan completed but no stocks were found.**\n\n"
                "Most likely cause: **Kite Access Token has expired** (tokens expire daily at midnight IST).\n\n"
                "**How to get a fresh token:**\n"
                "1. Open the login URL in your browser: "
                "`https://kite.zerodha.com/connect/login?api_key=plz6ik09bgb62mey&v=3`\n"
                "2. Login → you'll be redirected to Google (or your redirect URL)\n"
                "3. Copy the `request_token` value from the URL\n"
                "4. Run the exchange script (Python) to convert it to an Access Token\n"
                "5. Paste the new Access Token in the sidebar and click **🔍 Scan** again"
            )
        else:
            st.info("Click **🔍 Scan** to analyse all F&O stocks. Takes 2–3 minutes.")


# ── PAGE: Sensex Expiry Moves ─────────────────────────────────────────────────
def page_sensex_expiry():
    _em_kite_key   = st.session_state.get("kite_api_key",      _get_secret("KITE_API_KEY", ""))
    _em_kite_token = st.session_state.get("kite_access_token", _get_secret("KITE_ACCESS_TOKEN", ""))
    _em_kite_live  = bool(_em_kite_key and _em_kite_token)

    st.markdown("#### 🚀 Sensex Weekly Expiry — Option Moves (2:15 PM → 3:15 PM)")

    # ── PRE-EXPIRY ANALYSIS (live today → predict Friday) ────────────────────
    st.markdown("##### 🎯 Pre-Expiry Radar — Tomorrow's Rocket Candidates (Live)")
    st.markdown(
        '<small style="color:#8b949e;">'
        "Uses today's <b>live</b> SENSEX option chain (BFO via Kite) · "
        "Finds options you can buy <b>today</b> that would move ≥200% if Sensex closes at Max Pain on expiry day · "
        "Entry = current LTP · Exit = intrinsic value at Max Pain · Requires Kite credentials"
        "</small>",
        unsafe_allow_html=True,
    )

    if not _em_kite_live:
        st.warning("⚡ Connect Zerodha Kite in the sidebar to enable live pre-expiry analysis.")
    else:
        _pre_col1, _pre_col2 = st.columns([5, 1])
        _run_pre = _pre_col2.button("🔄 Load Live Chain", type="primary",
                                    use_container_width=True, key="run_pre_expiry")
        if _run_pre:
            with st.spinner("Fetching live SENSEX option chain from BFO…"):
                try:
                    _pre_result = run_preexpiry_analysis(_em_kite_key, _em_kite_token)
                    st.session_state["pre_expiry"] = _pre_result
                except Exception as _pe:
                    st.error(f"Failed: {_pe}")

        if "pre_expiry" in st.session_state:
            _pr = st.session_state["pre_expiry"]
            _gap   = _pr["gap_pts"]
            _dir_c = "#00d4aa" if _gap > 0 else "#f85149"
            _dir_a = "▲" if _gap > 0 else "▼"

            # ── Key metrics ──────────────────────────────────────────────────
            _pm1, _pm2, _pm3, _pm4, _pm5, _pm6 = st.columns(6)
            _pm1.metric("Sensex (Live)",  f"{_pr['spot']:,.2f}")
            _pm2.metric("Max Pain",       f"{_pr['max_pain']:,.0f}",
                        f"{_dir_a} {abs(_gap):,.0f} pts to target")
            _pm3.metric("PCR",            f"{_pr['pcr']:.2f}",
                        "Bullish" if _pr['pcr'] >= 1.2 else ("Bearish" if _pr['pcr'] < 0.8 else "Neutral"))
            _pm4.metric("ATM Strike",     f"{_pr['atm']:,.0f}")
            _pm5.metric("CE Wall (Res.)", f"{_pr['ce_wall']:,.0f}")
            _pm6.metric("PE Wall (Sup.)", f"{_pr['pe_wall']:,.0f}")

            # ── Direction signal ─────────────────────────────────────────────
            st.markdown(
                f"""<div style="background:rgba({('0,212,170' if _gap > 0 else '248,81,73')},0.1);
                border:1.5px solid {_dir_c};border-radius:10px;
                padding:14px 20px;margin:10px 0 16px;">
                <div style="font-size:1.4rem;font-weight:800;color:{_dir_c};">
                    {_dir_a} {_pr['direction']}
                </div>
                <div style="color:#8b949e;font-size:0.85rem;margin-top:4px;">
                    Sensex needs to move <strong style="color:{_dir_c};">
                    {abs(_gap):,.0f} pts</strong>
                    ({abs(_gap)/_pr['spot']*100:.2f}%) to reach Max Pain
                    <strong style="color:#e6edf3;">{_pr['max_pain']:,.0f}</strong>
                    by expiry <strong style="color:#e6edf3;">{_pr['expiry']}</strong>
                </div></div>""",
                unsafe_allow_html=True,
            )

            # ── Rocket candidates table ───────────────────────────────────────
            _rkt = _pr.get("rockets_df", pd.DataFrame())
            if _rkt.empty:
                st.info("No options found with ≥200% estimated move to Max Pain. "
                        "Market may already be very close to Max Pain.")
            else:
                st.markdown(
                    f"**{len(_rkt)} option(s)** you can buy TODAY that would move "
                    f"significantly if Sensex expires at Max Pain ({_pr['max_pain']:,.0f}):"
                )

                def _pre_type_style(val):
                    return ("color:#f85149;font-weight:700" if val == "CE"
                            else "color:#00d4aa;font-weight:700")

                def _pre_pct_style(val):
                    try:
                        v = float(val)
                        if v >= 1000: return "color:#00d4aa;font-weight:800;font-size:1.05rem"
                        if v >= 500:  return "color:#58d68d;font-weight:700"
                        if v >= 200:  return "color:#f0b429;font-weight:600"
                    except Exception:
                        pass
                    return ""

                _styled_pre = (
                    _rkt.style
                    .map(_pre_type_style,  subset=["Type"])
                    .map(_pre_pct_style,   subset=["Est. % Move"])
                    .format({
                        "Strike":          "{:,}",
                        "Entry (LTP)":     "₹{:.2f}",
                        "Exit @ Max Pain": "₹{:.2f}",
                        "Est. % Move":     "{:,.1f}%",
                    })
                )
                st.dataframe(_styled_pre, use_container_width=True, hide_index=True)
                st.download_button(
                    "⬇️ Export Rocket Candidates CSV",
                    _rkt.to_csv(index=False).encode("utf-8"),
                    "sensex_preexpiry_rockets.csv", "text/csv",
                    key="dl_pre_rockets",
                )

            st.caption(
                "⚠️ Exit price = intrinsic value at Max Pain. Actual exit depends on where "
                "Sensex closes at expiry — this is a probability-based estimate, not a guarantee."
            )

    st.divider()

    # ── POSITION BUILDUP RADAR ────────────────────────────────────────────────
    st.markdown("##### 🔭 Position Buildup Radar — Detect Accumulation Before the Move")
    st.markdown(
        '<small style="color:#8b949e;">'
        "Live Volume/OI analysis on near-ATM SENSEX options · "
        "<b>Vol/OI ratio &gt; 0.3</b> = fresh positions opened today (not old OI) · "
        "<b>High volume + tiny LTP move</b> = stealth accumulation (smart money entering quietly) · "
        "Near-ATM CE vs PE volume imbalance reveals likely direction · Requires Kite"
        "</small>",
        unsafe_allow_html=True,
    )

    if not _em_kite_live:
        st.warning("⚡ Connect Zerodha Kite in the sidebar to enable Position Buildup Radar.")
    else:
        _bd_col1, _bd_col2 = st.columns([5, 1])
        _run_buildup = _bd_col2.button(
            "🔭 Scan Buildup", type="primary",
            use_container_width=True, key="run_buildup",
        )
        if _run_buildup:
            with st.spinner("Scanning live OI + volume for position buildup…"):
                try:
                    _bd_result = run_oi_buildup_scanner(_em_kite_key, _em_kite_token)
                    st.session_state["oi_buildup"] = _bd_result
                except Exception as _bde:
                    st.error(f"Buildup scan failed: {_bde}")

        if "oi_buildup" in st.session_state:
            _bd      = st.session_state["oi_buildup"]
            _bd_dir  = _bd["direction"]
            _bd_dir_c = (
                "#00d4aa" if "BULLISH" in _bd_dir
                else "#f85149" if "BEARISH" in _bd_dir
                else "#e6b800"
            )
            _bd_dir_rgb = (
                "0,212,170" if "BULLISH" in _bd_dir
                else "248,81,73" if "BEARISH" in _bd_dir
                else "230,184,0"
            )

            # ── Key metrics ──────────────────────────────────────────────────
            _bd_m1, _bd_m2, _bd_m3, _bd_m4, _bd_m5 = st.columns(5)
            _bd_m1.metric("Sensex (Live)",         f"{_bd['spot']:,.2f}")
            _bd_m2.metric("ATM Strike",             f"{int(_bd['atm']):,}")
            _bd_m3.metric("CE Vol (±5 strikes)",    f"{int(_bd['ce_vol_total']):,}")
            _bd_m4.metric("PE Vol (±5 strikes)",    f"{int(_bd['pe_vol_total']):,}")
            _bd_m5.metric(
                "CE/PE Vol Ratio",
                f"{_bd['vol_ratio']:.2f}",
                "Bullish" if _bd["vol_ratio"] > 1.3 else (
                    "Bearish" if _bd["vol_ratio"] < 0.7 else "Neutral"
                ),
            )

            # ── Direction signal card ─────────────────────────────────────────
            st.markdown(
                f"""<div style="background:rgba({_bd_dir_rgb},0.1);
                border:1.5px solid {_bd_dir_c};border-radius:10px;
                padding:14px 20px;margin:10px 0 16px;">
                <div style="font-size:1.4rem;font-weight:800;color:{_bd_dir_c};">
                    {_bd_dir}
                </div>
                <div style="color:#8b949e;font-size:0.85rem;margin-top:4px;">
                    Near-ATM CE Volume: <strong style="color:#f85149;">{int(_bd['ce_vol_total']):,}</strong>
                    &nbsp;·&nbsp;
                    Near-ATM PE Volume: <strong style="color:#00d4aa;">{int(_bd['pe_vol_total']):,}</strong>
                    &nbsp;·&nbsp;
                    Expiry: <strong style="color:#e6edf3;">{_bd['expiry']}</strong>
                </div></div>""",
                unsafe_allow_html=True,
            )

            # ── Hot zones table ───────────────────────────────────────────────
            _hz = _bd.get("hot_zones", pd.DataFrame())
            if not _hz.empty:
                st.markdown("**🔥 Hot Buildup Zones — Ranked by Accumulation Score**")
                st.caption(
                    "Buildup Score ≥ 6 = very hot zone  ·  "
                    "Stealth = high volume + small price move  ·  "
                    "Fresh = high Vol/OI ratio (new positions today)"
                )

                def _hz_score_style(val):
                    try:
                        v = int(val)
                        if v >= 6: return "color:#00d4aa;font-weight:800"
                        if v >= 4: return "color:#58d68d;font-weight:700"
                        if v >= 2: return "color:#f0b429;font-weight:600"
                    except Exception:
                        pass
                    return ""

                def _hz_netvol_style(val):
                    try:
                        v = float(val)
                        return ("color:#f85149;font-weight:600" if v > 0
                                else "color:#00d4aa;font-weight:600")
                    except Exception:
                        return ""

                def _hz_voloi_style(val):
                    try:
                        v = float(val)
                        if v > 0.30: return "color:#00d4aa;font-weight:700"
                        if v > 0.15: return "color:#f0b429;font-weight:600"
                    except Exception:
                        pass
                    return ""

                _hz_styled = (
                    _hz.style
                    .map(_hz_score_style,  subset=["Buildup Score"])
                    .map(_hz_netvol_style, subset=["Net Vol (C-P)"])
                    .map(_hz_voloi_style,  subset=["CE Vol/OI", "PE Vol/OI"])
                    .format({
                        "Strike":        "{:,.0f}",
                        "ATM Dist":      "{:.0f} pts",
                        "CE OI":         "{:,}",
                        "CE Vol":        "{:,}",
                        "CE Vol/OI":     "{:.3f}",
                        "CE LTP":        "₹{:.1f}",
                        "CE LTP Chg %":  "{:+.2f}%",
                        "PE OI":         "{:,}",
                        "PE Vol":        "{:,}",
                        "PE Vol/OI":     "{:.3f}",
                        "PE LTP":        "₹{:.1f}",
                        "PE LTP Chg %":  "{:+.2f}%",
                        "Net Vol (C-P)": "{:+,}",
                        "Buildup Score": "{:d}",
                    })
                )
                st.dataframe(_hz_styled, use_container_width=True, hide_index=True)

            # ── Charts ────────────────────────────────────────────────────────
            _bd_plot = _bd.get("chain_df", pd.DataFrame())
            if not _bd_plot.empty:
                _bd_plot = _bd_plot[_bd_plot["ATM Dist"] <= 1000].copy()
                if not _bd_plot.empty:
                    _str_x  = _bd_plot["Strike"].astype(int).astype(str).tolist()
                    _atm_x  = str(int(_bd["atm"]))

                    _bc1, _bc2 = st.columns(2)

                    with _bc1:
                        st.markdown("**CE vs PE Volume near ATM**")
                        _fig_vol = go.Figure()
                        _fig_vol.add_trace(go.Bar(
                            x=_str_x, y=_bd_plot["CE Vol"].tolist(),
                            name="CE Volume", marker_color="#f85149", opacity=0.85,
                        ))
                        _fig_vol.add_trace(go.Bar(
                            x=_str_x, y=_bd_plot["PE Vol"].tolist(),
                            name="PE Volume", marker_color="#00d4aa", opacity=0.85,
                        ))
                        if _atm_x in _str_x:
                            _fig_vol.add_vline(x=_atm_x, line_dash="dash", line_color="#00d4aa")
                            _fig_vol.add_annotation(
                                x=_atm_x, y=1, yref="paper", text="ATM",
                                font=dict(color="#00d4aa", size=11),
                                showarrow=False, yanchor="bottom",
                            )
                        _fig_vol.update_layout(
                            barmode="group", template="plotly_dark",
                            plot_bgcolor="#131722", paper_bgcolor="#131722",
                            height=300, margin=dict(t=10, b=50, l=10, r=10),
                            legend=dict(orientation="h", y=1.05, x=0,
                                        font=dict(color="#c9d1d9"),
                                        bgcolor="rgba(0,0,0,0)"),
                            xaxis=dict(tickfont=dict(color="#8b949e", size=9),
                                       showgrid=False, tickangle=-45),
                            yaxis=dict(tickfont=dict(color="#8b949e"),
                                       gridcolor="#2a2e39", title="Contracts"),
                        )
                        st.plotly_chart(_fig_vol, use_container_width=True)

                    with _bc2:
                        st.markdown("**Vol/OI Ratio — Freshness Indicator**")
                        _fig_ratio = go.Figure()
                        _fig_ratio.add_trace(go.Bar(
                            x=_str_x, y=_bd_plot["CE Vol/OI"].tolist(),
                            name="CE Vol/OI", marker_color="#f85149", opacity=0.85,
                        ))
                        _fig_ratio.add_trace(go.Bar(
                            x=_str_x, y=_bd_plot["PE Vol/OI"].tolist(),
                            name="PE Vol/OI", marker_color="#00d4aa", opacity=0.85,
                        ))
                        _fig_ratio.add_hline(
                            y=0.3, line_dash="dot", line_color="#f0b429",
                            annotation_text="High Freshness (0.3)",
                            annotation_font=dict(color="#f0b429", size=10),
                            annotation_position="right",
                        )
                        if _atm_x in _str_x:
                            _fig_ratio.add_vline(x=_atm_x, line_dash="dash",
                                                 line_color="#00d4aa")
                        _fig_ratio.update_layout(
                            barmode="group", template="plotly_dark",
                            plot_bgcolor="#131722", paper_bgcolor="#131722",
                            height=300, margin=dict(t=10, b=50, l=10, r=10),
                            legend=dict(orientation="h", y=1.05, x=0,
                                        font=dict(color="#c9d1d9"),
                                        bgcolor="rgba(0,0,0,0)"),
                            xaxis=dict(tickfont=dict(color="#8b949e", size=9),
                                       showgrid=False, tickangle=-45),
                            yaxis=dict(tickfont=dict(color="#8b949e"),
                                       gridcolor="#2a2e39", title="Vol / OI",
                                       title_font=dict(color="#8b949e")),
                        )
                        st.plotly_chart(_fig_ratio, use_container_width=True)

            # ── How to read legend ────────────────────────────────────────────
            st.markdown(
                """<div style="background:#1e222d;border:1px solid #2a2e39;
                border-radius:8px;padding:14px 18px;margin-top:6px;">
                <div style="color:#00d4aa;font-size:0.73rem;font-weight:700;
                text-transform:uppercase;letter-spacing:1px;margin-bottom:8px;">
                📖 How to Read This</div>
                <div style="color:#8b949e;font-size:0.83rem;line-height:1.8;">
                <b style="color:#c9d1d9;">Vol/OI &gt; 0.30</b> — More than 30% of existing OI traded today → fresh position opening, active accumulation<br>
                <b style="color:#c9d1d9;">High Vol + Small LTP Move (&lt;5%)</b> — Stealth accumulation: large activity with no price reaction → smart money absorbing supply quietly<br>
                <b style="color:#c9d1d9;">CE Vol &gt;&gt; PE Vol near ATM</b> — Aggressive call buying / put writing → bulls expect a rally<br>
                <b style="color:#c9d1d9;">PE Vol &gt;&gt; CE Vol near ATM</b> — Aggressive put buying / call writing → bears expect a fall<br>
                <b style="color:#c9d1d9;">Buildup Score ≥ 6</b> — Very high confidence accumulation zone → watch this strike for an explosive move
                </div></div>""",
                unsafe_allow_html=True,
            )

    st.divider()

    # ── PATTERN ANALYSIS (9:15 AM → 3:30 PM) ─────────────────────────────────
    st.markdown("##### 📈 Expiry Day Pattern Analysis — 9:15 AM to 3:30 PM (Last 4 Weeks)")
    st.markdown(
        '<small style="color:#8b949e;">'
        "Full-day 5-min Sensex candles for last 4 expiry Fridays · "
        "Finds recurring intraday patterns: when does the big move happen? "
        "Which session dominates? Does the final hour reverse or continue the trend?"
        "</small>",
        unsafe_allow_html=True,
    )

    _pat_col1, _pat_col2 = st.columns([5, 1])
    _run_pat = _pat_col2.button("📊 Analyse Patterns", type="primary",
                                use_container_width=True, key="run_pattern")
    if _run_pat:
        with st.spinner("Fetching 4 expiry Fridays of intraday data…"):
            try:
                _pat = analyze_expiry_day_patterns(
                    n_weeks=4,
                    api_key=_em_kite_key,
                    access_token=_em_kite_token,
                )
                st.session_state["expiry_patterns"] = _pat
            except Exception as _pe:
                st.error(f"Pattern analysis failed: {_pe}")

    if "expiry_patterns" in st.session_state:
        _pat = st.session_state["expiry_patterns"]
        _days       = _pat.get("days", [])
        _pattern_df = _pat.get("pattern_df", pd.DataFrame())
        _slot_df    = _pat.get("slot_df",    pd.DataFrame())
        _session_df = _pat.get("session_df", pd.DataFrame())

        if not _days:
            st.warning("No intraday data found for the last 4 expiry Fridays.")
        else:
            # ── 1. Day summary table ──────────────────────────────────────────
            st.markdown("**Day-level summary**")

            def _pat_dir_style(val):
                if "▲" in str(val): return "color:#00d4aa;font-weight:700"
                if "▼" in str(val): return "color:#f85149;font-weight:700"
                return ""

            def _pat_move_style(val):
                try:
                    v = float(str(val).replace("%","").replace("+",""))
                    return ("color:#00d4aa;font-weight:600" if v > 0
                            else "color:#f85149;font-weight:600" if v < 0 else "")
                except Exception:
                    return ""

            _pat_show_cols = ["Date","Open","Close","Day Move (pts)","Day Move %",
                              "Day Direction","Day Range (pts)","High set at",
                              "Low set at","Final Hr Range","Final Hr % of Day"]
            _pat_show = [c for c in _pat_show_cols if c in _pattern_df.columns]
            _pat_styled = (_pattern_df[_pat_show].style
                           .map(_pat_dir_style, subset=["Day Direction"])
                           .map(_pat_move_style, subset=["Day Move %"])
                           .format({"Day Move %": "{:+.3f}%",
                                    "Final Hr % of Day": "{:.1f}%"}))
            st.dataframe(_pat_styled, use_container_width=True, hide_index=True)

            # ── 2. Normalised overlay chart ───────────────────────────────────
            st.markdown("**Normalised intraday chart — all 4 expiry Fridays (open = 0%)**")
            _colors = ["#00d4aa", "#f85149", "#f0b429", "#82aaff"]
            _fig_norm = go.Figure()
            for _i, _d in enumerate(_days):
                _norm_s = _d["norm"]
                _ts     = [t.strftime("%H:%M") for t in _norm_s.index]
                _fig_norm.add_trace(go.Scatter(
                    x=_ts, y=_norm_s.values,
                    name=_d["label"],
                    line=dict(color=_colors[_i % len(_colors)], width=2),
                    mode="lines",
                ))
            _fig_norm.add_hline(y=0, line_dash="dash", line_color="#4a5568", line_width=1)
            # Shade final hour
            _fig_norm.add_vrect(x0="14:15", x1="15:30",
                                fillcolor="rgba(0,212,170,0.06)",
                                layer="below", line_width=0,
                                annotation_text="Final Hour",
                                annotation_position="top left",
                                annotation_font=dict(color="#00d4aa", size=11))
            _fig_norm.update_layout(
                template="plotly_dark", plot_bgcolor="#131722",
                paper_bgcolor="#131722", height=380,
                margin=dict(t=20, b=50, l=10, r=10),
                xaxis=dict(tickfont=dict(color="#8b949e", size=9),
                           showgrid=False, tickangle=-45,
                           tickvals=["09:15","10:15","11:15","12:15",
                                     "13:15","14:15","15:15","15:30"]),
                yaxis=dict(tickfont=dict(color="#8b949e"),
                           gridcolor="#2a2e39", title="Move from Open (%)",
                           title_font=dict(color="#8b949e")),
                legend=dict(orientation="h", y=1.05, x=0,
                            font=dict(color="#c9d1d9"),
                            bgcolor="rgba(0,0,0,0)"),
            )
            st.plotly_chart(_fig_norm, use_container_width=True)

            # ── 3. Hourly slot analysis ───────────────────────────────────────
            if not _slot_df.empty:
                _pcol1, _pcol2 = st.columns(2)

                with _pcol1:
                    st.markdown("**Average move % per hour slot (all expiry days)**")
                    _slot_colors = [
                        "#00d4aa" if float(v) > 0 else "#f85149"
                        for v in _slot_df["Avg Move %"]
                    ]
                    _fig_slot = go.Figure(go.Bar(
                        x=_slot_df["Slot"].tolist(),
                        y=_slot_df["Avg Move %"].round(3).tolist(),
                        marker_color=_slot_colors,
                        text=[f"{v:+.3f}%" for v in _slot_df["Avg Move %"]],
                        textposition="outside",
                        textfont=dict(color="#c9d1d9", size=10),
                    ))
                    _fig_slot.update_layout(
                        template="plotly_dark", plot_bgcolor="#131722",
                        paper_bgcolor="#131722", height=300,
                        margin=dict(t=10, b=60, l=10, r=10),
                        xaxis=dict(tickfont=dict(color="#8b949e", size=9),
                                   showgrid=False, tickangle=-30),
                        yaxis=dict(tickfont=dict(color="#8b949e"),
                                   gridcolor="#2a2e39"),
                    )
                    st.plotly_chart(_fig_slot, use_container_width=True)

                with _pcol2:
                    st.markdown("**Average range % per hour (volatility by slot)**")
                    _fig_rng = go.Figure(go.Bar(
                        x=_slot_df["Slot"].tolist(),
                        y=_slot_df["Avg Range %"].round(3).tolist(),
                        marker_color="#f0b429",
                        text=[f"{v:.3f}%" for v in _slot_df["Avg Range %"]],
                        textposition="outside",
                        textfont=dict(color="#c9d1d9", size=10),
                    ))
                    _fig_rng.update_layout(
                        template="plotly_dark", plot_bgcolor="#131722",
                        paper_bgcolor="#131722", height=300,
                        margin=dict(t=10, b=60, l=10, r=10),
                        xaxis=dict(tickfont=dict(color="#8b949e", size=9),
                                   showgrid=False, tickangle=-30),
                        yaxis=dict(tickfont=dict(color="#8b949e"),
                                   gridcolor="#2a2e39"),
                    )
                    st.plotly_chart(_fig_rng, use_container_width=True)

                # Slot table with up/down count
                _slot_show = _slot_df.copy()
                _slot_show["Avg Move %"]  = _slot_show["Avg Move %"].round(3)
                _slot_show["Avg Range %"] = _slot_show["Avg Range %"].round(3)
                st.dataframe(_slot_show, use_container_width=True, hide_index=True)

            # ── 4. Key pattern observations ───────────────────────────────────
            if not _pattern_df.empty:
                st.divider()
                st.markdown("**🔍 Pattern Observations**")

                _n        = len(_pattern_df)
                _bull     = (_pattern_df["Day Direction"].str.contains("▲")).sum()
                _bear     = _n - _bull
                _fin_avg  = _pattern_df["Final Hr % of Day"].mean()
                _fin_max  = _pattern_df["Final Hr % of Day"].max()
                _high_pm  = (_pattern_df["High set at"] >= "14:00").sum()
                _low_pm   = (_pattern_df["Low set at"]  >= "14:00").sum()

                _obs_cols = st.columns(4)
                _obs_cols[0].metric("Bullish days",  f"{_bull}/{_n}",
                                    "▲ bias" if _bull > _bear else "▼ bias")
                _obs_cols[1].metric("Bearish days",  f"{_bear}/{_n}")
                _obs_cols[2].metric("Avg final-hour % of day range",
                                    f"{_fin_avg:.1f}%", f"max {_fin_max:.1f}%")
                _obs_cols[3].metric("Day High/Low set after 2PM",
                                    f"{max(_high_pm, _low_pm)}/{_n} days")

                # Natural language observations
                _obs = []
                if _fin_avg > 30:
                    _obs.append(f"🔥 **Final hour dominates** — on average **{_fin_avg:.0f}%** of the day's range is made in the 2:15–3:30 PM session.")
                if _bull >= 3:
                    _obs.append(f"📈 **Bullish bias on expiry** — {_bull} out of {_n} recent expiry Fridays closed higher than they opened.")
                elif _bear >= 3:
                    _obs.append(f"📉 **Bearish bias on expiry** — {_bear} out of {_n} recent expiry Fridays closed lower than they opened.")
                if _high_pm >= 2 or _low_pm >= 2:
                    _obs.append(f"⏰ **Extremes set late** — day's high or low was set after 2 PM on {max(_high_pm,_low_pm)}/{_n} days, confirming the final-hour dominance.")
                if not _slot_df.empty:
                    _max_vol_slot = _slot_df.loc[_slot_df["Avg Range %"].idxmax(), "Slot"]
                    _obs.append(f"⚡ **Most volatile slot**: {_max_vol_slot} has the highest average range across all 4 expiry days.")

                for _o in _obs:
                    st.markdown(f"- {_o}")

    st.divider()

    # ── HISTORICAL SCAN (past 5 weeks) ────────────────────────────────────────
    st.markdown("##### 📅 Historical Expiry Analysis — Past 5 Weeks")
    st.markdown(
        '<small style="color:#8b949e;">'
        "Index data via yfinance (^BSESN) · Option prices are <b>estimated</b> using an expiry-day model "
        "(intrinsic value + decaying time premium) · Kite credentials unlock actual option candle data "
        "for any expiry still in the BFO master"
        "</small>",
        unsafe_allow_html=True,
    )

    with st.container(border=True):
        emc1, emc2, emc3 = st.columns([2, 2, 2])
        em_weeks   = emc1.slider("Expiry weeks to scan", 1, 5, 5, key="em_weeks")
        em_thresh  = emc2.slider("Min % move threshold", 200, 2000, 500, step=100, key="em_thresh")
        run_em_btn = emc3.button(
            "🔍 Run Expiry Analysis", type="primary",
            use_container_width=True, key="run_em",
        )
        if not _em_kite_live:
            emc1.caption("💡 Zerodha Kite optional — index data uses yfinance")
        else:
            emc1.caption("⚡ Kite connected — will also try actual BFO option candles")

    _em_fridays = get_sensex_expiry_fridays(5)
    _em_friday_strs = "  ·  ".join(d.strftime("%d %b") for d in _em_fridays)
    st.caption(f"📅 Scanning expiry dates: {_em_friday_strs}")

    if run_em_btn:
        _em_prog = st.progress(0, text="Starting Sensex expiry analysis…")
        try:
            _em_summary, _em_weekly, _em_actual = run_sensex_option_moves_scan(
                n_weeks=em_weeks,
                pct_threshold=float(em_thresh),
                api_key=_em_kite_key,
                access_token=_em_kite_token,
                progress_cb=lambda p, m: _em_prog.progress(p, text=m),
            )
            st.session_state["em_summary"] = _em_summary
            st.session_state["em_weekly"]  = _em_weekly
            st.session_state["em_actual"]  = _em_actual
            st.session_state["em_thresh"]  = em_thresh
        except Exception as _eme:
            st.error(f"Scan failed: {_eme}")
        _em_prog.empty()

    if "em_summary" in st.session_state:
        em_sum_df: pd.DataFrame = st.session_state["em_summary"]
        em_weekly_data          = st.session_state.get("em_weekly", [])
        em_actual_df: pd.DataFrame = st.session_state.get("em_actual", pd.DataFrame())
        em_thresh_used          = st.session_state.get("em_thresh", 500)

        # ── Summary table ─────────────────────────────────────────────────────
        st.markdown("##### 📊 Sensex Movement — 2:15 PM to 3:15 PM on Each Expiry")

        def _em_dir_style(val: str) -> str:
            if "▲" in str(val): return "color:#00d4aa;font-weight:700"
            if "▼" in str(val): return "color:#f85149;font-weight:700"
            return ""

        def _em_move_style(val: str) -> str:
            try:
                v = float(str(val).replace("%", "").replace("+", ""))
                if v > 0: return "color:#00d4aa;font-weight:600"
                if v < 0: return "color:#f85149;font-weight:600"
            except Exception:
                pass
            return ""

        styled_sum = (em_sum_df.style
                      .map(_em_dir_style, subset=["Direction"])
                      .map(_em_move_style, subset=["Pts Move", "% Move"]))
        st.dataframe(styled_sum, use_container_width=True, hide_index=True)

        st.divider()

        # ── Per-date detail ───────────────────────────────────────────────────
        st.markdown(f"##### 🎯 Estimated Rocket Options (≥{em_thresh_used}% move) — Per Expiry")
        st.markdown(
            '<small style="color:#6e7681;">Option prices at 2:15 PM are <i>estimated</i> '
            "using an expiry-day model (intrinsic value + decaying time premium). "
            "The move % reflects what an option buyer holding from 2:15 PM to 3:15 PM would have seen. "
            "Actual prices depend on IV, supply/demand, and exact Sensex level at 2:15 PM.</small>",
            unsafe_allow_html=True,
        )

        for wk in em_weekly_data:
            if "error" in wk and "spot_open" not in wk:
                with st.expander(f"📅 {wk['date'].strftime('%d %b %Y (%A)')} — ⚠️ No data", expanded=False):
                    st.warning(wk.get("error", "Data unavailable"))
                continue

            date_label  = wk["date"].strftime("%d %b %Y (%A)")
            pts_move    = wk.get("pts_move", 0)
            pct_move    = wk.get("pct_move", 0)
            rocket_df: pd.DataFrame = wk.get("rocket_df", pd.DataFrame())
            move_arrow  = "▲" if pts_move > 0 else "▼"
            move_color  = "#00d4aa" if pts_move > 0 else "#f85149"

            expander_label = (
                f"📅 {date_label}  |  "
                f"Sensex {wk['spot_open']:,.2f} → {wk['spot_close']:,.2f}  "
                f"({move_arrow} {abs(pts_move):,.2f} pts / {abs(pct_move):.3f}%)  |  "
                f"{'🚀 ' + str(len(rocket_df)) + ' rocket option(s)' if not rocket_df.empty else 'No 500%+ options'}"
            )

            with st.expander(expander_label, expanded=(not rocket_df.empty)):
                ic1, ic2, ic3, ic4, ic5 = st.columns(5)
                ic1.metric("Sensex @ 2:15", f"{wk['spot_open']:,.2f}")
                ic2.metric("Sensex @ 3:15", f"{wk['spot_close']:,.2f}",
                           f"{'+' if pts_move >= 0 else ''}{pts_move:,.2f} pts",
                           delta_color="normal" if pts_move >= 0 else "inverse")
                ic3.metric("High in window", f"{wk['spot_high']:,.2f}")
                ic4.metric("Low in window",  f"{wk['spot_low']:,.2f}")
                ic5.metric("ATM at 2:15",    f"{wk['atm']:,}")

                if rocket_df.empty:
                    st.info(
                        f"No estimated option moves ≥{em_thresh_used}% for this expiry. "
                        f"Sensex only moved {abs(pts_move):.0f} pts — not enough to generate "
                        "large OTM option moves in this window."
                    )
                else:
                    st.markdown(
                        f'<span style="color:{move_color};font-weight:700;font-size:1.1rem;">'
                        f"{move_arrow} {'RALLY' if pts_move > 0 else 'SELL-OFF'}: "
                        f"+{abs(pts_move):,.0f} pts ({abs(pct_move):.3f}%) in final hour"
                        "</span>",
                        unsafe_allow_html=True,
                    )
                    st.markdown(
                        f"**{len(rocket_df)} option(s)** estimated to move ≥{em_thresh_used}% "
                        f"buying from 2:15 PM to 3:15 PM:"
                    )

                    def _em_type_style(val: str) -> str:
                        return ("color:#f85149;font-weight:700" if val == "CE"
                                else "color:#00d4aa;font-weight:700")

                    def _em_pct_style(val) -> str:
                        try:
                            v = float(val)
                            if v >= 1000: return "color:#00d4aa;font-weight:800;font-size:1.05rem"
                            if v >= 500:  return "color:#58d68d;font-weight:700"
                        except Exception:
                            pass
                        return ""

                    styled_rockets = (
                        rocket_df.style
                        .map(_em_type_style, subset=["Type"])
                        .map(_em_pct_style, subset=["Est. % Move"])
                        .format({
                            "Strike":          "{:,}",
                            "Est. @ 2:15 PM":  "₹{:.2f}",
                            "Est. @ 3:15 PM":  "₹{:.2f}",
                            "Est. % Move":     "{:,.1f}%",
                        })
                    )
                    st.dataframe(styled_rockets, use_container_width=True, hide_index=True)

                    csv_rkt = rocket_df.to_csv(index=False).encode("utf-8")
                    st.download_button(
                        f"⬇️ Export {date_label} CSV",
                        csv_rkt,
                        f"sensex_rockets_{wk['date'].strftime('%Y%m%d')}.csv",
                        "text/csv",
                        key=f"dl_rkt_{wk['date']}",
                    )

        # ── Actual Kite data (if any) ─────────────────────────────────────────
        if not em_actual_df.empty:
            st.divider()
            st.markdown("##### ⚡ Actual Option Data from Kite BFO (live candles)")
            st.success(
                f"Found **{len(em_actual_df)} option(s)** with ≥{em_thresh_used}% "
                "actual price move from Kite BFO instruments master."
            )

            def _act_type_style(val: str) -> str:
                return "color:#f85149;font-weight:700" if val == "CE" else "color:#00d4aa;font-weight:700"

            styled_actual = (
                em_actual_df.style
                .map(_act_type_style, subset=["Type"])
                .format({
                    "Strike":      "{:,}",
                    "Open @ 2:15": "₹{:.2f}",
                    "Peak":        "₹{:.2f}",
                    "% Move":      "{:,.1f}%",
                    "Volume":      "{:,}",
                })
            )
            st.dataframe(styled_actual, use_container_width=True, hide_index=True)
            csv_act = em_actual_df.to_csv(index=False).encode("utf-8")
            st.download_button("⬇️ Export Actual Data CSV", csv_act,
                               "sensex_actual_movers.csv", "text/csv", key="dl_em_actual")

        elif _em_kite_live and "em_summary" in st.session_state:
            st.divider()
            st.caption(
                "ℹ️ No actual option data found in Kite BFO master for past expiry dates — "
                "expired options are removed from the instruments master within 1–2 days. "
                "The estimated moves above are based on Sensex index candles."
            )

    else:
        st.info(
            "🔎 Click **Run Expiry Analysis** to scan the last 5 Sensex expiry Fridays.\n\n"
            "**What this shows:**\n"
            "- Sensex index movement between 2:15 PM and 3:15 PM on each expiry Friday\n"
            "- Which CE/PE options (estimated) would have moved ≥500% in that window\n"
            "- The 'rocket' options are typically near-OTM strikes that go deep ITM "
            "during a sharp final-hour move\n\n"
            "⚡ *Zerodha Kite credentials are optional but unlock actual BFO option candle data "
            "for any expiry still in the instruments master.*"
        )


# ── TAB 9 — Trending OI ───────────────────────────────────────────────────────

def _build_toi_html(rows: list) -> str:
    """Build NiftyTrader-style Trending OI HTML table from computed row list."""
    if not rows:
        return "<p style='color:#6e7681;padding:12px;'>No data yet — click Snapshot Now or enable Auto-refresh.</p>"

    hdr = (
        '<div class="oc-wrap"><table class="oc-tbl">'
        '<thead><tr>'
        '<th class="c">TIME</th>'
        '<th class="r">SPOT</th>'
        '<th class="r">CALLS CHNG OI</th>'
        '<th class="r">PUTS CHNG OI</th>'
        '<th class="r">DIFF. IN OI</th>'
        '<th class="c">DIFF %</th>'
        '<th class="c">DIR OF CHNG</th>'
        '<th class="r">CHNG IN DIR</th>'
        '<th class="c">PCR</th>'
        '<th class="c">COI PCR</th>'
        '<th class="c">VOL PCR</th>'
        '<th class="c">SENTIMENT</th>'
        '<th class="c">VERDICT</th>'
        '</tr></thead><tbody>'
    )

    rows_html = []
    for i, r in enumerate(reversed(rows)):
        is_latest = i == 0
        row_bg = ' style="background:rgba(0,212,170,0.06);"' if is_latest else ""

        dir_val = r["dir_chng"]
        dir_cls = "cup" if dir_val == "▲" else ("cdn" if dir_val == "▼" else "")

        sent     = r["sentiment"]
        sent_col = "#00d4aa" if sent == "Bullish" else ("#f85149" if sent == "Bearish" else "#e6b800")

        cid     = r["chng_in_dir"]
        cid_sgn = "+" if cid > 0 else ""
        cid_cls = "cup" if cid > 0 else ("cdn" if cid < 0 else "")

        dp      = r["diff_pct"]
        dp_cls  = "cup" if dp > 0 else ("cdn" if dp < 0 else "")

        # Verdict badge
        verdict = r.get("verdict", "—")
        reason  = r.get("verdict_reason", "").replace('"', "&quot;")
        if verdict == "GOOD":
            vbadge = (
                f'<span title="{reason}" style="background:rgba(0,212,170,0.18);'
                f'border:1px solid #00d4aa;color:#00d4aa;font-size:0.7rem;font-weight:800;'
                f'padding:2px 8px;border-radius:10px;letter-spacing:.5px;cursor:help;">GOOD ✓</span>'
            )
        elif verdict == "FAKE":
            vbadge = (
                f'<span title="{reason}" style="background:rgba(248,81,73,0.12);'
                f'border:1px solid rgba(248,81,73,0.5);color:#f85149;font-size:0.7rem;font-weight:700;'
                f'padding:2px 8px;border-radius:10px;letter-spacing:.5px;cursor:help;">FAKE ✗</span>'
            )
        else:
            vbadge = f'<span style="color:#4a5568;font-size:0.75rem;">—</span>'

        rows_html.append(
            f'<tr{row_bg}>'
            f'<td class="c">{r["time"]}</td>'
            f'<td class="r">{r["spot"]:,.2f}</td>'
            f'<td class="r">{toi_ind_fmt(r["ce_chng_oi"])}</td>'
            f'<td class="r">{toi_ind_fmt(r["pe_chng_oi"])}</td>'
            f'<td class="r">{toi_ind_fmt(r["diff_oi"])}</td>'
            f'<td class="c"><span class="{dp_cls}">{dp:+.1f}%</span></td>'
            f'<td class="c"><span class="{dir_cls}" style="font-size:1rem;">{dir_val}</span></td>'
            f'<td class="r"><span class="{cid_cls}">{cid_sgn}{toi_ind_fmt(cid)}</span></td>'
            f'<td class="c">{r["pcr"]:.3f}</td>'
            f'<td class="c">{r["coi_pcr"]:.3f}</td>'
            f'<td class="c">{r["vol_pcr"]:.3f}</td>'
            f'<td class="c"><span style="color:{sent_col};font-weight:700;">{sent}</span></td>'
            f'<td class="c">{vbadge}</td>'
            f'</tr>'
        )

    return hdr + "".join(rows_html) + "</tbody></table></div>"


def page_trending_oi():
    _toi_kite_key   = st.session_state.get("kite_api_key",      _get_secret("KITE_API_KEY", ""))
    _toi_kite_token = st.session_state.get("kite_access_token", _get_secret("KITE_ACCESS_TOKEN", ""))
    _toi_kite_live  = bool(_toi_kite_key and _toi_kite_token)

    st.markdown("#### 📡 Trending OI Data")
    st.markdown(
        '<small style="color:#8b949e;">'
        "Replicates NiftyTrader's Trending OI table · "
        "Polls Zerodha Kite every 1 / 3 / 5 / 15 min · "
        "OI change computed from day-start baseline snapshot · "
        "Works for NIFTY, BANKNIFTY, SENSEX, BANKEX, FINNIFTY, MIDCPNIFTY"
        "</small>",
        unsafe_allow_html=True,
    )

    if not _toi_kite_live:
        st.warning("⚡ Connect Zerodha Kite in the sidebar to enable Trending OI tracking.")
    else:
        # ── Controls ──────────────────────────────────────────────────────────
        tc1, tc2, tc3, tc4, tc5 = st.columns([2, 2, 2, 2, 2])

        _toi_symbol = tc1.selectbox("Index", TOI_SYMBOLS, key="toi_symbol",
                                    index=TOI_SYMBOLS.index("SENSEX"))
        _toi_iname  = tc2.selectbox("Interval", list(TOI_INTERVALS.keys()),
                                    key="toi_interval_name",
                                    index=list(TOI_INTERVALS.keys()).index("1 Min"))
        _toi_n      = tc3.slider("Strikes ±ATM", 3, 10, 5, key="toi_n_strikes")

        _toi_instr_key = f"toi_instr_{_toi_symbol}"
        _toi_instr_df  = st.session_state.get(_toi_instr_key)
        _toi_expiries  = toi_get_expiries(_toi_instr_df) if _toi_instr_df is not None else []
        _toi_exp_opts  = _toi_expiries if _toi_expiries else ["— initialize first —"]
        _toi_expiry    = tc4.selectbox("Expiry", _toi_exp_opts, key="toi_expiry",
                                       disabled=not _toi_expiries)

        _toi_init_btn = tc5.button("🚀 Initialize", type="primary",
                                   use_container_width=True, key="toi_init")

        ar1, ar2 = st.columns([3, 5])
        _toi_threshold = int(ar1.number_input(
            "OI Alert Threshold", min_value=10_000, max_value=10_000_000,
            value=500_000, step=50_000, key="toi_threshold",
        ))

        _toi_isec = TOI_INTERVALS[_toi_iname]

        # ── Initialize handler ────────────────────────────────────────────────
        if _toi_init_btn:
            with st.spinner(f"Downloading {_toi_symbol} instruments…"):
                try:
                    _instr = toi_fetch_instruments(_toi_kite_key, _toi_kite_token, _toi_symbol)
                    st.session_state[_toi_instr_key] = _instr
                    _toi_instr_df = _instr
                    _toi_expiries = toi_get_expiries(_instr)
                except Exception as _ie:
                    st.error(f"Instruments download failed: {_ie}")
                    st.stop()

            if not _toi_expiries:
                st.error("No upcoming expiries found for this symbol.")
                st.stop()

            _use_exp = _toi_expiry if (_toi_expiry and _toi_expiry in _toi_expiries) else _toi_expiries[0]

            with st.spinner("Taking day-start baseline snapshot…"):
                try:
                    _spot0   = toi_get_spot(_toi_kite_key, _toi_kite_token, _toi_symbol)
                    _strikes = toi_get_atm_strikes(_spot0, _toi_symbol, _toi_n)
                    _ds      = toi_fetch_snapshot(
                        _toi_kite_key, _toi_kite_token,
                        _toi_symbol, _use_exp, _strikes, _toi_instr_df,
                    )
                    st.session_state["toi_day_start"]    = _ds
                    st.session_state["toi_strikes"]      = _strikes
                    st.session_state["toi_init_symbol"]  = _toi_symbol
                    st.session_state["toi_init_expiry"]  = _use_exp
                    st.session_state["toi_rows"]         = []
                    st.session_state["toi_initialized"]  = True
                    st.session_state["toi_last_fetch"]   = 0.0
                    st.success(
                        f"✅ Initialized for {_toi_symbol} · {len(_strikes)} strikes · "
                        f"Expiry {_use_exp} · Spot baseline {_spot0:,.2f}. "
                        f"Enable Auto-refresh or click Snapshot Now to record the first row."
                    )
                    st.rerun()
                except Exception as _se:
                    st.error(f"Baseline snapshot failed: {_se}")
                    st.stop()

        # ── Live tracking section ─────────────────────────────────────────────
        _initialized = st.session_state.get("toi_initialized", False)
        _init_sym    = st.session_state.get("toi_init_symbol", "")

        if _initialized and _init_sym == _toi_symbol:
            # ── Auto-refresh widget — always on when initialized ───────────────
            if _HAS_AUTOREFRESH:
                _st_autorefresh(interval=_toi_isec * 1000, key="toi_ar")

            # ── Auto-snapshot on timer ────────────────────────────────────────
            _last_fetch = st.session_state.get("toi_last_fetch", 0.0)
            if time.time() - _last_fetch >= _toi_isec * 0.9:
                try:
                    _snap = toi_fetch_snapshot(
                        _toi_kite_key, _toi_kite_token,
                        _toi_symbol,
                        st.session_state["toi_init_expiry"],
                        st.session_state["toi_strikes"],
                        st.session_state.get(_toi_instr_key),
                    )
                    _prev = st.session_state.get("toi_rows", [])
                    _row  = toi_compute_row(_snap, st.session_state["toi_day_start"],
                                            _prev[-1] if _prev else None)
                    _prev.append(_row)
                    st.session_state["toi_rows"]       = _prev
                    st.session_state["toi_last_fetch"] = time.time()
                    _tg_tok  = st.session_state.get("toi_tg_token", "")
                    _tg_chat = st.session_state.get("toi_tg_chat", "")
                    # OI spike alerts (threshold-based)
                    _oi_alerts = toi_check_alerts(_row, _toi_threshold)
                    for _a in _oi_alerts:
                        st.toast(_a, icon="🚨")
                    if _oi_alerts and _tg_tok and _tg_chat:
                        toi_send_telegram("\n".join(_oi_alerts), _tg_tok, _tg_chat)
                    # GOOD verdict alert — actionable signal
                    if _row.get("verdict") == "GOOD":
                        _vmsg = (
                            f"✅ *GOOD move* at {_row['time']} — "
                            f"Spot {_row['spot']:,.2f} · "
                            f"Diff OI {toi_ind_fmt(_row['diff_oi'])} · "
                            f"{_row['verdict_reason']}"
                        )
                        st.toast(_vmsg, icon="✅")
                        if _tg_tok and _tg_chat:
                            toi_send_telegram(_vmsg, _tg_tok, _tg_chat)
                except Exception as _ae:
                    st.warning(f"Auto-snapshot failed: {_ae}")

            # ── Manual snapshot button ────────────────────────────────────────
            _snap_c1, _snap_c2, _snap_c3 = st.columns([5, 2, 2])
            _snap_c2.caption(
                f"Last: {int(time.time() - st.session_state.get('toi_last_fetch', time.time()))}s ago"
                if st.session_state.get("toi_last_fetch", 0) > 0 else "No snapshot yet"
            )
            _manual_snap = _snap_c3.button("📸 Snapshot Now", key="toi_manual",
                                           use_container_width=True)
            if _manual_snap:
                with st.spinner("Taking snapshot…"):
                    try:
                        _snap = toi_fetch_snapshot(
                            _toi_kite_key, _toi_kite_token,
                            _toi_symbol,
                            st.session_state["toi_init_expiry"],
                            st.session_state["toi_strikes"],
                            st.session_state.get(_toi_instr_key),
                        )
                        _prev = st.session_state.get("toi_rows", [])
                        _row  = toi_compute_row(_snap, st.session_state["toi_day_start"],
                                                _prev[-1] if _prev else None)
                        _prev.append(_row)
                        st.session_state["toi_rows"]       = _prev
                        st.session_state["toi_last_fetch"] = time.time()
                        _tg_tok  = st.session_state.get("toi_tg_token", "")
                        _tg_chat = st.session_state.get("toi_tg_chat", "")
                        # OI spike alerts (threshold-based)
                        _oi_alerts = toi_check_alerts(_row, _toi_threshold)
                        for _a in _oi_alerts:
                            st.toast(_a, icon="🚨")
                        if _oi_alerts and _tg_tok and _tg_chat:
                            toi_send_telegram("\n".join(_oi_alerts), _tg_tok, _tg_chat)
                        # GOOD verdict alert — actionable signal
                        if _row.get("verdict") == "GOOD":
                            _vmsg = (
                                f"✅ *GOOD move* at {_row['time']} — "
                                f"Spot {_row['spot']:,.2f} · "
                                f"Diff OI {toi_ind_fmt(_row['diff_oi'])} · "
                                f"{_row['verdict_reason']}"
                            )
                            st.toast(_vmsg, icon="✅")
                            if _tg_tok and _tg_chat:
                                toi_send_telegram(_vmsg, _tg_tok, _tg_chat)
                        st.rerun()
                    except Exception as _me:
                        st.error(f"Snapshot failed: {_me}")

            # ── Selected strikes chips ────────────────────────────────────────
            _stks = st.session_state.get("toi_strikes", [])
            _exp  = st.session_state.get("toi_init_expiry", "")
            st.markdown(
                f'<div style="margin:6px 0 12px;">'
                f'<span style="color:#6e7681;font-size:0.74rem;font-weight:700;'
                f'text-transform:uppercase;letter-spacing:1px;">Tracking Strikes ({_exp}): </span>'
                + "".join(
                    f'<span style="background:rgba(0,212,170,0.12);'
                    f'border:1px solid rgba(0,212,170,0.25);color:#00d4aa;'
                    f'font-size:0.71rem;font-weight:700;padding:2px 8px;'
                    f'border-radius:12px;margin:0 3px;">{s:,}</span>'
                    for s in sorted(_stks)
                )
                + "</div>",
                unsafe_allow_html=True,
            )

            # ── Trending OI table ─────────────────────────────────────────────
            _toi_rows = st.session_state.get("toi_rows", [])

            if _toi_rows:
                _lat = _toi_rows[-1]
                _sent_col = (
                    "#00d4aa" if _lat["sentiment"] == "Bullish"
                    else "#f85149" if _lat["sentiment"] == "Bearish"
                    else "#e6b800"
                )
                _lm1, _lm2, _lm3, _lm4, _lm5, _lm6 = st.columns(6)
                _lm1.metric("Spot",         f"{_lat['spot']:,.2f}", f"@ {_lat['time']}")
                _lm2.metric("PCR",          f"{_lat['pcr']:.3f}")
                _lm3.metric("COI PCR",      f"{_lat['coi_pcr']:.3f}")
                _lm4.metric("VOL PCR",      f"{_lat['vol_pcr']:.3f}")
                _lm5.metric("Diff OI",      toi_ind_fmt(_lat["diff_oi"]))
                _lm6.metric("Rows",         len(_toi_rows))

                st.markdown(
                    f'<div style="background:rgba({("0,212,170" if _lat["sentiment"]=="Bullish" else "248,81,73" if _lat["sentiment"]=="Bearish" else "230,184,0")},0.08);'
                    f'border:1.5px solid {_sent_col};border-radius:10px;'
                    f'padding:12px 20px;margin:10px 0 14px;">'
                    f'<span style="font-size:1.3rem;font-weight:800;color:{_sent_col};">'
                    f'{_lat["sentiment"].upper()} &nbsp;·&nbsp; '
                    f'<span style="font-size:0.9rem;font-weight:600;color:#c9d1d9;">'
                    f'Dir: <span style="color:{"#00d4aa" if _lat["dir_chng"]=="▲" else "#f85149" if _lat["dir_chng"]=="▼" else "#8b949e"};">{_lat["dir_chng"]}</span>'
                    f' &nbsp; Diff OI: {toi_ind_fmt(_lat["diff_oi"])} '
                    f'({_lat["diff_pct"]:+.1f}%)'
                    f'</span></span></div>',
                    unsafe_allow_html=True,
                )

                st.markdown(
                    '<span style="color:#6e7681;font-size:0.72rem;">Most recent row on top · '
                    'Highlighted row = latest snapshot</span>',
                    unsafe_allow_html=True,
                )
                st.markdown(_build_toi_html(_toi_rows), unsafe_allow_html=True)

                _toi_dl = pd.DataFrame([
                    {k: v for k, v in r.items() if k not in ("per_strike", "strike_deltas")}
                    for r in _toi_rows
                ])
                st.download_button(
                    "⬇️ Export CSV",
                    _toi_dl.to_csv(index=False).encode("utf-8"),
                    f"trending_oi_{_toi_symbol}.csv",
                    "text/csv",
                    key="dl_toi",
                )

                # ── Reset button ──────────────────────────────────────────────
                if st.button("🔄 Reset / New Day-Start Baseline", key="toi_reset"):
                    for _k in ("toi_initialized", "toi_day_start", "toi_rows",
                               "toi_last_fetch", "toi_init_symbol", "toi_init_expiry",
                               "toi_strikes"):
                        st.session_state.pop(_k, None)
                    st.rerun()

            else:
                st.info(
                    "📸 Click **Snapshot Now** above or enable **Auto-refresh** to collect the first data row.\n\n"
                    f"Tracking **{len(_stks)} strikes** around ATM for **{_toi_symbol}** expiry **{_exp}**."
                )

            # ── Telegram / alert settings ─────────────────────────────────────
            with st.expander("🔔 Telegram Alert Settings", expanded=False):
                st.text_input("Bot Token", type="password", key="toi_tg_token",
                              placeholder="From @BotFather — leave blank to disable")
                st.text_input("Chat ID", key="toi_tg_chat",
                              placeholder="e.g. -1001234567890 (from @userinfobot)")
                st.caption(
                    "Alerts fire when: Aggregate Diff OI spike ≥ threshold, "
                    "or any per-strike CE/PE delta ≥ threshold."
                )
                if st.button("🧪 Test Telegram", key="toi_tg_test"):
                    _ok = toi_send_telegram(
                        "✅ MarketPulse Trending OI — Telegram alerts are working!",
                        st.session_state.get("toi_tg_token", ""),
                        st.session_state.get("toi_tg_chat", ""),
                    )
                    st.success("Message sent!") if _ok else st.error(
                        "Failed — check Bot Token and Chat ID."
                    )

        elif _initialized and _init_sym != _toi_symbol:
            st.info(
                f"⚠️ Currently tracking **{_init_sym}**. "
                f"Click **Initialize** to switch to **{_toi_symbol}** "
                "(this resets the day-start baseline)."
            )
        else:
            st.info(
                "📡 **How to start Trending OI tracking:**\n\n"
                "1. Select the **index** and **interval** above\n"
                "2. Click **🚀 Initialize** — downloads instruments and takes a day-start baseline snapshot\n"
                "3. Enable **Auto-refresh** to poll automatically every interval, "
                "or click **📸 Snapshot Now** for a manual snapshot\n"
                "4. The table grows with each snapshot, showing cumulative OI change from day-start\n\n"
                "**Column guide:**  "
                "CALLS CHNG OI / PUTS CHNG OI = cumulative CE/PE OI change since day-start  ·  "
                "DIFF. IN OI = PUTS − CALLS change  ·  "
                "COI PCR = PUTS CHNG OI / CALLS CHNG OI  ·  "
                "SENTIMENT = Bullish if COI PCR ≥ 1.2 or rising, Bearish if ≤ 0.8 or falling"
            )


# ── PAGE: Smart Alerts ────────────────────────────────────────────────────────
def page_smart_alerts():

    # ── Design tokens (injected once per render) ──────────────────────────────
    st.markdown("""
<style>
.sa-card{background:#1e222d;border:1px solid #2a2e39;border-radius:12px;padding:18px 22px;margin-bottom:12px;}
.sa-sec{font-size:0.58rem;font-weight:800;text-transform:uppercase;letter-spacing:2px;color:#3d4a5c;margin-bottom:10px;}
.sa-kv-lbl{font-size:0.58rem;font-weight:700;text-transform:uppercase;letter-spacing:1.1px;color:#4a5568;margin-bottom:3px;}
.sa-kv-val{font-size:1.08rem;font-weight:800;color:#e6edf3;line-height:1.3;}
.sa-divider{height:1px;background:#2a2e39;margin:14px 0;}
.sa-bbull{background:rgba(0,212,170,0.13);border:1px solid rgba(0,212,170,0.4);color:#00d4aa;
  font-size:0.62rem;font-weight:800;padding:2px 9px;border-radius:6px;letter-spacing:.5px;white-space:nowrap;}
.sa-bbear{background:rgba(248,81,73,0.11);border:1px solid rgba(248,81,73,0.4);color:#f85149;
  font-size:0.62rem;font-weight:700;padding:2px 9px;border-radius:6px;white-space:nowrap;}
.sa-bneut{background:rgba(230,184,0,0.09);border:1px solid rgba(230,184,0,0.3);color:#e6b800;
  font-size:0.62rem;font-weight:600;padding:2px 9px;border-radius:6px;white-space:nowrap;}
.sa-tbl{width:100%;border-collapse:collapse;}
.sa-tbl th{padding:8px 14px;font-size:0.58rem;font-weight:800;text-transform:uppercase;
  letter-spacing:1.2px;color:#3d4a5c;text-align:left;background:#131722;border-bottom:1px solid #2a2e39;}
.sa-tbl th.r{text-align:right;}
.sa-tbl td{padding:7px 14px;border-bottom:1px solid rgba(26,32,53,0.7);vertical-align:middle;}
.sa-tbl tr:last-child td{border-bottom:none;}
.sa-tbl tfoot td{background:#131722;border-top:2px solid #2a2e39;padding:8px 14px;}
/* ── Active Trade Monitor ── */
.atm-card{border-radius:14px;padding:20px 24px;margin:6px 0 14px;position:relative;overflow:hidden;}
.atm-open{background:linear-gradient(135deg,#0a1a12 0%,#0d1f1a 100%);border:1.5px solid #00d4aa;}
.atm-sl{background:linear-gradient(135deg,#1a0a0a 0%,#1f0d0d 100%);border:1.5px solid #f85149;animation:atm-pulse .9s ease-in-out infinite;}
.atm-tgt{background:linear-gradient(135deg,#0a1a12 0%,#081510 100%);border:2px solid #00d4aa;box-shadow:0 0 18px rgba(0,212,170,.25);animation:atm-pulse-g .9s ease-in-out infinite;}
.atm-rev{background:linear-gradient(135deg,#1a1200 0%,#1f1a00 100%);border:1.5px solid #e6b800;}
@keyframes atm-pulse{0%,100%{box-shadow:0 0 0 rgba(248,81,73,0);}50%{box-shadow:0 0 14px rgba(248,81,73,.35);}}
@keyframes atm-pulse-g{0%,100%{box-shadow:0 0 8px rgba(0,212,170,.2);}50%{box-shadow:0 0 22px rgba(0,212,170,.45);}}
.atm-label{font-size:0.56rem;font-weight:800;letter-spacing:2px;text-transform:uppercase;margin-bottom:4px;}
.atm-pnl-pos{font-size:1.5rem;font-weight:900;color:#00d4aa;}
.atm-pnl-neg{font-size:1.5rem;font-weight:900;color:#f85149;}
.atm-bar-wrap{position:relative;height:10px;background:#2a2e39;border-radius:5px;margin:10px 0 4px;overflow:visible;}
.atm-bar-fill{height:100%;border-radius:5px;transition:width .4s ease;}
.atm-marker{position:absolute;top:-4px;width:3px;height:18px;border-radius:2px;transform:translateX(-50%);}
.atm-rev-banner{background:rgba(230,184,0,.1);border:1px solid rgba(230,184,0,.4);border-radius:8px;
  padding:8px 14px;margin-top:12px;font-size:0.8rem;color:#e6b800;font-weight:700;}
</style>""", unsafe_allow_html=True)

    _sa_kite_key   = st.session_state.get("kite_api_key",      _get_secret("KITE_API_KEY", ""))
    _sa_kite_token = st.session_state.get("kite_access_token", _get_secret("KITE_ACCESS_TOKEN", ""))
    _sa_kite_live  = bool(_sa_kite_key and _sa_kite_token)

    # ── Page header ───────────────────────────────────────────────────────────
    st.markdown("""
<div style="display:flex;align-items:center;gap:12px;margin:2px 0 4px;">
  <span style="font-size:1.25rem;font-weight:900;color:#fff;letter-spacing:-.5px;">💡 Smart Alerts</span>
  <span style="font-size:0.62rem;font-weight:700;background:rgba(0,212,170,0.1);border:1px solid
  rgba(0,212,170,0.25);color:#00d4aa;padding:2px 10px;border-radius:20px;letter-spacing:1.3px;
  text-transform:uppercase;">OPTION SIGNAL</span>
</div>
<div style="font-size:0.78rem;color:#4a5568;margin-bottom:18px;">
  8 OI factors aggregated live → single BUY CE / BUY PE / WAIT with strike · entry · SL · target
</div>""", unsafe_allow_html=True)

    if not _sa_kite_live:
        st.markdown("""
<div class="sa-card" style="text-align:center;padding:36px 24px;">
  <div style="font-size:2rem;margin-bottom:12px;opacity:.35;">🔌</div>
  <div style="color:#e6edf3;font-size:1rem;font-weight:700;margin-bottom:6px;">Kite not connected</div>
  <div style="color:#4a5568;font-size:0.82rem;">Enter API Key + Access Token in the sidebar to unlock live signals</div>
</div>""", unsafe_allow_html=True)

    else:
        # ── CONTROL PANEL ─────────────────────────────────────────────────────
        st.markdown('<div class="sa-sec">Configuration</div>', unsafe_allow_html=True)
        with st.container(border=True):
            _sc1, _sc2, _sc3, _sc4, _sc5 = st.columns([2.2, 2.2, 1.8, 1.6, 1.8])

            _sa_symbol = _sc1.selectbox(
                "Index", TOI_SYMBOLS, key="sa_symbol",
                index=TOI_SYMBOLS.index("SENSEX"),
            )

            _sa_instr_key = f"toi_instr_{_sa_symbol}"
            _sa_instr_df  = st.session_state.get(_sa_instr_key)
            _sa_expiries  = toi_get_expiries(_sa_instr_df) if _sa_instr_df is not None else []
            _sa_exp_opts  = _sa_expiries if _sa_expiries else ["— load first —"]

            _sa_expiry = _sc2.selectbox(
                "Expiry", _sa_exp_opts, key="sa_expiry",
                disabled=not _sa_expiries,
            )
            _sa_auto_int = _sc3.selectbox(
                "Scan every", ["1 Min", "3 Min", "5 Min"],
                key="sa_interval", index=0,
            )
            _sa_isec = {"1 Min": 60, "3 Min": 180, "5 Min": 300}[_sa_auto_int]

            _sc4.markdown('<div style="height:28px;"></div>', unsafe_allow_html=True)
            _sa_load_btn = _sc4.button(
                "📥 Load", use_container_width=True, key="sa_load",
            )
            _sa_elapsed = int(time.time() - st.session_state.get("sa_last_fetch", time.time()))
            _sa_ts_lbl  = f"Updated {_sa_elapsed}s ago" if st.session_state.get("sa_last_fetch", 0) > 0 else "Not yet scanned"
            _sc5.caption(_sa_ts_lbl)
            _sa_manual = _sc5.button(
                "🔍 Analyze Now", type="primary",
                use_container_width=True, key="sa_manual",
            )

        # ── Load instruments handler ──────────────────────────────────────────
        if _sa_load_btn:
            with st.spinner(f"Downloading {_sa_symbol} instruments…"):
                try:
                    _instr = toi_fetch_instruments(_sa_kite_key, _sa_kite_token, _sa_symbol)
                    st.session_state[_sa_instr_key] = _instr
                    _sa_instr_df = _instr
                    _sa_expiries = toi_get_expiries(_instr)
                    st.rerun()
                except Exception as _ile:
                    st.error(f"Instruments download failed: {_ile}")

        # ── Empty state: no instruments ───────────────────────────────────────
        if _sa_instr_df is None:
            st.markdown("""
<div class="sa-card" style="text-align:center;padding:32px 24px;">
  <div style="font-size:2rem;margin-bottom:12px;opacity:.35;">📥</div>
  <div style="color:#e6edf3;font-weight:700;margin-bottom:6px;">Load option instruments to begin</div>
  <div style="color:#4a5568;font-size:0.82rem;max-width:420px;margin:0 auto;">
    Click <strong style="color:#c9d1d9;">📥 Load</strong> above to fetch option data from Kite.
    If Trending OI is already running for this index, instruments are already cached — just pick an expiry.
  </div>
</div>""", unsafe_allow_html=True)

        elif _sa_expiries and _sa_expiry and _sa_expiry != "— load first —":

            # ── Auto-refresh ──────────────────────────────────────────────────
            if _HAS_AUTOREFRESH:
                _st_autorefresh(interval=_sa_isec * 1000, key="sa_ar")

            # ── Helpers ───────────────────────────────────────────────────────
            def _run_sa_signal():
                _toi_rows = st.session_state.get("toi_rows", [])
                if st.session_state.get("toi_init_symbol") != _sa_symbol:
                    _toi_rows = []
                return run_smart_signal(
                    _sa_kite_key, _sa_kite_token,
                    _sa_symbol, _sa_expiry,
                    _sa_instr_df,
                    _toi_rows if _toi_rows else None,
                )

            def _record_signal(sig: dict):
                _h = st.session_state.get("sa_history", [])
                _h.append({
                    "ts":     sig["ts"].strftime("%H:%M"),
                    "signal": sig.get("signal", "WAIT"),
                    "score":  sig.get("score", 0),
                    "spot":   sig.get("spot", 0),
                    "strike": sig.get("strike"),
                    "ltp":    sig.get("ltp"),
                })
                st.session_state["sa_history"] = _h[-30:]

            def _update_active_trade_ltp():
                """Fetch current LTP for the open trade and check SL/Target."""
                trade = st.session_state.get("sa_active_trade")
                if not trade or trade.get("status") != "OPEN":
                    return
                import requests as _rq
                _xch = "BFO" if trade["symbol"] in ("SENSEX", "BANKEX") else "NFO"
                _sym = f"{_xch}:{trade['tradingsymbol']}"
                try:
                    _r = _rq.get(
                        "https://api.kite.trade/quote/ltp",
                        headers={"X-Kite-Version": "3", "Authorization": f"token {_sa_kite_key}:{_sa_kite_token}"},
                        params={"i": [_sym]},
                        timeout=8,
                    )
                    if _r.ok:
                        _ltp = float(_r.json().get("data", {}).get(_sym, {}).get("last_price", 0))
                        if _ltp > 0:
                            trade["current_ltp"] = _ltp
                            trade["last_update_ts"] = datetime.datetime.now(
                                datetime.timezone(datetime.timedelta(hours=5, minutes=30))
                            ).strftime("%H:%M:%S")
                            if _ltp <= trade["sl"]:
                                trade["status"] = "SL HIT"
                                st.toast(f"🛑 STOP LOSS HIT — {trade['symbol']} {int(trade['strike'])} {trade['opt_type']} @ ₹{_ltp:.1f}", icon="🛑")
                            elif _ltp >= trade["target"]:
                                trade["status"] = "TARGET HIT"
                                st.toast(f"🎯 TARGET HIT — {trade['symbol']} {int(trade['strike'])} {trade['opt_type']} @ ₹{_ltp:.1f}", icon="🎯")
                            st.session_state["sa_active_trade"] = trade
                except Exception:
                    pass

            def _maybe_alert(sig: dict):
                if sig.get("signal", "WAIT") == "WAIT":
                    return
                _msg = (
                    f"💡 *{sig['signal']}* · {_sa_symbol} "
                    f"{sig.get('strike','—')} @ ₹{sig.get('ltp') or 0:.1f} "
                    f"· Score {sig['score']:+d} · Spot {sig['spot']:,.2f}"
                )
                st.toast(_msg, icon="💡")
                _tg_tok  = st.session_state.get("toi_tg_token", "")
                _tg_chat = st.session_state.get("toi_tg_chat", "")
                if _tg_tok and _tg_chat:
                    toi_send_telegram(_msg, _tg_tok, _tg_chat)

            # ── Auto-analyze on timer ─────────────────────────────────────────
            _sa_last = st.session_state.get("sa_last_fetch", 0.0)
            if time.time() - _sa_last >= _sa_isec * 0.9:
                try:
                    _sig = _run_sa_signal()
                    st.session_state["sa_last_signal"] = _sig
                    st.session_state["sa_last_fetch"]  = time.time()
                    _record_signal(_sig)
                    _maybe_alert(_sig)
                    _update_active_trade_ltp()
                except Exception as _sae:
                    st.warning(f"Auto-scan failed: {_sae}")

            # ── Manual analyze ────────────────────────────────────────────────
            if _sa_manual:
                with st.spinner("Fetching live OI data…"):
                    try:
                        _sig = _run_sa_signal()
                        st.session_state["sa_last_signal"] = _sig
                        st.session_state["sa_last_fetch"]  = time.time()
                        _record_signal(_sig)
                        _maybe_alert(_sig)
                        _update_active_trade_ltp()
                        st.rerun()
                    except Exception as _sme:
                        st.error(f"Analysis failed: {_sme}")

            # ── SIGNAL DISPLAY ────────────────────────────────────────────────
            _sa_sig = st.session_state.get("sa_last_signal")

            if not _sa_sig:
                st.markdown("""
<div class="sa-card" style="text-align:center;padding:44px 24px;margin-top:6px;">
  <div style="font-size:2.6rem;opacity:.2;margin-bottom:14px;">📡</div>
  <div style="color:#e6edf3;font-weight:700;font-size:1.05rem;margin-bottom:8px;">No signal yet</div>
  <div style="color:#4a5568;font-size:0.82rem;max-width:480px;margin:0 auto;line-height:1.7;">
    Click <strong style="color:#c9d1d9;">🔍 Analyze Now</strong> or wait for the auto-scan.<br>
    Checks: PCR · Max Pain · OI Walls · COI PCR · Vol PCR · Verdict · Sentiment · Diff OI<br>
    Gates: Time (blocks 9:15–9:30 &amp; after 3PM) · VIX (blocks &lt;10 or &gt;25)
  </div>
</div>""", unsafe_allow_html=True)

            elif "error" in _sa_sig:
                st.error(f"Signal error: {_sa_sig['error']}")

            else:
                _signal  = _sa_sig.get("signal", "WAIT")
                _score   = _sa_sig.get("score", 0)
                _conf    = _sa_sig.get("confidence", "LOW")
                _sa_strike = _sa_sig.get("strike")
                _sa_ltp    = _sa_sig.get("ltp") or 0
                _sa_sl     = _sa_sig.get("sl")
                _sa_tgt    = _sa_sig.get("target")
                _sa_rr     = _sa_sig.get("rr")
                _sa_opt    = _sa_sig.get("option_type", "")
                _sa_ts_str = _sa_sig["ts"].strftime("%H:%M") if hasattr(_sa_sig.get("ts"), "strftime") else str(_sa_sig.get("ts", "—"))
                _sa_atm    = _sa_sig.get("atm", 0)
                _sa_pcr    = _sa_sig.get("pcr", 0)
                _sa_mp     = _sa_sig.get("max_pain", 0)
                _sa_cew    = _sa_sig.get("max_ce_wall", 0)
                _sa_pew    = _sa_sig.get("max_pe_wall", 0)

                # color scheme
                if "STRONG BUY CE" in _signal:
                    _sc, _sbg = "#00d4aa", "rgba(0,212,170,0.09)"
                elif "BUY CE" in _signal:
                    _sc, _sbg = "#58d68d", "rgba(88,214,141,0.08)"
                elif "STRONG BUY PE" in _signal:
                    _sc, _sbg = "#f85149", "rgba(248,81,73,0.11)"
                elif "BUY PE" in _signal:
                    _sc, _sbg = "#ff7043", "rgba(255,112,67,0.09)"
                else:
                    _sc, _sbg = "#e6b800", "rgba(230,184,0,0.07)"

                # score gauge: map -12..+12 → 0%..100%
                _gauge_pct = max(2, min(98, ((_score + 12) / 24) * 100))
                _conf_icon = {"HIGH": "🔥", "MEDIUM": "⚡", "LOW": "⏳"}.get(_conf, "")
                _sa_gate_pass_banner = _sa_sig.get("gate_pass", True)
                _sa_raw_sig          = _sa_sig.get("raw_signal", _signal)
                _trade_line = (
                    f"<span style='color:{_sc};font-size:0.95rem;font-weight:700;'>"
                    f"Buy {_sa_symbol} {_sa_strike} {_sa_opt}</span>"
                    f"<span style='color:#4a5568;font-size:0.8rem;margin-left:10px;'>expiry {_sa_expiry}</span>"
                    if _signal != "WAIT" and _sa_strike else
                    "<span style='color:#4a5568;font-size:0.82rem;'>No directional setup — stand aside</span>"
                )
                _gate_extra = (
                    f"<span style='color:#f85149;font-size:0.72rem;margin-left:8px;'>⚠ Gate blocked · raw signal: {_sa_raw_sig}</span>"
                    if not _sa_gate_pass_banner and _sa_raw_sig != "WAIT" else ""
                )

                # ── ① SIGNAL BANNER ───────────────────────────────────────────
                st.markdown(f"""
<div style="background:{_sbg};border:1.5px solid {_sc};border-radius:14px;padding:22px 28px 18px;margin:6px 0 14px;position:relative;overflow:hidden;">
  <div style="position:absolute;top:0;left:0;right:0;height:3px;background:linear-gradient(90deg,transparent,{_sc},transparent);opacity:.6;"></div>
  <div style="display:flex;align-items:flex-start;justify-content:space-between;flex-wrap:wrap;gap:16px;margin-bottom:18px;">
    <div>
      <div style="font-size:0.58rem;font-weight:800;letter-spacing:2px;text-transform:uppercase;color:{_sc};opacity:.75;margin-bottom:5px;">Signal</div>
      <div style="font-size:2.1rem;font-weight:900;color:{_sc};letter-spacing:1px;line-height:1;">{_signal}</div>
      <div style="margin-top:10px;">{_trade_line}{_gate_extra}</div>
    </div>
    <div style="text-align:right;">
      <div style="font-size:0.58rem;font-weight:800;letter-spacing:2px;text-transform:uppercase;color:#3d4a5c;margin-bottom:5px;">Score</div>
      <div style="font-size:2.4rem;font-weight:900;color:{_sc};line-height:1;">{_score:+d}</div>
      <div style="font-size:0.72rem;color:#6e7681;margin-top:6px;">{_conf_icon} {_conf} confidence &nbsp;·&nbsp; {_sa_ts_str}</div>
    </div>
  </div>
  <div>
    <div style="display:flex;justify-content:space-between;font-size:0.56rem;color:#3d4a5c;font-weight:700;text-transform:uppercase;letter-spacing:.8px;margin-bottom:5px;"><span>Strong PE -12</span><span>Neutral 0</span><span>Strong CE +12</span></div>
    <div style="height:7px;background:#2a2e39;border-radius:4px;overflow:hidden;"><div style="height:100%;width:100%;background:linear-gradient(90deg,#f85149 0%,#e6b800 50%,#00d4aa 100%);border-radius:4px;"></div></div>
    <div style="position:relative;height:10px;"><div style="position:absolute;left:{_gauge_pct:.1f}%;transform:translateX(-50%);top:0;width:3px;height:10px;background:{_sc};border-radius:2px;box-shadow:0 0 6px {_sc};"></div></div>
  </div>
</div>""", unsafe_allow_html=True)

                # ── ① b  ACTIVE TRADE MONITOR ────────────────────────────────
                _at = st.session_state.get("sa_active_trade")
                if _at:
                    _at_status  = _at.get("status", "OPEN")
                    _at_cur     = _at.get("current_ltp", _at["entry_ltp"])
                    _at_entry   = _at["entry_ltp"]
                    _at_sl      = _at["sl"]
                    _at_tgt     = _at["target"]
                    _at_pnl_pts = _at_cur - _at_entry
                    _at_pnl_pct = (_at_pnl_pts / _at_entry * 100) if _at_entry else 0
                    _at_elapsed = int((datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5, minutes=30))) - _at["entry_time"]).total_seconds() / 60)

                    # Card class
                    if _at_status == "TARGET HIT":
                        _at_cls = "atm-tgt"
                    elif _at_status == "SL HIT":
                        _at_cls = "atm-sl"
                    else:
                        _at_cls = "atm-open"

                    # Progress bar: position of SL, entry, current, target on a 0-100% scale
                    _at_rng = _at_tgt - _at_sl
                    def _at_pct(v):
                        return max(2, min(98, (v - _at_sl) / _at_rng * 100)) if _at_rng > 0 else 50
                    _at_entry_pct  = _at_pct(_at_entry)
                    _at_cur_pct    = _at_pct(_at_cur)
                    _at_fill_color = "#00d4aa" if _at_pnl_pts >= 0 else "#f85149"
                    _at_pnl_cls    = "atm-pnl-pos" if _at_pnl_pts >= 0 else "atm-pnl-neg"
                    _at_pnl_sign   = "+" if _at_pnl_pts >= 0 else ""

                    # Signal reversal check
                    _at_reversal = False
                    _at_rev_msg  = ""
                    if _at_status == "OPEN" and _sa_sig:
                        _cur_sig = _sa_sig.get("signal", "WAIT")
                        _at_is_ce = _at["opt_type"] == "CE"
                        if _at_is_ce and "PE" in _cur_sig:
                            _at_reversal = True
                            _at_rev_msg  = f"Signal reversed to {_cur_sig} — consider exiting your CE position"
                            _at_cls      = "atm-rev"
                        elif not _at_is_ce and "CE" in _cur_sig:
                            _at_reversal = True
                            _at_rev_msg  = f"Signal reversed to {_cur_sig} — consider exiting your PE position"
                            _at_cls      = "atm-rev"

                    # Status badge
                    if _at_status == "TARGET HIT":
                        _at_status_html = '<span style="color:#00d4aa;font-size:1.1rem;font-weight:900;">🎯 TARGET HIT — EXIT NOW</span>'
                    elif _at_status == "SL HIT":
                        _at_status_html = '<span style="color:#f85149;font-size:1.1rem;font-weight:900;">🛑 STOP LOSS HIT — EXIT NOW</span>'
                    else:
                        _at_status_html = f'<span style="color:#00d4aa;font-size:0.85rem;font-weight:700;">🟢 OPEN · {_at_elapsed} min in trade</span>'

                    _rev_html = f'<div class="atm-rev-banner">⚠ {_at_rev_msg}</div>' if _at_reversal else ""

                    st.markdown(f"""
<div class="atm-card {_at_cls}">
  <div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:12px;">
    <div>
      <div class="atm-label" style="color:#00d4aa;">Active Trade Monitor</div>
      <div style="font-size:1rem;font-weight:800;color:#f0f6fc;">{_at["symbol"]} {int(_at["strike"])} {_at["opt_type"]} · expiry {_at["expiry"]}</div>
      <div style="margin-top:4px;">{_at_status_html}</div>
    </div>
    <div style="text-align:right;">
      <div class="atm-label" style="color:#6e7681;">P&amp;L</div>
      <div class="{_at_pnl_cls}">{_at_pnl_sign}{_at_pnl_pct:.1f}%</div>
      <div style="font-size:0.78rem;color:#6e7681;">{_at_pnl_sign}₹{_at_pnl_pts:.2f} per unit</div>
    </div>
  </div>
  <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin-top:14px;">
    <div>
      <div class="atm-label" style="color:#6e7681;">Entry LTP</div>
      <div style="font-size:1rem;font-weight:800;color:#e6edf3;">₹{_at_entry:.1f}</div>
    </div>
    <div>
      <div class="atm-label" style="color:#6e7681;">Current LTP</div>
      <div style="font-size:1.1rem;font-weight:900;color:{_at_fill_color};">₹{_at_cur:.1f}</div>
    </div>
    <div>
      <div class="atm-label" style="color:#6e7681;">Last updated</div>
      <div style="font-size:0.82rem;color:#6e7681;">{_at.get("last_update_ts","—")}</div>
    </div>
  </div>
  <div style="margin-top:16px;">
    <div style="display:flex;justify-content:space-between;font-size:0.65rem;color:#6e7681;margin-bottom:4px;">
      <span style="color:#f85149;">SL ₹{_at_sl:.1f}</span>
      <span style="color:#adbac7;">Entry ₹{_at_entry:.1f}</span>
      <span style="color:#00d4aa;">Target ₹{_at_tgt:.1f}</span>
    </div>
    <div class="atm-bar-wrap">
      <div class="atm-bar-fill" style="width:{_at_cur_pct:.1f}%;background:{_at_fill_color};opacity:.35;"></div>
      <div class="atm-marker" style="left:{_at_entry_pct:.1f}%;background:#ffffff;opacity:.5;"></div>
      <div class="atm-marker" style="left:{_at_cur_pct:.1f}%;background:{_at_fill_color};box-shadow:0 0 6px {_at_fill_color};"></div>
    </div>
  </div>
  {_rev_html}
</div>""", unsafe_allow_html=True)

                    # Exit button
                    _at_ecol1, _at_ecol2 = st.columns([1, 3])
                    with _at_ecol1:
                        if st.button("📤 I Exited", key="sa_exit_trade", type="secondary", use_container_width=True):
                            _at_final = st.session_state.get("sa_active_trade", {})
                            _at_final["status"] = "EXITED"
                            _at_final["exit_ltp"] = _at_cur
                            _at_final["exit_pnl_pct"] = _at_pnl_pct
                            # Record outcome in history
                            _oh = st.session_state.get("sa_history", [])
                            if _oh:
                                _oh[-1]["outcome"] = f"{_at_pnl_sign}{_at_pnl_pct:.1f}%"
                            st.session_state["sa_history"] = _oh
                            st.session_state["sa_active_trade"] = None
                            st.toast(f"Trade closed · P&L: {_at_pnl_sign}{_at_pnl_pct:.1f}%", icon="📤")
                            st.rerun()

                # ── ② TRADE SETUP  |  MARKET SNAPSHOT ────────────────────────
                _lc, _rc = st.columns([11, 9])

                with _lc:
                    if _signal != "WAIT" and _sa_ltp and _sa_ltp > 0.5:
                        _tc  = "#00d4aa" if "CE" in _signal else "#f85149"
                        ltp_s = f"₹{_sa_ltp:.1f}"
                        sl_s  = f"₹{_sa_sl:.1f}"  if _sa_sl  else "—"
                        tgt_s = f"₹{_sa_tgt:.1f}" if _sa_tgt else "—"
                        rr_s  = f"1 : {_sa_rr}"    if _sa_rr  else "—"
                        st.markdown(f"""
<div class="sa-card">
  <div class="sa-sec">Trade Setup</div>
  <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin-bottom:14px;">
    <div>
      <div class="sa-kv-lbl">Strike</div>
      <div class="sa-kv-val" style="color:{_tc};font-size:1.35rem;">{_sa_strike}</div>
    </div>
    <div>
      <div class="sa-kv-lbl">Type</div>
      <div class="sa-kv-val" style="color:{_tc};">{_sa_opt}</div>
    </div>
    <div>
      <div class="sa-kv-lbl">Expiry</div>
      <div class="sa-kv-val" style="color:#adbac7;font-size:0.88rem;">{_sa_expiry}</div>
    </div>
  </div>
  <div class="sa-divider"></div>
  <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-top:14px;">
    <div>
      <div class="sa-kv-lbl">Entry (LTP)</div>
      <div class="sa-kv-val">{ltp_s}</div>
    </div>
    <div>
      <div class="sa-kv-lbl">Stop Loss</div>
      <div class="sa-kv-val" style="color:#f85149;">{sl_s}</div>
    </div>
    <div>
      <div class="sa-kv-lbl">Target</div>
      <div class="sa-kv-val" style="color:#00d4aa;">{tgt_s}</div>
    </div>
    <div>
      <div class="sa-kv-lbl">Risk : Reward</div>
      <div class="sa-kv-val">{rr_s}</div>
    </div>
  </div>
  <div style="font-size:0.67rem;color:#3d4a5c;margin-top:14px;">
    ⚠ For educational purposes only — not financial advice.
  </div>
</div>""", unsafe_allow_html=True)
                        # ── "I Entered" button ─────────────────────────────────
                        _existing_trade = st.session_state.get("sa_active_trade")
                        _trade_open = bool(_existing_trade and _existing_trade.get("status") == "OPEN")
                        if not _trade_open:
                            st.markdown('<div style="margin-top:6px;"></div>', unsafe_allow_html=True)
                            if st.button("✅  I Entered This Trade", key="sa_enter_trade", type="primary", use_container_width=True):
                                _ts_rows = _sa_instr_df[
                                    (_sa_instr_df["expiry"] == _sa_expiry) &
                                    (_sa_instr_df["strike"].astype(float) == float(_sa_strike)) &
                                    (_sa_instr_df["instrument_type"] == _sa_opt)
                                ]
                                _ts = _ts_rows.iloc[0]["tradingsymbol"] if not _ts_rows.empty else ""
                                _ist_tz = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
                                st.session_state["sa_active_trade"] = {
                                    "symbol":        _sa_symbol,
                                    "expiry":        _sa_expiry,
                                    "strike":        _sa_strike,
                                    "opt_type":      _sa_opt,
                                    "tradingsymbol": _ts,
                                    "entry_ltp":     _sa_ltp,
                                    "current_ltp":   _sa_ltp,
                                    "sl":            _sa_sl,
                                    "target":        _sa_tgt,
                                    "entry_time":    datetime.datetime.now(_ist_tz),
                                    "entry_score":   _score,
                                    "entry_signal":  _signal,
                                    "status":        "OPEN",
                                    "last_update_ts": "just now",
                                }
                                st.toast(f"Trade recorded: {_sa_symbol} {_sa_strike} {_sa_opt} @ ₹{_sa_ltp:.1f}", icon="✅")
                                st.rerun()
                        else:
                            st.markdown(
                                '<div style="text-align:center;padding:8px;background:rgba(0,212,170,0.08);'
                                'border:1px solid rgba(0,212,170,0.3);border-radius:8px;margin-top:6px;'
                                'font-size:0.78rem;color:#00d4aa;">🟢 Trade active — monitor above</div>',
                                unsafe_allow_html=True,
                            )
                    else:
                        st.markdown("""
<div class="sa-card" style="text-align:center;padding:28px 20px;">
  <div style="color:#4a5568;font-size:0.88rem;margin-bottom:4px;">No trade setup</div>
  <div style="color:#3d4a5c;font-size:0.78rem;">Score below threshold — stand aside</div>
</div>""", unsafe_allow_html=True)

                with _rc:
                    _pcr_col = "#00d4aa" if _sa_pcr >= 1.0 else "#f85149"
                    st.markdown(f"""
<div class="sa-card">
  <div class="sa-sec">Market Snapshot</div>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;">
    <div>
      <div class="sa-kv-lbl">Spot</div>
      <div class="sa-kv-val">{_sa_sig['spot']:,.2f}</div>
    </div>
    <div>
      <div class="sa-kv-lbl">ATM Strike</div>
      <div class="sa-kv-val">{_sa_atm:,}</div>
    </div>
    <div>
      <div class="sa-kv-lbl">PCR</div>
      <div class="sa-kv-val" style="color:{_pcr_col};">{_sa_pcr:.3f}</div>
    </div>
    <div>
      <div class="sa-kv-lbl">Max Pain</div>
      <div class="sa-kv-val">{_sa_mp:,}</div>
    </div>
    <div>
      <div class="sa-kv-lbl">CE Wall (resistance)</div>
      <div class="sa-kv-val" style="color:#f85149;">{_sa_cew:,}</div>
    </div>
    <div>
      <div class="sa-kv-lbl">PE Wall (support)</div>
      <div class="sa-kv-val" style="color:#00d4aa;">{_sa_pew:,}</div>
    </div>
  </div>
</div>""", unsafe_allow_html=True)

                    # Trending OI feed status
                    _sa_toi_rows = st.session_state.get("toi_rows", [])
                    _toi_on      = bool(_sa_toi_rows and st.session_state.get("toi_init_symbol") == _sa_symbol)
                    if _toi_on:
                        _toi_status = (
                            f'<span class="sa-bbull">✓ Active · {len(_sa_toi_rows)} rows</span>'
                        )
                        _toi_note   = "5 extra factors feeding into score"
                    else:
                        _toi_status = '<span class="sa-bneut">— Not initialized</span>'
                        _toi_note   = "Initialize Tab 9 to unlock COI PCR · Verdict · Sentiment"
                    st.markdown(f"""
<div class="sa-card" style="margin-top:10px;padding:12px 18px;">
  <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;">
    <div>
      <div class="sa-kv-lbl" style="margin-bottom:4px;">Trending OI feed</div>
      <div style="font-size:0.75rem;color:#4a5568;">{_toi_note}</div>
    </div>
    {_toi_status}
  </div>
</div>""", unsafe_allow_html=True)

                # ── ② b GATE STATUS ──────────────────────────────────────────
                _sa_gates     = _sa_sig.get("gates", {})
                _sa_gate_pass = _sa_sig.get("gate_pass", True)
                _sa_block_rsn = _sa_sig.get("block_reason", "")
                if _sa_gates:
                    _g_icons = {"Time": "⏱", "VIX": "📊"}
                    _g_html  = '<div class="sp-gate-row">'
                    for _gk, (_gp, _gr) in _sa_gates.items():
                        _gcls = "sp-gate-pass" if _gp else "sp-gate-fail"
                        _g_html += f'<span class="{_gcls}" title="{_gr}">{_g_icons.get(_gk,"•")} {_gk} {"✓" if _gp else "✗"}</span>'
                    _g_html += "</div>"
                    st.markdown('<div class="sa-sec" style="margin-top:10px;">Signal Gates</div>', unsafe_allow_html=True)
                    st.markdown(_g_html, unsafe_allow_html=True)
                    if not _sa_gate_pass and _sa_block_rsn:
                        st.markdown(f'<div class="sp-conflict">🚧 Gate blocked: {_sa_block_rsn}</div>', unsafe_allow_html=True)

                # ── ③ FACTOR BREAKDOWN ────────────────────────────────────────
                _factors = _sa_sig.get("factors", [])
                if _factors:
                    def _dbadge(d: str) -> str:
                        if d == "BULL":  return '<span class="sa-bbull">▲ BULL</span>'
                        if d == "BEAR":  return '<span class="sa-bbear">▼ BEAR</span>'
                        return '<span class="sa-bneut">— NEUTRAL</span>'

                    def _phtmls(p: int) -> str:
                        if p > 0: return f'<span style="color:#00d4aa;font-weight:800;">{p:+d}</span>'
                        if p < 0: return f'<span style="color:#f85149;font-weight:800;">{p:+d}</span>'
                        return '<span style="color:#3d4a5c;font-weight:700;">0</span>'

                    _total = sum(f["points"] for f in _factors)
                    _f_rows = "".join(
                        f'<tr>'
                        f'<td style="color:#adbac7;font-weight:600;font-size:0.82rem;white-space:nowrap;">{f["name"]}</td>'
                        f'<td style="color:#e6edf3;font-weight:700;font-size:0.84rem;">{f["value"]}</td>'
                        f'<td>{_dbadge(f["direction"])}</td>'
                        f'<td class="r">{_phtmls(f["points"])}</td>'
                        f'<td style="color:#6e7681;font-size:0.77rem;">{f["reason"]}</td>'
                        f'</tr>'
                        for f in _factors
                    )
                    st.markdown('<div class="sa-sec" style="margin-top:4px;">Signal Factors</div>', unsafe_allow_html=True)
                    st.markdown(f"""
<div class="sa-card" style="padding:0;overflow:hidden;">
  <div style="overflow-x:auto;">
    <table class="sa-tbl">
      <thead><tr>
        <th>Factor</th><th>Value</th><th>Direction</th>
        <th class="r">Pts</th><th>Interpretation</th>
      </tr></thead>
      <tbody>{_f_rows}</tbody>
      <tfoot><tr>
        <td colspan="3" style="color:#6e7681;font-size:0.72rem;font-weight:700;
        text-transform:uppercase;letter-spacing:.8px;">Total Score</td>
        <td class="r" style="font-size:1.05rem;">{_phtmls(_total)}</td>
        <td></td>
      </tr></tfoot>
    </table>
  </div>
</div>""", unsafe_allow_html=True)

                # ── ④ SIGNAL HISTORY ──────────────────────────────────────────
                _sa_hist_disp = st.session_state.get("sa_history", [])
                if len(_sa_hist_disp) > 1:
                    def _sig_col(sig: str) -> str:
                        if "CE" in sig:  return "#00d4aa"
                        if "PE" in sig:  return "#f85149"
                        return "#e6b800"

                    def _score_col(sc: int) -> str:
                        if sc > 0: return "#00d4aa"
                        if sc < 0: return "#f85149"
                        return "#6e7681"

                    _h_rows = "".join(
                        f'<tr>'
                        f'<td style="color:#6e7681;font-size:0.8rem;white-space:nowrap;">{h["ts"]}</td>'
                        f'<td style="font-weight:700;color:{_sig_col(h["signal"])};font-size:0.81rem;">{h["signal"]}</td>'
                        f'<td style="color:#c9d1d9;font-size:0.81rem;">{h["spot"]:,.2f}</td>'
                        f'<td style="font-weight:700;color:{_score_col(h["score"])};font-size:0.84rem;">{h["score"]:+d}</td>'
                        f'<td style="color:#adbac7;font-size:0.81rem;">{h["strike"] if h["strike"] else "—"}</td>'
                        f'<td style="color:#adbac7;font-size:0.81rem;">{"₹"+str(round(h["ltp"],1)) if h["ltp"] else "—"}</td>'
                        f'</tr>'
                        for h in reversed(_sa_hist_disp[-20:])
                    )
                    st.markdown('<div class="sa-sec" style="margin-top:4px;">Signal History</div>', unsafe_allow_html=True)
                    st.markdown(f"""
<div class="sa-card" style="padding:0;overflow:hidden;">
  <div style="overflow-x:auto;">
    <table class="sa-tbl">
      <thead><tr>
        <th>Time</th><th>Signal</th><th>Spot</th>
        <th>Score</th><th>Strike</th><th>LTP</th>
      </tr></thead>
      <tbody>{_h_rows}</tbody>
    </table>
  </div>
</div>""", unsafe_allow_html=True)


# ── PAGE: Smart Alerts Pro ────────────────────────────────────────────────────
def page_smart_alerts_pro():

    # ── CSS (shared sa- classes already injected in tab10; add pro-specific) ──
    st.markdown("""
<style>
.sp-gate-pass{display:inline-flex;align-items:center;gap:5px;background:rgba(0,212,170,0.1);border:1px solid rgba(0,212,170,0.3);color:#00d4aa;font-size:0.65rem;font-weight:700;padding:3px 10px;border-radius:20px;white-space:nowrap;}
.sp-gate-fail{display:inline-flex;align-items:center;gap:5px;background:rgba(248,81,73,0.1);border:1px solid rgba(248,81,73,0.3);color:#f85149;font-size:0.65rem;font-weight:700;padding:3px 10px;border-radius:20px;white-space:nowrap;}
.sp-gate-row{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0 14px;}
.sp-conflict{background:rgba(248,81,73,0.08);border:1px solid rgba(248,81,73,0.25);border-radius:8px;padding:8px 14px;font-size:0.78rem;color:#f85149;margin-bottom:10px;}
.sp-expiry-badge{background:rgba(230,184,0,0.12);border:1px solid rgba(230,184,0,0.35);color:#e6b800;font-size:0.65rem;font-weight:700;padding:2px 9px;border-radius:10px;letter-spacing:.5px;}
.sp-ctx-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;}
.sp-ctx-item{background:#131722;border:1px solid #2a2e39;border-radius:8px;padding:10px 12px;}
</style>""", unsafe_allow_html=True)

    _sp_kite_key   = st.session_state.get("kite_api_key",      _get_secret("KITE_API_KEY", ""))
    _sp_kite_token = st.session_state.get("kite_access_token", _get_secret("KITE_ACCESS_TOKEN", ""))
    _sp_kite_live  = bool(_sp_kite_key and _sp_kite_token)

    # ── Header ────────────────────────────────────────────────────────────────
    st.markdown("""
<div style="display:flex;align-items:center;gap:12px;margin:2px 0 4px;">
  <span style="font-size:1.25rem;font-weight:900;color:#fff;letter-spacing:-.5px;">⚡ Smart Alerts Pro</span>
  <span style="font-size:0.62rem;font-weight:700;background:rgba(230,184,0,0.1);border:1px solid rgba(230,184,0,0.3);color:#e6b800;padding:2px 10px;border-radius:20px;letter-spacing:1.3px;text-transform:uppercase;">13-FACTOR · PRECISION</span>
</div>
<div style="font-size:0.78rem;color:#4a5568;margin-bottom:18px;">
  VWAP · India VIX · IV Spike · OI Velocity · Time Gate · Consecutive Confirmation · Expiry Mode · Dynamic SL
</div>""", unsafe_allow_html=True)

    if not _sp_kite_live:
        st.markdown("""
<div class="sa-card" style="text-align:center;padding:36px 24px;">
  <div style="font-size:2rem;margin-bottom:12px;opacity:.35;">🔌</div>
  <div style="color:#e6edf3;font-size:1rem;font-weight:700;margin-bottom:6px;">Kite not connected</div>
  <div style="color:#4a5568;font-size:0.82rem;">Enter API Key + Access Token in the sidebar to unlock precision signals</div>
</div>""", unsafe_allow_html=True)

    else:
        # ── CONTROL PANEL ─────────────────────────────────────────────────────
        st.markdown('<div class="sa-sec">Configuration</div>', unsafe_allow_html=True)
        with st.container(border=True):
            _sp1, _sp2, _sp3, _sp4, _sp5 = st.columns([2.2, 2.2, 1.8, 1.6, 1.8])

            _sp_symbol = _sp1.selectbox(
                "Index", TOI_SYMBOLS, key="sp_symbol",
                index=TOI_SYMBOLS.index("SENSEX"),
            )
            _sp_instr_key = f"toi_instr_{_sp_symbol}"
            _sp_instr_df  = st.session_state.get(_sp_instr_key)
            _sp_expiries  = toi_get_expiries(_sp_instr_df) if _sp_instr_df is not None else []
            _sp_exp_opts  = _sp_expiries if _sp_expiries else ["— load first —"]

            _sp_expiry = _sp2.selectbox(
                "Expiry", _sp_exp_opts, key="sp_expiry",
                disabled=not _sp_expiries,
            )
            _sp_auto_int = _sp3.selectbox(
                "Scan every", ["1 Min", "3 Min", "5 Min"],
                key="sp_interval", index=0,
            )
            _sp_isec = {"1 Min": 60, "3 Min": 180, "5 Min": 300}[_sp_auto_int]

            _sp4.markdown('<div style="height:28px;"></div>', unsafe_allow_html=True)
            _sp_load_btn = _sp4.button("📥 Load", use_container_width=True, key="sp_load")
            _sp_elapsed  = int(time.time() - st.session_state.get("sa2_last_fetch", time.time()))
            _sp_ts_lbl   = f"Updated {_sp_elapsed}s ago" if st.session_state.get("sa2_last_fetch", 0) > 0 else "Not yet scanned"
            _sp5.caption(_sp_ts_lbl)
            _sp_manual = _sp5.button("🔍 Analyze Now", type="primary", use_container_width=True, key="sp_manual")

        # ── Load instruments ──────────────────────────────────────────────────
        if _sp_load_btn:
            with st.spinner(f"Downloading {_sp_symbol} instruments…"):
                try:
                    _instr = toi_fetch_instruments(_sp_kite_key, _sp_kite_token, _sp_symbol)
                    st.session_state[_sp_instr_key] = _instr
                    _sp_instr_df = _instr
                    _sp_expiries = toi_get_expiries(_instr)
                    # Also load peer index instruments for cross-index conflict check
                    _peer_map = {"NIFTY": "BANKNIFTY", "BANKNIFTY": "NIFTY"}
                    _peer = _peer_map.get(_sp_symbol)
                    if _peer and f"toi_instr_{_peer}" not in st.session_state:
                        try:
                            _peer_instr = toi_fetch_instruments(_sp_kite_key, _sp_kite_token, _peer)
                            st.session_state[f"toi_instr_{_peer}"] = _peer_instr
                        except Exception:
                            pass
                    st.rerun()
                except Exception as _ile:
                    st.error(f"Instruments download failed: {_ile}")

        if _sp_instr_df is None:
            st.markdown("""
<div class="sa-card" style="text-align:center;padding:32px 24px;">
  <div style="font-size:2rem;margin-bottom:12px;opacity:.35;">📥</div>
  <div style="color:#e6edf3;font-weight:700;margin-bottom:6px;">Load option instruments to begin</div>
  <div style="color:#4a5568;font-size:0.82rem;max-width:420px;margin:0 auto;">Click <strong style="color:#c9d1d9;">📥 Load</strong> above. For NIFTY/BANKNIFTY, peer instruments are also fetched for cross-index conflict detection.</div>
</div>""", unsafe_allow_html=True)

        elif _sp_expiries and _sp_expiry and _sp_expiry != "— load first —":

            if _HAS_AUTOREFRESH:
                _st_autorefresh(interval=_sp_isec * 1000, key="sp_ar")

            # ── Signal runner ─────────────────────────────────────────────────
            def _run_sp_signal():
                _toi_rows = st.session_state.get("toi_rows", [])
                if st.session_state.get("toi_init_symbol") != _sp_symbol:
                    _toi_rows = []
                _ltp_hist  = st.session_state.get("sa2_ltp_history", [])
                _dir_hist  = st.session_state.get("sa2_direction_history", [])
                _peer_sym  = {"NIFTY": "BANKNIFTY", "BANKNIFTY": "NIFTY"}.get(_sp_symbol)
                _peer_df   = st.session_state.get(f"toi_instr_{_peer_sym}") if _peer_sym else None
                return run_smart_signal_v2(
                    _sp_kite_key, _sp_kite_token,
                    _sp_symbol, _sp_expiry,
                    _sp_instr_df,
                    toi_rows=_toi_rows if _toi_rows else None,
                    ltp_history=_ltp_hist,
                    direction_history=_dir_hist,
                    scan_interval_sec=_sp_isec,
                    instr_df_peer=_peer_df,
                )

            def _record_sp_signal(sig: dict):
                # Update rolling histories
                _ltp_h = st.session_state.get("sa2_ltp_history", [])
                if sig.get("atm_ltp", 0) > 0:
                    _ltp_h.append(sig["atm_ltp"])
                    st.session_state["sa2_ltp_history"] = _ltp_h[-10:]

                _dir_h = st.session_state.get("sa2_direction_history", [])
                _dir_h.append(sig.get("direction", "NEUTRAL"))
                st.session_state["sa2_direction_history"] = _dir_h[-5:]

                # History log
                _h = st.session_state.get("sa2_history", [])
                _h.append({
                    "ts":       sig["ts"].strftime("%H:%M"),
                    "signal":   sig.get("signal", "WAIT"),
                    "score":    sig.get("score", 0),
                    "spot":     sig.get("spot", 0),
                    "strike":   sig.get("strike"),
                    "ltp":      sig.get("ltp"),
                    "vix":      sig.get("vix", 0),
                    "iv_ratio": sig.get("iv_ratio", 1.0),
                    "gate_pass": sig.get("gate_pass", False),
                    "expiry_day": sig.get("expiry_day", False),
                })
                st.session_state["sa2_history"] = _h[-30:]

            def _maybe_sp_alert(sig: dict):
                if sig.get("signal", "WAIT") == "WAIT":
                    return
                _msg = (
                    f"⚡ *{sig['signal']}* · {_sp_symbol} "
                    f"{sig.get('strike','—')} @ ₹{sig.get('ltp') or 0:.1f} "
                    f"· Score {sig['score']:+d} · VIX {sig.get('vix',0):.1f} "
                    f"· Spot {sig['spot']:,.2f}"
                )
                st.toast(_msg, icon="⚡")
                _tg_tok  = st.session_state.get("toi_tg_token", "")
                _tg_chat = st.session_state.get("toi_tg_chat", "")
                if _tg_tok and _tg_chat:
                    toi_send_telegram(_msg, _tg_tok, _tg_chat)

            # ── Auto-analyze ──────────────────────────────────────────────────
            _sp_last = st.session_state.get("sa2_last_fetch", 0.0)
            if time.time() - _sp_last >= _sp_isec * 0.9:
                try:
                    _sp_sig = _run_sp_signal()
                    st.session_state["sa2_last_signal"] = _sp_sig
                    st.session_state["sa2_last_fetch"]  = time.time()
                    _record_sp_signal(_sp_sig)
                    _maybe_sp_alert(_sp_sig)
                except Exception as _spe:
                    st.warning(f"Auto-scan failed: {_spe}")

            if _sp_manual:
                with st.spinner("Fetching live OI + VWAP + VIX…"):
                    try:
                        _sp_sig = _run_sp_signal()
                        st.session_state["sa2_last_signal"] = _sp_sig
                        st.session_state["sa2_last_fetch"]  = time.time()
                        _record_sp_signal(_sp_sig)
                        _maybe_sp_alert(_sp_sig)
                        st.rerun()
                    except Exception as _sme:
                        st.error(f"Analysis failed: {_sme}")

            # ── DISPLAY ───────────────────────────────────────────────────────
            _sp_res = st.session_state.get("sa2_last_signal")

            if not _sp_res:
                st.markdown("""
<div class="sa-card" style="text-align:center;padding:44px 24px;margin-top:6px;">
  <div style="font-size:2.6rem;opacity:.2;margin-bottom:14px;">⚡</div>
  <div style="color:#e6edf3;font-weight:700;font-size:1.05rem;margin-bottom:8px;">No signal yet</div>
  <div style="color:#4a5568;font-size:0.82rem;max-width:480px;margin:0 auto;line-height:1.7;">Click <strong style="color:#c9d1d9;">🔍 Analyze Now</strong> or wait for the auto-scan.<br>13 factors: PCR · Max Pain · OI Walls · COI PCR · Vol PCR · Verdict · Sentiment · Diff OI · VWAP · VIX · IV Spike · Time · OI Velocity</div>
</div>""", unsafe_allow_html=True)

            elif "error" in _sp_res:
                st.error(f"Signal error: {_sp_res['error']}")

            else:
                _sp_signal  = _sp_res.get("signal", "WAIT")
                _sp_score   = _sp_res.get("score", 0)
                _sp_conf    = _sp_res.get("confidence", "LOW")
                _sp_gates   = _sp_res.get("gates", {})
                _sp_gate_ok = _sp_res.get("gate_pass", False)
                _sp_vix     = _sp_res.get("vix", 0)
                _sp_vwap    = _sp_res.get("vwap", 0)
                _sp_ivr     = _sp_res.get("iv_ratio", 1.0)
                _sp_exp_day = _sp_res.get("expiry_day", False)
                _sp_conflict = _sp_res.get("conflict", False)
                _sp_cross_sym = _sp_res.get("cross_symbol", "")
                _sp_cross_dir = _sp_res.get("cross_direction", "N/A")
                _sp_strike  = _sp_res.get("strike")
                _sp_ltp     = _sp_res.get("ltp") or 0
                _sp_sl      = _sp_res.get("sl")
                _sp_tgt     = _sp_res.get("target")
                _sp_rr      = _sp_res.get("rr")
                _sp_opt     = _sp_res.get("option_type", "")
                _sp_ts_str  = _sp_res["ts"].strftime("%H:%M") if hasattr(_sp_res.get("ts"), "strftime") else "—"
                _sp_atm     = _sp_res.get("atm", 0)
                _sp_pcr     = _sp_res.get("pcr", 0)
                _sp_mp      = _sp_res.get("max_pain", 0)
                _sp_cew     = _sp_res.get("max_ce_wall", 0)
                _sp_pew     = _sp_res.get("max_pe_wall", 0)
                _sp_sl_mult = _sp_res.get("sl_mult", 0.65)
                _sp_tgt_mult= _sp_res.get("tgt_mult", 1.65)
                _sp_block   = _sp_res.get("block_reason", "")

                # Signal color
                if "STRONG BUY CE" in _sp_signal:
                    _spc, _spbg = "#00d4aa", "rgba(0,212,170,0.09)"
                elif "BUY CE" in _sp_signal:
                    _spc, _spbg = "#58d68d", "rgba(88,214,141,0.08)"
                elif "STRONG BUY PE" in _sp_signal:
                    _spc, _spbg = "#f85149", "rgba(248,81,73,0.11)"
                elif "BUY PE" in _sp_signal:
                    _spc, _spbg = "#ff7043", "rgba(255,112,67,0.09)"
                else:
                    _spc, _spbg = "#e6b800", "rgba(230,184,0,0.07)"

                # ── Gate status row ───────────────────────────────────────────
                st.markdown('<div class="sa-sec">Signal Gates</div>', unsafe_allow_html=True)
                _gate_icons = {"Time": "⏱", "VIX": "📊", "IV": "📈", "Confirm": "🔁"}
                _gate_html  = '<div class="sp-gate-row">'
                for _gk, (_gpass, _greason) in _sp_gates.items():
                    _gicon = _gate_icons.get(_gk, "•")
                    _gcls  = "sp-gate-pass" if _gpass else "sp-gate-fail"
                    _gmark = "✓" if _gpass else "✗"
                    _gate_html += f'<span class="{_gcls}" title="{_greason}">{_gicon} {_gk} {_gmark}</span>'
                if _sp_exp_day:
                    _gate_html += '<span class="sp-expiry-badge">🗓 EXPIRY DAY</span>'
                _gate_html += "</div>"
                st.markdown(_gate_html, unsafe_allow_html=True)

                # Conflict warning
                if _sp_conflict:
                    st.markdown(f'<div class="sp-conflict">⚠ Cross-index conflict: {_sp_symbol} is {_sp_res.get("direction","?")} but {_sp_cross_sym} is {_sp_cross_dir} — WAIT forced</div>', unsafe_allow_html=True)

                # Gate block reason
                if not _sp_gate_ok and _sp_block:
                    st.markdown(f'<div class="sp-conflict">🚧 Gate blocked: {_sp_block}</div>', unsafe_allow_html=True)

                # ── Signal banner ─────────────────────────────────────────────
                _sp_gauge   = max(2, min(98, ((_sp_score + 14) / 28) * 100))
                _sp_ci      = {"HIGH": "🔥", "MEDIUM": "⚡", "LOW": "⏳"}.get(_sp_conf, "")
                _sp_tl = (
                    f"<span style='color:{_spc};font-size:0.95rem;font-weight:700;'>Buy {_sp_symbol} {_sp_strike} {_sp_opt}</span><span style='color:#4a5568;font-size:0.8rem;margin-left:10px;'>expiry {_sp_expiry}</span>"
                    if _sp_signal != "WAIT" and _sp_strike else
                    "<span style='color:#4a5568;font-size:0.82rem;'>No directional setup — stand aside</span>"
                )
                _sp_extra = ""
                if not _sp_gate_ok:
                    _sp_extra = f"<span style='color:#f85149;font-size:0.72rem;margin-left:8px;'>⚠ Gate blocked</span>"
                elif _sp_exp_day:
                    _sp_extra = "<span style='color:#e6b800;font-size:0.72rem;margin-left:8px;'>🗓 Expiry-day thresholds active</span>"

                st.markdown(f"""
<div style="background:{_spbg};border:1.5px solid {_spc};border-radius:14px;padding:22px 28px 18px;margin:6px 0 14px;position:relative;overflow:hidden;">
  <div style="position:absolute;top:0;left:0;right:0;height:3px;background:linear-gradient(90deg,transparent,{_spc},transparent);opacity:.6;"></div>
  <div style="display:flex;align-items:flex-start;justify-content:space-between;flex-wrap:wrap;gap:16px;margin-bottom:18px;">
    <div>
      <div style="font-size:0.58rem;font-weight:800;letter-spacing:2px;text-transform:uppercase;color:{_spc};opacity:.75;margin-bottom:5px;">Signal</div>
      <div style="font-size:2.1rem;font-weight:900;color:{_spc};letter-spacing:1px;line-height:1;">{_sp_signal}</div>
      <div style="margin-top:10px;">{_sp_tl}{_sp_extra}</div>
    </div>
    <div style="text-align:right;">
      <div style="font-size:0.58rem;font-weight:800;letter-spacing:2px;text-transform:uppercase;color:#3d4a5c;margin-bottom:5px;">Score</div>
      <div style="font-size:2.4rem;font-weight:900;color:{_spc};line-height:1;">{_sp_score:+d}</div>
      <div style="font-size:0.72rem;color:#6e7681;margin-top:6px;">{_sp_ci} {_sp_conf} confidence &nbsp;·&nbsp; {_sp_ts_str}</div>
    </div>
  </div>
  <div>
    <div style="display:flex;justify-content:space-between;font-size:0.56rem;color:#3d4a5c;font-weight:700;text-transform:uppercase;letter-spacing:.8px;margin-bottom:5px;"><span>Strong PE -14</span><span>Neutral 0</span><span>Strong CE +14</span></div>
    <div style="height:7px;background:#2a2e39;border-radius:4px;overflow:hidden;"><div style="height:100%;width:100%;background:linear-gradient(90deg,#f85149 0%,#e6b800 50%,#00d4aa 100%);border-radius:4px;"></div></div>
    <div style="position:relative;height:10px;"><div style="position:absolute;left:{_sp_gauge:.1f}%;transform:translateX(-50%);top:0;width:3px;height:10px;background:{_spc};border-radius:2px;box-shadow:0 0 6px {_spc};"></div></div>
  </div>
</div>""", unsafe_allow_html=True)

                # ── Trade + Context columns ───────────────────────────────────
                _lc2, _rc2 = st.columns([11, 9])

                with _lc2:
                    if _sp_signal != "WAIT" and _sp_ltp and _sp_ltp > 0.5:
                        _tc2  = "#00d4aa" if "CE" in _sp_signal else "#f85149"
                        _ltp_s2 = f"₹{_sp_ltp:.1f}"
                        _sl_s2  = f"₹{_sp_sl:.1f}"  if _sp_sl  else "—"
                        _tgt_s2 = f"₹{_sp_tgt:.1f}" if _sp_tgt else "—"
                        _rr_s2  = f"1 : {_sp_rr}"   if _sp_rr  else "—"
                        _sl_pct  = f"{int((1 - _sp_sl_mult)*100)}%"
                        _tgt_pct = f"{int((_sp_tgt_mult - 1)*100)}%"
                        st.markdown(f"""
<div class="sa-card">
  <div class="sa-sec">Trade Setup <span style="color:#e6b800;font-size:0.55rem;">(VIX-adjusted · SL -{_sl_pct} / Tgt +{_tgt_pct})</span></div>
  <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin-bottom:14px;">
    <div><div class="sa-kv-lbl">Strike</div><div class="sa-kv-val" style="color:{_tc2};font-size:1.35rem;">{_sp_strike}</div></div>
    <div><div class="sa-kv-lbl">Type</div><div class="sa-kv-val" style="color:{_tc2};">{_sp_opt}</div></div>
    <div><div class="sa-kv-lbl">Expiry</div><div class="sa-kv-val" style="color:#adbac7;font-size:0.88rem;">{_sp_expiry}</div></div>
  </div>
  <div class="sa-divider"></div>
  <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-top:14px;">
    <div><div class="sa-kv-lbl">Entry (LTP)</div><div class="sa-kv-val">{_ltp_s2}</div></div>
    <div><div class="sa-kv-lbl">Stop Loss</div><div class="sa-kv-val" style="color:#f85149;">{_sl_s2}</div></div>
    <div><div class="sa-kv-lbl">Target</div><div class="sa-kv-val" style="color:#00d4aa;">{_tgt_s2}</div></div>
    <div><div class="sa-kv-lbl">Risk : Reward</div><div class="sa-kv-val">{_rr_s2}</div></div>
  </div>
  <div style="font-size:0.67rem;color:#3d4a5c;margin-top:14px;">⚠ For educational purposes only — not financial advice.</div>
</div>""", unsafe_allow_html=True)
                    else:
                        st.markdown("""
<div class="sa-card" style="text-align:center;padding:28px 20px;">
  <div style="color:#4a5568;font-size:0.88rem;margin-bottom:4px;">No trade setup</div>
  <div style="color:#3d4a5c;font-size:0.78rem;">Gates blocked or score below threshold</div>
</div>""", unsafe_allow_html=True)

                with _rc2:
                    _pcr_c2 = "#00d4aa" if _sp_pcr >= 1.0 else "#f85149"
                    _vwap_c = "#00d4aa" if _sp_res.get("spot", 0) > _sp_vwap > 0 else ("#f85149" if 0 < _sp_vwap < _sp_res.get("spot", 0) else "#adbac7")
                    _vix_c  = "#00d4aa" if 12 <= _sp_vix <= 18 else ("#e6b800" if _sp_vix <= 22 else "#f85149")
                    _ivr_c  = "#00d4aa" if _sp_ivr > 1.1 else ("#f85149" if _sp_ivr < 0.9 else "#adbac7")
                    _vwap_disp = f"{_sp_vwap:,.0f}" if _sp_vwap > 0 else "—"
                    _vix_disp  = f"{_sp_vix:.1f}" if _sp_vix > 0 else "—"
                    _ivr_disp  = f"{_sp_ivr:.2f}x" if _sp_ivr != 1.0 else "—"
                    _cross_disp = f"{_sp_cross_sym}: {_sp_cross_dir}" if _sp_cross_sym and _sp_cross_dir not in ("N/A", "") else "—"
                    _cross_c = "#f85149" if _sp_conflict else ("#00d4aa" if _sp_cross_dir == _sp_res.get("direction") else "#adbac7")
                    st.markdown(f"""
<div class="sa-card">
  <div class="sa-sec">Market Context</div>
  <div class="sp-ctx-grid">
    <div class="sp-ctx-item"><div class="sa-kv-lbl">Spot</div><div class="sa-kv-val" style="font-size:0.95rem;">{_sp_res.get("spot",0):,.2f}</div></div>
    <div class="sp-ctx-item"><div class="sa-kv-lbl">ATM</div><div class="sa-kv-val" style="font-size:0.95rem;">{_sp_atm:,}</div></div>
    <div class="sp-ctx-item"><div class="sa-kv-lbl">VWAP</div><div class="sa-kv-val" style="color:{_vwap_c};font-size:0.95rem;">{_vwap_disp}</div></div>
    <div class="sp-ctx-item"><div class="sa-kv-lbl">PCR</div><div class="sa-kv-val" style="color:{_pcr_c2};font-size:0.95rem;">{_sp_pcr:.3f}</div></div>
    <div class="sp-ctx-item"><div class="sa-kv-lbl">Max Pain</div><div class="sa-kv-val" style="font-size:0.95rem;">{_sp_mp:,}</div></div>
    <div class="sp-ctx-item"><div class="sa-kv-lbl">India VIX</div><div class="sa-kv-val" style="color:{_vix_c};font-size:0.95rem;">{_vix_disp}</div></div>
    <div class="sp-ctx-item"><div class="sa-kv-lbl">CE Wall</div><div class="sa-kv-val" style="color:#f85149;font-size:0.95rem;">{_sp_cew:,}</div></div>
    <div class="sp-ctx-item"><div class="sa-kv-lbl">PE Wall</div><div class="sa-kv-val" style="color:#00d4aa;font-size:0.95rem;">{_sp_pew:,}</div></div>
    <div class="sp-ctx-item"><div class="sa-kv-lbl">IV Ratio</div><div class="sa-kv-val" style="color:{_ivr_c};font-size:0.95rem;">{_ivr_disp}</div></div>
  </div>
  <div class="sa-divider"></div>
  <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;">
    <div><div class="sa-kv-lbl" style="margin-bottom:2px;">Cross-index</div><div style="font-size:0.8rem;font-weight:700;color:{_cross_c};">{_cross_disp}</div></div>
    <div style="text-align:right;"><div class="sa-kv-lbl" style="margin-bottom:2px;">SL / Target</div><div style="font-size:0.78rem;color:#adbac7;">-{int((1-_sp_sl_mult)*100)}% / +{int((_sp_tgt_mult-1)*100)}% (VIX {_vix_disp})</div></div>
  </div>
</div>""", unsafe_allow_html=True)

                # ── Factor Breakdown ──────────────────────────────────────────
                _sp_factors = _sp_res.get("factors", [])
                if _sp_factors:
                    def _sp_badge(d: str) -> str:
                        if d == "BULL": return '<span class="sa-bbull">▲ BULL</span>'
                        if d == "BEAR": return '<span class="sa-bbear">▼ BEAR</span>'
                        return '<span class="sa-bneut">— NEUTRAL</span>'

                    def _sp_pts(p: int) -> str:
                        if p > 0: return f'<span style="color:#00d4aa;font-weight:800;">{p:+d}</span>'
                        if p < 0: return f'<span style="color:#f85149;font-weight:800;">{p:+d}</span>'
                        return '<span style="color:#3d4a5c;font-weight:700;">0</span>'

                    _sp_total = sum(f["points"] for f in _sp_factors)
                    _v2_names = {"VWAP", "India VIX", "IV Spike", "Time Window", "OI Velocity"}
                    _sp_frows = "".join(
                        f'<tr style="{"background:rgba(230,184,0,0.04);" if f["name"] in _v2_names else ""}">'
                        f'<td style="color:#adbac7;font-weight:600;font-size:0.82rem;white-space:nowrap;">'
                        f'{"⚡ " if f["name"] in _v2_names else ""}{f["name"]}</td>'
                        f'<td style="color:#e6edf3;font-weight:700;font-size:0.84rem;">{f["value"]}</td>'
                        f'<td>{_sp_badge(f["direction"])}</td>'
                        f'<td class="r">{_sp_pts(f["points"])}</td>'
                        f'<td style="color:#6e7681;font-size:0.77rem;">{f["reason"]}</td>'
                        f'</tr>'
                        for f in _sp_factors
                    )
                    st.markdown('<div class="sa-sec" style="margin-top:4px;">Signal Factors <span style="color:#e6b800;font-weight:500;font-size:0.55rem;">⚡ = new v2 factors</span></div>', unsafe_allow_html=True)
                    st.markdown(f"""
<div class="sa-card" style="padding:0;overflow:hidden;">
  <div style="overflow-x:auto;">
    <table class="sa-tbl">
      <thead><tr><th>Factor</th><th>Value</th><th>Direction</th><th class="r">Pts</th><th>Interpretation</th></tr></thead>
      <tbody>{_sp_frows}</tbody>
      <tfoot><tr><td colspan="3" style="color:#6e7681;font-size:0.72rem;font-weight:700;text-transform:uppercase;letter-spacing:.8px;">Total Score (max ±14)</td><td class="r" style="font-size:1.05rem;">{_sp_pts(_sp_total)}</td><td></td></tr></tfoot>
    </table>
  </div>
</div>""", unsafe_allow_html=True)

                # ── Signal History ────────────────────────────────────────────
                _sp_hist = st.session_state.get("sa2_history", [])
                if len(_sp_hist) > 1:
                    def _sp_sig_col(s: str) -> str:
                        if "CE" in s: return "#00d4aa"
                        if "PE" in s: return "#f85149"
                        return "#e6b800"

                    def _sp_sc_col(sc: int) -> str:
                        if sc > 0: return "#00d4aa"
                        if sc < 0: return "#f85149"
                        return "#6e7681"

                    _sp_hrows = ""
                    for h in reversed(_sp_hist[-20:]):
                        _gate_badge = '<span class="sa-bbull">✓ Gates OK</span>' if h["gate_pass"] else '<span class="sa-bbear">✗ Blocked</span>'
                        _ltp_cell = ("₹" + str(round(h["ltp"], 1))) if h["ltp"] else "—"
                        _sp_hrows += (
                            f'<tr>'
                            f'<td style="color:#6e7681;font-size:0.8rem;white-space:nowrap;">{h["ts"]}</td>'
                            f'<td style="font-weight:700;color:{_sp_sig_col(h["signal"])};font-size:0.81rem;">{h["signal"]}</td>'
                            f'<td style="color:#c9d1d9;font-size:0.81rem;">{h["spot"]:,.2f}</td>'
                            f'<td style="font-weight:700;color:{_sp_sc_col(h["score"])};font-size:0.84rem;">{h["score"]:+d}</td>'
                            f'<td style="color:#adbac7;font-size:0.81rem;">{h["strike"] if h["strike"] else "—"}</td>'
                            f'<td style="color:#adbac7;font-size:0.81rem;">{_ltp_cell}</td>'
                            f'<td style="color:#6e7681;font-size:0.79rem;">{h["vix"]:.1f}</td>'
                            f'<td style="color:#6e7681;font-size:0.79rem;">{h["iv_ratio"]:.2f}x</td>'
                            f'<td>{_gate_badge}</td>'
                            f'</tr>'
                        )
                    st.markdown('<div class="sa-sec" style="margin-top:4px;">Signal History</div>', unsafe_allow_html=True)
                    st.markdown(f"""
<div class="sa-card" style="padding:0;overflow:hidden;">
  <div style="overflow-x:auto;">
    <table class="sa-tbl">
      <thead><tr><th>Time</th><th>Signal</th><th>Spot</th><th>Score</th><th>Strike</th><th>LTP</th><th>VIX</th><th>IV Ratio</th><th>Gates</th></tr></thead>
      <tbody>{_sp_hrows}</tbody>
    </table>
  </div>
</div>""", unsafe_allow_html=True)

# ── PAGE: Expiry Gamma Blast ──────────────────────────────────────────────────
def page_gamma_blast():
    _IST_GB = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

    st.markdown("""
<style>
.gb-header{display:flex;align-items:center;gap:12px;margin-bottom:4px;}
.gb-title{font-size:1.45rem;font-weight:800;color:#f0f6fc;letter-spacing:-0.5px;}
.gb-badge-exp{background:#1a2535;border:1px solid #e6b800;color:#e6b800;font-size:0.68rem;font-weight:700;padding:3px 9px;border-radius:20px;letter-spacing:0.8px;}
.gb-badge-live{background:#0d2818;border:1px solid #2ea043;color:#2ea043;font-size:0.68rem;font-weight:700;padding:3px 9px;border-radius:20px;letter-spacing:0.8px;}
.gb-countdown{font-size:1.1rem;font-weight:700;color:#e6b800;font-variant-numeric:tabular-nums;}
.gb-countdown-lbl{font-size:0.72rem;color:#6e7681;margin-right:6px;}
.gb-cdown-wrap{display:flex;align-items:center;gap:6px;background:#12191f;border:1px solid #21262d;border-radius:8px;padding:6px 14px;margin-bottom:10px;}
/* Strike Ladder */
.gb-ladder-wrap{overflow-x:auto;border-radius:10px;border:1px solid #21262d;}
.gb-tbl{width:100%;border-collapse:collapse;font-size:0.82rem;}
.gb-tbl th{background:#0d1117;color:#6e7681;font-size:0.72rem;font-weight:600;letter-spacing:0.6px;padding:7px 10px;text-transform:uppercase;border-bottom:1px solid #21262d;}
.gb-tbl td{padding:7px 10px;border-bottom:1px solid #161b22;white-space:nowrap;vertical-align:middle;}
.gb-tbl tr:last-child td{border-bottom:none;}
.gb-row-atm td{background:rgba(230,184,0,0.07)!important;}
.gb-row-atm .gb-strike-cell{color:#e6b800!important;font-weight:800;}
.gb-row-blast td{background:rgba(248,81,73,0.10)!important;animation:gb-pulse 1.2s ease-in-out infinite;}
@keyframes gb-pulse{0%,100%{background:rgba(248,81,73,0.10)!important;}50%{background:rgba(248,81,73,0.22)!important;}}
.gb-ce-side{text-align:right;}
.gb-pe-side{text-align:left;}
.gb-strike-cell{text-align:center;font-weight:700;color:#c9d1d9;font-size:0.88rem;}
.gb-dist-cell{text-align:center;font-size:0.73rem;color:#6e7681;}
.gb-ltp{font-weight:600;color:#c9d1d9;}
.gb-oi-bar-ce{display:flex;align-items:center;justify-content:flex-end;gap:6px;}
.gb-oi-bar-pe{display:flex;align-items:center;justify-content:flex-start;gap:6px;}
.gb-bar-fill-ce{height:6px;background:#00d4aa;border-radius:3px;min-width:2px;}
.gb-bar-fill-pe{height:6px;background:#f85149;border-radius:3px;min-width:2px;}
.gb-oi-num{color:#adbac7;font-size:0.79rem;}
.gb-delta-pos{color:#f85149;font-size:0.73rem;font-weight:600;}
.gb-delta-neg{color:#00d4aa;font-size:0.73rem;font-weight:600;}
.gb-delta-neu{color:#6e7681;font-size:0.73rem;}
.gb-fire{color:#ff6b35;font-size:0.85rem;}
/* Signal banners */
.gb-banner{border-radius:12px;padding:20px 24px;margin:12px 0;position:relative;overflow:hidden;}
.gb-banner-blast{background:linear-gradient(135deg,#1a0a00 0%,#2a0d0d 100%);border:2px solid #f85149;box-shadow:0 0 20px rgba(248,81,73,0.3);}
.gb-banner-preblast{background:linear-gradient(135deg,#1a1000 0%,#1f1a00 100%);border:2px solid #e6b800;}
.gb-banner-building{background:linear-gradient(135deg,#0d1117 0%,#12191f 100%);border:1px solid #e6b800;}
.gb-banner-watch{background:linear-gradient(135deg,#0d1117 0%,#12191f 100%);border:1px solid #30363d;}
.gb-banner-wait{background:#0d1117;border:1px solid #21262d;}
.gb-sig-label{font-size:0.72rem;color:#6e7681;letter-spacing:1px;text-transform:uppercase;margin-bottom:4px;}
.gb-sig-text-blast{font-size:1.8rem;font-weight:900;color:#f85149;letter-spacing:-1px;}
.gb-sig-text-pre{font-size:1.5rem;font-weight:800;color:#e6b800;}
.gb-sig-text-building{font-size:1.4rem;font-weight:700;color:#e6b800;}
.gb-sig-text-watch{font-size:1.4rem;font-weight:700;color:#adbac7;}
.gb-sig-text-wait{font-size:1.4rem;font-weight:700;color:#6e7681;}
.gb-sig-sub{font-size:0.84rem;color:#6e7681;margin-top:4px;}
.gb-score-box{position:absolute;top:16px;right:20px;text-align:right;}
.gb-score-lbl{font-size:0.68rem;color:#6e7681;letter-spacing:0.8px;}
.gb-score-ce{font-size:1.1rem;font-weight:700;color:#00d4aa;}
.gb-score-pe{font-size:1.1rem;font-weight:700;color:#f85149;}
/* Trade setup */
.gb-setup{background:#0d1117;border:1px solid #21262d;border-radius:10px;padding:16px;}
.gb-kv{display:flex;justify-content:space-between;align-items:center;padding:5px 0;border-bottom:1px solid #161b22;}
.gb-kv:last-child{border-bottom:none;}
.gb-kv-lbl{font-size:0.74rem;color:#6e7681;letter-spacing:0.4px;}
.gb-kv-val{font-size:0.92rem;font-weight:700;color:#f0f6fc;}
.gb-kv-sl{color:#f85149!important;}
.gb-kv-tgt{color:#00d4aa!important;}
/* Factor table */
.gb-factor-dir-bull{display:inline-block;background:#0d2818;color:#2ea043;border:1px solid #2ea043;font-size:0.68rem;padding:1px 7px;border-radius:4px;font-weight:700;}
.gb-factor-dir-bear{display:inline-block;background:#2a0d0d;color:#f85149;border:1px solid #f85149;font-size:0.68rem;padding:1px 7px;border-radius:4px;font-weight:700;}
.gb-factor-dir-neu{display:inline-block;background:#161b22;color:#6e7681;border:1px solid #30363d;font-size:0.68rem;padding:1px 7px;border-radius:4px;}
</style>
""", unsafe_allow_html=True)

    # ── Header ────────────────────────────────────────────────────────────────
    _gb_now = datetime.datetime.now(_IST_GB)
    _gb_close = _gb_now.replace(hour=15, minute=30, second=0, microsecond=0)
    _gb_secs_left = max(0, int((_gb_close - _gb_now).total_seconds()))
    _gb_hh = _gb_secs_left // 3600
    _gb_mm = (_gb_secs_left % 3600) // 60
    _gb_ss = _gb_secs_left % 60

    st.markdown("""
<div class="gb-header">
  <span class="gb-title">💥 Expiry Gamma Blast</span>
  <span class="gb-badge-exp">EXPIRY DAY</span>
  <span class="gb-badge-live">LIVE</span>
</div>""", unsafe_allow_html=True)

    st.markdown(
        f'<div class="gb-cdown-wrap">'
        f'<span class="gb-countdown-lbl">Market closes in</span>'
        f'<span class="gb-countdown">{_gb_hh:02d}:{_gb_mm:02d}:{_gb_ss:02d}</span>'
        f'<span class="gb-countdown-lbl" style="margin-left:10px;">Gamma blast window: 2:00 PM – 3:15 PM IST</span>'
        f'</div>',
        unsafe_allow_html=True,
    )

    # ── Kite check ───────────────────────────────────────────────────────────
    _gb_key  = st.session_state.get("kite_api_key", "")
    _gb_tok  = st.session_state.get("kite_access_token", "")
    _kite_ok = bool(_gb_key and _gb_tok)

    if not _kite_ok:
        st.markdown('<div class="sa-card" style="text-align:center;padding:40px 20px;"><div style="font-size:2rem;">🔑</div><div style="color:#6e7681;margin-top:8px;">Enter Kite API Key &amp; Access Token in the sidebar to start scanning</div></div>', unsafe_allow_html=True)
    else:
        # ── Control panel ─────────────────────────────────────────────────────
        with st.container(border=True):
            _gb_c1, _gb_c2, _gb_c3, _gb_c4 = st.columns([2, 3, 2, 3])
            with _gb_c1:
                _gb_sym = st.selectbox("Index", ["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX", "BANKEX", "MIDCPNIFTY", "CRUDEOIL", "GOLD", "SILVER"], key="gb_sym")
            with _gb_c2:
                # Get expiry list from sa_instr if already loaded, else show text input
                _gb_instr = st.session_state.get("gb_instr")
                if _gb_instr is not None and not _gb_instr.empty:
                    _gb_exp_opts = sorted(
                        _gb_instr[(_gb_instr["name"] == _gb_sym) & (_gb_instr["instrument_type"].isin(["CE", "PE"]))]["expiry"].unique()
                    )
                    _gb_exp_default = _gb_exp_opts[0] if _gb_exp_opts else ""
                    _gb_expiry = st.selectbox("Expiry", _gb_exp_opts, key="gb_expiry_sel") if _gb_exp_opts else st.text_input("Expiry (e.g. 2025-07-03)", key="gb_expiry_txt")
                else:
                    _gb_expiry = st.text_input("Expiry (load first)", key="gb_expiry_txt", placeholder="e.g. 2025-07-03")
            with _gb_c3:
                _gb_interval = st.selectbox("Refresh", [30, 45, 60], format_func=lambda x: f"{x}s", key="gb_interval")
            with _gb_c4:
                _gb_lc1, _gb_lc2 = st.columns(2)
                with _gb_lc1:
                    _gb_load_btn = st.button("📥 Load", key="gb_load", use_container_width=True)
                with _gb_lc2:
                    _gb_scan_btn = st.button("🔍 Scan Now", key="gb_scan", use_container_width=True, type="primary")

        # ── Load instruments ──────────────────────────────────────────────────
        if _gb_load_btn:
            with st.spinner("Downloading NFO instruments…"):
                try:
                    _gb_raw = toi_fetch_instruments(_gb_key, _gb_tok, _gb_sym)
                    st.session_state["gb_instr"] = _gb_raw
                    st.session_state["gb_prev_chain"] = None
                    st.session_state["gb_spot_history"] = []
                    st.session_state["gb_history"] = []
                    st.success(f"Loaded {len(_gb_raw)} instruments")
                    st.rerun()
                except Exception as _gb_e:
                    st.error(f"Load failed: {_gb_e}")

        # ── Auto-refresh timer ────────────────────────────────────────────────
        _gb_h_now = _gb_now.hour * 60 + _gb_now.minute
        _gb_in_window = (14 * 60) <= _gb_h_now < (15 * 60 + 15)
        if _gb_in_window and st.session_state.get("gb_instr") is not None:
            _st_autorefresh(interval=_gb_interval * 1000, key="gb_refresh")

        # ── Run scan ──────────────────────────────────────────────────────────
        def _do_gb_scan():
            _instr = st.session_state.get("gb_instr")
            if _instr is None or _instr.empty:
                st.warning("Load instruments first (click 📥 Load)")
                return
            _exp = st.session_state.get("gb_expiry_sel") or st.session_state.get("gb_expiry_txt", "")
            if not _exp:
                st.warning("Select an expiry date")
                return
            with st.spinner("Scanning option chain…"):
                try:
                    _res = run_gamma_blast_scan(
                        _gb_key, _gb_tok,
                        _gb_sym, _exp, _instr,
                        spot_history=st.session_state.get("gb_spot_history"),
                        prev_chain=st.session_state.get("gb_prev_chain"),
                    )
                    if "error" in _res:
                        st.error(_res["error"])
                        return
                    st.session_state["gb_last_signal"] = _res
                    st.session_state["gb_last_fetch"]  = datetime.datetime.now(_IST_GB)
                    st.session_state["gb_prev_chain"]  = _res["new_prev_chain"]
                    # Spot history
                    _sh = st.session_state.get("gb_spot_history", [])
                    _sh.append(_res["spot"])
                    st.session_state["gb_spot_history"] = _sh[-20:]
                    # Signal history (record if actionable)
                    _sig = _res["signal"]
                    if _sig not in ("WAIT", "WATCH"):
                        _hist = st.session_state.get("gb_history", [])
                        _hist.append({
                            "ts":      _res["ist_now"].strftime("%H:%M:%S"),
                            "signal":  _sig,
                            "spot":    _res["spot"],
                            "ce_sc":   _res["score_ce"],
                            "pe_sc":   _res["score_pe"],
                            "strike":  _res.get("recommended_strike"),
                            "type":    _res.get("recommended_type"),
                            "ltp":     _res.get("ltp_entry"),
                        })
                        st.session_state["gb_history"] = _hist[-30:]
                except Exception as _e:
                    st.error(f"Scan error: {_e}")

        if _gb_scan_btn:
            _do_gb_scan()
        elif _gb_in_window and st.session_state.get("gb_instr") is not None:
            _do_gb_scan()

        # ── Display results ───────────────────────────────────────────────────
        _gb_res = st.session_state.get("gb_last_signal")
        _gb_fetch_ts = st.session_state.get("gb_last_fetch")

        if _gb_fetch_ts:
            _gb_age = int((datetime.datetime.now(_IST_GB) - _gb_fetch_ts).total_seconds())
            st.caption(f"Last scan: {_gb_fetch_ts.strftime('%H:%M:%S')} IST  ·  {_gb_age}s ago")

        if _gb_res:
            _gb_sig   = _gb_res["signal"]
            _gb_chain = _gb_res["chain"]
            _gb_spot  = _gb_res["spot"]
            _gb_atm   = _gb_res["atm"]

            # ── Gamma signal banner ───────────────────────────────────────────
            if "GAMMA BLAST" in _gb_sig:
                _banner_cls = "gb-banner-blast"
                _sig_cls    = "gb-sig-text-blast"
                _sig_emoji  = "💥 "
            elif "PRE-BLAST" in _gb_sig:
                _banner_cls = "gb-banner-preblast"
                _sig_cls    = "gb-sig-text-pre"
                _sig_emoji  = "⚡ "
            elif "BUILDING" in _gb_sig:
                _banner_cls = "gb-banner-building"
                _sig_cls    = "gb-sig-text-building"
                _sig_emoji  = "🔥 "
            elif _gb_sig == "WATCH":
                _banner_cls = "gb-banner-watch"
                _sig_cls    = "gb-sig-text-watch"
                _sig_emoji  = "👁 "
            else:
                _banner_cls = "gb-banner-wait"
                _sig_cls    = "gb-sig-text-wait"
                _sig_emoji  = ""

            _gb_detail = _gb_res.get("signal_detail", "")
            _gb_sd_txt = _gb_res.get("spot_direction", "FLAT")
            _gb_dir_arrow = {"UP": "⬆ Spot rising", "DOWN": "⬇ Spot falling", "FLAT": "➡ Sideways"}.get(_gb_sd_txt, "")

            st.markdown(
                f'<div class="gb-banner {_banner_cls}">'
                f'<div class="gb-sig-label">GAMMA SIGNAL</div>'
                f'<div class="{_sig_cls}">{_sig_emoji}{_gb_sig}</div>'
                f'<div class="gb-sig-sub">{_gb_detail if _gb_detail else _gb_dir_arrow}</div>'
                f'<div class="gb-score-box">'
                f'<div class="gb-score-lbl">CE SCORE</div>'
                f'<div class="gb-score-ce">+{_gb_res["score_ce"]}</div>'
                f'<div class="gb-score-lbl" style="margin-top:4px;">PE SCORE</div>'
                f'<div class="gb-score-pe">+{_gb_res["score_pe"]}</div>'
                f'</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

            # ── Two columns: Setup | Strike Ladder ───────────────────────────
            _gb_col1, _gb_col2 = st.columns([1, 2])

            with _gb_col1:
                st.markdown('<div class="sa-sec">Trade Setup</div>', unsafe_allow_html=True)
                _rs  = _gb_res.get("recommended_strike")
                _rt  = _gb_res.get("recommended_type")
                _ltp = _gb_res.get("ltp_entry")
                _sl  = _gb_res.get("ltp_sl")
                _tgt = _gb_res.get("ltp_target")

                if _rs and _rt and _ltp:
                    st.markdown(
                        f'<div class="gb-setup">'
                        f'<div class="gb-kv"><span class="gb-kv-lbl">INSTRUMENT</span><span class="gb-kv-val">{_gb_sym} {int(_rs)} {_rt}</span></div>'
                        f'<div class="gb-kv"><span class="gb-kv-lbl">ENTRY LTP</span><span class="gb-kv-val">₹{_ltp:.1f}</span></div>'
                        f'<div class="gb-kv"><span class="gb-kv-lbl">STOP LOSS</span><span class="gb-kv-val gb-kv-sl">₹{_sl:.1f} (−50%)</span></div>'
                        f'<div class="gb-kv"><span class="gb-kv-lbl">TARGET</span><span class="gb-kv-val gb-kv-tgt">₹{_tgt:.1f} (3×)</span></div>'
                        f'<div class="gb-kv"><span class="gb-kv-lbl">SPOT</span><span class="gb-kv-val">{_gb_spot:,.2f}</span></div>'
                        f'<div class="gb-kv"><span class="gb-kv-lbl">ATM</span><span class="gb-kv-val">{int(_gb_atm)}</span></div>'
                        f'<div class="gb-kv"><span class="gb-kv-lbl">DIRECTION</span><span class="gb-kv-val">{_gb_sd_txt} {_gb_dir_arrow}</span></div>'
                        f'<div style="margin-top:10px;padding:8px;background:#1a0000;border-radius:6px;border:1px solid #f85149;">'
                        f'<div style="color:#f85149;font-size:0.72rem;font-weight:700;">⚠ EXPIRY DAY RULES</div>'
                        f'<div style="color:#6e7681;font-size:0.71rem;margin-top:4px;">Exit by 3:15 PM — no exceptions.<br>50% SL is firm. 3× target then exit.<br>Do NOT hold through 3:20 PM.</div>'
                        f'</div>'
                        f'</div>',
                        unsafe_allow_html=True,
                    )
                else:
                    st.markdown(
                        '<div class="gb-setup" style="text-align:center;padding:30px 16px;">'
                        '<div style="color:#6e7681;font-size:0.84rem;">No trade setup yet.<br>Waiting for blast zone signal.</div>'
                        '</div>',
                        unsafe_allow_html=True,
                    )

                # Blast zone indicators
                st.markdown('<div class="sa-sec" style="margin-top:12px;">Pressure Walls</div>', unsafe_allow_html=True)
                _ce_bs = _gb_res.get("blast_strike_ce")
                _pe_bs = _gb_res.get("blast_strike_pe")
                _ce_oi = _gb_res.get("pressure_oi_ce", 0)
                _pe_oi = _gb_res.get("pressure_oi_pe", 0)
                _cov_ce = _gb_res.get("covering_ce", False)
                _cov_pe = _gb_res.get("covering_pe", False)

                _wall_rows = ""
                if _ce_bs:
                    _dist_ce = abs(_ce_bs - _gb_spot) / _gb_spot * 100
                    _cov_badge = ' <span style="color:#ff6b35;font-size:0.7rem;">🔥 COVERING</span>' if _cov_ce else ""
                    _wall_rows += (
                        f'<div class="gb-kv">'
                        f'<span class="gb-kv-lbl">CE WALL {int(_ce_bs)}{_cov_badge}</span>'
                        f'<span class="gb-kv-val" style="color:#00d4aa;">{_ce_oi/1e5:.1f}L · {_dist_ce:.2f}% away</span>'
                        f'</div>'
                    )
                if _pe_bs:
                    _dist_pe = abs(_gb_spot - _pe_bs) / _gb_spot * 100
                    _cov_badge2 = ' <span style="color:#ff6b35;font-size:0.7rem;">🔥 COVERING</span>' if _cov_pe else ""
                    _wall_rows += (
                        f'<div class="gb-kv">'
                        f'<span class="gb-kv-lbl">PE WALL {int(_pe_bs)}{_cov_badge2}</span>'
                        f'<span class="gb-kv-val" style="color:#f85149;">{_pe_oi/1e5:.1f}L · {_dist_pe:.2f}% away</span>'
                        f'</div>'
                    )
                if _wall_rows:
                    st.markdown(f'<div class="gb-setup">{_wall_rows}</div>', unsafe_allow_html=True)

            with _gb_col2:
                st.markdown('<div class="sa-sec">Strike Ladder — Nearby OI</div>', unsafe_allow_html=True)

                # Build strike ladder HTML
                _max_ce_oi = max(_gb_chain["CE OI"].max(), 1)
                _max_pe_oi = max(_gb_chain["PE OI"].max(), 1)

                _ladder_rows = ""
                for _, _row in _gb_chain.iterrows():
                    _s    = _row["Strike"]
                    _ce_o = int(_row["CE OI"])
                    _pe_o = int(_row["PE OI"])
                    _ce_l = _row["CE LTP"]
                    _pe_l = _row["PE LTP"]
                    _ce_d = int(_row.get("CE ΔOI", 0))
                    _pe_d = int(_row.get("PE ΔOI", 0))

                    _is_atm   = (_s == _gb_atm)
                    _is_blast = (
                        (_ce_bs is not None and _s == _ce_bs and abs(_s - _gb_spot) / _gb_spot * 100 < 0.30) or
                        (_pe_bs is not None and _s == _pe_bs and abs(_gb_spot - _s) / _gb_spot * 100 < 0.30)
                    )
                    _row_cls = "gb-row-blast" if _is_blast else ("gb-row-atm" if _is_atm else "")

                    # Distance from spot
                    _dist_pct = (_s - _gb_spot) / _gb_spot * 100
                    if abs(_dist_pct) < 0.01:
                        _dist_str = "← SPOT"
                    else:
                        _dist_str = f"{_dist_pct:+.2f}%"

                    # OI bars (max 120px wide)
                    _ce_bar_w = int(_ce_o / _max_ce_oi * 120)
                    _pe_bar_w = int(_pe_o / _max_pe_oi * 120)

                    # Delta strings
                    def _delta_str(d):
                        if d == 0: return '<span class="gb-delta-neu">—</span>'
                        cls = "gb-delta-neg" if d < 0 else "gb-delta-pos"
                        return f'<span class="{cls}">{d/1e5:+.1f}L</span>'

                    _blast_icon = "⚡" if _is_blast else ""
                    _fire_ce = " 🔥" if (_cov_ce and _s == _ce_bs) else ""
                    _fire_pe = " 🔥" if (_cov_pe and _s == _pe_bs) else ""

                    _ladder_rows += (
                        f'<tr class="{_row_cls}">'
                        f'<td class="gb-ce-side">'
                        f'  <div class="gb-oi-bar-ce">'
                        f'    <span class="gb-oi-num">{_ce_o/1e5:.1f}L{_fire_ce}</span>'
                        f'    <div class="gb-bar-fill-ce" style="width:{_ce_bar_w}px;"></div>'
                        f'  </div>'
                        f'</td>'
                        f'<td class="gb-ce-side"><span class="gb-ltp">{"₹"+str(round(_ce_l,1)) if _ce_l > 0 else "—"}</span></td>'
                        f'<td class="gb-ce-side">{_delta_str(_ce_d)}</td>'
                        f'<td class="gb-strike-cell">{_blast_icon}{int(_s)}</td>'
                        f'<td class="gb-dist-cell">{_dist_str}</td>'
                        f'<td class="gb-pe-side">{_delta_str(_pe_d)}</td>'
                        f'<td class="gb-pe-side"><span class="gb-ltp">{"₹"+str(round(_pe_l,1)) if _pe_l > 0 else "—"}</span></td>'
                        f'<td class="gb-pe-side">'
                        f'  <div class="gb-oi-bar-pe">'
                        f'    <div class="gb-bar-fill-pe" style="width:{_pe_bar_w}px;"></div>'
                        f'    <span class="gb-oi-num">{_pe_o/1e5:.1f}L{_fire_pe}</span>'
                        f'  </div>'
                        f'</td>'
                        f'</tr>'
                    )

                st.markdown(
                    f'<div class="gb-ladder-wrap">'
                    f'<table class="gb-tbl">'
                    f'<thead><tr>'
                    f'<th class="gb-ce-side">CE OI (L)</th>'
                    f'<th class="gb-ce-side">CE LTP</th>'
                    f'<th class="gb-ce-side">CE ΔOI</th>'
                    f'<th style="text-align:center;">STRIKE</th>'
                    f'<th style="text-align:center;">DIST</th>'
                    f'<th class="gb-pe-side">PE ΔOI</th>'
                    f'<th class="gb-pe-side">PE LTP</th>'
                    f'<th class="gb-pe-side">PE OI (L)</th>'
                    f'</tr></thead>'
                    f'<tbody>{_ladder_rows}</tbody>'
                    f'</table>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

            # ── Factor breakdown ──────────────────────────────────────────────
            _gb_factors = _gb_res.get("factors", [])
            if _gb_factors:
                st.markdown('<div class="sa-sec" style="margin-top:14px;">Factor Breakdown</div>', unsafe_allow_html=True)
                _gb_frows = ""
                for _fn, _fv, _fd, _fp in _gb_factors:
                    _dir_cls = "gb-factor-dir-bull" if _fd == "BULL" else ("gb-factor-dir-bear" if _fd == "BEAR" else "gb-factor-dir-neu")
                    _pts_col = "#00d4aa" if _fp > 0 else ("#f85149" if _fp < 0 else "#6e7681")
                    _gb_frows += (
                        f'<tr>'
                        f'<td style="color:#adbac7;font-size:0.82rem;white-space:nowrap;">{_fn}</td>'
                        f'<td style="color:#c9d1d9;font-size:0.8rem;">{_fv}</td>'
                        f'<td><span class="{_dir_cls}">{_fd}</span></td>'
                        f'<td style="font-weight:700;color:{_pts_col};font-size:0.85rem;">{_fp:+d}</td>'
                        f'</tr>'
                    )
                st.markdown(
                    f'<div class="sa-card" style="padding:0;overflow:hidden;">'
                    f'<div style="overflow-x:auto;">'
                    f'<table class="sa-tbl">'
                    f'<thead><tr><th>Factor</th><th>Reading</th><th>Direction</th><th>Pts</th></tr></thead>'
                    f'<tbody>{_gb_frows}</tbody>'
                    f'</table>'
                    f'</div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

        # ── Signal history ────────────────────────────────────────────────────
        _gb_hist = st.session_state.get("gb_history", [])
        if _gb_hist:
            st.markdown('<div class="sa-sec" style="margin-top:14px;">Signal History (This Session)</div>', unsafe_allow_html=True)

            def _gb_sig_col(sig):
                if "BLAST" in sig: return "#f85149"
                if "PRE" in sig:   return "#e6b800"
                if "BUILD" in sig: return "#ff6b35"
                return "#adbac7"

            _gb_hrows = ""
            for _h in reversed(_gb_hist[-20:]):
                _ltp_s = ("₹" + str(round(_h["ltp"], 1))) if _h.get("ltp") else "—"
                _str_s = str(int(_h["strike"])) if _h.get("strike") else "—"
                _gb_hrows += (
                    f'<tr>'
                    f'<td style="color:#6e7681;font-size:0.8rem;white-space:nowrap;">{_h["ts"]}</td>'
                    f'<td style="font-weight:700;color:{_gb_sig_col(_h["signal"])};font-size:0.81rem;">{_h["signal"]}</td>'
                    f'<td style="color:#c9d1d9;font-size:0.81rem;">{_h["spot"]:,.2f}</td>'
                    f'<td style="color:#00d4aa;font-size:0.82rem;">{_h["ce_sc"]:+d}</td>'
                    f'<td style="color:#f85149;font-size:0.82rem;">{_h["pe_sc"]:+d}</td>'
                    f'<td style="color:#adbac7;">{_str_s} {_h.get("type","")}</td>'
                    f'<td style="color:#adbac7;">{_ltp_s}</td>'
                    f'</tr>'
                )
            st.markdown(
                f'<div class="sa-card" style="padding:0;overflow:hidden;">'
                f'<div style="overflow-x:auto;">'
                f'<table class="sa-tbl">'
                f'<thead><tr><th>Time</th><th>Signal</th><th>Spot</th><th>CE Sc</th><th>PE Sc</th><th>Strike</th><th>LTP</th></tr></thead>'
                f'<tbody>{_gb_hrows}</tbody>'
                f'</table>'
                f'</div>'
                f'</div>',
                unsafe_allow_html=True,
            )


# ── PAGE: CPR Retracement ─────────────────────────────────────────────────────
def page_cpr_retracement():
    st.markdown("#### 🎯 CPR Retracement — Bounces & Rejections at Pivot Levels")
    st.markdown(
        '<small style="color:#8b949e;">'
        'Scans NIFTY 500 daily candles for two patterns:<br>'
        '&nbsp;&nbsp;<b style="color:#00d4aa;">📈 CPR Bounce</b> — stock was falling, low tagged TC/Pivot/BC, closed back above (support held)<br>'
        '&nbsp;&nbsp;<b style="color:#f85149;">📉 CPR Rejection</b> — stock was rising, high tagged TC/Pivot/BC, closed back below (resistance held)<br>'
        'CPR for each day is calculated from the prior day\'s High + Low + Close. '
        'Touch tolerance = % distance allowed between candle extreme and CPR level.'
        '</small>',
        unsafe_allow_html=True,
    )

    with st.container(border=True):
        cpr_c1, cpr_c2, cpr_c3 = st.columns([2, 4, 2])
        cpr_touch_pct = cpr_c1.slider(
            "Touch tolerance (%)", min_value=1, max_value=3, value=1,
            key="cpr_touch_pct",
            help="How close the candle's High/Low must be to a CPR level to count as a touch",
        )
        cpr_c2.markdown(
            '<small style="color:#8b949e;">'
            'Pattern A: today\'s candle touches today\'s CPR and closes in reversal direction<br>'
            'Pattern B: yesterday\'s candle touched yesterday\'s CPR, today confirms the move'
            '</small>',
            unsafe_allow_html=True,
        )
        cpr_run_btn = cpr_c3.button(
            "🔍 Run CPR Scan", type="primary", use_container_width=True, key="run_cpr"
        )

    if cpr_run_btn:
        with st.spinner("Scanning NIFTY 500 for CPR retracements… (3–5 min)"):
            _cpr_kite_key   = st.session_state.get("kite_api_key", "")
            _cpr_kite_token = st.session_state.get("kite_access_token", "")
            _cpr_syms       = get_nifty500_symbols()
            cpr_results     = run_cpr_retracement_scan(
                _cpr_syms,
                touch_pct=cpr_touch_pct / 100,
                api_key=_cpr_kite_key,
                access_token=_cpr_kite_token,
            )
            st.session_state["cpr_results"] = cpr_results

    if "cpr_results" in st.session_state:
        cpr_df: pd.DataFrame = st.session_state["cpr_results"]
        if cpr_df.empty:
            st.warning(
                "No CPR retracements found. Try increasing touch tolerance or check back "
                "after today's session closes."
            )
        else:
            _n_bounce = len(cpr_df[cpr_df["Direction"] == "BULL"])
            _n_reject = len(cpr_df[cpr_df["Direction"] == "BEAR"])
            _n_2day   = len(cpr_df[cpr_df["Pattern"] == "2-Day"])
            cc1, cc2, cc3, cc4 = st.columns(4)
            cc1.metric("Total Signals",       len(cpr_df))
            cc2.metric("📈 Bounces (BULL)",   _n_bounce)
            cc3.metric("📉 Rejections (BEAR)", _n_reject)
            cc4.metric("2-Day Patterns",       _n_2day)

            # Filter toggle
            cpr_f1, cpr_f2 = st.columns([3, 1])
            _cpr_dir_filter = cpr_f1.radio(
                "Show:", ["All", "📈 Bounces only", "📉 Rejections only"],
                horizontal=True, key="cpr_dir_filter",
            )
            _display_df = cpr_df.copy()
            if _cpr_dir_filter == "📈 Bounces only":
                _display_df = _display_df[_display_df["Direction"] == "BULL"]
            elif _cpr_dir_filter == "📉 Rejections only":
                _display_df = _display_df[_display_df["Direction"] == "BEAR"]

            if _display_df.empty:
                st.info("No signals match the selected filter.")
            else:
                _CPR_SIGNAL_COLORS = {
                    "📈 CPR Bounce":    "color: #00d4aa; font-weight: 700",
                    "📉 CPR Rejection": "color: #f85149; font-weight: 700",
                }
                _CPR_LEVEL_COLORS = {
                    "TC":    "color: #00d4aa",
                    "Pivot": "color: #e6b800",
                    "BC":    "color: #f85149",
                }

                def _cpr_signal_style(v):
                    return _CPR_SIGNAL_COLORS.get(v, "")

                def _cpr_level_style(v):
                    return _CPR_LEVEL_COLORS.get(v, "")

                _price_cols  = ["Price", "Touch Price", "Level Price", "CPR TC", "CPR Pivot", "CPR BC"]
                _pct_cols    = ["Change%", "5D Trend%"]
                _show_cols   = [
                    "Symbol", "Company", "Signal", "Pattern", "CPR Level",
                    "Level Price", "CPR TC", "CPR Pivot", "CPR BC",
                    "Touch Price", "Price", "Change%", "5D Trend%", "Vol Ratio",
                ]
                _show_cols = [c for c in _show_cols if c in _display_df.columns]

                styled_cpr = (
                    _display_df[_show_cols].copy().style
                    .map(_cpr_signal_style, subset=["Signal"])
                    .map(_cpr_level_style,  subset=["CPR Level"])
                    .map(change_style,      subset=["Change%", "5D Trend%"])
                    .format({c: "₹{:.2f}" for c in _price_cols if c in _display_df.columns})
                    .format({c: "{:+.2f}%" for c in _pct_cols   if c in _display_df.columns})
                    .format({"Vol Ratio": "{:.2f}x"} if "Vol Ratio" in _display_df.columns else {})
                )

                st.caption("👆 Click any row to open chart · Esc to close")
                cpr_sel = st.dataframe(
                    styled_cpr, use_container_width=True, height=520,
                    on_select="rerun", selection_mode="single-row", key="cpr_table",
                )

                csv_cpr = _display_df.to_csv(index=False).encode("utf-8")
                st.download_button("⬇️ Export CSV", csv_cpr, "cpr_retracement.csv", "text/csv")

                cpr_rows = cpr_sel.selection.get("rows", []) if cpr_sel else []
                if cpr_rows:
                    _cr = _display_df.iloc[cpr_rows[0]]
                    _cpr_lvls = {
                        k: float(_cr[k])
                        for k in ["CPR TC", "CPR Pivot", "CPR BC"]
                        if k in _cr
                    }
                    chart_modal(_cr["Symbol"] + ".NS", _cr["Company"], "1D",
                                extra_levels=_cpr_lvls)
    else:
        st.info(
            "🔎 Click **Run CPR Scan** to find stocks bouncing off or being rejected by "
            "CPR (Central Pivot Range) levels."
        )


# ── PAGE: Intraday OI Pulse ───────────────────────────────────────────────────
def page_oi_pulse():
    _IST_OP = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

    st.markdown("""
<style>
.op-header{display:flex;align-items:center;gap:12px;margin-bottom:4px;}
.op-title{font-size:1.45rem;font-weight:800;color:#f0f6fc;letter-spacing:-0.5px;}
.op-badge-live{background:#0d2818;border:1px solid #2ea043;color:#2ea043;font-size:0.68rem;font-weight:700;padding:3px 9px;border-radius:20px;letter-spacing:0.8px;}
.op-badge-day{background:#12191f;border:1px solid #00d4aa;color:#00d4aa;font-size:0.68rem;font-weight:700;padding:3px 9px;border-radius:20px;letter-spacing:0.8px;}
.op-cdown-wrap{display:flex;align-items:center;gap:6px;background:#12191f;border:1px solid #21262d;border-radius:8px;padding:6px 14px;margin-bottom:10px;}
.op-countdown{font-size:1.1rem;font-weight:700;color:#00d4aa;font-variant-numeric:tabular-nums;}
.op-countdown-lbl{font-size:0.72rem;color:#6e7681;margin-right:6px;}
/* Strike Ladder */
.op-ladder-wrap{overflow-x:auto;border-radius:10px;border:1px solid #21262d;}
.op-tbl{width:100%;border-collapse:collapse;font-size:0.82rem;}
.op-tbl th{background:#0d1117;color:#6e7681;font-size:0.72rem;font-weight:600;letter-spacing:0.6px;padding:7px 10px;text-transform:uppercase;border-bottom:1px solid #21262d;}
.op-tbl td{padding:7px 10px;border-bottom:1px solid #161b22;white-space:nowrap;vertical-align:middle;}
.op-tbl tr:last-child td{border-bottom:none;}
.op-row-atm td{background:rgba(0,212,170,0.07)!important;}
.op-row-atm .op-strike-cell{color:#00d4aa!important;font-weight:800;}
.op-row-ce-wall td{background:rgba(248,81,73,0.07)!important;}
.op-row-pe-wall td{background:rgba(0,212,170,0.07)!important;}
.op-ce-side{text-align:right;}
.op-pe-side{text-align:left;}
.op-strike-cell{text-align:center;font-weight:700;color:#c9d1d9;font-size:0.88rem;}
.op-dist-cell{text-align:center;font-size:0.73rem;color:#6e7681;}
.op-ltp{font-weight:600;color:#c9d1d9;}
.op-oi-bar-ce{display:flex;align-items:center;justify-content:flex-end;gap:6px;}
.op-oi-bar-pe{display:flex;align-items:center;justify-content:flex-start;gap:6px;}
.op-bar-fill-ce{height:6px;background:#00d4aa;border-radius:3px;min-width:2px;}
.op-bar-fill-pe{height:6px;background:#f85149;border-radius:3px;min-width:2px;}
.op-oi-num{color:#adbac7;font-size:0.79rem;}
.op-delta-pos{color:#f85149;font-size:0.73rem;font-weight:600;}
.op-delta-neg{color:#00d4aa;font-size:0.73rem;font-weight:600;}
.op-delta-neu{color:#6e7681;font-size:0.73rem;}
/* Signal banners */
.op-banner{border-radius:12px;padding:20px 24px;margin:12px 0;position:relative;overflow:hidden;}
.op-banner-strong-ce{background:linear-gradient(135deg,#001a0d 0%,#002a15 100%);border:2px solid #2ea043;box-shadow:0 0 20px rgba(46,160,67,0.3);}
.op-banner-ce{background:linear-gradient(135deg,#001008 0%,#001a10 100%);border:2px solid #00d4aa;}
.op-banner-strong-pe{background:linear-gradient(135deg,#1a0000 0%,#2a0d0d 100%);border:2px solid #f85149;box-shadow:0 0 20px rgba(248,81,73,0.3);}
.op-banner-pe{background:linear-gradient(135deg,#120000 0%,#1a0808 100%);border:2px solid #da3633;}
.op-banner-watch{background:linear-gradient(135deg,#0d1117 0%,#12191f 100%);border:1px solid #e6b800;}
.op-banner-wait{background:#0d1117;border:1px solid #21262d;}
.op-sig-label{font-size:0.72rem;color:#6e7681;letter-spacing:1px;text-transform:uppercase;margin-bottom:4px;}
.op-sig-strong-ce{font-size:1.8rem;font-weight:900;color:#2ea043;letter-spacing:-1px;}
.op-sig-ce{font-size:1.5rem;font-weight:800;color:#00d4aa;}
.op-sig-strong-pe{font-size:1.8rem;font-weight:900;color:#f85149;letter-spacing:-1px;}
.op-sig-pe{font-size:1.5rem;font-weight:800;color:#da3633;}
.op-sig-watch{font-size:1.4rem;font-weight:700;color:#e6b800;}
.op-sig-wait{font-size:1.4rem;font-weight:700;color:#6e7681;}
.op-sig-sub{font-size:0.84rem;color:#6e7681;margin-top:4px;}
.op-score-box{position:absolute;top:16px;right:20px;text-align:right;}
.op-score-lbl{font-size:0.68rem;color:#6e7681;letter-spacing:0.8px;}
.op-score-ce{font-size:1.1rem;font-weight:700;color:#00d4aa;}
.op-score-pe{font-size:1.1rem;font-weight:700;color:#f85149;}
/* Trade setup */
.op-setup{background:#0d1117;border:1px solid #21262d;border-radius:10px;padding:16px;}
.op-kv{display:flex;justify-content:space-between;align-items:center;padding:5px 0;border-bottom:1px solid #161b22;}
.op-kv:last-child{border-bottom:none;}
.op-kv-lbl{font-size:0.74rem;color:#6e7681;letter-spacing:0.4px;}
.op-kv-val{font-size:0.92rem;font-weight:700;color:#f0f6fc;}
.op-kv-sl{color:#f85149!important;}
.op-kv-tgt{color:#00d4aa!important;}
/* Factor table */
.op-factor-dir-bull{display:inline-block;background:#0d2818;color:#2ea043;border:1px solid #2ea043;font-size:0.68rem;padding:1px 7px;border-radius:4px;font-weight:700;}
.op-factor-dir-bear{display:inline-block;background:#2a0d0d;color:#f85149;border:1px solid #f85149;font-size:0.68rem;padding:1px 7px;border-radius:4px;font-weight:700;}
.op-factor-dir-neu{display:inline-block;background:#161b22;color:#6e7681;border:1px solid #30363d;font-size:0.68rem;padding:1px 7px;border-radius:4px;}
</style>
""", unsafe_allow_html=True)

    # ── Header ────────────────────────────────────────────────────────────────
    _op_now    = datetime.datetime.now(_IST_OP)
    _op_sym_now = st.session_state.get("op_sym", "NIFTY")
    _op_is_mcx  = _op_sym_now in {"CRUDEOIL", "GOLD", "SILVER"}
    _op_close  = _op_now.replace(
        hour=23, minute=30, second=0, microsecond=0
    ) if _op_is_mcx else _op_now.replace(hour=15, minute=30, second=0, microsecond=0)
    _op_left   = max(0, int((_op_close - _op_now).total_seconds()))
    _op_hh, _op_mm, _op_ss = _op_left // 3600, (_op_left % 3600) // 60, _op_left % 60

    st.markdown("""
<div class="op-header">
  <span class="op-title">📡 Intraday OI Pulse</span>
  <span class="op-badge-day">ANY DAY</span>
  <span class="op-badge-live">LIVE</span>
</div>""", unsafe_allow_html=True)

    _op_session_lbl = "MCX session closes in" if _op_is_mcx else "Market closes in"
    _op_session_sub = "MCX Crude Oil · 9:00 AM – 11:30 PM IST · ATM options" if _op_is_mcx else "OI build-up / unwinding scanner · ATM options"
    st.markdown(
        f'<div class="op-cdown-wrap">'
        f'<span class="op-countdown-lbl">{_op_session_lbl}</span>'
        f'<span class="op-countdown">{_op_hh:02d}:{_op_mm:02d}:{_op_ss:02d}</span>'
        f'<span class="op-countdown-lbl" style="margin-left:10px;">{_op_session_sub}</span>'
        f'</div>',
        unsafe_allow_html=True,
    )

    # ── Kite check ────────────────────────────────────────────────────────────
    _op_key = st.session_state.get("kite_api_key", "")
    _op_tok = st.session_state.get("kite_access_token", "")
    _kite_ok = bool(_op_key and _op_tok)

    if not _kite_ok:
        st.markdown(
            '<div class="sa-card" style="text-align:center;padding:40px 20px;">'
            '<div style="font-size:2rem;">🔑</div>'
            '<div style="color:#6e7681;margin-top:8px;">Enter Kite API Key &amp; Access Token in the sidebar to start scanning</div>'
            '</div>',
            unsafe_allow_html=True,
        )
    else:
        # ── Control panel ─────────────────────────────────────────────────────
        with st.container(border=True):
            _op_c1, _op_c2, _op_c3, _op_c4 = st.columns([2, 3, 2, 3])
            with _op_c1:
                _op_sym = st.selectbox("Index", TOI_SYMBOLS, key="op_sym",
                                       index=TOI_SYMBOLS.index("NIFTY") if "NIFTY" in TOI_SYMBOLS else 0)
            with _op_c2:
                _op_instr = st.session_state.get("op_instr")
                if _op_instr is not None and not _op_instr.empty:
                    _op_exp_opts = sorted(
                        _op_instr[
                            (_op_instr["name"] == _op_sym) &
                            (_op_instr["instrument_type"].isin(["CE", "PE"]))
                        ]["expiry"].unique()
                    )
                    _op_expiry = (
                        st.selectbox("Expiry", _op_exp_opts, key="op_expiry_sel")
                        if _op_exp_opts
                        else st.text_input("Expiry (load first)", key="op_expiry_txt", placeholder="e.g. 2025-07-10")
                    )
                else:
                    _op_expiry = st.text_input("Expiry (load first)", key="op_expiry_txt",
                                               placeholder="e.g. 2025-07-10")
            with _op_c3:
                _op_interval = st.selectbox("Refresh", [30, 45, 60], format_func=lambda x: f"{x}s",
                                            key="op_interval")
            with _op_c4:
                _op_lc1, _op_lc2 = st.columns(2)
                with _op_lc1:
                    _op_load_btn = st.button("📥 Load", key="op_load", use_container_width=True)
                with _op_lc2:
                    _op_scan_btn = st.button("🔍 Scan Now", key="op_scan",
                                             use_container_width=True, type="primary")

        # ── Load instruments ──────────────────────────────────────────────────
        if _op_load_btn:
            with st.spinner("Downloading instruments…"):
                try:
                    _op_raw = toi_fetch_instruments(_op_key, _op_tok, _op_sym)
                    st.session_state["op_instr"]      = _op_raw
                    st.session_state["op_prev_chain"] = None
                    st.session_state["op_spot_hist"]  = []
                    st.session_state["op_history"]    = []
                    st.success(f"Loaded {len(_op_raw)} instruments")
                    st.rerun()
                except Exception as _e:
                    st.error(f"Load failed: {_e}")

        # ── Auto-refresh during market hours ──────────────────────────────────
        _op_h_now = _op_now.hour * 60 + _op_now.minute
        # MCX (CRUDEOIL) trades 9 AM – 11:30 PM; equity is 9:30 AM – 3:15 PM
        if _op_is_mcx:
            _op_mkt = (9 * 60) <= _op_h_now < (23 * 60 + 30)
        else:
            _op_mkt = (9 * 60 + 30) <= _op_h_now < (15 * 60 + 15)
        if _op_mkt and st.session_state.get("op_instr") is not None:
            _st_autorefresh(interval=_op_interval * 1000, key="op_refresh")

        # ── Run scan ──────────────────────────────────────────────────────────
        def _do_op_scan():
            _instr = st.session_state.get("op_instr")
            if _instr is None or _instr.empty:
                st.warning("Load instruments first (click 📥 Load)")
                return
            _exp = st.session_state.get("op_expiry_sel") or st.session_state.get("op_expiry_txt", "")
            if not _exp:
                st.warning("Select an expiry date")
                return
            with st.spinner("Scanning option chain…"):
                try:
                    _res = run_oi_pulse_scan(
                        _op_key, _op_tok,
                        _op_sym, _exp, _instr,
                        spot_history=st.session_state.get("op_spot_hist"),
                        prev_chain=st.session_state.get("op_prev_chain"),
                    )
                    if "error" in _res:
                        st.error(_res["error"])
                        return
                    st.session_state["op_last_signal"] = _res
                    st.session_state["op_last_fetch"]  = datetime.datetime.now(_IST_OP)
                    st.session_state["op_prev_chain"]  = _res["new_prev_chain"]
                    _sh = st.session_state.get("op_spot_hist", [])
                    _sh.append(_res["spot"])
                    st.session_state["op_spot_hist"] = _sh[-20:]
                    _sig = _res["signal"]
                    if _sig not in ("WAIT", "WATCH"):
                        _hist = st.session_state.get("op_history", [])
                        _hist.append({
                            "ts":     _res["ist_now"].strftime("%H:%M:%S"),
                            "signal": _sig,
                            "spot":   _res["spot"],
                            "ce_sc":  _res["score_ce"],
                            "pe_sc":  _res["score_pe"],
                            "strike": _res.get("recommended_strike"),
                            "type":   _res.get("recommended_type"),
                            "ltp":    _res.get("ltp_entry"),
                        })
                        st.session_state["op_history"] = _hist[-30:]
                except Exception as _e:
                    st.error(f"Scan error: {_e}")

        if _op_scan_btn:
            _do_op_scan()
        elif _op_mkt and st.session_state.get("op_instr") is not None:
            _do_op_scan()

        # ── Display results ───────────────────────────────────────────────────
        _op_res      = st.session_state.get("op_last_signal")
        _op_fetch_ts = st.session_state.get("op_last_fetch")

        if _op_fetch_ts:
            _op_age = int((datetime.datetime.now(_IST_OP) - _op_fetch_ts).total_seconds())
            st.caption(f"Last scan: {_op_fetch_ts.strftime('%H:%M:%S')} IST  ·  {_op_age}s ago")

        if _op_res:
            _op_sig   = _op_res["signal"]
            _op_chain = _op_res["chain"]
            _op_spot  = _op_res["spot"]
            _op_atm   = _op_res["atm"]

            # ── Signal banner ─────────────────────────────────────────────────
            if _op_sig == "STRONG BUY CE":
                _banner_cls = "op-banner-strong-ce"
                _sig_cls    = "op-sig-strong-ce"
                _sig_emoji  = "🚀 "
            elif _op_sig == "BUY CE":
                _banner_cls = "op-banner-ce"
                _sig_cls    = "op-sig-ce"
                _sig_emoji  = "📈 "
            elif _op_sig == "STRONG BUY PE":
                _banner_cls = "op-banner-strong-pe"
                _sig_cls    = "op-sig-strong-pe"
                _sig_emoji  = "🔻 "
            elif _op_sig == "BUY PE":
                _banner_cls = "op-banner-pe"
                _sig_cls    = "op-sig-pe"
                _sig_emoji  = "📉 "
            elif _op_sig == "WATCH":
                _banner_cls = "op-banner-watch"
                _sig_cls    = "op-sig-watch"
                _sig_emoji  = "👁 "
            else:
                _banner_cls = "op-banner-wait"
                _sig_cls    = "op-sig-wait"
                _sig_emoji  = ""

            _op_detail  = _op_res.get("signal_detail", "")
            _op_sd_txt  = _op_res.get("spot_direction", "FLAT")
            _op_dir_arr = {"UP": "⬆ Spot rising", "DOWN": "⬇ Spot falling", "FLAT": "➡ Sideways"}.get(_op_sd_txt, "")

            st.markdown(
                f'<div class="op-banner {_banner_cls}">'
                f'<div class="op-sig-label">OI PULSE SIGNAL</div>'
                f'<div class="{_sig_cls}">{_sig_emoji}{_op_sig}</div>'
                f'<div class="op-sig-sub">{_op_detail if _op_detail else _op_dir_arr}</div>'
                f'<div class="op-score-box">'
                f'<div class="op-score-lbl">CE SCORE</div>'
                f'<div class="op-score-ce">+{_op_res["score_ce"]}</div>'
                f'<div class="op-score-lbl" style="margin-top:4px;">PE SCORE</div>'
                f'<div class="op-score-pe">+{_op_res["score_pe"]}</div>'
                f'</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

            # ── Two columns: Setup | Strike Ladder ───────────────────────────
            _op_col1, _op_col2 = st.columns([1, 2])

            with _op_col1:
                st.markdown('<div class="sa-sec">Trade Setup</div>', unsafe_allow_html=True)
                _rs  = _op_res.get("recommended_strike")
                _rt  = _op_res.get("recommended_type")
                _ltp = _op_res.get("ltp_entry")
                _sl  = _op_res.get("ltp_sl")
                _tgt = _op_res.get("ltp_target")

                if _rs and _rt and _ltp:
                    st.markdown(
                        f'<div class="op-setup">'
                        f'<div class="op-kv"><span class="op-kv-lbl">INSTRUMENT</span>'
                        f'<span class="op-kv-val">{_op_sym} {int(_rs)} {_rt}</span></div>'
                        f'<div class="op-kv"><span class="op-kv-lbl">ENTRY LTP</span>'
                        f'<span class="op-kv-val">₹{_ltp:.1f}</span></div>'
                        f'<div class="op-kv"><span class="op-kv-lbl">STOP LOSS</span>'
                        f'<span class="op-kv-val op-kv-sl">₹{_sl:.1f} (−30%)</span></div>'
                        f'<div class="op-kv"><span class="op-kv-lbl">TARGET</span>'
                        f'<span class="op-kv-val op-kv-tgt">₹{_tgt:.1f} (2×)</span></div>'
                        f'<div class="op-kv"><span class="op-kv-lbl">SPOT</span>'
                        f'<span class="op-kv-val">{_op_spot:,.2f}</span></div>'
                        f'<div class="op-kv"><span class="op-kv-lbl">ATM</span>'
                        f'<span class="op-kv-val">{int(_op_atm)}</span></div>'
                        f'<div class="op-kv"><span class="op-kv-lbl">DIRECTION</span>'
                        f'<span class="op-kv-val">{_op_sd_txt} {_op_dir_arr}</span></div>'
                        f'<div style="margin-top:10px;padding:8px;background:#001a0d;border-radius:6px;border:1px solid #2ea043;">'
                        f'<div style="color:#2ea043;font-size:0.72rem;font-weight:700;">ℹ INTRADAY RULES</div>'
                        f'<div style="color:#6e7681;font-size:0.71rem;margin-top:4px;">'
                        f'Exit by 3:00 PM — no overnight holds.<br>'
                        f'30% SL is firm. 2× target then exit.<br>'
                        f'OI signal confirms on 2nd+ scan.</div>'
                        f'</div>'
                        f'</div>',
                        unsafe_allow_html=True,
                    )
                else:
                    st.markdown(
                        '<div class="op-setup" style="text-align:center;padding:30px 16px;">'
                        '<div style="color:#6e7681;font-size:0.84rem;">No trade setup yet.<br>'
                        'Scan again to detect OI changes.</div>'
                        '</div>',
                        unsafe_allow_html=True,
                    )

                # OI Walls
                st.markdown('<div class="sa-sec" style="margin-top:12px;">OI Walls</div>', unsafe_allow_html=True)
                _ce_ws   = _op_res.get("top_ce_strike")
                _pe_ws   = _op_res.get("top_pe_strike")
                _ce_oi_w = _op_res.get("ce_oi_wall", 0)
                _pe_oi_w = _op_res.get("pe_oi_wall", 0)
                _cov_ce  = _op_res.get("covering_ce", False)
                _cov_pe  = _op_res.get("covering_pe", False)

                _wall_rows = ""
                if _ce_ws:
                    _dist_ce = abs(_ce_ws - _op_spot) / _op_spot * 100
                    _cov_b   = ' <span style="color:#ff6b35;font-size:0.7rem;">🔥 COVERING</span>' if _cov_ce else ""
                    _wall_rows += (
                        f'<div class="op-kv">'
                        f'<span class="op-kv-lbl">CE WALL {int(_ce_ws)}{_cov_b}</span>'
                        f'<span class="op-kv-val" style="color:#f85149;">{_ce_oi_w/1e5:.1f}L · {_dist_ce:.2f}% above</span>'
                        f'</div>'
                    )
                if _pe_ws:
                    _dist_pe = abs(_op_spot - _pe_ws) / _op_spot * 100
                    _cov_b2  = ' <span style="color:#ff6b35;font-size:0.7rem;">🔥 COVERING</span>' if _cov_pe else ""
                    _wall_rows += (
                        f'<div class="op-kv">'
                        f'<span class="op-kv-lbl">PE WALL {int(_pe_ws)}{_cov_b2}</span>'
                        f'<span class="op-kv-val" style="color:#00d4aa;">{_pe_oi_w/1e5:.1f}L · {_dist_pe:.2f}% below</span>'
                        f'</div>'
                    )
                if _wall_rows:
                    st.markdown(f'<div class="op-setup">{_wall_rows}</div>', unsafe_allow_html=True)

            with _op_col2:
                st.markdown('<div class="sa-sec">Strike Ladder — Nearby OI</div>', unsafe_allow_html=True)

                _max_ce = max(_op_chain["CE OI"].max(), 1)
                _max_pe = max(_op_chain["PE OI"].max(), 1)

                _ladder_rows = ""
                for _, _row in _op_chain.iterrows():
                    _s    = _row["Strike"]
                    _ce_o = int(_row["CE OI"])
                    _pe_o = int(_row["PE OI"])
                    _ce_l = _row["CE LTP"]
                    _pe_l = _row["PE LTP"]
                    _ce_d = int(_row.get("CE ΔOI", 0))
                    _pe_d = int(_row.get("PE ΔOI", 0))

                    _is_atm    = (_s == _op_atm)
                    _is_ce_wall = (_ce_ws is not None and _s == _ce_ws)
                    _is_pe_wall = (_pe_ws is not None and _s == _pe_ws)
                    _row_cls = (
                        "op-row-atm"    if _is_atm    else
                        "op-row-ce-wall" if _is_ce_wall else
                        "op-row-pe-wall" if _is_pe_wall else ""
                    )

                    _dist_pct = (_s - _op_spot) / _op_spot * 100
                    _dist_str = "← SPOT" if abs(_dist_pct) < 0.01 else f"{_dist_pct:+.2f}%"

                    _ce_bar_w = int(_ce_o / _max_ce * 120)
                    _pe_bar_w = int(_pe_o / _max_pe * 120)

                    def _dstr(d):
                        if d == 0: return '<span class="op-delta-neu">—</span>'
                        cls = "op-delta-neg" if d < 0 else "op-delta-pos"
                        return f'<span class="{cls}">{d/1e5:+.1f}L</span>'

                    _wall_icon = (
                        "🔥" if ((_is_ce_wall and _cov_ce) or (_is_pe_wall and _cov_pe))
                        else ("⬆" if _is_ce_wall else ("⬇" if _is_pe_wall else ""))
                    )

                    _ladder_rows += (
                        f'<tr class="{_row_cls}">'
                        f'<td class="op-ce-side">'
                        f'  <div class="op-oi-bar-ce">'
                        f'    <span class="op-oi-num">{_ce_o/1e5:.1f}L</span>'
                        f'    <div class="op-bar-fill-ce" style="width:{_ce_bar_w}px;"></div>'
                        f'  </div>'
                        f'</td>'
                        f'<td class="op-ce-side"><span class="op-ltp">{"₹"+str(round(_ce_l,1)) if _ce_l > 0 else "—"}</span></td>'
                        f'<td class="op-ce-side">{_dstr(_ce_d)}</td>'
                        f'<td class="op-strike-cell">{_wall_icon}{int(_s)}</td>'
                        f'<td class="op-dist-cell">{_dist_str}</td>'
                        f'<td class="op-pe-side">{_dstr(_pe_d)}</td>'
                        f'<td class="op-pe-side"><span class="op-ltp">{"₹"+str(round(_pe_l,1)) if _pe_l > 0 else "—"}</span></td>'
                        f'<td class="op-pe-side">'
                        f'  <div class="op-oi-bar-pe">'
                        f'    <div class="op-bar-fill-pe" style="width:{_pe_bar_w}px;"></div>'
                        f'    <span class="op-oi-num">{_pe_o/1e5:.1f}L</span>'
                        f'  </div>'
                        f'</td>'
                        f'</tr>'
                    )

                st.markdown(
                    f'<div class="op-ladder-wrap">'
                    f'<table class="op-tbl">'
                    f'<thead><tr>'
                    f'<th class="op-ce-side">CE OI (L)</th>'
                    f'<th class="op-ce-side">CE LTP</th>'
                    f'<th class="op-ce-side">CE ΔOI</th>'
                    f'<th style="text-align:center;">STRIKE</th>'
                    f'<th style="text-align:center;">DIST</th>'
                    f'<th class="op-pe-side">PE ΔOI</th>'
                    f'<th class="op-pe-side">PE LTP</th>'
                    f'<th class="op-pe-side">PE OI (L)</th>'
                    f'</tr></thead>'
                    f'<tbody>{_ladder_rows}</tbody>'
                    f'</table>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

            # ── Factor breakdown ──────────────────────────────────────────────
            _op_factors = _op_res.get("factors", [])
            if _op_factors:
                st.markdown('<div class="sa-sec" style="margin-top:14px;">Factor Breakdown</div>', unsafe_allow_html=True)
                _op_frows = ""
                for _fn, _fv, _fd, _fp in _op_factors:
                    _dir_cls = (
                        "op-factor-dir-bull" if _fd == "BULL" else
                        ("op-factor-dir-bear" if _fd == "BEAR" else "op-factor-dir-neu")
                    )
                    _pts_col = "#00d4aa" if _fp > 0 else ("#f85149" if _fp < 0 else "#6e7681")
                    _op_frows += (
                        f'<tr>'
                        f'<td style="color:#adbac7;font-size:0.82rem;white-space:nowrap;">{_fn}</td>'
                        f'<td style="color:#c9d1d9;font-size:0.8rem;">{_fv}</td>'
                        f'<td><span class="{_dir_cls}">{_fd}</span></td>'
                        f'<td style="font-weight:700;color:{_pts_col};font-size:0.85rem;">{_fp:+d}</td>'
                        f'</tr>'
                    )
                st.markdown(
                    f'<div class="sa-card" style="padding:0;overflow:hidden;">'
                    f'<div style="overflow-x:auto;">'
                    f'<table class="sa-tbl">'
                    f'<thead><tr><th>Factor</th><th>Reading</th><th>Direction</th><th>Pts</th></tr></thead>'
                    f'<tbody>{_op_frows}</tbody>'
                    f'</table>'
                    f'</div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

        # ── Signal history ────────────────────────────────────────────────────
        _op_hist = st.session_state.get("op_history", [])
        if _op_hist:
            st.markdown('<div class="sa-sec" style="margin-top:14px;">Signal History (This Session)</div>', unsafe_allow_html=True)

            def _op_sig_col(sig):
                if "STRONG BUY CE" in sig: return "#2ea043"
                if "BUY CE"        in sig: return "#00d4aa"
                if "STRONG BUY PE" in sig: return "#f85149"
                if "BUY PE"        in sig: return "#da3633"
                return "#adbac7"

            _op_hrows = ""
            for _h in reversed(_op_hist[-20:]):
                _ltp_s = ("₹" + str(round(_h["ltp"], 1))) if _h.get("ltp") else "—"
                _str_s = str(int(_h["strike"])) if _h.get("strike") else "—"
                _op_hrows += (
                    f'<tr>'
                    f'<td style="color:#6e7681;font-size:0.8rem;white-space:nowrap;">{_h["ts"]}</td>'
                    f'<td style="font-weight:700;color:{_op_sig_col(_h["signal"])};font-size:0.81rem;">{_h["signal"]}</td>'
                    f'<td style="color:#c9d1d9;font-size:0.81rem;">{_h["spot"]:,.2f}</td>'
                    f'<td style="color:#00d4aa;font-size:0.82rem;">{_h["ce_sc"]:+d}</td>'
                    f'<td style="color:#f85149;font-size:0.82rem;">{_h["pe_sc"]:+d}</td>'
                    f'<td style="color:#adbac7;">{_str_s} {_h.get("type","")}</td>'
                    f'<td style="color:#adbac7;">{_ltp_s}</td>'
                    f'</tr>'
                )
            st.markdown(
                f'<div class="sa-card" style="padding:0;overflow:hidden;">'
                f'<div style="overflow-x:auto;">'
                f'<table class="sa-tbl">'
                f'<thead><tr><th>Time</th><th>Signal</th><th>Spot</th>'
                f'<th>CE Sc</th><th>PE Sc</th><th>Strike</th><th>LTP</th></tr></thead>'
                f'<tbody>{_op_hrows}</tbody>'
                f'</table>'
                f'</div>'
                f'</div>',
                unsafe_allow_html=True,
            )
        elif st.session_state.get("op_last_signal") is None:
            st.markdown(
                '<div class="sa-card" style="text-align:center;padding:32px 20px;">'
                '<div style="font-size:1.8rem;">📡</div>'
                '<div style="color:#6e7681;margin-top:8px;">'
                'Load instruments → select an expiry → click <b style="color:#f0f6fc;">Scan Now</b><br>'
                '<span style="font-size:0.78rem;">OI change signals appear from the 2nd scan onward</span>'
                '</div>'
                '</div>',
                unsafe_allow_html=True,
            )


# ── Red Flag Radar ───────────────────────────────────────────────────────────

def page_red_flag():
    st.markdown("""
<style>
.rf-header{display:flex;align-items:center;gap:12px;margin-bottom:4px;}
.rf-title{font-size:1.45rem;font-weight:800;color:#f0f6fc;letter-spacing:-0.5px;}
.rf-badge{background:#2a0d0d;border:1px solid #f85149;color:#f85149;font-size:0.68rem;font-weight:700;padding:3px 9px;border-radius:20px;letter-spacing:0.8px;}
.rf-sub{font-size:0.82rem;color:#6e7681;margin-bottom:14px;}
/* Flag chips */
.rf-chip{display:inline-block;font-size:0.65rem;font-weight:700;padding:2px 8px;border-radius:10px;margin:1px 2px;white-space:nowrap;}
.rf-chip-VOL_SPIKE{background:rgba(248,81,73,0.15);color:#f85149;border:1px solid #f85149;}
.rf-chip-BIG_MOVE{background:rgba(230,184,0,0.15);color:#e6b800;border:1px solid #e6b800;}
.rf-chip-OI_SPIKE{background:rgba(210,168,255,0.15);color:#d2a8ff;border:1px solid #d2a8ff;}
.rf-chip-PCR_LOW{background:rgba(255,166,87,0.15);color:#ffa657;border:1px solid #ffa657;}
.rf-chip-PCR_HIGH{background:rgba(121,192,255,0.15);color:#79c0ff;border:1px solid #79c0ff;}
.rf-chip-HIGH_IV{background:rgba(255,123,114,0.15);color:#ff7b72;border:1px solid #ff7b72;}
.rf-chip-BULK_DEAL{background:rgba(86,211,100,0.15);color:#56d364;border:1px solid #56d364;}
/* Table */
.rf-tbl-wrap{overflow-x:auto;border-radius:10px;border:1px solid #21262d;margin-top:12px;}
.rf-tbl{width:100%;border-collapse:collapse;font-size:0.82rem;}
.rf-tbl th{background:#0d1117;color:#6e7681;font-size:0.70rem;font-weight:700;letter-spacing:0.7px;padding:8px 12px;text-transform:uppercase;border-bottom:1px solid #21262d;white-space:nowrap;}
.rf-tbl td{padding:7px 12px;border-bottom:1px solid #161b22;white-space:nowrap;vertical-align:middle;color:#c9d1d9;}
.rf-tbl tr:last-child td{border-bottom:none;}
.rf-tbl tr:hover td{background:rgba(255,255,255,0.025);}
.rf-count-0{color:#6e7681;font-weight:600;}
.rf-count-1{color:#e6b800;font-weight:700;}
.rf-count-2{color:#ffa657;font-weight:800;}
.rf-count-3{color:#f85149;font-weight:900;font-size:1.05rem;}
.rf-count-hi{color:#ff0000;font-weight:900;font-size:1.1rem;animation:rf-pulse 1s infinite;}
@keyframes rf-pulse{0%,100%{opacity:1;}50%{opacity:0.5;}}
.rf-sym{font-weight:700;color:#f0f6fc;font-size:0.88rem;}
.rf-up{color:#00d4aa;font-weight:600;}
.rf-dn{color:#f85149;font-weight:600;}
.rf-neutral{color:#6e7681;}
/* Trade signal badges */
.rf-trade-sce{display:inline-block;background:rgba(0,212,170,.22);color:#00d4aa;border:1px solid #00d4aa;font-size:0.65rem;font-weight:800;padding:3px 8px;border-radius:6px;letter-spacing:.6px;white-space:nowrap;}
.rf-trade-ce{display:inline-block;background:rgba(0,212,170,.1);color:#00d4aa;border:1px solid rgba(0,212,170,.5);font-size:0.65rem;font-weight:700;padding:3px 8px;border-radius:6px;white-space:nowrap;}
.rf-trade-spe{display:inline-block;background:rgba(248,81,73,.22);color:#f85149;border:1px solid #f85149;font-size:0.65rem;font-weight:800;padding:3px 8px;border-radius:6px;letter-spacing:.6px;white-space:nowrap;}
.rf-trade-pe{display:inline-block;background:rgba(248,81,73,.1);color:#f85149;border:1px solid rgba(248,81,73,.5);font-size:0.65rem;font-weight:700;padding:3px 8px;border-radius:6px;white-space:nowrap;}
.rf-trade-watch{display:inline-block;background:rgba(110,118,129,.12);color:#8b949e;border:1px solid #2a2e39;font-size:0.65rem;font-weight:600;padding:3px 8px;border-radius:6px;white-space:nowrap;}
/* Legend */
.rf-legend{display:flex;flex-wrap:wrap;gap:8px;padding:12px 0 4px;}
.rf-leg-item{display:flex;align-items:center;gap:5px;font-size:0.73rem;color:#8b949e;}
</style>
""", unsafe_allow_html=True)

    st.markdown("""
<div class="rf-header">
  <span class="rf-title">🚨 Red Flag Radar</span>
  <span class="rf-badge">F&amp;O SURVEILLANCE</span>
</div>""", unsafe_allow_html=True)
    st.markdown(
        '<div class="rf-sub">Scans all F&amp;O stocks for unusual activity — volume spikes, big moves, '
        'options anomalies, bulk deals. Sort by flag count to spot suspicious setups.</div>',
        unsafe_allow_html=True,
    )

    _rf_key  = st.session_state.get("kite_api_key", "")
    _rf_tok  = st.session_state.get("kite_access_token", "")
    _kite_ok = bool(_rf_key and _rf_tok)

    # ── Auto-refresh every 30 s ───────────────────────────────────────────────
    _rf_tick = 0
    if _HAS_AUTOREFRESH:
        _rf_tick = _st_autorefresh(interval=30_000, key="rf_autorefresh")

    # Detect genuine auto-refresh ticks (counter changed) vs user interactions
    _rf_prev_tick = st.session_state.get("rf_prev_tick", -1)
    _rf_is_tick   = _rf_tick != _rf_prev_tick
    st.session_state["rf_prev_tick"] = _rf_tick

    # Auto-scan: on first load (no prior result) or on each 30 s tick
    _rf_auto_trigger = _rf_is_tick or st.session_state.get("rf_result") is None

    if not _kite_ok:
        st.info("Add Kite API Key + Access Token in the sidebar to unlock OI, IV, and PCR flags. Volume, price, and bulk deal flags run without Kite.", icon="🔑")

    # ── Flag legend ──────────────────────────────────────────────────────────
    _legend_html = '<div class="rf-legend">'
    for _col, (_lbl, _desc, _clr) in [
        ("_vol_spike", ("VOL_SPIKE", "Volume > 3× avg",     "#f85149")),
        ("_big_move",  ("BIG_MOVE",  "Price move > 5%",     "#e6b800")),
        ("_oi_spike",  ("OI_SPIKE",  "OI > 100 lots ATM",   "#d2a8ff")),
        ("_pcr_low",   ("PCR_LOW",   "PCR < 0.5 (calls heavy)", "#ffa657")),
        ("_pcr_high",  ("PCR_HIGH",  "PCR > 2.5 (puts heavy)",  "#79c0ff")),
        ("_high_iv",   ("HIGH_IV",   "Straddle > 3% of spot",   "#ff7b72")),
        ("bulk_deal",  ("BULK_DEAL", "NSE bulk deal today",      "#56d364")),
    ]:
        _legend_html += (
            f'<div class="rf-leg-item">'
            f'<span class="rf-chip rf-chip-{_lbl}" style="font-size:0.6rem;">{_lbl}</span>'
            f'{_desc}</div>'
        )
    _legend_html += "</div>"
    st.markdown(_legend_html, unsafe_allow_html=True)

    # ── Controls ─────────────────────────────────────────────────────────────
    _rfc1, _rfc2, _rfc3, _rfc4 = st.columns([2, 1.5, 1.5, 5])
    with _rfc1:
        _rf_scan_btn = st.button("🔍 Scan Now", key="rf_scan", type="primary",
                                 use_container_width=True)
    with _rfc2:
        _rf_clear_btn = st.button("🗑 Clear", key="rf_clear", use_container_width=True)
    with _rfc3:
        _rf_auto_badge = (
            '<span style="display:inline-block;background:rgba(0,212,170,.15);'
            'color:#00d4aa;border:1px solid rgba(0,212,170,.4);'
            'font-size:0.65rem;font-weight:700;padding:4px 10px;border-radius:6px;">'
            '⟳ AUTO 30s</span>'
            if _HAS_AUTOREFRESH else
            '<span style="display:inline-block;color:#6e7681;font-size:0.72rem;">'
            'Install streamlit-autorefresh for auto-refresh</span>'
        )
        st.markdown(_rf_auto_badge, unsafe_allow_html=True)

    if _rf_clear_btn:
        st.session_state.pop("rf_result", None)
        st.session_state.pop("rf_scanned_at", None)
        st.rerun()

    _rf_should_scan = _rf_scan_btn or _rf_auto_trigger

    if _rf_should_scan:
        _rf_prog_bar = st.progress(0, text="Scanning F&O stocks…")
        _rf_status   = st.empty()

        def _rf_progress(msg: str, pct: int):
            _rf_prog_bar.progress(pct, text=msg)
            _rf_status.caption(msg)

        try:
            _rf_df = _run_red_flag(
                api_key=_rf_key,
                access_token=_rf_tok,
                progress_cb=_rf_progress,
            )
            _rf_prog_bar.progress(100, text="Done!")
            _rf_status.empty()
            st.session_state["rf_result"] = _rf_df
            st.session_state["rf_scanned_at"] = datetime.datetime.now(
                datetime.timezone(datetime.timedelta(hours=5, minutes=30))
            )
        except Exception as _rf_e:
            st.error(f"Scan error: {_rf_e}")

    # ── Results ───────────────────────────────────────────────────────────────
    _rf_df   = st.session_state.get("rf_result")
    _rf_when = st.session_state.get("rf_scanned_at")

    if _rf_df is not None and not _rf_df.empty:
        _rf_flagged = _rf_df[_rf_df["flag_count"] > 0]
        _rf_total   = len(_rf_df)
        _rf_n_flag  = len(_rf_flagged)

        if _rf_when:
            st.caption(f"Scanned at {_rf_when.strftime('%H:%M:%S')} IST  ·  "
                       f"{_rf_total} stocks scanned  ·  {_rf_n_flag} flagged")

        # Summary metrics
        _sm1, _sm2, _sm3, _sm4, _sm5 = st.columns(5)
        _sm1.metric("Total Scanned",  _rf_total)
        _sm2.metric("Flagged",        _rf_n_flag)
        _sm3.metric("Vol Spikes",     int(_rf_df.get("_vol_spike_count", _rf_df["flags"].apply(lambda f: "VOL_SPIKE" in f).sum()) if "_vol_spike_count" in _rf_df.columns else _rf_df["flags"].apply(lambda f: "VOL_SPIKE" in f).sum()))
        _sm4.metric("Big Moves",      int(_rf_df["flags"].apply(lambda f: "BIG_MOVE" in f).sum()))
        _sm5.metric("Bulk Deals",     int(_rf_df["flags"].apply(lambda f: "BULK_DEAL" in f).sum()))

        # Filter
        _all_flag_types = ["VOL_SPIKE","BIG_MOVE","OI_SPIKE","PCR_LOW","PCR_HIGH","HIGH_IV","BULK_DEAL"]
        _rf_filter = st.multiselect(
            "Filter by flag", _all_flag_types, default=[],
            placeholder="Show all (no filter)",
            key="rf_filter",
        )
        _display_df = _rf_df.copy()
        if _rf_filter:
            _display_df = _display_df[
                _display_df["flags"].apply(lambda f: any(x in f for x in _rf_filter))
            ]

        _display_df = _display_df[_display_df["flag_count"] > 0]

        if _display_df.empty:
            st.info("No stocks match the selected flag filters.")
        else:
            # HTML table
            _col_has_kite = "pcr" in _display_df.columns

            _th_pcr = '<th>PCR</th><th>Straddle%</th>' if _col_has_kite else ''
            _tbl_html = (
                '<div class="rf-tbl-wrap">'
                '<table class="rf-tbl">'
                '<thead><tr>'
                '<th>#</th><th>Symbol</th><th>Price</th><th>Chg%</th>'
                '<th>Vol Ratio</th>' + _th_pcr +
                '<th>Trade</th>'
                '<th>Flags</th>'
                '</tr></thead><tbody>'
            )

            for _ri, _rrow in _display_df.head(150).iterrows():
                _rank    = int(_ri) + 1
                _sym     = str(_rrow["symbol"])
                _close   = f"₹{_rrow['close']:,.1f}" if "close" in _rrow else "—"
                _chg     = _rrow.get("chg_pct", 0)
                _chg_cls = "rf-up" if _chg >= 0 else "rf-dn"
                _chg_str = f"{_chg:+.2f}%"
                _vr      = _rrow.get("vol_ratio", 0)
                _vr_str  = f"<b style='color:#f85149;'>{_vr:.1f}×</b>" if _vr > 3 else f"{_vr:.1f}×"
                _fc      = int(_rrow.get("flag_count", 0))
                _fc_cls  = "rf-count-hi" if _fc >= 4 else f"rf-count-{min(_fc, 3)}"

                _chips = "".join(
                    f'<span class="rf-chip rf-chip-{f}">{f}</span>'
                    for f in _rrow.get("flags", [])
                )

                _pcr_td = ""
                if _col_has_kite:
                    _pcr_v = _rrow.get("pcr", 0) or 0
                    _str_v = _rrow.get("straddle_pct", 0) or 0
                    _pcr_col = (
                        f"<span style='color:#ffa657;font-weight:700;'>{_pcr_v:.2f}</span>"
                        if _pcr_v < 0.5 else
                        f"<span style='color:#79c0ff;font-weight:700;'>{_pcr_v:.2f}</span>"
                        if _pcr_v > 2.5 else
                        f"{_pcr_v:.2f}"
                    )
                    _str_col = (
                        f"<span style='color:#ff7b72;font-weight:700;'>{_str_v:.2f}%</span>"
                        if _str_v > 3 else f"{_str_v:.2f}%"
                    )
                    _pcr_td = f"<td>{_pcr_col}</td><td>{_str_col}</td>"

                _trade    = str(_rrow.get("trade", "WATCH"))
                _trade_css = {
                    "STRONG CE": "rf-trade-sce",
                    "BUY CE":    "rf-trade-ce",
                    "STRONG PE": "rf-trade-spe",
                    "BUY PE":    "rf-trade-pe",
                    "WATCH":     "rf-trade-watch",
                }.get(_trade, "rf-trade-watch")
                _trade_td = f'<span class="{_trade_css}">{_trade}</span>'

                _tbl_html += (
                    f'<tr>'
                    f'<td class="rf-neutral" style="font-size:0.75rem;">{_rank}</td>'
                    f'<td class="rf-sym">{_sym}</td>'
                    f'<td>{_close}</td>'
                    f'<td class="{_chg_cls}">{_chg_str}</td>'
                    f'<td>{_vr_str}</td>'
                    + _pcr_td +
                    f'<td>{_trade_td}</td>'
                    f'<td class="{_fc_cls}" style="font-size:0.75rem;padding-right:6px;">{_fc}</td>'
                    f'<td>{_chips}</td>'
                    f'</tr>'
                )
            _tbl_html += '</tbody></table></div>'
            st.markdown(_tbl_html, unsafe_allow_html=True)

    elif _rf_df is not None and _rf_df.empty:
        st.warning("Scan returned no data. Check API credentials and try again.")
    else:
        st.markdown(
            '<div class="sa-card" style="text-align:center;padding:40px 20px;">'
            '<div style="font-size:2.5rem;">🚨</div>'
            '<div style="color:#6e7681;margin-top:10px;">'
            'Click <b style="color:#f0f6fc;">Scan All F&amp;O</b> to run the red flag check<br>'
            '<span style="font-size:0.78rem;">Takes 1–2 min · Volume &amp; price checks run without Kite</span>'
            '</div>'
            '</div>',
            unsafe_allow_html=True,
        )


# ── RPCI Screener page ────────────────────────────────────────────────────────

def page_rpci():
    _IST_TZ = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

    _ABBR = {
        "Valuation":             "VAL",
        "Earnings Power":        "ERN",
        "Momentum":              "MOM",
        "Price Contraction":     "CON",
        "Timeframe Alignment":   "TFA",
        "Outperformance":        "OPF",
        "Institutional Candles": "INS",
        "Short-term Extension":  "STX",
        "Long-term Extension":   "LTX",
        "Stage Analysis":        "STG",
        "Dow Theory (W)":        "DOW",
    }
    _COND_FULL = {
        "VAL": "1. VALUATION",      "ERN": "2. EARNINGS POWER",
        "MOM": "3. MOMENTUM",       "CON": "4. PRICE CONTRACTION",
        "TFA": "5. TIMEFRAME ALIGNMENT", "OPF": "6. OUTPERFORMANCE",
        "INS": "7. INSTITUTIONAL CANDLES", "STX": "8. SHORT-TERM EXTENSION",
        "LTX": "9. LONG-TERM EXTENSION", "STG": "10. STAGE ANALYSIS",
        "DOW": "11. DOW THEORY (W)",
    }
    _LABEL_CLS = {
        "Strongly favourable": ("rpci-lbl-sf", "#00d4aa"),
        "Favourable":          ("rpci-lbl-fv", "#64dd17"),
        "Neutral":             ("rpci-lbl-nt", "#ffd600"),
        "Unfavourable":        ("rpci-lbl-uf", "#f85149"),
    }

    def _to_sector(industry: str) -> str:
        ind = str(industry).lower()
        if any(k in ind for k in ["pharma", "hospital", "medical", "health", "diagnostic", "biotech"]):
            return "Healthcare"
        if any(k in ind for k in ["bank", "finance", "insurance", "nbfc", "financial", "leasing", "brokerage", "investment", "asset management"]):
            return "Financial Services"
        if any(k in ind for k in ["software", "it ", "computer", "technology", "saas", "information technology"]):
            return "Information Technology"
        if any(k in ind for k in ["fmcg", "personal care", "household", "tobacco", "edible"]):
            return "Fast Moving Consumer Goods"
        if any(k in ind for k in ["food", "beverage", "consumer food"]):
            return "Fast Moving Consumer Goods"
        if any(k in ind for k in ["automobile", "auto", "vehicle", "tyre", "2 wheeler", "4 wheeler"]):
            return "Consumer Discretionary"
        if any(k in ind for k in ["retail", "hotel", "media", "entertainment", "textile", "fashion", "apparel", "travel", "tourism"]):
            return "Consumer Discretionary"
        if any(k in ind for k in ["oil", "gas", "petroleum", "refinery", "energy"]):
            return "Energy"
        if any(k in ind for k in ["power", "electricity", "utility", "transmission", "renewable"]):
            return "Utilities"
        if any(k in ind for k in ["cement", "steel", "metal", "mining", "chemical", "fertiliser", "paper", "material", "plastic"]):
            return "Materials"
        if any(k in ind for k in ["telecom", "communication", "broadband"]):
            return "Telecommunication"
        if any(k in ind for k in ["real estate", "realty", "construction"]):
            return "Real Estate"
        if any(k in ind for k in ["engineering", "capital goods", "defence", "aerospace", "industrial", "machinery", "infrastructure"]):
            return "Industrials"
        return "Diversified"

    st.markdown("""<style>
/* RPCI dashboard */
.rpci-stat-card{background:#1e222d;border:1px solid #2a2e39;border-radius:10px;padding:14px 18px;text-align:center;}
.rpci-stat-val{font-size:1.7rem;font-weight:800;color:#f0f6fc;line-height:1.1;}
.rpci-stat-lbl{font-size:0.63rem;font-weight:700;color:#6e7681;letter-spacing:.8px;text-transform:uppercase;margin-top:4px;}
.rpci-stat-sub{font-size:0.68rem;color:#8b949e;margin-top:2px;}
/* Sector cards */
.rpci-sec-grid{display:flex;flex-wrap:wrap;gap:10px;margin:10px 0 16px;}
.rpci-sec-card{background:#1e222d;border:1px solid #2a2e39;border-radius:10px;padding:12px 14px;min-width:150px;flex:1;cursor:pointer;transition:border-color .2s;}
.rpci-sec-card:hover{border-color:#00d4aa;}
.rpci-sec-card.selected{border-color:#00d4aa;background:rgba(0,212,170,.07);}
.rpci-sec-name{font-size:0.7rem;font-weight:700;color:#8b949e;text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px;}
.rpci-sec-score{font-size:1.4rem;font-weight:800;color:#f0f6fc;line-height:1;}
.rpci-sec-delta{font-size:0.7rem;font-weight:600;margin-left:5px;}
.rpci-sec-sub{font-size:0.67rem;color:#6e7681;margin-top:3px;}
/* What moved */
.rpci-mv-up{background:rgba(0,212,170,.07);border:1px solid rgba(0,212,170,.25);border-radius:8px;padding:10px 14px;margin-bottom:6px;}
.rpci-mv-dn{background:rgba(248,81,73,.07);border:1px solid rgba(248,81,73,.2);border-radius:8px;padding:10px 14px;margin-bottom:6px;}
.rpci-mv-sym{font-weight:800;font-size:.9rem;color:#f0f6fc;}
.rpci-mv-arr{font-weight:700;color:#8b949e;font-size:.8rem;margin:0 4px;}
.rpci-mv-badge{font-size:.62rem;font-weight:800;padding:2px 7px;border-radius:4px;letter-spacing:.5px;}
.rpci-mv-badge-up{background:rgba(0,212,170,.2);color:#00d4aa;border:1px solid rgba(0,212,170,.4);}
.rpci-mv-badge-dn{background:rgba(248,81,73,.15);color:#f85149;border:1px solid rgba(248,81,73,.35);}
.rpci-mv-cond{font-size:.68rem;color:#8b949e;margin-top:3px;}
/* Condition pass-rate bar */
.rpci-pbar-wrap{margin:4px 0;display:flex;align-items:center;gap:8px;}
.rpci-pbar-lbl{font-size:0.67rem;font-weight:700;color:#c9d1d9;width:30px;text-align:right;}
.rpci-pbar-bg{flex:1;background:#0d1117;border-radius:3px;height:8px;overflow:hidden;}
.rpci-pbar-fill{background:#00d4aa;border-radius:3px;height:8px;}
.rpci-pbar-pct{font-size:0.67rem;color:#8b949e;width:34px;}
/* Condition blocks */
.rpci-blk-pass{display:inline-block;width:13px;height:13px;background:#00d4aa;border-radius:2px;margin:1px;vertical-align:middle;}
.rpci-blk-fail{display:inline-block;width:13px;height:13px;background:#21262d;border:1px solid #2a2e39;border-radius:2px;margin:1px;vertical-align:middle;}
/* Results table */
.rpci-tbl-wrap{width:100%;overflow-x:auto;border-radius:10px;border:1px solid #21262d;margin:10px 0;}
.rpci-tbl{width:100%;border-collapse:collapse;font-size:0.78rem;}
.rpci-tbl th{background:#0d1117;color:#6e7681;font-size:0.62rem;font-weight:700;letter-spacing:.6px;padding:7px 10px;text-transform:uppercase;border-bottom:1px solid #21262d;white-space:nowrap;text-align:left;}
.rpci-tbl td{padding:5px 10px;border-bottom:1px solid #161b22;white-space:nowrap;color:#c9d1d9;vertical-align:middle;}
.rpci-tbl tr:last-child td{border-bottom:none;}
.rpci-tbl tr:hover td{background:rgba(255,255,255,0.025);}
.rpci-s9{color:#00d4aa;font-weight:800;}
.rpci-s7{color:#64dd17;font-weight:700;}
.rpci-s5{color:#ffd600;font-weight:600;}
.rpci-slow{color:#f85149;font-weight:600;}
.rpci-sym{font-weight:700;color:#f0f6fc;}
.rpci-up{color:#00d4aa;font-weight:600;}
.rpci-dn{color:#f85149;font-weight:600;}
.rpci-neutral{color:#6e7681;}
.rpci-held{font-size:.7rem;color:#6e7681;text-align:center;}
/* Detail panel */
.rpci-panel{background:#1e222d;border:1px solid #2a2e39;border-radius:12px;overflow:hidden;margin-bottom:16px;}
.rpci-panel-hdr{background:#0d1117;padding:10px 16px;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #2a2e39;}
.rpci-ind-lbl{font-size:0.67rem;font-weight:800;letter-spacing:1.3px;text-transform:uppercase;color:#00d4aa;}
.rpci-res-lbl{font-size:0.67rem;font-weight:700;color:#6e7681;text-transform:uppercase;letter-spacing:.8px;}
.rpci-cond-row{display:flex;justify-content:space-between;align-items:center;padding:7px 16px;border-bottom:1px solid rgba(42,46,57,.5);font-size:.81rem;}
.rpci-cond-row:last-child{border-bottom:none;}
.rpci-cond-row:hover{background:rgba(255,255,255,.02);}
.rpci-cname{color:#c9d1d9;font-weight:500;}
.rpci-cval{color:#8b949e;font-size:.74rem;margin-right:8px;}
.rpci-pass{background:rgba(0,212,170,.14);color:#00d4aa;font-size:.62rem;font-weight:800;letter-spacing:.8px;padding:2px 9px;border-radius:4px;border:1px solid rgba(0,212,170,.3);}
.rpci-fail{background:rgba(248,81,73,.11);color:#f85149;font-size:.62rem;font-weight:800;letter-spacing:.8px;padding:2px 9px;border-radius:4px;border:1px solid rgba(248,81,73,.3);}
.rpci-score-row{background:rgba(0,212,170,.04);padding:11px 16px;display:flex;justify-content:space-between;align-items:center;border-top:2px solid #2a2e39;}
.rpci-lbl-sf{color:#00d4aa;font-weight:700;font-size:.83rem;}
.rpci-lbl-fv{color:#64dd17;font-weight:700;font-size:.83rem;}
.rpci-lbl-nt{color:#ffd600;font-weight:700;font-size:.83rem;}
.rpci-lbl-uf{color:#f85149;font-weight:700;font-size:.83rem;}
</style>""", unsafe_allow_html=True)

    st.markdown(
        '<h2 style="font-size:1.55rem;font-weight:800;margin:0 0 2px;">📈 RPCI Screener</h2>'
        '<p style="color:#6e7681;font-size:0.8rem;margin-bottom:14px;">'
        '11-condition analysis per stock · VAL · ERN · MOM · CON · TFA · OPF · INS · STX · LTX · STG · DOW'
        '</p>',
        unsafe_allow_html=True,
    )

    # ── Controls ─────────────────────────────────────────────────────────────
    _cc1, _cc2, _cc3 = st.columns([2, 1, 1])
    with _cc1:
        _ticker_search = st.text_input("Search ticker", placeholder="e.g. RELIANCE", key="rpci_ticker", label_visibility="collapsed")
    with _cc2:
        _scan_btn  = st.button("▶ Scan NIFTY 500", type="primary", use_container_width=True, key="rpci_scan_btn")
    with _cc3:
        _clear_btn = st.button("Clear", use_container_width=True, key="rpci_clear_btn")

    if _clear_btn:
        for _k in ("rpci_result", "rpci_prev_result", "rpci_held", "rpci_scanned_at",
                   "rpci_selected", "rpci_tab", "rpci_sector_filter"):
            st.session_state.pop(_k, None)
        st.rerun()

    if _scan_btn:
        try:
            _syms_df = get_nifty500_symbols()
            _symbols = [s.replace(".NS", "") for s in _syms_df["NSE_Symbol"].dropna().tolist() if str(s).endswith(".NS")]
            # Build sector_map from NSE industry data
            _sector_map: dict = {}
            for _, _srow in _syms_df.iterrows():
                _ind = str(_srow.get("Industry", "Unknown"))
                _sector_map[str(_srow["Symbol"]).strip()] = {
                    "sector":   _to_sector(_ind),
                    "industry": _ind if _ind not in ("Unknown", "nan") else "—",
                }
        except Exception:
            _symbols = []
            _sector_map = {}

        if not _symbols:
            st.error("Could not fetch NIFTY 500 symbols.")
        else:
            # Store previous scan for "What moved"
            if "rpci_result" in st.session_state and st.session_state["rpci_result"] is not None:
                st.session_state["rpci_prev_result"] = st.session_state["rpci_result"].copy()

            _prog = st.progress(0, text="Starting…")
            _stat = st.empty()

            def _rpci_cb(msg: str, pct: int):
                _prog.progress(max(0, min(pct, 100)), text=msg)
                _stat.caption(msg)

            _res_df = _rpci_scan(_symbols, progress_cb=_rpci_cb, sector_map=_sector_map)
            _prog.progress(100, text="Done!")
            _stat.empty()

            # Compute HELD (consecutive sessions at same score)
            _prev_held: dict = st.session_state.get("rpci_held", {})
            _prev_res  = st.session_state.get("rpci_prev_result")
            _prev_sc: dict = {}
            if _prev_res is not None and not _prev_res.empty:
                _prev_sc = dict(zip(_prev_res["symbol"].tolist(), _prev_res["score"].tolist()))
            _new_held: dict = {}
            for _sym_h, _sc_h in zip(_res_df["symbol"].tolist(), _res_df["score"].tolist()):
                if _prev_sc.get(_sym_h) == _sc_h:
                    _new_held[_sym_h] = _prev_held.get(_sym_h, 0) + 1
                else:
                    _new_held[_sym_h] = 0
            _res_df["held"] = _res_df["symbol"].map(_new_held).fillna(0).astype(int)

            st.session_state["rpci_result"]     = _res_df
            st.session_state["rpci_held"]        = _new_held
            st.session_state["rpci_scanned_at"]  = datetime.datetime.now(_IST_TZ)
            st.session_state.pop("rpci_selected", None)

    # ── Results ───────────────────────────────────────────────────────────────
    _rdf = st.session_state.get("rpci_result")
    _rat = st.session_state.get("rpci_scanned_at")

    if _rdf is None or _rdf.empty:
        if _rdf is not None:
            st.warning("Scan returned no results.")
        else:
            st.markdown(
                '<div class="sa-card" style="text-align:center;padding:40px 20px;">'
                '<div style="font-size:2.5rem;">📊</div>'
                '<div style="color:#6e7681;margin-top:10px;">'
                'Click <b style="color:#f0f6fc;">▶ Scan NIFTY 500</b> to run the RPCI screener<br>'
                '<span style="font-size:0.78rem;">Analyses 11 conditions per stock · Takes 2–3 min · No Kite API needed</span>'
                '</div></div>',
                unsafe_allow_html=True,
            )
        return

    # ── Stats row ─────────────────────────────────────────────────────────────
    _n_total   = len(_rdf)
    _mean_sc   = round(float(_rdf["score"].mean()), 2)
    _n_9plus   = int((_rdf["score"] >= 9).sum())
    _n_7plus   = int((_rdf["score"] >= 7).sum())
    _n_below5  = int((_rdf["score"] < 5).sum())
    _n_above20 = int((_rdf.get("vs_20dma", pd.Series(dtype=float)) > 0).sum()) if "vs_20dma" in _rdf.columns else 0

    if _rat:
        st.caption(f"Scanned {_rat.strftime('%d %b %Y %H:%M')} IST  ·  {_n_total} stocks")

    _st1, _st2, _st3, _st4, _st5, _st6 = st.columns(6)
    for _col, _val, _lbl, _sub in [
        (_st1, f"{_mean_sc:.2f}", "MEAN SCORE",    f"median {int(_rdf['score'].median())}"),
        (_st2, str(_n_9plus),     "AT 9+ / 11",    f"{_n_9plus/_n_total*100:.1f}% of universe"),
        (_st3, str(_n_7plus),     "AT 7+",         f"{_n_7plus/_n_total*100:.1f}% of universe"),
        (_st4, str(_n_below5),    "BELOW 5",       f"{_n_below5/_n_total*100:.1f}% of universe"),
        (_st5, str(_n_above20),   "ABOVE 20 DMA",  f"{_n_above20/_n_total*100:.1f}% of universe"),
        (_st6, str(_n_total),     "SCANNED",       "NIFTY 500"),
    ]:
        with _col:
            st.markdown(
                f'<div class="rpci-stat-card">'
                f'<div class="rpci-stat-val">{_val}</div>'
                f'<div class="rpci-stat-lbl">{_lbl}</div>'
                f'<div class="rpci-stat-sub">{_sub}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

    st.markdown("<br>", unsafe_allow_html=True)

    # ── Condition names list ───────────────────────────────────────────────────
    _ccols  = [c for c in _rdf.columns if c.startswith("_") and not c.endswith("_val")]
    _cnames = [c[1:] for c in _ccols]

    # ── Sector-wise cards ─────────────────────────────────────────────────────
    if "sector" in _rdf.columns:
        _prev_res = st.session_state.get("rpci_prev_result")
        _prev_sec_mean: dict = {}
        if _prev_res is not None and not _prev_res.empty and "sector" in _prev_res.columns:
            _prev_sec_mean = _prev_res.groupby("sector")["score"].mean().to_dict()

        _sec_data = (
            _rdf.groupby("sector")
            .agg(mean_score=("score", "mean"), count=("score", "size"),
                 at9=("score", lambda x: (x >= 9).sum()))
            .reset_index()
            .sort_values("mean_score", ascending=False)
        )

        _active_sector = st.session_state.get("rpci_sector_filter", "")
        _sec_html = '<div class="rpci-sec-grid">'
        for _, _sr in _sec_data.iterrows():
            _sname = str(_sr["sector"])
            _smean = float(_sr["mean_score"])
            _sn9   = int(_sr["at9"])
            _scnt  = int(_sr["count"])
            _prev_m = _prev_sec_mean.get(_sname, _smean)
            _sdelt  = _smean - _prev_m
            _delt_color = "#00d4aa" if _sdelt >= 0 else "#f85149"
            _delt_str   = f"+{_sdelt:.2f}" if _sdelt >= 0 else f"{_sdelt:.2f}"
            _is_sel = "selected" if _active_sector == _sname else ""
            _sec_html += (
                f'<div class="rpci-sec-card {_is_sel}">'
                f'<div class="rpci-sec-name">{_sname}</div>'
                f'<div>'
                f'<span class="rpci-sec-score">{_smean:.2f}</span>'
                + (f'<span class="rpci-sec-delta" style="color:{_delt_color};">&nbsp;{_delt_str} vs prev</span>' if _prev_res is not None else '')
                + f'</div>'
                f'<div class="rpci-sec-sub">{_scnt} names · {_sn9} at 9+</div>'
                f'</div>'
            )
        _sec_html += '</div>'
        st.markdown(_sec_html, unsafe_allow_html=True)

        # Sector filter control (below the cards)
        _sector_opts = ["All sectors"] + sorted(_rdf["sector"].dropna().unique().tolist())
        _sec_sel = st.selectbox("Filter by sector", _sector_opts, key="rpci_sector_sel",
                                label_visibility="collapsed",
                                format_func=lambda x: f"Sector: {x}")
        if _sec_sel != "All sectors":
            st.session_state["rpci_sector_filter"] = _sec_sel
        else:
            st.session_state.pop("rpci_sector_filter", None)
        _active_sector = st.session_state.get("rpci_sector_filter", "")
    else:
        _active_sector = ""

    # ── What moved ────────────────────────────────────────────────────────────
    _prev_res = st.session_state.get("rpci_prev_result")
    if _prev_res is not None and not _prev_res.empty:
        _prev_lookup = _prev_res.set_index("symbol")

        _upgrades:   list[dict] = []
        _downgrades: list[dict] = []

        for _, _rrow in _rdf.iterrows():
            _sym_m = str(_rrow["symbol"])
            _sc_m  = int(_rrow["score"])
            if _sym_m not in _prev_lookup.index:
                continue
            _prev_row  = _prev_lookup.loc[_sym_m]
            _sc_prev_m = int(_prev_row["score"])
            _diff_m    = _sc_m - _sc_prev_m
            if _diff_m == 0:
                continue

            _gained, _lost = [], []
            for _cn in _cnames:
                _col = f"_{_cn}"
                _now_pass  = bool(_rrow.get(_col, False))
                _prev_pass = bool(_prev_row.get(_col, False)) if _col in _prev_row.index else False
                _abr = _ABBR.get(_cn, _cn[:3])
                if _now_pass and not _prev_pass:
                    _gained.append(_abr)
                elif not _now_pass and _prev_pass:
                    _lost.append(_abr)

            _entry = {"sym": _sym_m, "old": _sc_prev_m, "new": _sc_m, "diff": _diff_m,
                      "gained": _gained, "lost": _lost}
            if _diff_m > 0:
                _upgrades.append(_entry)
            else:
                _downgrades.append(_entry)

        _upgrades   = sorted(_upgrades,   key=lambda x: -x["diff"])
        _downgrades = sorted(_downgrades, key=lambda x:  x["diff"])
        _n_up = len(_upgrades); _n_dn = len(_downgrades)
        _crossed_in  = sum(1 for u in _upgrades   if u["old"] < 9 <= u["new"])
        _crossed_out = sum(1 for d in _downgrades if d["old"] >= 9 > d["new"])

        with st.expander(f"What moved  ·  ▲ {_n_up} upgrades  ·  ▼ {_n_dn} downgrades", expanded=True):
            if _n_up > 0 or _n_dn > 0:
                st.caption(
                    f"{_n_up} stocks gained at least one condition and {_n_dn} lost at least one. "
                    f"Of those, **{_crossed_in}** crossed into the top tier (9+) and "
                    f"**{_crossed_out}** dropped out of it."
                )

            _mv_tab_opts = ["ALL", "9–11", "7–8", "5–6", "<5", "±1", "±2"]
            _mv_sel = st.radio("What moved filter", _mv_tab_opts, horizontal=True,
                               key="rpci_mv_tab", label_visibility="collapsed")

            def _mv_filter(entries: list[dict], tab: str) -> list[dict]:
                if tab == "ALL":   return entries
                if tab == "9–11": return [e for e in entries if e["new"] >= 9]
                if tab == "7–8":  return [e for e in entries if 7 <= e["new"] <= 8]
                if tab == "5–6":  return [e for e in entries if 5 <= e["new"] <= 6]
                if tab == "<5":   return [e for e in entries if e["new"] < 5]
                if tab == "±1":   return [e for e in entries if abs(e["diff"]) == 1]
                if tab == "±2":   return [e for e in entries if abs(e["diff"]) >= 2]
                return entries

            _ups_f = _mv_filter(_upgrades,   _mv_sel)
            _dns_f = _mv_filter(_downgrades, _mv_sel)

            _mu1, _mu2 = st.columns(2)
            with _mu1:
                st.markdown(f"**UPGRADES {len(_ups_f)} of {_n_up}**")
                for _ue in _ups_f[:30]:
                    _tier_str = " · ENTERED 9+" if _ue["old"] < 9 <= _ue["new"] else ""
                    _g_str = f"gained {' · '.join(_ue['gained'])}" if _ue["gained"] else ""
                    st.markdown(
                        f'<div class="rpci-mv-up">'
                        f'<span class="rpci-mv-sym">{_ue["sym"]}</span>'
                        f'<span class="rpci-mv-arr">&nbsp;{_ue["old"]} → {_ue["new"]}&nbsp;</span>'
                        f'<span class="rpci-mv-badge rpci-mv-badge-up">+{_ue["diff"]}{_tier_str}</span>'
                        + (f'<div class="rpci-mv-cond">{_g_str}</div>' if _g_str else '')
                        + f'</div>',
                        unsafe_allow_html=True,
                    )

            with _mu2:
                st.markdown(f"**DOWNGRADES {len(_dns_f)} of {_n_dn}**")
                for _de in _dns_f[:30]:
                    _tier_str = " · LEFT 9+" if _de["old"] >= 9 > _de["new"] else ""
                    _l_str = f"lost {' · '.join(_de['lost'])}" if _de["lost"] else ""
                    st.markdown(
                        f'<div class="rpci-mv-dn">'
                        f'<span class="rpci-mv-sym">{_de["sym"]}</span>'
                        f'<span class="rpci-mv-arr">&nbsp;{_de["old"]} → {_de["new"]}&nbsp;</span>'
                        f'<span class="rpci-mv-badge rpci-mv-badge-dn">{_de["diff"]}{_tier_str}</span>'
                        + (f'<div class="rpci-mv-cond">{_l_str}</div>' if _l_str else '')
                        + f'</div>',
                        unsafe_allow_html=True,
                    )
    else:
        with st.expander("What moved  ·  run scan twice to compare sessions"):
            st.caption("Run the scan a second time to see which stocks gained or lost conditions since the previous scan.")

    # ── Condition pass rates bar chart ────────────────────────────────────────
    with st.expander("Condition pass rates", expanded=False):
        _pass_rates = {
            _ABBR.get(cn, cn[:3]): round(float(_rdf[f"_{cn}"].sum()) / _n_total * 100, 1)
            for cn in _cnames if f"_{cn}" in _rdf.columns
        }
        _pr_cols = st.columns(2)
        for _idx, (_abr, _pct) in enumerate(_pass_rates.items()):
            with _pr_cols[_idx % 2]:
                st.markdown(
                    f'<div class="rpci-pbar-wrap">'
                    f'<span class="rpci-pbar-lbl">{_abr}</span>'
                    f'<div class="rpci-pbar-bg">'
                    f'<div class="rpci-pbar-fill" style="width:{_pct}%;"></div>'
                    f'</div>'
                    f'<span class="rpci-pbar-pct">{_pct}%</span>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

    st.markdown("<br>", unsafe_allow_html=True)

    # ── Score-band tabs + Filter by condition row ─────────────────────────────
    _tab_opts  = ["ALL", "9–11", "7–8", "5–6", "<5"]
    _tab_counts = {
        "ALL":  _n_total,
        "9–11": int((_rdf["score"] >= 9).sum()),
        "7–8":  int(((_rdf["score"] >= 7) & (_rdf["score"] < 9)).sum()),
        "5–6":  int(((_rdf["score"] >= 5) & (_rdf["score"] < 7)).sum()),
        "<5":   _n_below5,
    }
    _sel_tab = st.radio(
        "Score band",
        _tab_opts,
        horizontal=True,
        key="rpci_tab",
        format_func=lambda t: f"{t}  ({_tab_counts[t]})",
        label_visibility="collapsed",
    )

    # ── Filter by condition ───────────────────────────────────────────────────
    with st.expander("▼ Filter by condition", expanded=False):
        _cond_keys = list(_ABBR.items())   # [(full_name, abr), ...]
        _cond_filters: dict[str, str] = {}
        _fc_cols = st.columns(6)
        for _fi, (_cn_f, _abr_f) in enumerate(_cond_keys):
            with _fc_cols[_fi % 6]:
                _full_label = _COND_FULL.get(_abr_f, _cn_f)
                _cond_filters[_cn_f] = st.selectbox(
                    _full_label,
                    ["Any", "PASS", "FAIL"],
                    key=f"rpci_cond_{_abr_f}",
                )
        _clear_cf = st.button("clear condition filters", key="rpci_clear_cf")
        if _clear_cf:
            for _cn_f, _ in _cond_keys:
                _abr_f = _ABBR[_cn_f]
                st.session_state.pop(f"rpci_cond_{_abr_f}", None)
            st.rerun()

    # ── Apply filters ─────────────────────────────────────────────────────────
    _fdf = _rdf.copy()
    # Score band
    if _sel_tab == "9–11":
        _fdf = _fdf[_fdf["score"] >= 9]
    elif _sel_tab == "7–8":
        _fdf = _fdf[(_fdf["score"] >= 7) & (_fdf["score"] < 9)]
    elif _sel_tab == "5–6":
        _fdf = _fdf[(_fdf["score"] >= 5) & (_fdf["score"] < 7)]
    elif _sel_tab == "<5":
        _fdf = _fdf[_fdf["score"] < 5]
    # Sector
    if _active_sector:
        _fdf = _fdf[_fdf.get("sector", pd.Series()) == _active_sector] if "sector" in _fdf.columns else _fdf
    # Ticker search
    if _ticker_search.strip():
        _fdf = _fdf[_fdf["symbol"].str.upper().str.contains(_ticker_search.strip().upper(), na=False)]
    # Condition filters
    for _cn_f, _cv_f in _cond_filters.items():
        _col_f = f"_{_cn_f}"
        if _cv_f == "PASS" and _col_f in _fdf.columns:
            _fdf = _fdf[_fdf[_col_f].astype(bool)]
        elif _cv_f == "FAIL" and _col_f in _fdf.columns:
            _fdf = _fdf[~_fdf[_col_f].astype(bool)]

    # ── Row count + download ──────────────────────────────────────────────────
    _dl1, _dl2 = st.columns([5, 1])
    with _dl1:
        st.caption(f"{len(_fdf)} of {_n_total} rows")
    with _dl2:
        _csv_data = _fdf[["symbol"] + (["sector","industry"] if "sector" in _fdf.columns else []) +
                          ["score","price","chg_pct","vs_20dma","vol_cr"] +
                          [f"_{cn}" for cn in _cnames if f"_{cn}" in _fdf.columns]].to_csv(index=False)
        st.download_button("⬇ CSV", _csv_data, "rpci_scan.csv", "text/csv",
                           use_container_width=True, key="rpci_dl_csv")

    # ── All stocks table ──────────────────────────────────────────────────────
    _has_extra  = "chg_pct" in _fdf.columns and "vs_20dma" in _fdf.columns
    _has_sector = "sector"  in _fdf.columns
    _has_held   = "held"    in _fdf.columns

    _tbl_html = (
        '<div class="rpci-tbl-wrap"><table class="rpci-tbl"><thead><tr>'
        '<th>#</th><th>TICKER</th><th>SCORE</th>'
    )
    if _has_sector:
        _tbl_html += '<th>SECTOR</th><th>INDUSTRY</th>'
    _tbl_html += '<th style="text-align:center;">CONDITIONS 1–11</th><th>CLOSE</th>'
    if _has_extra:
        _tbl_html += '<th>CHG%</th><th>VS 20 DMA</th><th>VOLUME (Cr)</th>'
    if _has_held:
        _tbl_html += '<th title="Consecutive sessions at this score">HELD</th>'
    _tbl_html += '</tr></thead><tbody>'

    for _ri, _rr in _fdf.head(300).iterrows():
        _sym  = str(_rr["symbol"])
        _sc   = int(_rr["score"])
        _scls = "rpci-s9" if _sc >= 9 else "rpci-s7" if _sc >= 7 else "rpci-s5" if _sc >= 5 else "rpci-slow"
        _pr   = f"₹{_rr['price']:,.1f}"

        _blocks = "".join(
            f'<span class="rpci-blk-pass" title="{_ABBR.get(cn,cn)}"></span>'
            if bool(_rr.get(f"_{cn}", False))
            else f'<span class="rpci-blk-fail" title="{_ABBR.get(cn,cn)}"></span>'
            for cn in _cnames
        )

        _row = (
            f'<tr>'
            f'<td class="rpci-neutral" style="font-size:.72rem;">{int(_ri)+1}</td>'
            f'<td class="rpci-sym">{_sym}</td>'
            f'<td class="{_scls}" style="font-weight:800;">{_sc}</td>'
        )
        if _has_sector:
            _sec_v = str(_rr.get("sector", "—"))
            _ind_v = str(_rr.get("industry", "—"))
            _row += f'<td class="rpci-neutral" style="font-size:.72rem;">{_sec_v}</td>'
            _row += f'<td class="rpci-neutral" style="font-size:.72rem;">{_ind_v[:30]}</td>'
        _row += f'<td style="line-height:1;">{_blocks}</td><td>{_pr}</td>'
        if _has_extra:
            _chg = float(_rr.get("chg_pct", 0) or 0)
            _dma = float(_rr.get("vs_20dma", 0) or 0)
            _vol = float(_rr.get("vol_cr", 0) or 0)
            _chg_cls = "rpci-up" if _chg >= 0 else "rpci-dn"
            _dma_cls = "rpci-up" if _dma >= 0 else "rpci-dn"
            _row += (
                f'<td class="{_chg_cls}">{_chg:+.2f}%</td>'
                f'<td class="{_dma_cls}">{_dma:+.2f}%</td>'
                f'<td class="rpci-neutral">{_vol:.2f}</td>'
            )
        if _has_held:
            _hld = int(_rr.get("held", 0))
            _row += f'<td class="rpci-held">{_hld if _hld > 0 else "—"}</td>'
        _row += '</tr>'
        _tbl_html += _row

    _tbl_html += '</tbody></table></div>'
    st.markdown(_tbl_html, unsafe_allow_html=True)

    # Legend
    _leg = " · ".join(f"<b>{v}</b> {k}" for k, v in _ABBR.items())
    st.caption(_leg, unsafe_allow_html=True)

    # ── Detail view ───────────────────────────────────────────────────────────
    st.markdown("<div style='margin-top:20px;'></div>", unsafe_allow_html=True)
    _syms_list = _fdf["symbol"].tolist()
    _detail_sel = st.selectbox(
        "Full RPCI detail for stock",
        [""] + _syms_list,
        key="rpci_detail_sel",
        format_func=lambda x: "— select a stock for full conditions + momentum chart —" if x == "" else x,
    )

    if _detail_sel:
        st.session_state["rpci_selected"] = _detail_sel

    _selected = st.session_state.get("rpci_selected", "")
    if _selected and _selected in _syms_list:
        st.markdown("---")
        st.markdown(f"#### RPCI — **{_selected}**")

        with st.spinner(f"Fetching daily + weekly data for {_selected}…"):
            _det = _rpci_detail(_selected)

        if _det.get("error"):
            st.error(_det["error"])
        else:
            _d1, _d2 = st.columns([1, 1])

            with _d1:
                _conds_d = _det["conditions"]
                _score_d = _det["score"]
                _total_d = _det["total"]
                _label_d = _det["label"]
                _lcls_d, _ = _LABEL_CLS.get(_label_d, ("", "#6e7681"))

                _rows_html = ""
                for _num, (_cn, _cv) in enumerate(_conds_d.items(), 1):
                    _abr   = _ABBR.get(_cn, _cn[:3])
                    _badge = '<span class="rpci-pass">PASS</span>' if _cv["pass"] else '<span class="rpci-fail">FAIL</span>'
                    _rows_html += (
                        f'<div class="rpci-cond-row">'
                        f'<span style="color:#6e7681;font-size:.7rem;margin-right:6px;">{_num}</span>'
                        f'<span class="rpci-cname">{_cn} <span style="color:#6e7681;font-size:.68rem;">({_abr})</span></span>'
                        f'<span style="display:flex;align-items:center;gap:8px;">'
                        f'<span class="rpci-cval">{_cv["result"]}</span>'
                        f'{_badge}</span></div>'
                    )

                _scls_d = "rpci-s9" if _score_d >= 9 else "rpci-s7" if _score_d >= 7 else "rpci-s5" if _score_d >= 5 else "rpci-slow"

                st.markdown(
                    f'<div class="rpci-panel">'
                    f'<div class="rpci-panel-hdr">'
                    f'<span class="rpci-ind-lbl">RPCI INDICATOR</span>'
                    f'<span class="rpci-res-lbl">Result</span>'
                    f'</div>'
                    + _rows_html +
                    f'<div class="rpci-score-row">'
                    f'<span style="color:#6e7681;font-size:.73rem;font-weight:700;text-transform:uppercase;">Score</span>'
                    f'<span style="display:flex;align-items:center;gap:12px;">'
                    f'<span class="rpci-score-val {_scls_d}">{_score_d} / {_total_d}</span>'
                    f'<span class="{_lcls_d}">{_label_d}</span>'
                    f'</span></div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

            with _d2:
                _hist = _det.get("momentum_hist", pd.Series(dtype=float))
                if not _hist.empty:
                    _hs = _hist.iloc[-126:]
                    _fig_hist = go.Figure()
                    _fig_hist.add_trace(go.Bar(
                        x=_hs.index,
                        y=_hs.values,
                        marker_color=["#00d4aa" if float(v) >= 0 else "#f85149" for v in _hs.values],
                        name="RPCI Momentum",
                    ))
                    _fig_hist.add_hline(y=0, line_color="#2a2e39", line_width=1)
                    _fig_hist.update_layout(
                        title=dict(text=f"RPCI Momentum — {_selected}", font=dict(color="#c9d1d9", size=13)),
                        height=380,
                        paper_bgcolor="#131722", plot_bgcolor="#131722",
                        font=dict(color="#6e7681"),
                        xaxis=dict(gridcolor="#2a2e39", showgrid=False, tickfont=dict(size=10)),
                        yaxis=dict(gridcolor="#2a2e39", zeroline=False,
                                   title=dict(text="Momentum", font=dict(size=11))),
                        margin=dict(l=0, r=0, t=40, b=0),
                        showlegend=False,
                    )
                    st.plotly_chart(_fig_hist, use_container_width=True)
                else:
                    st.info("Momentum data not available.")


# ── Late Session Blaster page ─────────────────────────────────────────────────

def page_late_session():
    _IST_LS = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

    st.markdown("""<style>
/* Late Session Blaster */
.lhb-header{display:flex;align-items:center;gap:10px;margin-bottom:4px;}
.lhb-title{font-size:1.45rem;font-weight:800;color:#f0f6fc;}
.lhb-badge-live{background:#0d2818;border:1px solid #2ea043;color:#2ea043;font-size:.68rem;font-weight:700;padding:3px 9px;border-radius:20px;letter-spacing:.8px;}
.lhb-badge-cas{background:#1a1200;border:1px solid #ffa657;color:#ffa657;font-size:.68rem;font-weight:700;padding:3px 9px;border-radius:20px;letter-spacing:.8px;}
.lhb-badge-wait{background:#161b22;border:1px solid #30363d;color:#6e7681;font-size:.68rem;font-weight:700;padding:3px 9px;border-radius:20px;}
.lhb-cdown{display:flex;align-items:center;gap:8px;background:#12191f;border:1px solid #21262d;border-radius:8px;padding:7px 14px;margin-bottom:12px;}
.lhb-cdown-lbl{font-size:.72rem;color:#6e7681;}
.lhb-cdown-val{font-size:1.1rem;font-weight:700;color:#e6b800;font-variant-numeric:tabular-nums;}
/* Signal card */
.lhb-card{border-radius:12px;padding:20px 24px;margin:10px 0;position:relative;overflow:hidden;}
.lhb-card-strong-ce{background:linear-gradient(135deg,#001a12,#003020);border:2px solid #00d4aa;box-shadow:0 0 20px rgba(0,212,170,.25);}
.lhb-card-ce{background:linear-gradient(135deg,#0d1a16,#0a1f18);border:2px solid rgba(0,212,170,.5);}
.lhb-card-strong-pe{background:linear-gradient(135deg,#1a0007,#2d000c);border:2px solid #f85149;box-shadow:0 0 20px rgba(248,81,73,.25);}
.lhb-card-pe{background:linear-gradient(135deg,#1a0d0d,#200e0e);border:2px solid rgba(248,81,73,.5);}
.lhb-card-watch{background:linear-gradient(135deg,#0d1117,#12191f);border:1px solid #e6b800;}
.lhb-card-wait{background:#0d1117;border:1px solid #21262d;}
.lhb-sig-lbl{font-size:.7rem;color:#6e7681;text-transform:uppercase;letter-spacing:1px;margin-bottom:4px;}
.lhb-sig-strong-ce{font-size:1.9rem;font-weight:900;color:#00d4aa;letter-spacing:-1px;}
.lhb-sig-ce{font-size:1.6rem;font-weight:800;color:#00d4aa;}
.lhb-sig-strong-pe{font-size:1.9rem;font-weight:900;color:#f85149;letter-spacing:-1px;}
.lhb-sig-pe{font-size:1.6rem;font-weight:800;color:#f85149;}
.lhb-sig-watch{font-size:1.5rem;font-weight:700;color:#e6b800;}
.lhb-sig-wait{font-size:1.5rem;font-weight:700;color:#6e7681;}
.lhb-sig-sub{font-size:.84rem;color:#6e7681;margin-top:5px;}
.lhb-score-box{position:absolute;top:16px;right:20px;text-align:right;}
.lhb-score-lbl{font-size:.68rem;color:#6e7681;letter-spacing:.8px;text-transform:uppercase;}
.lhb-score-ce{font-size:1.2rem;font-weight:800;color:#00d4aa;}
.lhb-score-pe{font-size:1.2rem;font-weight:800;color:#f85149;}
.lhb-score-nt{font-size:1.2rem;font-weight:700;color:#6e7681;}
/* flip guard banner */
.lhb-flip-warn{background:rgba(230,184,0,.08);border:1px solid rgba(230,184,0,.35);border-radius:8px;padding:8px 14px;margin:6px 0;font-size:.78rem;color:#e6b800;}
/* Factor table */
.lhb-factor-tbl{width:100%;border-collapse:collapse;font-size:.8rem;margin:10px 0;}
.lhb-factor-tbl th{color:#6e7681;font-size:.65rem;font-weight:700;text-transform:uppercase;letter-spacing:.7px;border-bottom:1px solid #21262d;padding:5px 8px;text-align:left;}
.lhb-factor-tbl td{padding:5px 8px;border-bottom:1px solid #161b22;vertical-align:middle;color:#c9d1d9;}
.lhb-factor-tbl tr:last-child td{border-bottom:none;}
.lhb-bias-ce{display:inline-block;background:#0d2818;color:#2ea043;border:1px solid #2ea043;font-size:.65rem;padding:1px 7px;border-radius:4px;font-weight:700;}
.lhb-bias-pe{display:inline-block;background:#2a0d0d;color:#f85149;border:1px solid #f85149;font-size:.65rem;padding:1px 7px;border-radius:4px;font-weight:700;}
.lhb-bias-neu{display:inline-block;background:#161b22;color:#6e7681;border:1px solid #30363d;font-size:.65rem;padding:1px 7px;border-radius:4px;}
.lhb-pts-pos{color:#00d4aa;font-weight:700;}
.lhb-pts-neg{color:#f85149;font-weight:700;}
.lhb-pts-neu{color:#6e7681;}
/* Stats row */
.lhb-stat{background:#1e222d;border:1px solid #2a2e39;border-radius:8px;padding:10px 14px;text-align:center;}
.lhb-stat-val{font-size:1.2rem;font-weight:800;color:#f0f6fc;}
.lhb-stat-lbl{font-size:.62rem;color:#6e7681;text-transform:uppercase;letter-spacing:.7px;margin-top:2px;}
/* ATM box */
.lhb-atm-box{background:#1e222d;border:1px solid #2a2e39;border-radius:10px;padding:14px 18px;}
.lhb-atm-title{font-size:.68rem;font-weight:700;color:#6e7681;text-transform:uppercase;letter-spacing:.8px;margin-bottom:8px;}
.lhb-atm-row{display:flex;justify-content:space-between;align-items:center;padding:4px 0;border-bottom:1px solid #161b22;font-size:.82rem;}
.lhb-atm-row:last-child{border-bottom:none;}
.lhb-atm-sym{font-weight:700;color:#f0f6fc;}
.lhb-atm-note{font-size:.68rem;color:#6e7681;}
.lhb-ce-txt{color:#00d4aa;font-weight:700;}
.lhb-pe-txt{color:#f85149;font-weight:700;}
/* CAS box */
.lhb-cas-box{background:rgba(255,166,87,.06);border:1px solid rgba(255,166,87,.35);border-radius:10px;padding:14px 18px;margin-top:12px;}
.lhb-cas-title{font-size:.68rem;font-weight:700;color:#ffa657;text-transform:uppercase;letter-spacing:.8px;margin-bottom:6px;}
</style>""", unsafe_allow_html=True)

    # ── Header & countdown ────────────────────────────────────────────────────
    _in_late, _in_cas, _mins_left = _ls_window()
    _now_ls = datetime.datetime.now(_IST_LS)

    _badge = (
        '<span class="lhb-badge-live">ACTIVE</span>' if _in_late
        else '<span class="lhb-badge-wait">OUTSIDE WINDOW</span>'
    )
    _cas_badge = '<span class="lhb-badge-cas">CAS LIVE</span>' if _in_cas else ''

    st.markdown(
        f'<div class="lhb-header">'
        f'<span class="lhb-title">🌅 Late Session Blaster</span>'
        f'{_badge}{_cas_badge}'
        f'</div>'
        f'<div style="color:#6e7681;font-size:.8rem;margin-bottom:10px;">'
        f'Last 45 min NIFTY/BANKNIFTY/SENSEX CE & PE · CAS wild-move detection · Flip-guard prevents conflicting signals'
        f'</div>',
        unsafe_allow_html=True,
    )

    _hh = _mins_left // 60; _mm = _mins_left % 60
    st.markdown(
        f'<div class="lhb-cdown">'
        f'<span class="lhb-cdown-lbl">NSE closes in</span>'
        f'<span class="lhb-cdown-val">{_hh:02d}:{_mm:02d}</span>'
        f'<span class="lhb-cdown-lbl" style="margin-left:12px;">Active window: 2:30–3:30 PM NSE  |  3:00–4:00 PM CAS (SENSEX)</span>'
        f'</div>',
        unsafe_allow_html=True,
    )

    # ── Auto-refresh (always on 30 s) ─────────────────────────────────────────
    _ls_tick     = _st_autorefresh(interval=30_000, key="ls_autorefresh")
    _ls_prev_tick = st.session_state.get("ls_prev_tick", -1)
    _should_refresh = (_ls_tick != _ls_prev_tick)
    st.session_state["ls_prev_tick"] = _ls_tick

    # ── Index selector ─────────────────────────────────────────────────────────
    _lc1, _lc2, _lc3 = st.columns([2, 1, 1])
    with _lc1:
        _ls_sym = st.radio("Index", list(_LS_CONFIG.keys()), horizontal=True,
                           key="ls_sym", label_visibility="collapsed")
    with _lc2:
        _ls_refresh_btn = st.button("⟳ Refresh Now", use_container_width=True, key="ls_refresh_btn")
    with _lc3:
        _ls_clear_btn = st.button("✕ Clear Signal", use_container_width=True, key="ls_clear_btn")

    _cfg = _LS_CONFIG[_ls_sym]
    _step = _cfg["step"]
    _color = _cfg["color"]
    _is_cas = _cfg["cas"] and _in_cas

    if _ls_clear_btn:
        for _k in (f"ls_{_ls_sym}_signal", f"ls_{_ls_sym}_signal_time",
                   f"ls_{_ls_sym}_pending_count", f"ls_{_ls_sym}_data"):
            st.session_state.pop(_k, None)
        st.rerun()

    # ── Fetch + score ─────────────────────────────────────────────────────────
    if _should_refresh or _ls_refresh_btn or f"ls_{_ls_sym}_data" not in st.session_state:
        with st.spinner(f"Fetching {_ls_sym} intraday data…"):
            _ls_data = _ls_fetch(_ls_sym)
        if _ls_data.get("error"):
            st.error(_ls_data["error"])
            st.session_state.pop(f"ls_{_ls_sym}_data", None)
        else:
            st.session_state[f"ls_{_ls_sym}_data"]     = _ls_data
            st.session_state[f"ls_{_ls_sym}_fetched"]  = _now_ls

    _ls_data   = st.session_state.get(f"ls_{_ls_sym}_data")
    _ls_fetched = st.session_state.get(f"ls_{_ls_sym}_fetched")

    if _ls_data is None:
        st.info("Click **⟳ Refresh Now** to load intraday data.")
        return

    _df5 = _ls_data["df_5m"]
    _df1 = _ls_data.get("df_1m")

    # Score
    _sc_res = _ls_score(_df5, _df1, is_cas=_is_cas)
    if _sc_res.get("error"):
        st.warning(_sc_res["error"])
        return

    _new_raw = _ls_raw_signal(_sc_res["score"])

    # Apply flip guard
    _prev_sig   = st.session_state.get(f"ls_{_ls_sym}_signal",       "WATCH")
    _prev_time  = st.session_state.get(f"ls_{_ls_sym}_signal_time",   None)
    _pend_count = st.session_state.get(f"ls_{_ls_sym}_pending_count", 0)

    _final_sig, _new_pcount, _flip_msg = _ls_flip_guard(
        _new_raw, _prev_sig, _prev_time, _pend_count
    )

    # Only update stored signal if direction changed
    if _final_sig != _prev_sig:
        st.session_state[f"ls_{_ls_sym}_signal"]       = _final_sig
        st.session_state[f"ls_{_ls_sym}_signal_time"]  = _now_ls
        st.session_state[f"ls_{_ls_sym}_pending_count"] = _new_pcount
    else:
        st.session_state[f"ls_{_ls_sym}_pending_count"] = _new_pcount

    _spot        = _sc_res["spot"]
    _vwap        = _sc_res["vwap"]
    _velocity    = _sc_res["velocity"]
    _vol_ratio   = _sc_res["volume_ratio"]
    _score       = _sc_res["score"]
    _factors     = _sc_res["factors"]
    _atm         = _ls_atm(_spot, _step) if not (isinstance(_spot, float) and _spot != _spot) else 0

    # Signal age
    _sig_age_str = ""
    if _prev_time:
        _elapsed_s = int((_now_ls - _prev_time).total_seconds())
        _sig_age_str = f"{_elapsed_s // 60}m {_elapsed_s % 60}s"

    # ── Signal card ───────────────────────────────────────────────────────────
    def _card_cls(sig: str) -> tuple[str, str, str]:
        """Returns (card_class, sig_class, sig_text)."""
        if sig == "STRONG BUY CE":
            return "lhb-card-strong-ce", "lhb-sig-strong-ce", "🟢 STRONG BUY CE"
        if sig == "BUY CE":
            return "lhb-card-ce",        "lhb-sig-ce",        "🟢 BUY CE"
        if sig == "STRONG BUY PE":
            return "lhb-card-strong-pe", "lhb-sig-strong-pe", "🔴 STRONG BUY PE"
        if sig == "BUY PE":
            return "lhb-card-pe",        "lhb-sig-pe",        "🔴 BUY PE"
        if sig in ("WATCH CE", "WATCH PE"):
            return "lhb-card-watch",     "lhb-sig-watch",     f"⚠️ {sig}"
        return "lhb-card-wait",          "lhb-sig-wait",      "⏳ WATCH / WAIT"

    _cc, _sc2, _st2 = _card_cls(_final_sig)
    _score_cls = "lhb-score-ce" if _score > 0 else ("lhb-score-pe" if _score < 0 else "lhb-score-nt")

    _atm_display = f"ATM: **{_atm} {('CE' if 'CE' in _final_sig else 'PE' if 'PE' in _final_sig else 'CE/PE')}**" if _atm else ""
    _sub_txt = (
        f"Strike: {_atm} · Vol {_vol_ratio:.1f}× avg"
        + (f" · Signal held {_sig_age_str}" if _sig_age_str else "")
        + (" · CAS auction active" if _is_cas else "")
    )

    st.markdown(
        f'<div class="lhb-card {_cc}">'
        f'<div class="lhb-sig-lbl">SIGNAL — {_ls_sym}</div>'
        f'<div class="{_sc2}">{_st2}</div>'
        f'<div class="lhb-sig-sub">{_sub_txt}</div>'
        f'<div class="lhb-score-box">'
        f'<div class="lhb-score-lbl">SCORE</div>'
        f'<div class="{_score_cls}">{_score:+d}/10</div>'
        f'</div></div>',
        unsafe_allow_html=True,
    )

    # Flip guard warning
    if _flip_msg:
        st.markdown(f'<div class="lhb-flip-warn">⏳ {_flip_msg}</div>', unsafe_allow_html=True)

    if _ls_fetched:
        st.caption(f"Data as of {_ls_fetched.strftime('%H:%M:%S')} IST · auto-refresh 30s")

    # ── Stat row ──────────────────────────────────────────────────────────────
    _s1, _s2, _s3, _s4 = st.columns(4)
    for _col, _val, _lbl in [
        (_s1, f"₹{_spot:,.1f}",    f"{_ls_sym} SPOT"),
        (_s2, f"₹{_vwap:,.1f}" if not (isinstance(_vwap, float) and _vwap != _vwap) else "N/A",
              "VWAP"),
        (_s3, f"{_velocity:+.1f}",  "VEL (pts/5m)"),
        (_s4, f"{_vol_ratio:.1f}×", "VOLUME"),
    ]:
        with _col:
            st.markdown(
                f'<div class="lhb-stat">'
                f'<div class="lhb-stat-val">{_val}</div>'
                f'<div class="lhb-stat-lbl">{_lbl}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

    st.markdown("<br>", unsafe_allow_html=True)

    # ── Factor table + ATM box ─────────────────────────────────────────────────
    _fl, _fr = st.columns([3, 2])

    with _fl:
        _rows_html = ""
        for _f in _factors:
            _pts = _f["pts"]
            _bias_cls = "lhb-bias-ce" if _f["bias"] == "CE" else (
                "lhb-bias-pe" if _f["bias"] == "PE" else "lhb-bias-neu")
            _pts_cls = "lhb-pts-pos" if _pts > 0 else ("lhb-pts-neg" if _pts < 0 else "lhb-pts-neu")
            _rows_html += (
                f'<tr>'
                f'<td><b>{_f["name"]}</b></td>'
                f'<td style="color:#8b949e;">{_f["value"]}</td>'
                f'<td><span class="{_bias_cls}">{_f["bias"]}</span></td>'
                f'<td class="{_pts_cls}" style="text-align:right;">{_pts:+d}</td>'
                f'</tr>'
            )
        # Total row
        _tot_cls = "lhb-pts-pos" if _score > 0 else ("lhb-pts-neg" if _score < 0 else "lhb-pts-neu")
        _rows_html += (
            f'<tr style="border-top:2px solid #2a2e39;">'
            f'<td colspan="3" style="font-weight:700;color:#f0f6fc;">TOTAL SCORE</td>'
            f'<td class="{_tot_cls}" style="text-align:right;font-weight:800;font-size:.95rem;">{_score:+d}</td>'
            f'</tr>'
        )
        st.markdown(
            f'<table class="lhb-factor-tbl"><thead><tr>'
            f'<th>FACTOR</th><th>READING</th><th>BIAS</th><th style="text-align:right;">PTS</th>'
            f'</tr></thead><tbody>{_rows_html}</tbody></table>',
            unsafe_allow_html=True,
        )

    with _fr:
        _ce_strike = _atm
        _pe_strike = _atm
        _itm_ce    = _atm - _step
        _itm_pe    = _atm + _step

        st.markdown(
            f'<div class="lhb-atm-box">'
            f'<div class="lhb-atm-title">⚡ Strikes to Watch</div>'
            f'<div class="lhb-atm-row">'
            f'<span class="lhb-atm-sym lhb-ce-txt">{_ce_strike} CE</span>'
            f'<span class="lhb-atm-note">ATM · lot {_cfg["lot"]}</span>'
            f'</div>'
            f'<div class="lhb-atm-row">'
            f'<span class="lhb-atm-sym lhb-pe-txt">{_pe_strike} PE</span>'
            f'<span class="lhb-atm-note">ATM · lot {_cfg["lot"]}</span>'
            f'</div>'
            f'<div class="lhb-atm-row" style="opacity:.6;">'
            f'<span class="lhb-atm-sym lhb-ce-txt">{_itm_ce} CE</span>'
            f'<span class="lhb-atm-note">1-ITM CE</span>'
            f'</div>'
            f'<div class="lhb-atm-row" style="opacity:.6;">'
            f'<span class="lhb-atm-sym lhb-pe-txt">{_itm_pe} PE</span>'
            f'<span class="lhb-atm-note">1-ITM PE</span>'
            f'</div>'
            f'</div>',
            unsafe_allow_html=True,
        )

        # CAS box — show when SENSEX is selected and CAS window is active
        if _cfg.get("cas"):
            _cas_status = "🟠 CAS ACTIVE NOW (3:00–4:00 PM)" if _in_cas else "CAS window: 3:00–4:00 PM IST"
            _cas_note = (
                "BSE Closing Auction Session running — institutional orders dominate. "
                "Trend into CAS typically continues with amplified velocity."
                if _in_cas else
                "CAS not yet active. Watch for volume build-up from 2:30 PM onward."
            )
            st.markdown(
                f'<div class="lhb-cas-box">'
                f'<div class="lhb-cas-title">{_cas_status}</div>'
                f'<div style="font-size:.77rem;color:#8b949e;">{_cas_note}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

    # ── 5-min price chart with VWAP + EMA9/21 ────────────────────────────────
    st.markdown("<div style='margin-top:14px;'></div>", unsafe_allow_html=True)
    with st.expander("5-min chart · VWAP · EMA 9/21", expanded=True):
        try:
            import plotly.graph_objects as _lgo
            _d5  = _df5.copy()
            if _d5.index.tz is None:
                _d5.index = _d5.index.tz_localize("UTC").tz_convert(_IST_LS)
            else:
                _d5.index = _d5.index.tz_convert(_IST_LS)
            # Today only
            _today_mask = pd.Series(_d5.index.date, index=_d5.index) == datetime.date.today()
            _d5 = _d5[_today_mask]

            if not _d5.empty:
                from screener.late_session import _ema as _ls_ema_fn
                _c5  = _d5["Close"].dropna()
                _e9  = _ls_ema_fn(_c5, 9)
                _e21 = _ls_ema_fn(_c5, 21)

                # VWAP from 1m if available, else 5m
                _vwap_line = None
                _src_df = _df1 if (_df1 is not None and not _df1.empty) else _d5
                if not _src_df.empty:
                    _s1m = _src_df.copy()
                    if _s1m.index.tz is None:
                        _s1m.index = _s1m.index.tz_localize("UTC").tz_convert(_IST_LS)
                    else:
                        _s1m.index = _s1m.index.tz_convert(_IST_LS)
                    _s1m = _s1m[pd.Series(_s1m.index.date, index=_s1m.index) == datetime.date.today()]
                    if not _s1m.empty and "Volume" in _s1m.columns:
                        _tp = (_s1m["High"] + _s1m["Low"] + _s1m["Close"]) / 3
                        _vp = (_tp * _s1m["Volume"].replace(0, float("nan"))).cumsum()
                        _vs = _s1m["Volume"].replace(0, float("nan")).cumsum()
                        _vwap_s = _vp / _vs
                        # Resample to 5m for overlay
                        _vwap_5m = _vwap_s.resample("5min").last().ffill()
                        # Align with _d5 index
                        _common = _d5.index.intersection(_vwap_5m.index)
                        if len(_common) > 0:
                            _vwap_line = _vwap_5m.loc[_common]

                _fig_ls = _lgo.Figure()

                # Candlestick
                _fig_ls.add_trace(_lgo.Candlestick(
                    x=_d5.index, open=_d5["Open"], high=_d5["High"],
                    low=_d5["Low"], close=_d5["Close"],
                    increasing_line_color="#00d4aa", decreasing_line_color="#f85149",
                    name=_ls_sym, showlegend=False,
                ))

                # EMA 9
                _fig_ls.add_trace(_lgo.Scatter(
                    x=_c5.index, y=_e9.values,
                    line=dict(color="#e6b800", width=1.5), name="EMA 9",
                ))
                # EMA 21
                _fig_ls.add_trace(_lgo.Scatter(
                    x=_c5.index, y=_e21.values,
                    line=dict(color="#ffa657", width=1.5, dash="dot"), name="EMA 21",
                ))
                # VWAP
                if _vwap_line is not None:
                    _fig_ls.add_trace(_lgo.Scatter(
                        x=_vwap_line.index, y=_vwap_line.values,
                        line=dict(color="#79c0ff", width=1.5, dash="dashdot"), name="VWAP",
                    ))

                # Highlight late session (2:30 PM onward)
                _late_start_dt = _d5.index[-1].normalize().replace(
                    hour=14, minute=30, tzinfo=_IST_LS)
                _fig_ls.add_vrect(
                    x0=str(_late_start_dt), x1=str(_d5.index[-1]),
                    fillcolor="rgba(0,212,170,0.05)", line_width=0,
                    annotation_text="Late Session", annotation_position="top left",
                    annotation_font=dict(size=10, color="#00d4aa"),
                )

                _fig_ls.update_layout(
                    height=380,
                    paper_bgcolor="#131722", plot_bgcolor="#131722",
                    font=dict(color="#6e7681"),
                    xaxis=dict(gridcolor="#1e222d", rangeslider=dict(visible=False),
                               tickfont=dict(size=9), showgrid=True),
                    yaxis=dict(gridcolor="#1e222d", tickfont=dict(size=10)),
                    legend=dict(orientation="h", yanchor="bottom", y=1.02,
                                font=dict(size=10)),
                    margin=dict(l=0, r=0, t=10, b=0),
                )
                st.plotly_chart(_fig_ls, use_container_width=True)
        except Exception as _e:
            st.caption(f"Chart error: {_e}")


# ── Elder Ray Index Options Trading page ──────────────────────────────────────

def page_elder_ray():
    _IST_TZ = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

    st.markdown("""<style>
/* Elder Ray page */
.er-card{background:#1e222d;border:1px solid #2a2e39;border-radius:12px;padding:20px;margin-bottom:12px;}
.er-signal-strong-ce{background:linear-gradient(135deg,rgba(0,212,170,.22),rgba(0,212,170,.08));border:1.5px solid #00d4aa;border-radius:16px;padding:26px 20px;text-align:center;}
.er-signal-ce{background:linear-gradient(135deg,rgba(0,212,170,.13),rgba(0,212,170,.04));border:1.5px solid rgba(0,212,170,.5);border-radius:16px;padding:26px 20px;text-align:center;}
.er-signal-strong-pe{background:linear-gradient(135deg,rgba(248,81,73,.22),rgba(248,81,73,.08));border:1.5px solid #f85149;border-radius:16px;padding:26px 20px;text-align:center;}
.er-signal-pe{background:linear-gradient(135deg,rgba(248,81,73,.13),rgba(248,81,73,.04));border:1.5px solid rgba(248,81,73,.5);border-radius:16px;padding:26px 20px;text-align:center;}
.er-signal-watch{background:linear-gradient(135deg,rgba(230,184,0,.15),rgba(230,184,0,.05));border:1.5px solid rgba(230,184,0,.6);border-radius:16px;padding:26px 20px;text-align:center;}
.er-signal-wait{background:#1e222d;border:1.5px solid #2a2e39;border-radius:16px;padding:26px 20px;text-align:center;}
.er-sig-label{font-size:2.4rem;font-weight:900;letter-spacing:-1px;margin:0;}
.er-sig-sub{font-size:0.8rem;color:#6e7681;margin-top:6px;}
.er-score-bar-wrap{margin:8px 0;display:flex;align-items:center;gap:8px;}
.er-score-bar-lbl{font-size:0.72rem;font-weight:700;color:#8b949e;width:30px;text-align:right;}
.er-score-bar-bg{flex:1;background:#0d1117;border-radius:4px;height:10px;overflow:hidden;}
.er-score-bar-fill-ce{background:#00d4aa;border-radius:4px;height:10px;transition:width .4s;}
.er-score-bar-fill-pe{background:#f85149;border-radius:4px;height:10px;transition:width .4s;}
.er-score-val{font-size:0.72rem;font-weight:700;width:20px;}
.er-factor-row{display:flex;align-items:flex-start;gap:10px;padding:7px 14px;border-bottom:1px solid rgba(42,46,57,.5);font-size:0.79rem;}
.er-factor-row:last-child{border-bottom:none;}
.er-factor-name{color:#c9d1d9;font-weight:600;min-width:120px;}
.er-factor-detail{color:#8b949e;flex:1;}
.er-factor-bull{color:#00d4aa;font-weight:700;font-size:0.7rem;min-width:44px;text-align:center;background:rgba(0,212,170,.1);border-radius:4px;padding:2px 6px;}
.er-factor-bear{color:#f85149;font-weight:700;font-size:0.7rem;min-width:44px;text-align:center;background:rgba(248,81,73,.1);border-radius:4px;padding:2px 6px;}
.er-factor-neut{color:#6e7681;font-weight:600;font-size:0.7rem;min-width:44px;text-align:center;background:rgba(110,118,129,.1);border-radius:4px;padding:2px 6px;}
.er-opt-card{background:#0d1117;border:1px solid #2a2e39;border-radius:10px;padding:14px 18px;text-align:center;}
.er-opt-label{font-size:0.63rem;font-weight:800;letter-spacing:1.2px;text-transform:uppercase;margin-bottom:6px;}
.er-opt-ltp{font-size:1.85rem;font-weight:800;}
.er-opt-meta{font-size:0.72rem;color:#6e7681;margin-top:4px;}
.er-hist-row{display:flex;align-items:center;gap:10px;padding:6px 14px;border-bottom:1px solid rgba(42,46,57,.4);font-size:0.78rem;}
.er-hist-row:last-child{border-bottom:none;}
</style>""", unsafe_allow_html=True)

    st.markdown(
        '<h2 style="font-size:1.6rem;font-weight:800;margin:0 0 4px;">⚡ Elder Ray Index Trader</h2>'
        '<p style="color:#6e7681;font-size:0.82rem;margin-bottom:18px;">Multi-timeframe ATM options signal engine · NIFTY · BANKNIFTY · SENSEX</p>',
        unsafe_allow_html=True,
    )

    # ── Auto-refresh every 30 s (always on) ──────────────────────────────────
    _er_tick = 0
    if _HAS_AUTOREFRESH:
        _er_tick = _st_autorefresh(interval=30_000, key="er_autorefresh")

    _er_prev_tick = st.session_state.get("er_prev_tick", -1)
    _er_is_tick   = _er_tick != _er_prev_tick
    st.session_state["er_prev_tick"] = _er_tick

    # ── Controls ──────────────────────────────────────────────────────────────
    col_sym, col_tf, col_ref, col_badge = st.columns([2, 2, 1.2, 1.8])
    with col_sym:
        er_symbol = st.selectbox("Index", list(_ER_INDEX_CONFIG.keys()), key="er_symbol")
    with col_tf:
        er_chart_tf = st.selectbox("Chart timeframe", ["5m", "15m", "1m"], key="er_chart_tf")
    with col_ref:
        er_refresh = st.button("🔄 Refresh", key="er_refresh")
    with col_badge:
        _er_badge = (
            '<span style="display:inline-block;background:rgba(0,212,170,.15);'
            'color:#00d4aa;border:1px solid rgba(0,212,170,.4);'
            'font-size:0.65rem;font-weight:700;padding:6px 10px;border-radius:6px;">'
            '⟳ AUTO 30s</span>'
        ) if _HAS_AUTOREFRESH else (
            '<span style="color:#6e7681;font-size:0.72rem;">No autorefresh</span>'
        )
        st.markdown(_er_badge, unsafe_allow_html=True)

    # Kite credentials (from sidebar session state already set elsewhere)
    _kite_key  = st.session_state.get("kite_api_key", "")
    _kite_tok  = st.session_state.get("kite_access_token", "")
    _kite_ok   = bool(_kite_key and _kite_tok)

    # ── Run / cache signal ────────────────────────────────────────────────────
    _er_cache_key = f"er_result_{er_symbol}"
    if er_refresh or _er_is_tick or _er_cache_key not in st.session_state:
        with st.spinner(f"Fetching Elder Ray data for {er_symbol} …"):
            _er_res = run_elder_ray_signal(er_symbol)
        st.session_state[_er_cache_key] = _er_res

        # Track signal history (last 8)
        _hist_key = f"er_hist_{er_symbol}"
        _hist = st.session_state.get(_hist_key, [])
        if not _er_res.get("error"):
            _hist.insert(0, {
                "time":   datetime.datetime.now(_IST_TZ).strftime("%H:%M:%S"),
                "signal": _er_res.get("signal", "WAIT"),
                "ce":     _er_res.get("score_ce", 0),
                "pe":     _er_res.get("score_pe", 0),
                "price":  _er_res.get("price", 0),
            })
            st.session_state[_hist_key] = _hist[:8]

    _er_res = st.session_state.get(_er_cache_key, {})

    if _er_res.get("error"):
        st.error(f"Data error: {_er_res['error']}")
        return

    signal    = _er_res.get("signal",    "WAIT")
    direction = _er_res.get("direction")
    sce       = _er_res.get("score_ce",  0)
    spe       = _er_res.get("score_pe",  0)
    price     = _er_res.get("price",     0)
    ema5      = _er_res.get("ema_5m",    0)
    rsi5      = _er_res.get("rsi_5m",    50)
    vwap5     = _er_res.get("vwap_5m",   0)
    bull5     = _er_res.get("bull5",     0)
    bear5     = _er_res.get("bear5",     0)
    atm       = _er_res.get("atm",       0)
    lot       = _er_res.get("lot_size",  25)
    factors   = _er_res.get("factors",   [])
    ist_now   = _er_res.get("ist_now",   datetime.datetime.now(_IST_TZ))
    df_15m    = _er_res.get("df_15m",    pd.DataFrame())
    df_5m     = _er_res.get("df_5m",     pd.DataFrame())
    df_1m     = _er_res.get("df_1m",     pd.DataFrame())

    cfg_color = _ER_INDEX_CONFIG.get(er_symbol, {}).get("color", "#00d4aa")

    # ── Signal card ───────────────────────────────────────────────────────────
    _sig_css_map = {
        "STRONG BUY CE": "er-signal-strong-ce",
        "BUY CE":        "er-signal-ce",
        "STRONG BUY PE": "er-signal-strong-pe",
        "BUY PE":        "er-signal-pe",
        "WATCH":         "er-signal-watch",
        "WAIT":          "er-signal-wait",
    }
    _sig_color_map = {
        "STRONG BUY CE": "#00d4aa",
        "BUY CE":        "#00d4aa",
        "STRONG BUY PE": "#f85149",
        "BUY PE":        "#f85149",
        "WATCH":         "#e6b800",
        "WAIT":          "#6e7681",
    }
    _sig_css   = _sig_css_map.get(signal, "er-signal-wait")
    _sig_color = _sig_color_map.get(signal, "#6e7681")
    _updated   = ist_now.strftime("%H:%M:%S IST")

    col_sig, col_scores = st.columns([1, 1])

    with col_sig:
        _max_score = 13
        _ce_pct    = min(100, round(sce / _max_score * 100))
        _pe_pct    = min(100, round(spe / _max_score * 100))

        st.markdown(
            f'<div class="{_sig_css}">'
            f'  <div class="er-sig-label" style="color:{_sig_color};">{signal}</div>'
            f'  <div class="er-sig-sub">'
            f'    ATM {atm:,} · Lot {lot} · Updated {_updated}'
            f'  </div>'
            f'  <div style="margin-top:14px;">'
            f'    <div class="er-score-bar-wrap">'
            f'      <span class="er-score-bar-lbl" style="color:#00d4aa;">CE</span>'
            f'      <div class="er-score-bar-bg">'
            f'        <div class="er-score-bar-fill-ce" style="width:{_ce_pct}%;"></div>'
            f'      </div>'
            f'      <span class="er-score-val" style="color:#00d4aa;">{sce}</span>'
            f'    </div>'
            f'    <div class="er-score-bar-wrap">'
            f'      <span class="er-score-bar-lbl" style="color:#f85149;">PE</span>'
            f'      <div class="er-score-bar-bg">'
            f'        <div class="er-score-bar-fill-pe" style="width:{_pe_pct}%;"></div>'
            f'      </div>'
            f'      <span class="er-score-val" style="color:#f85149;">{spe}</span>'
            f'    </div>'
            f'  </div>'
            f'</div>',
            unsafe_allow_html=True,
        )

    with col_scores:
        _vwap_vs = "ABOVE" if price > vwap5 else "BELOW"
        _vwap_col = "#00d4aa" if price > vwap5 else "#f85149"
        _rsi_col  = "#00d4aa" if rsi5 > 55 else ("#f85149" if rsi5 < 45 else "#e6b800")
        _bull_col = "#00d4aa" if bull5 > 0 else "#f85149"
        _bear_col = "#00d4aa" if bear5 > 0 else "#f85149"
        st.markdown(
            f'<div class="er-card" style="padding:14px 18px;">'
            f'  <div style="font-size:0.63rem;font-weight:800;letter-spacing:1.2px;color:#6e7681;margin-bottom:10px;">LIVE INDICATORS</div>'
            f'  <div style="display:flex;flex-direction:column;gap:8px;">'
            f'    <div style="display:flex;justify-content:space-between;font-size:0.82rem;">'
            f'      <span style="color:#8b949e;">Price</span>'
            f'      <span style="color:#f0f6fc;font-weight:700;">{price:,.2f}</span></div>'
            f'    <div style="display:flex;justify-content:space-between;font-size:0.82rem;">'
            f'      <span style="color:#8b949e;">EMA 13 (5m)</span>'
            f'      <span style="color:{cfg_color};font-weight:600;">{ema5:,.2f}</span></div>'
            f'    <div style="display:flex;justify-content:space-between;font-size:0.82rem;">'
            f'      <span style="color:#8b949e;">VWAP</span>'
            f'      <span style="color:{_vwap_col};font-weight:600;">{_vwap_vs} {vwap5:,.2f}</span></div>'
            f'    <div style="display:flex;justify-content:space-between;font-size:0.82rem;">'
            f'      <span style="color:#8b949e;">RSI (5m)</span>'
            f'      <span style="color:{_rsi_col};font-weight:600;">{rsi5:.1f}</span></div>'
            f'    <div style="display:flex;justify-content:space-between;font-size:0.82rem;">'
            f'      <span style="color:#8b949e;">Bull Power</span>'
            f'      <span style="color:{_bull_col};font-weight:600;">{bull5:+.2f}</span></div>'
            f'    <div style="display:flex;justify-content:space-between;font-size:0.82rem;">'
            f'      <span style="color:#8b949e;">Bear Power</span>'
            f'      <span style="color:{_bear_col};font-weight:600;">{bear5:+.2f}</span></div>'
            f'  </div>'
            f'</div>',
            unsafe_allow_html=True,
        )

    st.markdown("<br>", unsafe_allow_html=True)

    # ── Elder Ray chart ───────────────────────────────────────────────────────
    _tf_map = {"5m": df_5m, "15m": df_15m, "1m": df_1m}
    df_chart = _tf_map.get(er_chart_tf, df_5m)

    if not df_chart.empty:
        _fig = make_subplots(
            rows=2, cols=1,
            shared_xaxes=True,
            row_heights=[0.65, 0.35],
            vertical_spacing=0.04,
            subplot_titles=["", "Elder Ray: Bull / Bear Power"],
        )

        # ── Candlestick ───────────────────────────────────────────────────────
        _fig.add_trace(go.Candlestick(
            x=df_chart.index,
            open=df_chart["Open"], high=df_chart["High"],
            low=df_chart["Low"],   close=df_chart["Close"],
            increasing_line_color="#00d4aa", decreasing_line_color="#f85149",
            increasing_fillcolor="#00d4aa",  decreasing_fillcolor="#f85149",
            line_width=1,
            name="Price",
        ), row=1, col=1)

        # EMA(13)
        if "EMA" in df_chart.columns:
            _fig.add_trace(go.Scatter(
                x=df_chart.index, y=df_chart["EMA"],
                line=dict(color=cfg_color, width=1.5),
                name="EMA 13",
            ), row=1, col=1)

        # VWAP
        if "VWAP" in df_chart.columns:
            _fig.add_trace(go.Scatter(
                x=df_chart.index, y=df_chart["VWAP"],
                line=dict(color="#e6b800", width=1.2, dash="dot"),
                name="VWAP",
            ), row=1, col=1)

        # ── Bull Power / Bear Power histogram ─────────────────────────────────
        if "Bull_Power" in df_chart.columns and "Bear_Power" in df_chart.columns:
            _bp_colors  = ["#00d4aa" if v >= 0 else "#f85149" for v in df_chart["Bull_Power"]]
            _bep_colors = ["#00d4aa" if v >= 0 else "#f85149" for v in df_chart["Bear_Power"]]

            _fig.add_trace(go.Bar(
                x=df_chart.index,
                y=df_chart["Bull_Power"],
                marker_color=_bp_colors,
                marker_opacity=0.85,
                name="Bull Power",
                showlegend=True,
            ), row=2, col=1)

            _fig.add_trace(go.Bar(
                x=df_chart.index,
                y=df_chart["Bear_Power"],
                marker_color=_bep_colors,
                marker_opacity=0.55,
                name="Bear Power",
                showlegend=True,
            ), row=2, col=1)

            # Zero line
            _fig.add_hline(y=0, line_dash="solid", line_color="#6e7681",
                           line_width=1, row=2, col=1)

        _fig.update_layout(
            height=540,
            paper_bgcolor="#131722",
            plot_bgcolor="#131722",
            font=dict(color="#c9d1d9", size=11),
            margin=dict(l=8, r=8, t=30, b=8),
            showlegend=True,
            legend=dict(
                orientation="h", yanchor="bottom", y=1.01,
                xanchor="left", x=0,
                bgcolor="rgba(0,0,0,0)",
                font=dict(size=10),
            ),
            xaxis_rangeslider_visible=False,
            barmode="overlay",
        )
        _fig.update_xaxes(
            gridcolor="#1e222d", showgrid=True,
            tickfont=dict(size=9), zeroline=False,
        )
        _fig.update_yaxes(
            gridcolor="#1e222d", showgrid=True,
            tickfont=dict(size=9), zeroline=False,
        )

        st.plotly_chart(_fig, use_container_width=True, config={"displayModeBar": False})
    else:
        st.info(f"No {er_chart_tf} data available. Market may be closed or outside trading hours.")

    # ── Factor breakdown ──────────────────────────────────────────────────────
    st.markdown('<div style="font-size:0.63rem;font-weight:800;letter-spacing:1.2px;color:#6e7681;margin:8px 0 6px;">SIGNAL BREAKDOWN</div>', unsafe_allow_html=True)
    _factor_html = '<div class="er-card" style="padding:0;">'
    for _fname, _fdetail, _fdir, _fpts in factors:
        _dir_css = "er-factor-bull" if _fdir == "BULL" else ("er-factor-bear" if _fdir == "BEAR" else "er-factor-neut")
        _pts_str = f"+{_fpts}" if _fpts > 0 else str(_fpts)
        _pts_label = f"{_fdir} {_pts_str}" if _fpts > 0 else _fdir
        _factor_html += (
            f'<div class="er-factor-row">'
            f'  <span class="er-factor-name">{_fname}</span>'
            f'  <span class="er-factor-detail">{_fdetail}</span>'
            f'  <span class="{_dir_css}">{_pts_label}</span>'
            f'</div>'
        )
    _factor_html += '</div>'
    st.markdown(_factor_html, unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)

    # ── ATM option card (Kite) ────────────────────────────────────────────────
    col_opt_hdr, col_opt_btn = st.columns([3, 1])
    with col_opt_hdr:
        st.markdown(
            '<div style="font-size:0.63rem;font-weight:800;letter-spacing:1.2px;color:#6e7681;margin-bottom:6px;">ATM OPTIONS · LIVE QUOTES</div>',
            unsafe_allow_html=True,
        )
    with col_opt_btn:
        _fetch_opt = st.button("Fetch Kite Quotes", key="er_fetch_opt", disabled=not _kite_ok)

    if not _kite_ok:
        st.caption("Connect Zerodha Kite in the sidebar to fetch live option quotes.")
    else:
        _opt_cache_key = f"er_opt_{er_symbol}"
        if _fetch_opt or _opt_cache_key not in st.session_state:
            with st.spinner("Fetching ATM option quotes …"):
                _opt_data = fetch_atm_option(_kite_key, _kite_tok, er_symbol, price)
            st.session_state[_opt_cache_key] = _opt_data

        _opt_data = st.session_state.get(_opt_cache_key)

        if _opt_data:
            _exp_str = _opt_data.get("expiry_str", "")
            _pcr     = _opt_data.get("pcr", 0)
            _ce      = _opt_data.get("ce") or {}
            _pe      = _opt_data.get("pe") or {}
            _pcr_col = "#00d4aa" if _pcr > 1.2 else ("#f85149" if _pcr < 0.8 else "#e6b800")

            col_ce, col_pe, col_pcr = st.columns([2, 2, 1])
            with col_ce:
                _chg_col = "#00d4aa" if _ce.get("chg_pct", 0) >= 0 else "#f85149"
                st.markdown(
                    f'<div class="er-opt-card">'
                    f'  <div class="er-opt-label" style="color:#00d4aa;">ATM {atm} CE · {_exp_str}</div>'
                    f'  <div class="er-opt-ltp" style="color:#00d4aa;">₹{_ce.get("ltp",0):,.2f}</div>'
                    f'  <div class="er-opt-meta">'
                    f'    <span style="color:{_chg_col};">{_ce.get("chg_pct",0):+.2f}%</span> &nbsp;|&nbsp; '
                    f'    OI: {_ce.get("oi",0):,} &nbsp;|&nbsp; '
                    f'    IV: {_ce.get("iv",0):.1f}%'
                    f'  </div>'
                    f'  <div style="font-size:0.67rem;color:#6e7681;margin-top:4px;">{_ce.get("symbol","")}</div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )
            with col_pe:
                _chg_col_pe = "#00d4aa" if _pe.get("chg_pct", 0) >= 0 else "#f85149"
                st.markdown(
                    f'<div class="er-opt-card">'
                    f'  <div class="er-opt-label" style="color:#f85149;">ATM {atm} PE · {_exp_str}</div>'
                    f'  <div class="er-opt-ltp" style="color:#f85149;">₹{_pe.get("ltp",0):,.2f}</div>'
                    f'  <div class="er-opt-meta">'
                    f'    <span style="color:{_chg_col_pe};">{_pe.get("chg_pct",0):+.2f}%</span> &nbsp;|&nbsp; '
                    f'    OI: {_pe.get("oi",0):,} &nbsp;|&nbsp; '
                    f'    IV: {_pe.get("iv",0):.1f}%'
                    f'  </div>'
                    f'  <div style="font-size:0.67rem;color:#6e7681;margin-top:4px;">{_pe.get("symbol","")}</div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )
            with col_pcr:
                st.markdown(
                    f'<div class="er-opt-card">'
                    f'  <div class="er-opt-label" style="color:#6e7681;">PCR (ATM)</div>'
                    f'  <div class="er-opt-ltp" style="color:{_pcr_col};font-size:2rem;">{_pcr:.2f}</div>'
                    f'  <div class="er-opt-meta">'
                    f'    {"Bullish" if _pcr>1.2 else ("Bearish" if _pcr<0.8 else "Neutral")}'
                    f'  </div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )
        elif _opt_data is None:
            st.warning("Could not fetch option quotes. Check Kite API credentials or try later.")

    st.markdown("<br>", unsafe_allow_html=True)

    # ── Signal history ────────────────────────────────────────────────────────
    _hist_key  = f"er_hist_{er_symbol}"
    _hist_data = st.session_state.get(_hist_key, [])
    if _hist_data:
        st.markdown(
            '<div style="font-size:0.63rem;font-weight:800;letter-spacing:1.2px;color:#6e7681;margin-bottom:6px;">SIGNAL HISTORY (this session)</div>',
            unsafe_allow_html=True,
        )
        _hist_html = '<div class="er-card" style="padding:0;">'
        for _h in _hist_data:
            _hsig   = _h["signal"]
            _hcol   = "#00d4aa" if "CE" in _hsig else ("#f85149" if "PE" in _hsig else "#e6b800")
            _hist_html += (
                f'<div class="er-hist-row">'
                f'  <span style="color:#6e7681;min-width:70px;">{_h["time"]}</span>'
                f'  <span style="color:{_hcol};font-weight:700;min-width:140px;">{_hsig}</span>'
                f'  <span style="color:#8b949e;font-size:0.72rem;">CE {_h["ce"]} / PE {_h["pe"]}</span>'
                f'  <span style="color:#6e7681;margin-left:auto;font-size:0.72rem;">{_h["price"]:,.2f}</span>'
                f'</div>'
            )
        _hist_html += '</div>'
        st.markdown(_hist_html, unsafe_allow_html=True)


# ── Page routing (URL-driven via ?page=) ──────────────────────────────────────
{
    "smart_alerts_pro": page_smart_alerts_pro,
    "gamma_blast":      page_gamma_blast,
    "elder_ray":        page_elder_ray,
    "late_session":     page_late_session,
    "rpci":             page_rpci,
    "red_flag":         page_red_flag,
}.get(_cur_page, page_smart_alerts_pro)()
