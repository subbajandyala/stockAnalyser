import time
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
from screener.option_chain import (
    fetch_option_chain, get_expiries, parse_chain,
    atm_strike, calc_pcr, calc_max_pain,
)
from screener.fundamental import fetch_fundamental_stocks
from screener.fo_scanner import run_fo_scan
from screener.sensex_option_moves import (
    run_sensex_option_moves_scan,
    get_sensex_expiry_fridays,
    run_preexpiry_analysis,
    analyze_expiry_day_patterns,
)


st.set_page_config(page_title="MarketPulse", layout="wide", page_icon="🐂")

# ── Global CSS ────────────────────────────────────────────────────────────────
st.markdown("""
<style>
.stApp { background: #0a0e1a; }
.main .block-container { padding-top: 1rem; }
[data-testid="stSidebar"] { background: #0d1220 !important; border-right: 1px solid #1a2035 !important; }

/* Ticker */
.ticker-wrapper { background: #0d1220; border: 1px solid #1a2035; border-radius: 8px; overflow: hidden; white-space: nowrap; padding: 8px 0; margin-bottom: 18px; }
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
[data-testid="metric-container"] { background: #0f1523 !important; border: 1px solid #1a2035 !important; border-radius: 10px !important; padding: 12px 16px !important; }
[data-testid="stMetricLabel"] { color: #8b949e !important; font-size: 0.78rem !important; font-weight: 600 !important; text-transform: uppercase; }
[data-testid="stMetricValue"] { color: #e6edf3 !important; font-size: 1.4rem !important; font-weight: 700 !important; }
[data-testid="stMetricDelta"] { font-size: 0.85rem !important; }

/* Tabs */
[data-testid="stTabs"] [role="tablist"] { background: #0a0e1a; border-radius: 0; padding: 0; gap: 6px; border-bottom: 1px solid #1a2035; }
[data-testid="stTabs"] button[role="tab"] { border-radius: 6px 6px 0 0 !important; font-size: 0.75rem !important; font-weight: 600 !important; color: #6e7681 !important; padding: 5px 14px !important; border: 1px solid transparent !important; border-bottom: none !important; background: #0f1523 !important; letter-spacing: 0.3px !important; text-transform: uppercase !important; transition: all 0.15s !important; }
[data-testid="stTabs"] button[role="tab"]:hover { color: #c9d1d9 !important; background: #141928 !important; border-color: #1a2035 !important; }
[data-testid="stTabs"] button[role="tab"][aria-selected="true"] { background: #141928 !important; color: #00d4aa !important; border-color: #1a2035 !important; border-bottom: 2px solid #00d4aa !important; font-weight: 700 !important; }

/* Buttons */
button[kind="primary"] { background: linear-gradient(135deg, #00b894, #00d4aa) !important; border: none !important; border-radius: 8px !important; color: #0a0e1a !important; font-weight: 700 !important; box-shadow: 0 2px 8px rgba(0,212,170,0.3) !important; transition: all 0.2s !important; }
button[kind="primary"]:hover { box-shadow: 0 4px 14px rgba(0,212,170,0.5) !important; transform: translateY(-1px) !important; }

/* Misc */
[data-testid="stAlert"] { border-radius: 8px !important; }
[data-testid="stDialog"] > div { max-width: 98vw !important; width: 98vw !important; }
[data-testid="stDialog"] section { max-width: 98vw !important; }
[data-testid="stDataFrame"] { border-radius: 8px; overflow: hidden; }
hr { border-color: #1a2035 !important; margin: 10px 0 !important; }
.stCaption { color: #8b949e !important; }

/* Sidebar nav */
.sb-brand { padding: 20px 16px 14px; border-bottom: 1px solid #1a2035; }
.sb-brand-name { font-size: 1.3rem; font-weight: 900; color: #fff; letter-spacing: -0.5px; }
.sb-brand-sub { font-size: 0.67rem; color: #4a5568; margin-top: 2px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.8px; }
.sb-sec { font-size: 0.61rem; font-weight: 700; text-transform: uppercase; letter-spacing: 1.8px; color: #3d4a5c; padding: 14px 16px 5px; }
.sb-item { display: flex; align-items: center; justify-content: space-between; padding: 7px 16px; border-radius: 7px; margin: 1px 6px; }
.sb-item:hover { background: rgba(0,212,170,0.06); }
.sb-lbl { color: #adbac7; font-size: 0.8rem; }
.sb-badge { background: rgba(0,212,170,0.12); border: 1px solid rgba(0,212,170,0.25); color: #00d4aa; font-size: 0.61rem; font-weight: 700; padding: 1px 6px; border-radius: 10px; }
.sb-div { height: 1px; background: #1a2035; margin: 14px 0; }
.sb-live { display: flex; align-items: center; gap: 8px; padding: 8px 16px; font-size: 0.72rem; font-weight: 600; color: #00d4aa; }
.sb-dot { width: 7px; height: 7px; background: #00d4aa; border-radius: 50%; display: inline-block; animation: sb-pulse 1.5s ease-in-out infinite; }
@keyframes sb-pulse { 0%,100% { opacity:1; transform:scale(1); } 50% { opacity:0.3; transform:scale(0.6); } }

/* OI table */
.oc-wrap { width:100%; overflow-x:auto; margin:4px 0 16px; }
.oc-tbl { width:100%; border-collapse:collapse; font-size:0.78rem; }
.oc-tbl th { background:#0f1523; color:#6e7681; font-weight:700; text-transform:uppercase; font-size:0.64rem; letter-spacing:0.6px; padding:8px 10px; border-bottom:1px solid #1a2035; }
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
        yaxis=dict(title="Price (₹)", side="left", showgrid=True, gridcolor="#1a2035", gridwidth=1,
                   tickfont=dict(color="#8b949e"), title_font=dict(color="#8b949e")),
        yaxis2=dict(overlaying="y", side="right", showgrid=False, title="Volume",
                    title_font=dict(color="#8b949e"), tickfont=dict(color="#8b949e")),
        legend=dict(orientation="h", yanchor="bottom", y=1.01, x=0,
                    font=dict(color="#c9d1d9"), bgcolor="rgba(0,0,0,0)"),
        xaxis_rangeslider_visible=False,
        height=560,
        margin=dict(t=10, b=55, l=10, r=110),
        template="plotly_dark",
        plot_bgcolor="#0a0e1a",
        paper_bgcolor="#0a0e1a",
    )
    st.plotly_chart(fig, use_container_width=True)

    if extra_levels:
        with st.expander("📐 CPR / Support Levels", expanded=False):
            for label, price in extra_levels.items():
                st.markdown(f"**{label}:** ₹{price:,.2f}")


# ── Sidebar ───────────────────────────────────────────────────────────────────
_news_count  = len(st.session_state.get("news_results",  pd.DataFrame()))
_ma_count    = len(st.session_state.get("ma_results",    pd.DataFrame()))
_cross_count = len(st.session_state.get("cross_results", pd.DataFrame()))
_ma50_count  = len(st.session_state.get("ma50_results",  pd.DataFrame()))
_fund_count  = len(st.session_state.get("fund_results",  pd.DataFrame()))
_fo_count    = len(st.session_state.get("fo_results",    pd.DataFrame()))
_oc_loaded   = any(k in st.session_state for k in ("oc_NIFTY", "oc_BANKNIFTY", "oc_FINNIFTY", "oc_MIDCPNIFTY", "oc_SENSEX", "oc_BANKEX"))
_em_count    = len(st.session_state.get("em_summary",    pd.DataFrame()))

def _sb_row(icon: str, label: str, count: int) -> str:
    badge = f'<span class="sb-badge">{count}</span>' if count > 0 else ""
    return f'<div class="sb-item"><span class="sb-lbl">{icon} {label}</span>{badge}</div>'

def _get_secret(key: str, default: str = "") -> str:
    try:
        return st.secrets.get(key, default)
    except Exception:
        return default

with st.sidebar:
    st.markdown(f"""
<div class="sb-brand">
  <div class="sb-brand-name">🐂 MarketPulse</div>
  <div class="sb-brand-sub">NSE India · NIFTY 500</div>
</div>
<div class="sb-sec">WORKSPACE</div>
{_sb_row("📰", "News + Breakout", _news_count)}
{_sb_row("🔁", "20 MA Retracement", _ma_count)}
<div class="sb-sec">SCANNERS</div>
{_sb_row("📈", "EMA Crossover", _cross_count)}
{_sb_row("🛡️", "50 MA Support", _ma50_count)}
<div class="sb-sec">ANALYSIS</div>
{_sb_row("📊", "Fundamentals", _fund_count)}
<div class="sb-sec">TOOLS</div>
{_sb_row("🔗", "Option Chain", 1 if _oc_loaded else 0)}
{_sb_row("🎯", "F&O Scanner", _fo_count)}
{_sb_row("🚀", "Sensex Expiry Moves", _em_count)}
<div class="sb-div"></div>
<div class="sb-live"><span class="sb-dot"></span>NSE feed LIVE</div>
""", unsafe_allow_html=True)

    with st.expander("⚡ Zerodha Kite Connect", expanded=False):
        _sidebar_api_key = st.text_input(
            "API Key", type="password", key="kite_api_key",
            value=_get_secret("KITE_API_KEY", ""),
            placeholder="From kite.zerodha.com/apps",
        )
        st.text_input(
            "Access Token", type="password", key="kite_access_token",
            value=_get_secret("KITE_ACCESS_TOKEN", ""),
            placeholder="Daily token — refresh each morning",
        )
        _kite_ok = bool(
            st.session_state.get("kite_api_key", "")
            and st.session_state.get("kite_access_token", "")
        )
        if _kite_ok:
            st.success("✅ Kite connected — live OI data active")
            if st.button("🔬 Test Connection", key="kite_test", use_container_width=True):
                import requests as _tr
                _hdr = {"X-Kite-Version": "3",
                        "Authorization": f"token {st.session_state['kite_api_key']}:{st.session_state['kite_access_token']}"}
                try:
                    _p = _tr.get("https://api.kite.trade/user/profile", headers=_hdr, timeout=10)
                    if _p.ok:
                        _name = _p.json().get("data", {}).get("user_name", "?")
                        st.success(f"✅ Token valid — logged in as {_name}")
                    else:
                        st.error(f"❌ Token rejected: {_p.status_code} — generate a new token")
                except Exception as _te:
                    st.error(f"Network error: {_te}")

                try:
                    _i = _tr.get("https://api.kite.trade/instruments/NFO", headers=_hdr, timeout=30)
                    if _i.ok and "instrument_token" in _i.text[:200]:
                        _rows = len(_i.text.strip().splitlines()) - 1
                        st.info(f"📋 NFO instruments: {_rows:,} rows downloaded")
                    else:
                        st.error(f"❌ NFO instruments failed: status {_i.status_code} — first 200 chars: {_i.text[:200]}")
                except Exception as _ie:
                    st.error(f"Instruments error: {_ie}")
        else:
            _sidebar_key_for_link = st.session_state.get("kite_api_key", "") or _get_secret("KITE_API_KEY", "plz6ik09bgb62mey")
            st.info("Enter API Key + Access Token to enable real-time OI")
            if _sidebar_key_for_link:
                st.caption(
                    f"🔑 [Generate today's token](https://kite.zerodha.com/connect/login?api_key={_sidebar_key_for_link}&v=3) "
                    "→ login → copy `request_token` from URL → run exchange script"
                )


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


# ── Hero section ──────────────────────────────────────────────────────────────
st.markdown("""
<div class="hero-h1">Real-time technical pulse,<br><span class="tl">screened by the second.</span></div>
<div class="hero-sub">NIFTY 500 &nbsp;·&nbsp; Live technical screener &nbsp;·&nbsp; NSE India</div>
<div><span class="hero-badge">● LIVE · NSE INDIA</span></div>
<div style="margin-top:20px;"></div>
""", unsafe_allow_html=True)

# ── Timeframe bar ─────────────────────────────────────────────────────────────
tc1, tc2 = st.columns([1, 4])
tc1.markdown('<p style="color:#6e7681;font-size:.82rem;font-weight:600;text-transform:uppercase;letter-spacing:.5px;margin:8px 0 0;">⏱ Timeframe</p>', unsafe_allow_html=True)
with tc2:
    tf_key = st.segmented_control(
        label="Timeframe", options=list(TF_CONFIG.keys()),
        format_func=lambda k: TF_CONFIG[k]["label"],
        default="1D", key="global_tf", label_visibility="collapsed",
    )

tf       = TF_CONFIG[tf_key]
interval = tf["interval"]
period   = tf["screener_period"]

mkt_note = " &nbsp;·&nbsp; Charts: 9:15 AM – 3:30 PM IST only" if tf["market_hours"] else ""
st.markdown(
    f'<p style="color:#6e7681;font-size:.82rem;margin:2px 0 12px;">'
    f'📌 Screener on <b style="color:#adbac7">{tf["label"]}</b> candles &nbsp;·&nbsp; '
    f'Period: <b style="color:#adbac7">{tf["display_period"]}</b>{mkt_note}</p>',
    unsafe_allow_html=True,
)

st.divider()

# ── Tabs ──────────────────────────────────────────────────────────────────────
tab1, tab2, tab3, tab4, tab5, tab6, tab7, tab8 = st.tabs([
    "📰 News + Breakout",
    "🔁 20 MA Retracement",
    "📈 EMA Crossover",
    "🛡️ 50 MA Support",
    "🔗 Option Chain Insights",
    "📊 Fundamentals",
    "🎯 F&O Scanner",
    "🚀 Sensex Expiry Moves",
])

# ── TAB 1 ─────────────────────────────────────────────────────────────────────
with tab1:
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


# ── TAB 2 ─────────────────────────────────────────────────────────────────────
with tab2:
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
            ma_results = run_ma_retracement_scan(symbols_df, touch_pct=touch_pct/100,
                                                  interval=interval, period=period)
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


# ── TAB 3 ─────────────────────────────────────────────────────────────────────
with tab3:
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
            cross_results = run_crossover_scan(symbols_df)
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


# ── TAB 4 ─────────────────────────────────────────────────────────────────────
with tab4:
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
            ma50_results = run_ma50_support_scan(symbols_df, touch_pct=touch_pct_50/100,
                                                  interval=interval, period=period)
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


with tab5:
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
                    plot_bgcolor="#0a0e1a", paper_bgcolor="#0a0e1a",
                    height=340, margin=dict(t=10, b=50, l=10, r=10),
                    legend=dict(orientation="h", yanchor="bottom", y=1.01, x=0,
                                font=dict(color="#c9d1d9"), bgcolor="rgba(0,0,0,0)"),
                    xaxis=dict(tickfont=dict(color="#8b949e", size=9), showgrid=False, tickangle=-45),
                    yaxis=dict(tickfont=dict(color="#8b949e"), gridcolor="#1a2035"),
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
                    plot_bgcolor="#0a0e1a", paper_bgcolor="#0a0e1a",
                    height=340, margin=dict(t=10, b=50, l=10, r=10),
                    legend=dict(orientation="h", yanchor="bottom", y=1.01, x=0,
                                font=dict(color="#c9d1d9"), bgcolor="rgba(0,0,0,0)"),
                    xaxis=dict(tickfont=dict(color="#8b949e", size=9), showgrid=False, tickangle=-45),
                    yaxis=dict(tickfont=dict(color="#8b949e"), gridcolor="#1a2035"),
                )
                st.plotly_chart(fig_chng, use_container_width=True)


# ── TAB 6 — Fundamental Analysis ──────────────────────────────────────────────
with tab6:
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
            fund_results = fetch_fundamental_stocks(
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


# ── TAB 7 — F&O Scanner ───────────────────────────────────────────────────────
with tab7:
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


# ── TAB 8 — Sensex Expiry Moves ───────────────────────────────────────────────
with tab8:
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
                template="plotly_dark", plot_bgcolor="#0a0e1a",
                paper_bgcolor="#0a0e1a", height=380,
                margin=dict(t=20, b=50, l=10, r=10),
                xaxis=dict(tickfont=dict(color="#8b949e", size=9),
                           showgrid=False, tickangle=-45,
                           tickvals=["09:15","10:15","11:15","12:15",
                                     "13:15","14:15","15:15","15:30"]),
                yaxis=dict(tickfont=dict(color="#8b949e"),
                           gridcolor="#1a2035", title="Move from Open (%)",
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
                        template="plotly_dark", plot_bgcolor="#0a0e1a",
                        paper_bgcolor="#0a0e1a", height=300,
                        margin=dict(t=10, b=60, l=10, r=10),
                        xaxis=dict(tickfont=dict(color="#8b949e", size=9),
                                   showgrid=False, tickangle=-30),
                        yaxis=dict(tickfont=dict(color="#8b949e"),
                                   gridcolor="#1a2035"),
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
                        template="plotly_dark", plot_bgcolor="#0a0e1a",
                        paper_bgcolor="#0a0e1a", height=300,
                        margin=dict(t=10, b=60, l=10, r=10),
                        xaxis=dict(tickfont=dict(color="#8b949e", size=9),
                                   showgrid=False, tickangle=-30),
                        yaxis=dict(tickfont=dict(color="#8b949e"),
                                   gridcolor="#1a2035"),
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
