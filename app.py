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

st.set_page_config(page_title="NIFTY 500 Screener", layout="wide", page_icon="📈")

# ── Global CSS ────────────────────────────────────────────────────────────────
st.markdown("""
<style>
/* ── Hide sidebar ── */
[data-testid="stSidebar"] { display: none; }
[data-testid="collapsedControl"] { display: none; }

/* ── Page background ── */
.stApp { background: #0d1117; }

/* ── Timeframe pill selector ── */
.tf-bar {
    background: #161b22;
    border: 1px solid #21262d;
    border-radius: 10px;
    padding: 12px 20px;
    margin-bottom: 8px;
    display: flex;
    align-items: center;
    gap: 14px;
}
.tf-label { color: #8b949e; font-size: 0.82rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; white-space: nowrap; }

/* ── Tab content filter card ── */
.filter-card {
    background: #161b22;
    border: 1px solid #21262d;
    border-radius: 10px;
    padding: 16px 20px;
    margin-bottom: 18px;
}

/* ── Metric cards ── */
[data-testid="metric-container"] {
    background: #161b22 !important;
    border: 1px solid #21262d !important;
    border-radius: 10px !important;
    padding: 12px 16px !important;
}
[data-testid="stMetricLabel"] { color: #8b949e !important; font-size: 0.78rem !important; font-weight: 600 !important; text-transform: uppercase; }
[data-testid="stMetricValue"] { color: #e6edf3 !important; font-size: 1.4rem !important; font-weight: 700 !important; }
[data-testid="stMetricDelta"] { font-size: 0.85rem !important; }

/* ── Tab strip ── */
[data-testid="stTabs"] [role="tablist"] {
    background: #0d1117;
    border-radius: 0;
    padding: 0;
    gap: 6px;
    border-bottom: 1px solid #21262d;
}
[data-testid="stTabs"] button[role="tab"] {
    border-radius: 6px 6px 0 0 !important;
    font-size: 0.75rem !important;
    font-weight: 600 !important;
    color: #6e7681 !important;
    padding: 5px 14px !important;
    border: 1px solid transparent !important;
    border-bottom: none !important;
    background: #161b22 !important;
    letter-spacing: 0.3px !important;
    text-transform: uppercase !important;
    transition: all 0.15s !important;
}
[data-testid="stTabs"] button[role="tab"]:hover {
    color: #c9d1d9 !important;
    background: #1c2128 !important;
    border-color: #30363d !important;
}
[data-testid="stTabs"] button[role="tab"][aria-selected="true"] {
    background: #21262d !important;
    color: #58a6ff !important;
    border-color: #30363d !important;
    border-bottom: 2px solid #58a6ff !important;
    font-weight: 700 !important;
}

/* ── Run button ── */
button[kind="primary"] {
    background: linear-gradient(135deg, #1f6feb, #388bfd) !important;
    border: none !important;
    border-radius: 8px !important;
    font-weight: 700 !important;
    letter-spacing: 0.2px !important;
    box-shadow: 0 2px 8px rgba(56,139,253,0.35) !important;
    transition: all 0.2s !important;
}
button[kind="primary"]:hover {
    box-shadow: 0 4px 14px rgba(56,139,253,0.5) !important;
    transform: translateY(-1px) !important;
}

/* ── Info / warning ── */
[data-testid="stAlert"] { border-radius: 8px !important; }

/* ── Dialog / modal — full width ── */
[data-testid="stDialog"] > div { max-width: 98vw !important; width: 98vw !important; }
[data-testid="stDialog"] section { max-width: 98vw !important; }

/* ── Dataframe ── */
[data-testid="stDataFrame"] { border-radius: 8px; overflow: hidden; }

/* ── Divider ── */
hr { border-color: #21262d !important; margin: 10px 0 !important; }

/* ── Caption ── */
.stCaption { color: #8b949e !important; }
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

# ── Signal colours & column lists ─────────────────────────────────────────────
SIGNAL_COLORS = {"STRONG BUY": "#00C853", "BUY": "#64DD17", "WATCH": "#FFD600", "SKIP": "#FF5252"}
MA_SIGNAL_COLORS  = {"STRONG (20MA + CPR)": "#00C853", "GOOD (20MA + CPR)": "#64DD17",
                     "GOOD (20MA + Vol)": "#FFD600", "20MA Bounce": "#80CBC4"}
CROSS_SIGNAL_COLORS = {"STRONG BUY": "#00C853", "BUY": "#64DD17", "WATCH": "#FFD600"}
MA50_SIGNAL_COLORS  = {"STRONG (50MA + Monthly CPR)": "#00C853",
                       "BUY (50MA + Monthly CPR)": "#64DD17", "WATCH": "#FFD600"}

NEWS_COLS  = ["Symbol","Company","News Mentions","Price","Change Last%","Change Prev 5%","RSI","% from High","Vol Ratio","Signal","Top Headline"]
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

# ── Market-hours filter ───────────────────────────────────────────────────────
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

# ── Chart modal ───────────────────────────────────────────────────────────────
@st.dialog("📊 Chart", width="large")
def chart_modal(nse_symbol: str, company: str, tf_key: str, extra_levels: dict | None = None):
    cfg           = TF_CONFIG[tf_key]
    interval      = cfg["interval"]
    chart_per     = cfg["chart_period"]
    use_mkt_hours = cfg["market_hours"]

    # Header row
    hcol1, hcol2 = st.columns([3, 1])
    hcol1.markdown(f"**{company}** &nbsp; `{nse_symbol}` &nbsp; · &nbsp; `{TF_CONFIG[tf_key]['label']}`")
    sym = nse_symbol.replace(".NS", "")
    hcol2.link_button("🔗 TradingView", f"https://www.tradingview.com/chart/?symbol=NSE:{sym}",
                      use_container_width=True)

    with st.spinner("Loading chart data..."):
        df = yf.download(nse_symbol, period=chart_per, interval=interval,
                         progress=False, auto_adjust=True)

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
    chg_color = "#26a641" if chg >= 0 else "#f85149"
    chg_arrow = "▲" if chg >= 0 else "▼"

    # Metrics strip
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Price",   f"₹{latest:,.2f}", f"{chg_arrow} {abs(chg):.2f}%",
              delta_color="normal" if chg >= 0 else "inverse")
    m2.metric("EMA 20",  f"₹{float(ema20.iloc[-1]):,.2f}")
    m3.metric("EMA 50",  f"₹{float(ema50.iloc[-1]):,.2f}")
    m4.metric("Period High", f"₹{float(close.max()):,.2f}")
    m5.metric("Period Low",  f"₹{float(close.min()):,.2f}")

    # X-axis labels (category to remove gaps)
    if use_mkt_hours or cfg["interval"] not in ("1d", "1wk"):
        x_labels = df.index.strftime("%d %b %H:%M")
    else:
        x_labels = df.index.strftime("%d %b '%y")

    # Volume colours: green if bullish candle, red if bearish
    vol_colors = [
        "#26a641" if float(c) >= float(o) else "#f85149"
        for c, o in zip(close, open_)
    ]

    fig = go.Figure()

    # Candlesticks
    fig.add_trace(go.Candlestick(
        x=x_labels,
        open=open_, high=high, low=low, close=close,
        name="Price",
        increasing_line_color="#26a641", increasing_fillcolor="#26a641",
        decreasing_line_color="#f85149", decreasing_fillcolor="#f85149",
    ))

    # EMA lines
    fig.add_trace(go.Scatter(x=x_labels, y=ema20, name="EMA 20",
                             line=dict(color="#f0b429", width=1.6)))
    fig.add_trace(go.Scatter(x=x_labels, y=ema50, name="EMA 50",
                             line=dict(color="#e06c75", width=1.6)))

    # CPR / extra levels
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

    # Volume bars (green/red)
    fig.add_trace(go.Bar(
        x=x_labels, y=volume, name="Volume",
        marker_color=vol_colors,
        marker_opacity=0.7,
        yaxis="y2",
    ))

    # Ticks
    total_bars  = len(x_labels)
    tick_every  = max(1, total_bars // 18)
    vis_ticks   = x_labels[::tick_every].tolist()

    fig.update_layout(
        xaxis=dict(type="category", tickvals=vis_ticks, ticktext=vis_ticks,
                   tickangle=-40, tickfont=dict(size=10, color="#8b949e"),
                   showgrid=False, zeroline=False),
        yaxis=dict(title="Price (₹)", side="left", showgrid=True,
                   gridcolor="#21262d", gridwidth=1,
                   tickfont=dict(color="#8b949e"), title_font=dict(color="#8b949e")),
        yaxis2=dict(overlaying="y", side="right", showgrid=False,
                    title="Volume", title_font=dict(color="#8b949e"),
                    tickfont=dict(color="#8b949e")),
        legend=dict(orientation="h", yanchor="bottom", y=1.01, x=0,
                    font=dict(color="#c9d1d9"), bgcolor="rgba(0,0,0,0)"),
        xaxis_rangeslider_visible=False,
        height=560,
        margin=dict(t=10, b=55, l=10, r=110),
        template="plotly_dark",
        plot_bgcolor="#0d1117",
        paper_bgcolor="#0d1117",
    )

    st.plotly_chart(fig, use_container_width=True)

    if extra_levels:
        with st.expander("📐 CPR / Support Levels", expanded=False):
            for label, price in extra_levels.items():
                st.markdown(f"**{label}:** ₹{price:,.2f}")


# ════════════════════════════════════════════════════════════════════════════
# PAGE HEADER
# ════════════════════════════════════════════════════════════════════════════
# Timeframe bar
tc1, tc2 = st.columns([1, 4])
tc1.markdown('<p style="color:#8b949e;font-size:.82rem;font-weight:600;text-transform:uppercase;letter-spacing:.5px;margin:8px 0 0;">⏱ Timeframe</p>', unsafe_allow_html=True)
with tc2:
    tf_key = st.segmented_control(
        label="Timeframe", options=list(TF_CONFIG.keys()),
        format_func=lambda k: TF_CONFIG[k]["label"],
        default="1D", key="global_tf", label_visibility="collapsed",
    )

tf       = TF_CONFIG[tf_key]
interval = tf["interval"]
period   = tf["screener_period"]

mkt_note = " &nbsp;·&nbsp; Charts: **9:15 AM – 3:30 PM IST only**" if tf["market_hours"] else ""
st.markdown(
    f'<p style="color:#8b949e;font-size:.82rem;margin:2px 0 12px;">'
    f'📌 Screener on <b style="color:#c9d1d9">{tf["label"]}</b> candles &nbsp;·&nbsp; '
    f'Period: <b style="color:#c9d1d9">{tf["display_period"]}</b>{mkt_note}</p>',
    unsafe_allow_html=True,
)

st.divider()

# ════════════════════════════════════════════════════════════════════════════
# TABS
# ════════════════════════════════════════════════════════════════════════════
tab1, tab2, tab3, tab4 = st.tabs([
    "📰 News + Breakout",
    "🔁 20 MA Retracement",
    "📈 EMA Crossover",
    "🛡️ 50 MA Support",
])

# ── TAB 1 ────────────────────────────────────────────────────────────────────
with tab1:
    st.markdown("#### 📰 In-the-news stocks in uptrend — ready for breakout")

    with st.container(border=True):
        fc1, fc2, fc3, fc4, fc5 = st.columns([2, 2, 2, 1, 2])
        news_days    = fc1.slider("News lookback (days)", 7, 14, 10, key="nd")
        min_mentions = fc2.slider("Min mentions", 1, 10, 2, key="mm")
        breakout_pct = fc3.slider("Max % below High", 1, 20, 5, key="bp")
        show_all     = fc4.checkbox("Show SKIP", value=False, key="sa")
        run_btn      = fc5.button("🔍 Run News Screener", type="primary",
                                  use_container_width=True, key="run_news")

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
            sig_ord = {"STRONG BUY":0,"BUY":1,"WATCH":2,"SKIP":3}
            full_df = (full_df.assign(_r=full_df["Signal"].map(sig_ord))
                       .sort_values(["_r","News Mentions"],ascending=[True,False])
                       .drop(columns=["_r"]))
            filtered = full_df[full_df["News Mentions"] >= min_mentions]
            if not show_all:
                filtered = filtered[filtered["Signal"] != "SKIP"]

            mc1,mc2,mc3,mc4 = st.columns(4)
            mc1.metric("Total Screened",  len(full_df))
            mc2.metric("Strong Buy 🟢",   len(full_df[full_df["Signal"]=="STRONG BUY"]))
            mc3.metric("Buy 🟡",          len(full_df[full_df["Signal"]=="BUY"]))
            mc4.metric("Watch 👀",        len(full_df[full_df["Signal"]=="WATCH"]))
            st.caption("👆 Click any row to open chart · Esc to close")

            show_cols = [c for c in NEWS_COLS if c in filtered.columns]
            styled = (filtered[show_cols].copy().style
                      .map(_style(SIGNAL_COLORS), subset=["Signal"])
                      .map(change_style, subset=[c for c in ["Change Last%","Change Prev 5%"] if c in filtered.columns])
                      .format({c:"₹{:.2f}" for c in ["Price"] if c in filtered.columns})
                      .format({c:"{:+.2f}%" for c in ["Change Last%","Change Prev 5%","% from High"] if c in filtered.columns})
                      .format({c:"{:.1f}" for c in ["RSI"] if c in filtered.columns})
                      .format({c:"{:.2f}x" for c in ["Vol Ratio"] if c in filtered.columns}))
            sel = st.dataframe(styled, use_container_width=True, height=500,
                               on_select="rerun", selection_mode="single-row", key="news_table")
            csv = filtered.to_csv(index=False).encode("utf-8")
            st.download_button("⬇️ Export CSV", csv, "news_screener.csv", "text/csv")
            rows_sel = sel.selection.get("rows",[]) if sel else []
            if rows_sel:
                row = filtered.iloc[rows_sel[0]]
                chart_modal(row.get("NSE_Symbol", row["Symbol"]+".NS"), row["Company"], cached_tf)
    else:
        st.info("🔎 Configure filters above and click **Run News Screener** to begin.")


# ── TAB 2 ────────────────────────────────────────────────────────────────────
with tab2:
    st.markdown("#### 🔁 Stocks retesting 20 EMA in uptrend with bullish continuation")

    with st.container(border=True):
        rc1, rc2, rc3 = st.columns([2, 4, 2])
        touch_pct  = rc1.slider("Touch tolerance (%)", 1, 3, 1, key="tp")
        rc2.markdown(
            '<small style="color:#8b949e;">Price > EMA20 > EMA50 · Prev candle low touched EMA20 · '
            'Closed above EMA20 · Today bullish · Bonus: CPR confluence</small>',
            unsafe_allow_html=True)
        run_ma_btn = rc3.button("🔍 Run MA Screener", type="primary",
                                use_container_width=True, key="run_ma")

    if run_ma_btn:
        with st.spinner(f"Scanning NIFTY 500 on {tf['label']} candles… (3–5 min)"):
            symbols_df = get_nifty500_symbols()
            ma_results = run_ma_retracement_scan(symbols_df, touch_pct=touch_pct/100,
                                                  interval=interval, period=period)
            st.session_state["ma_results"] = ma_results
            st.session_state["ma_tf"] = tf_key

    if "ma_results" in st.session_state:
        ma_df: pd.DataFrame = st.session_state["ma_results"]
        cached_ma_tf = st.session_state.get("ma_tf","1D")
        if cached_ma_tf != tf_key:
            st.warning(f"⚠️ Results from **{TF_CONFIG[cached_ma_tf]['label']}**. Re-run for **{TF_CONFIG[tf_key]['label']}**.")
        if ma_df.empty:
            st.warning("No stocks found. Try increasing touch tolerance or switching timeframe.")
        else:
            mc1,mc2,mc3 = st.columns(3)
            mc1.metric("Stocks Found",     len(ma_df))
            mc2.metric("With CPR Support", len(ma_df[ma_df["CPR Support"]=="✅ Yes"]))
            mc3.metric("Vol Surge >1.2x",  len(ma_df[ma_df["Vol Ratio"]>1.2]))
            st.caption("👆 Click any row to open chart · Esc to close")

            show_cols = [c for c in MA_COLS if c in ma_df.columns]
            styled_ma = (ma_df[show_cols].copy().style
                         .map(_style(MA_SIGNAL_COLORS), subset=["Signal"])
                         .map(change_style, subset=[c for c in ["Change%"] if c in ma_df.columns])
                         .format({c:"₹{:.2f}" for c in ["Price","EMA20","CPR BC","CPR Pivot","CPR TC"] if c in ma_df.columns})
                         .format({c:"{:+.2f}%" for c in ["Change%","% Above EMA20","Touch%"] if c in ma_df.columns})
                         .format({c:"{:.2f}x" for c in ["Vol Ratio"] if c in ma_df.columns}))
            ma_sel = st.dataframe(styled_ma, use_container_width=True, height=500,
                                  on_select="rerun", selection_mode="single-row", key="ma_table")
            csv_ma = ma_df.to_csv(index=False).encode("utf-8")
            st.download_button("⬇️ Export CSV", csv_ma, "ma_retracement.csv", "text/csv")
            ma_rows = ma_sel.selection.get("rows",[]) if ma_sel else []
            if ma_rows:
                r = ma_df.iloc[ma_rows[0]]
                cpr = {k:float(r[k]) for k in ["CPR TC","CPR Pivot","CPR BC"] if k in r}
                chart_modal(r["Symbol"]+".NS", r["Company"], cached_ma_tf, extra_levels=cpr)
    else:
        st.info("🔎 Configure filters above and click **Run MA Screener** to scan.")


# ── TAB 3 ────────────────────────────────────────────────────────────────────
with tab3:
    st.markdown("#### 📈 20 EMA crossing above 50 EMA — position trading setups (Daily TF)")

    with st.container(border=True):
        xc1, xc2 = st.columns([5, 2])
        xc1.markdown(
            '<small style="color:#8b949e;">Price > 200 EMA · 20 EMA crossed above 50 EMA within last 1–5 days '
            '· Confirmed 2+ days · Volume surge on crossover · RSI > 30 · <b>Fixed: Daily timeframe</b></small>',
            unsafe_allow_html=True)
        run_cross_btn = xc2.button("🔍 Run Crossover Scan", type="primary",
                                   use_container_width=True, key="run_cross")

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
            mc1,mc2,mc3,mc4 = st.columns(4)
            mc1.metric("Crossovers Found",   len(cross_df))
            mc2.metric("Strong Buy 🟢",      len(cross_df[cross_df["Signal"]=="STRONG BUY"]))
            mc3.metric("Buy 🟡",             len(cross_df[cross_df["Signal"]=="BUY"]))
            mc4.metric("Vol Surge on Cross", len(cross_df[cross_df["Vol on Cross"]>1.3]))
            st.caption("👆 Click any row to open Daily chart · Esc to close")

            show_cols = [c for c in CROSS_COLS if c in cross_df.columns]
            styled_cross = (cross_df[show_cols].copy().style
                            .map(_style(CROSS_SIGNAL_COLORS), subset=["Signal"])
                            .map(change_style, subset=["Change 1D%"])
                            .format({c:"₹{:.2f}" for c in ["Price","EMA 20","EMA 50","EMA 200"] if c in cross_df.columns})
                            .format({c:"{:+.2f}%" for c in ["Change 1D%","% Above EMA200","EMA Gap%","Move Since Cross%"] if c in cross_df.columns})
                            .format({c:"{:.1f}" for c in ["RSI"] if c in cross_df.columns})
                            .format({c:"{:.2f}x" for c in ["Vol on Cross","Vol Today"] if c in cross_df.columns})
                            .format({c:"{:.0f} day(s) ago" for c in ["Cross Day"] if c in cross_df.columns})
                            .format({c:"{:.0f} days" for c in ["Confirmed Days"] if c in cross_df.columns}))
            cross_sel = st.dataframe(styled_cross, use_container_width=True, height=500,
                                     on_select="rerun", selection_mode="single-row", key="cross_table")
            csv_cross = cross_df.to_csv(index=False).encode("utf-8")
            st.download_button("⬇️ Export CSV", csv_cross, "ema_crossover.csv", "text/csv")
            cross_rows = cross_sel.selection.get("rows",[]) if cross_sel else []
            if cross_rows:
                cr = cross_df.iloc[cross_rows[0]]
                chart_modal(cr["Symbol"]+".NS", cr["Company"], "1D")
    else:
        st.info("🔎 Click **Run Crossover Scan** above to identify position trading setups.")


# ── TAB 4 ────────────────────────────────────────────────────────────────────
with tab4:
    st.markdown("#### 🛡️ Stocks bouncing off 50 EMA in strong uptrend, above Monthly CPR")

    with st.container(border=True):
        sc1, sc2, sc3 = st.columns([2, 4, 2])
        touch_pct_50 = sc1.slider("EMA50 touch tolerance (%)", 1, 3, 1, key="touch50")
        sc2.markdown(
            '<small style="color:#8b949e;">EMA20 > EMA50 > EMA200 · Prev candle low touched EMA50 '
            '· Closed above EMA50 · Today bullish · Price above Monthly CPR TC · Volume above avg</small>',
            unsafe_allow_html=True)
        run_ma50_btn = sc3.button("🔍 Run 50 MA Scan", type="primary",
                                  use_container_width=True, key="run_ma50")

    if run_ma50_btn:
        with st.spinner(f"Scanning NIFTY 500 on {tf['label']} candles… (3–6 min)"):
            symbols_df   = get_nifty500_symbols()
            ma50_results = run_ma50_support_scan(symbols_df, touch_pct=touch_pct_50/100,
                                                  interval=interval, period=period)
            st.session_state["ma50_results"] = ma50_results
            st.session_state["ma50_tf"]      = tf_key

    if "ma50_results" in st.session_state:
        ma50_df: pd.DataFrame = st.session_state["ma50_results"]
        cached_ma50_tf = st.session_state.get("ma50_tf","1D")
        if cached_ma50_tf != tf_key:
            st.warning(f"⚠️ Results from **{TF_CONFIG[cached_ma50_tf]['label']}**. Re-run for **{TF_CONFIG[tf_key]['label']}**.")
        if ma50_df.empty:
            st.warning("No stocks found. Try adjusting touch tolerance or timeframe.")
        else:
            mc1,mc2,mc3 = st.columns(3)
            mc1.metric("Stocks Found",      len(ma50_df))
            mc2.metric("Above Monthly CPR", len(ma50_df[ma50_df["Above Monthly CPR"]=="✅ Yes"]))
            mc3.metric("Vol Surge >1.3x",   len(ma50_df[ma50_df["Vol Ratio"]>1.3]))
            st.caption("👆 Click any row to open chart · Esc to close")

            show_cols = [c for c in MA50_COLS if c in ma50_df.columns]
            styled_ma50 = (ma50_df[show_cols].copy().style
                           .map(_style(MA50_SIGNAL_COLORS), subset=["Signal"])
                           .map(change_style, subset=["Change 1D%"])
                           .format({c:"₹{:.2f}" for c in ["Price","EMA 20","EMA 50","EMA 200","Monthly TC","Monthly Pivot","Monthly BC"] if c in ma50_df.columns})
                           .format({c:"{:+.2f}%" for c in ["Change 1D%","% Above EMA50","% Above EMA200","Touch%"] if c in ma50_df.columns})
                           .format({c:"{:.2f}x" for c in ["Vol Ratio"] if c in ma50_df.columns}))
            ma50_sel = st.dataframe(styled_ma50, use_container_width=True, height=500,
                                    on_select="rerun", selection_mode="single-row", key="ma50_table")
            csv_ma50 = ma50_df.to_csv(index=False).encode("utf-8")
            st.download_button("⬇️ Export CSV", csv_ma50, "ma50_support.csv", "text/csv")
            ma50_rows = ma50_sel.selection.get("rows",[]) if ma50_sel else []
            if ma50_rows:
                r = ma50_df.iloc[ma50_rows[0]]
                cpr = {k:float(r[k]) for k in ["Monthly TC","Monthly Pivot","Monthly BC"] if k in r}
                chart_modal(r["Symbol"]+".NS", r["Company"], cached_ma50_tf, extra_levels=cpr)
    else:
        st.info("🔎 Configure filters above and click **Run 50 MA Scan** to start.")
