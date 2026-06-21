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
_oc_loaded   = any(k in st.session_state for k in ("oc_NIFTY", "oc_BANKNIFTY", "oc_FINNIFTY", "oc_MIDCPNIFTY"))

def _sb_row(icon: str, label: str, count: int) -> str:
    badge = f'<span class="sb-badge">{count}</span>' if count > 0 else ""
    return f'<div class="sb-item"><span class="sb-lbl">{icon} {label}</span>{badge}</div>'

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
<div class="sb-div"></div>
<div class="sb-live"><span class="sb-dot"></span>NSE feed LIVE</div>
""", unsafe_allow_html=True)


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
tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs([
    "📰 News + Breakout",
    "🔁 20 MA Retracement",
    "📈 EMA Crossover",
    "🛡️ 50 MA Support",
    "🔗 Option Chain Insights",
    "📊 Fundamentals",
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
    st.markdown("#### 🔗 Option Chain Insights — Live NSE Data")

    oc1, oc2, oc3, oc4 = st.columns([2, 2, 2, 1])

    with oc1:
        oc_symbol = st.selectbox("Index", ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"], key="oc_symbol")

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
        with st.spinner(f"Fetching {oc_symbol} option chain from NSE…"):
            try:
                oc_raw = fetch_option_chain(oc_symbol)
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

            st.divider()

            # ── OI bar table ──────────────────────────────────────────────────
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
                    fig_oi.add_vline(x=atm_x, line_dash="dash", line_color="#00d4aa",
                                     annotation_text="ATM", annotation_font_color="#00d4aa",
                                     annotation_position="top")
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
                    fig_chng.add_vline(x=atm_x, line_dash="dash", line_color="#00d4aa",
                                       annotation_text="ATM", annotation_font_color="#00d4aa",
                                       annotation_position="top")
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
            "Market Cap >₹1000Cr &nbsp;·&nbsp; ROCE >15% &nbsp;·&nbsp; ROE >15% &nbsp;·&nbsp; D/E <0.5 &nbsp;·&nbsp; "
            "OPM >12% &nbsp;·&nbsp; Sales & Profit 3Y growth >8% &nbsp;·&nbsp; Piotroski ≥6 &nbsp;·&nbsp; "
            "Promoter ≥0.1% &nbsp;·&nbsp; Pledged <10% &nbsp;·&nbsp; DII+FII >5% &nbsp;·&nbsp; "
            "Down from 52W High >25% &nbsp;·&nbsp; PE &lt; Industry PE"
            "</small>",
            unsafe_allow_html=True,
        )
        run_fund_btn = gc2.button("🔍 Run", type="primary", use_container_width=True, key="run_fund")

    if run_fund_btn:
        with st.spinner("Fetching fundamental data from Screener.in…"):
            try:
                fund_results = fetch_fundamental_stocks()
                st.session_state["fund_results"] = fund_results
            except Exception as _fe:
                st.error(str(_fe))

    if "fund_results" in st.session_state:
        fund_df: pd.DataFrame = st.session_state["fund_results"]
        if fund_df.empty:
            st.warning("No stocks matched all criteria today. Try again after market hours when data refreshes.")
        else:
            # Detect name column (Screener.in calls it "Name")
            _name_col = next((c for c in ["Name", "Company", "Company Name"] if c in fund_df.columns), fund_df.columns[0])

            fm1, fm2, fm3 = st.columns(3)
            fm1.metric("Stocks Found", len(fund_df))
            fm2.metric("Top Pick", str(fund_df[_name_col].iloc[0]) if len(fund_df) > 0 else "—")
            if "ROCE %" in fund_df.columns:
                fm3.metric("Avg ROCE %", f"{pd.to_numeric(fund_df['ROCE %'], errors='coerce').mean():.1f}%")
            elif "Mar Cap ₹ Cr." in fund_df.columns:
                fm3.metric("Largest Cap", f"₹{pd.to_numeric(fund_df['Mar Cap ₹ Cr.'].iloc[0], errors='coerce'):,.0f} Cr.")

            st.caption("👆 Click any row to open Daily chart · Data: Screener.in · Esc to close")

            display_cols = [c for c in fund_df.columns if c != "NSE_Symbol"]
            fund_sel = st.dataframe(
                fund_df[display_cols],
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
                fr    = fund_df.iloc[fund_rows[0]]
                nse   = str(fr.get("NSE_Symbol", ""))
                cname = str(fr.get(_name_col, nse))
                if nse:
                    chart_modal(nse, cname, "1D")
                else:
                    st.warning("NSE symbol not available for this stock — cannot open chart.")
    else:
        st.info("🔎 Click **Run** above to screen fundamentally strong NIFTY stocks via Screener.in.")
