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

st.set_page_config(page_title="NIFTY 500 Stock Screener", layout="wide", page_icon="📈")

# ── Timeframe config ──────────────────────────────────────────────────────────
# screener_period: data range used for EMA calculations in the screener
# chart_period:    data range fetched for the chart modal
# display_period:  shown in the caption to the user
# market_hours:    if True, chart is filtered to NSE hours 9:10–15:00 IST
TF_CONFIG = {
    "5m":  {"interval": "5m",   "screener_period": "5d",  "chart_period": "2d",  "display_period": "2d",  "label": "5 Min",  "market_hours": True},
    "15m": {"interval": "15m",  "screener_period": "5d",  "chart_period": "2d",  "display_period": "2d",  "label": "15 Min", "market_hours": True},
    "1H":  {"interval": "1h",   "screener_period": "6mo", "chart_period": "6mo", "display_period": "6mo", "label": "1 Hour", "market_hours": False},
    "1D":  {"interval": "1d",   "screener_period": "1y",  "chart_period": "1y",  "display_period": "1y",  "label": "Daily",  "market_hours": False},
    "1W":  {"interval": "1wk",  "screener_period": "5y",  "chart_period": "5y",  "display_period": "5y",  "label": "Weekly", "market_hours": False},
}

# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("⚙️ Filters")

    st.subheader("📰 News Screener")
    news_days    = st.slider("News lookback (days)", 7, 14, 10)
    min_mentions = st.slider("Min news mentions", 1, 10, 2)
    breakout_pct = st.slider("Max % below Period High", 1, 20, 5)
    show_all     = st.checkbox("Show SKIP signals too", value=False)
    run_btn      = st.button("🔍 Run News Screener", type="primary", use_container_width=True)

    st.divider()

    st.subheader("🔁 MA Retracement")
    touch_pct  = st.slider("20 MA touch tolerance (%)", 1, 3, 1)
    run_ma_btn = st.button("🔍 Run MA Screener", type="primary", use_container_width=True)

    st.divider()

    st.subheader("📈 20/50 EMA Crossover")
    run_cross_btn = st.button("🔍 Run Crossover Scan", type="primary", use_container_width=True)

    st.divider()

    st.subheader("🛡️ 50 MA Support")
    touch_pct_50  = st.slider("EMA50 touch tolerance (%)", 1, 3, 1, key="touch50")
    run_ma50_btn  = st.button("🔍 Run 50 MA Support Scan", type="primary", use_container_width=True)

    st.caption("First run may take 2–5 minutes.")

# ── Constants ─────────────────────────────────────────────────────────────────
SIGNAL_COLORS = {
    "STRONG BUY": "#00C853",
    "BUY":        "#64DD17",
    "WATCH":      "#FFD600",
    "SKIP":       "#FF5252",
}
MA_SIGNAL_COLORS = {
    "STRONG (20MA + CPR)": "#00C853",
    "GOOD (20MA + CPR)":   "#64DD17",
    "GOOD (20MA + Vol)":   "#FFD600",
    "20MA Bounce":         "#80CBC4",
}
NEWS_COLS = [
    "Symbol", "Company", "News Mentions",
    "Price", "Change Last%", "Change Prev 5%",
    "RSI", "% from High", "Vol Ratio",
    "Signal", "Top Headline",
]
MA_COLS = [
    "Symbol", "Company",
    "Price", "Change%", "EMA20", "% Above EMA20",
    "Touch%", "Vol Ratio", "CPR Support",
    "CPR BC", "CPR Pivot", "CPR TC", "Signal",
]


# ── Style helpers ─────────────────────────────────────────────────────────────
def signal_style(val):
    color = SIGNAL_COLORS.get(val, "#ffffff")
    return f"background-color: {color}; color: black; font-weight: bold; border-radius: 4px;"

def ma_signal_style(val):
    color = MA_SIGNAL_COLORS.get(val, "#ffffff")
    return f"background-color: {color}; color: black; font-weight: bold; border-radius: 4px;"

def change_style(val):
    try:
        return "color: #00C853;" if float(val) > 0 else "color: #FF5252;"
    except Exception:
        return ""


# ── Chart modal ───────────────────────────────────────────────────────────────
def _filter_market_hours(df: pd.DataFrame) -> pd.DataFrame:
    """Keep only NSE market hours 9:10 AM – 3:00 PM IST."""
    if df.empty:
        return df
    idx = df.index
    if idx.tzinfo is None:
        idx = idx.tz_localize("UTC")
    idx = idx.tz_convert("Asia/Kolkata")
    df = df.copy()
    df.index = idx
    df = df.between_time("09:10", "15:00")
    return df


@st.dialog("📊 Stock Chart", width="large")
def chart_modal(nse_symbol: str, company: str, tf_key: str, extra_levels: dict | None = None):
    st.markdown(f"### {company} &nbsp; `{nse_symbol}`")

    cfg           = TF_CONFIG[tf_key]
    interval      = cfg["interval"]
    chart_per     = cfg["chart_period"]
    use_mkt_hours = cfg["market_hours"]

    with st.spinner(f"Loading {tf_key} chart..."):
        df = yf.download(nse_symbol, period=chart_per, interval=interval,
                         progress=False, auto_adjust=True)

    if df is None or df.empty:
        st.error("No data available.")
        return

    if use_mkt_hours:
        df = _filter_market_hours(df)
        if df.empty:
            st.warning("No data in market hours (9:10–15:00 IST) for today. Market may be closed.")
            return

    close  = df["Close"].squeeze()
    volume = df["Volume"].squeeze()
    ema20  = _ema(close, 20)
    ema50  = _ema(close, 50)

    latest   = float(close.iloc[-1])
    prev     = float(close.iloc[-2]) if len(close) > 1 else latest
    chg      = (latest - prev) / prev * 100 if prev else 0
    ph       = float(close.max())
    pl       = float(close.min())

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Price",        f"₹{latest:.2f}", f"{chg:+.2f}%")
    m2.metric("EMA 20",       f"₹{float(ema20.iloc[-1]):.2f}")
    m3.metric("Day High",     f"₹{ph:.2f}")
    m4.metric("Day Low",      f"₹{pl:.2f}")

    fig = go.Figure()
    fig.add_trace(go.Candlestick(
        x=df.index,
        open=df["Open"].squeeze(), high=df["High"].squeeze(),
        low=df["Low"].squeeze(),   close=close,
        name="Price",
        increasing_line_color="#00C853",
        decreasing_line_color="#FF5252",
    ))
    fig.add_trace(go.Scatter(x=df.index, y=ema20, name="EMA 20",
                             line=dict(color="#FFD600", width=1.8)))
    fig.add_trace(go.Scatter(x=df.index, y=ema50, name="EMA 50",
                             line=dict(color="#FF9100", width=1.8)))

    if extra_levels:
        level_colors = {"CPR TC": "#CE93D8", "CPR Pivot": "#9575CD", "CPR BC": "#7986CB"}
        for label, price in extra_levels.items():
            fig.add_hline(
                y=price, line_dash="dot",
                line_color=level_colors.get(label, "#aaa"), line_width=1.5,
                annotation_text=f" {label}: ₹{price:.2f}",
                annotation_position="right", annotation_font_size=11,
            )

    fig.add_trace(go.Bar(x=df.index, y=volume, name="Volume",
                         marker_color="rgba(255,255,255,0.85)", yaxis="y2"))

    fig.update_layout(
        xaxis_rangeslider_visible=False,
        yaxis=dict(title="Price (₹)", side="left", showgrid=True, gridcolor="#2a2a2a"),
        yaxis2=dict(overlaying="y", side="right", showgrid=False, title="Volume"),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        height=520,
        margin=dict(t=30, b=30, l=10, r=100),
        template="plotly_dark",
        plot_bgcolor="#0e1117",
        paper_bgcolor="#0e1117",
    )
    st.plotly_chart(fig, use_container_width=True)

    col_a, col_b = st.columns(2)
    with col_a:
        if extra_levels:
            st.markdown("**CPR Levels**")
            for label, price in extra_levels.items():
                st.markdown(f"- **{label}:** ₹{price:.2f}")
    with col_b:
        sym = nse_symbol.replace(".NS", "")
        st.link_button("🔗 Open on TradingView",
                       f"https://www.tradingview.com/chart/?symbol=NSE:{sym}",
                       use_container_width=True)


# ── Page header + Timeframe selector ─────────────────────────────────────────
st.title("📈 NIFTY 500 Stock Screener")

st.markdown("#### Select Timeframe")
tf_key = st.segmented_control(
    label="Timeframe",
    options=list(TF_CONFIG.keys()),
    format_func=lambda k: TF_CONFIG[k]["label"],
    default="1D",
    key="global_tf",
    label_visibility="collapsed",
)

tf       = TF_CONFIG[tf_key]
interval = tf["interval"]
period   = tf["screener_period"]

if tf["market_hours"]:
    mkt_note = f" · Charts show 9:10 AM – 3:00 PM IST for {tf['display_period']}"
else:
    mkt_note = ""
st.caption(
    f"📌 Screener running on **{tf['label']}** candles · "
    f"Data period: **{tf['display_period']}**"
    f"{mkt_note}"
)

st.divider()

tab1, tab2, tab3, tab4 = st.tabs(["📰 News + Breakout", "🔁 20 MA Retracement", "📈 50 & 20 EMA Crossover", "🛡️ 50 MA Support"])


# ════════════════════════════════════════════════════════════════════════════
# TAB 1 — News + Breakout
# ════════════════════════════════════════════════════════════════════════════
with tab1:
    if run_btn:
        with st.spinner("📰 Fetching news..."):
            symbols_df = get_nifty500_symbols()
            trending   = get_trending_stocks(symbols_df, days=news_days)

        if not trending:
            st.error("No trending stocks found. Try increasing the lookback period.")
        else:
            progress  = st.progress(0, text="Analyzing stocks...")
            rows_done = []
            for i, item in enumerate(trending):
                tech = analyze_stock(
                    item["NSE_Symbol"],
                    breakout_pct=breakout_pct / 100,
                    interval=interval,
                    period=period,
                )
                if tech:
                    rows_done.append({
                        "Symbol":        item["Symbol"],
                        "NSE_Symbol":    item["NSE_Symbol"],
                        "Company":       item["Company"],
                        "News Mentions": item["News_Mentions"],
                        "Top Headline":  item["Headlines"][0] if item["Headlines"] else "",
                        **tech,
                    })
                progress.progress((i + 1) / len(trending), text=f"Analyzing {item['Symbol']}...")
            progress.empty()
            st.session_state["news_results"] = pd.DataFrame(rows_done)
            st.session_state["news_tf"] = tf_key

    if "news_results" in st.session_state:
        full_df: pd.DataFrame = st.session_state["news_results"]
        cached_tf = st.session_state.get("news_tf", "1D")

        if cached_tf != tf_key:
            st.warning(f"⚠️ Results are from **{TF_CONFIG[cached_tf]['label']}** timeframe. Click **Run News Screener** to refresh for **{TF_CONFIG[tf_key]['label']}**.")

        if full_df.empty:
            st.warning("No results. Try relaxing the filters.")
        else:
            signal_order = {"STRONG BUY": 0, "BUY": 1, "WATCH": 2, "SKIP": 3}
            full_df["_rank"] = full_df["Signal"].map(signal_order)
            full_df = full_df.sort_values(["_rank", "News Mentions"], ascending=[True, False])
            full_df = full_df.drop(columns=["_rank"])

            filtered = full_df[full_df["News Mentions"] >= min_mentions]
            if not show_all:
                filtered = filtered[filtered["Signal"] != "SKIP"]

            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Screened",   len(full_df))
            m2.metric("Strong Buy", len(full_df[full_df["Signal"] == "STRONG BUY"]))
            m3.metric("Buy",        len(full_df[full_df["Signal"] == "BUY"]))
            m4.metric("Watch",      len(full_df[full_df["Signal"] == "WATCH"]))

            st.caption("👆 Click any row to open chart  ·  Press Esc to close")

            show_cols = [c for c in NEWS_COLS if c in filtered.columns]
            display   = filtered[show_cols].copy()
            styled = (
                display.style
                .map(signal_style, subset=["Signal"])
                .map(change_style, subset=[c for c in ["Change Last%", "Change Prev 5%"] if c in display.columns])
                .format({c: "₹{:.2f}" for c in ["Price"] if c in display.columns})
                .format({c: "{:+.2f}%" for c in ["Change Last%", "Change Prev 5%", "% from High"] if c in display.columns})
                .format({c: "{:.1f}" for c in ["RSI"] if c in display.columns})
                .format({c: "{:.2f}x" for c in ["Vol Ratio"] if c in display.columns})
            )

            selection = st.dataframe(
                styled,
                use_container_width=True,
                height=600,
                on_select="rerun",
                selection_mode="single-row",
                key="news_table",
            )

            csv = filtered.to_csv(index=False).encode("utf-8")
            st.download_button("⬇️ Download CSV", csv, "news_screener.csv", "text/csv")

            rows_sel = selection.selection.get("rows", []) if selection else []
            if rows_sel:
                row = filtered.iloc[rows_sel[0]]
                nse_sym = row.get("NSE_Symbol", row["Symbol"] + ".NS")
                chart_modal(nse_sym, row["Company"], cached_tf)
    else:
        st.info("👈 Click **Run News Screener** in the sidebar to start.")


# ════════════════════════════════════════════════════════════════════════════
# TAB 2 — 20 MA Retracement
# ════════════════════════════════════════════════════════════════════════════
with tab2:
    st.markdown("**Stocks retesting 20 EMA in an uptrend with bullish continuation candle**")
    with st.expander("📋 Criteria"):
        st.markdown("""
        - **Uptrend:** Price > EMA20 > EMA50, positive EMA20 slope
        - **Touch:** Previous candle's low within ±tolerance% of EMA20
        - **No breakdown:** Previous candle closed above EMA20
        - **Continuation:** Current candle is bullish and closing higher
        - 🟢 **CPR Bonus:** Previous candle found support at CPR level
        """)

    if run_ma_btn:
        with st.spinner(f"Scanning NIFTY 500 on {TF_CONFIG[tf_key]['label']} timeframe... (3–5 min)"):
            symbols_df = get_nifty500_symbols()
            ma_results = run_ma_retracement_scan(
                symbols_df,
                touch_pct=touch_pct / 100,
                interval=interval,
                period=period,
            )
            st.session_state["ma_results"] = ma_results
            st.session_state["ma_tf"] = tf_key

    if "ma_results" in st.session_state:
        ma_df: pd.DataFrame = st.session_state["ma_results"]
        cached_ma_tf = st.session_state.get("ma_tf", "1D")

        if cached_ma_tf != tf_key:
            st.warning(f"⚠️ Results are from **{TF_CONFIG[cached_ma_tf]['label']}** timeframe. Click **Run MA Screener** to refresh for **{TF_CONFIG[tf_key]['label']}**.")

        if ma_df.empty:
            st.warning("No stocks found. Try increasing touch tolerance or switching timeframe.")
        else:
            c1, c2, c3 = st.columns(3)
            c1.metric("Stocks Found",      len(ma_df))
            c2.metric("With CPR Support",  len(ma_df[ma_df["CPR Support"] == "✅ Yes"]))
            c3.metric("Vol Surge (>1.2x)", len(ma_df[ma_df["Vol Ratio"] > 1.2]))

            st.caption("👆 Click any row to open chart  ·  Press Esc to close")

            show_ma_cols = [c for c in MA_COLS if c in ma_df.columns]
            display_ma   = ma_df[show_ma_cols].copy()
            styled_ma = (
                display_ma.style
                .map(ma_signal_style, subset=["Signal"])
                .map(change_style, subset=[c for c in ["Change%"] if c in display_ma.columns])
                .format({c: "₹{:.2f}" for c in ["Price", "EMA20", "CPR BC", "CPR Pivot", "CPR TC"] if c in display_ma.columns})
                .format({c: "{:+.2f}%" for c in ["Change%", "% Above EMA20", "Touch%"] if c in display_ma.columns})
                .format({c: "{:.2f}x" for c in ["Vol Ratio"] if c in display_ma.columns})
            )

            ma_selection = st.dataframe(
                styled_ma,
                use_container_width=True,
                height=600,
                on_select="rerun",
                selection_mode="single-row",
                key="ma_table",
            )

            csv_ma = ma_df.to_csv(index=False).encode("utf-8")
            st.download_button("⬇️ Download CSV", csv_ma, "ma_retracement.csv", "text/csv")

            ma_rows = ma_selection.selection.get("rows", []) if ma_selection else []
            if ma_rows:
                ma_row = ma_df.iloc[ma_rows[0]]
                cpr_levels = {
                    "CPR TC":    float(ma_row["CPR TC"]),
                    "CPR Pivot": float(ma_row["CPR Pivot"]),
                    "CPR BC":    float(ma_row["CPR BC"]),
                }
                chart_modal(ma_row["Symbol"] + ".NS", ma_row["Company"],
                            cached_ma_tf, extra_levels=cpr_levels)
    else:
        st.info("👈 Click **Run MA Screener** in the sidebar to scan.")


# ════════════════════════════════════════════════════════════════════════════
# TAB 3 — 20/50 EMA Crossover (Position Trading)
# ════════════════════════════════════════════════════════════════════════════
CROSS_SIGNAL_COLORS = {
    "STRONG BUY": "#00C853",
    "BUY":        "#64DD17",
    "WATCH":      "#FFD600",
}
CROSS_COLS = [
    "Symbol", "Company",
    "Price", "Change 1D%", "RSI",
    "EMA 20", "EMA 50", "EMA 200", "% Above EMA200",
    "EMA Gap%", "Cross Day", "Confirmed Days",
    "Vol on Cross", "Vol Today", "Move Since Cross%",
    "Signal",
]

def cross_signal_style(val):
    color = CROSS_SIGNAL_COLORS.get(val, "#ffffff")
    return f"background-color: {color}; color: black; font-weight: bold; border-radius: 4px;"

with tab3:
    st.markdown("**Position Trading — 20 EMA crossing above 50 EMA with price above 200 EMA**")
    with st.expander("📋 Criteria"):
        st.markdown("""
        - **Timeframe:** Daily (fixed — position trading)
        - **Long-term uptrend:** Price > 200 EMA
        - **Crossover:** 20 EMA crossed above 50 EMA within last 1–5 days
        - **Fake breakout filter:** 20 EMA stayed above 50 EMA for 2+ consecutive days
        - **Volume surge:** Above-average volume on crossover day confirms conviction
        - **RSI > 30:** Stock is not in oversold/trapped zone
        - Sorted by: recency of crossover → volume surge
        """)

    if run_cross_btn:
        with st.spinner("Scanning NIFTY 500 for 20/50 EMA crossovers on Daily charts... (3–5 min)"):
            symbols_df    = get_nifty500_symbols()
            cross_results = run_crossover_scan(symbols_df)
            st.session_state["cross_results"] = cross_results

    if "cross_results" in st.session_state:
        cross_df: pd.DataFrame = st.session_state["cross_results"]

        if cross_df.empty:
            st.warning("No fresh crossovers found in NIFTY 500. Try again tomorrow or check back after market hours.")
        else:
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Crossovers Found",  len(cross_df))
            c2.metric("Strong Buy",        len(cross_df[cross_df["Signal"] == "STRONG BUY"]))
            c3.metric("Buy",               len(cross_df[cross_df["Signal"] == "BUY"]))
            c4.metric("Vol Surge on Cross", len(cross_df[cross_df["Vol on Cross"] > 1.3]))

            st.caption("👆 Click any row to open Daily chart  ·  Press Esc to close")

            show_cross_cols = [c for c in CROSS_COLS if c in cross_df.columns]
            display_cross   = cross_df[show_cross_cols].copy()
            styled_cross = (
                display_cross.style
                .map(cross_signal_style, subset=["Signal"])
                .map(change_style, subset=["Change 1D%"])
                .format({c: "₹{:.2f}" for c in ["Price", "EMA 20", "EMA 50", "EMA 200"] if c in display_cross.columns})
                .format({c: "{:+.2f}%" for c in ["Change 1D%", "% Above EMA200", "EMA Gap%", "Move Since Cross%"] if c in display_cross.columns})
                .format({c: "{:.1f}" for c in ["RSI"] if c in display_cross.columns})
                .format({c: "{:.2f}x" for c in ["Vol on Cross", "Vol Today"] if c in display_cross.columns})
                .format({c: "{:.0f} day(s) ago" for c in ["Cross Day"] if c in display_cross.columns})
                .format({c: "{:.0f} days" for c in ["Confirmed Days"] if c in display_cross.columns})
            )

            cross_selection = st.dataframe(
                styled_cross,
                use_container_width=True,
                height=600,
                on_select="rerun",
                selection_mode="single-row",
                key="cross_table",
            )

            csv_cross = cross_df.to_csv(index=False).encode("utf-8")
            st.download_button("⬇️ Download CSV", csv_cross, "ema_crossover.csv", "text/csv")

            cross_rows = cross_selection.selection.get("rows", []) if cross_selection else []
            if cross_rows:
                cr = cross_df.iloc[cross_rows[0]]
                chart_modal(cr["Symbol"] + ".NS", cr["Company"], "1D")
    else:
        st.info("👈 Click **Run Crossover Scan** in the sidebar to identify position trading setups.")


# ════════════════════════════════════════════════════════════════════════════
# TAB 4 — 50 MA Support
# ════════════════════════════════════════════════════════════════════════════
MA50_SIGNAL_COLORS = {
    "STRONG (50MA + Monthly CPR)": "#00C853",
    "BUY (50MA + Monthly CPR)":    "#64DD17",
    "WATCH":                        "#FFD600",
}
MA50_COLS = [
    "Symbol", "Company",
    "Price", "Change 1D%",
    "EMA 20", "EMA 50", "EMA 200",
    "% Above EMA50", "% Above EMA200",
    "Touch%", "Vol Ratio",
    "Above Monthly CPR", "Monthly TC", "Monthly Pivot", "Monthly BC",
    "Signal",
]

def ma50_signal_style(val):
    color = MA50_SIGNAL_COLORS.get(val, "#ffffff")
    return f"background-color: {color}; color: black; font-weight: bold; border-radius: 4px;"

with tab4:
    st.markdown("**Stocks taking support at 50 EMA in a strong uptrend, above Monthly CPR**")
    with st.expander("📋 Criteria"):
        st.markdown("""
        - **Uptrend:** EMA20 > EMA50 > EMA200 (strong multi-MA alignment)
        - **Touch:** Previous candle's low within ±tolerance% of EMA50
        - **No breakdown:** Previous candle closed above EMA50
        - **Continuation:** Current candle is bullish and closing higher
        - **Monthly CPR:** Price must be above Monthly TC (top of monthly CPR band)
        - **Volume:** Higher than 20-day average confirms conviction
        """)

    if run_ma50_btn:
        with st.spinner(f"Scanning NIFTY 500 for 50 MA support setups on {TF_CONFIG[tf_key]['label']} candles... (3–6 min)"):
            symbols_df    = get_nifty500_symbols()
            ma50_results  = run_ma50_support_scan(
                symbols_df,
                touch_pct=touch_pct_50 / 100,
                interval=interval,
                period=period,
            )
            st.session_state["ma50_results"] = ma50_results
            st.session_state["ma50_tf"]      = tf_key

    if "ma50_results" in st.session_state:
        ma50_df: pd.DataFrame = st.session_state["ma50_results"]
        cached_ma50_tf = st.session_state.get("ma50_tf", "1D")

        if cached_ma50_tf != tf_key:
            st.warning(f"⚠️ Results are from **{TF_CONFIG[cached_ma50_tf]['label']}** timeframe. Re-run to refresh for **{TF_CONFIG[tf_key]['label']}**.")

        if ma50_df.empty:
            st.warning("No stocks found. Markets may be closed, or try adjusting the touch tolerance.")
        else:
            c1, c2, c3 = st.columns(3)
            c1.metric("Stocks Found",          len(ma50_df))
            c2.metric("Above Monthly CPR",     len(ma50_df[ma50_df["Above Monthly CPR"] == "✅ Yes"]))
            c3.metric("Vol Surge (>1.3x)",      len(ma50_df[ma50_df["Vol Ratio"] > 1.3]))

            st.caption("👆 Click any row to open chart  ·  Press Esc to close")

            show_ma50_cols = [c for c in MA50_COLS if c in ma50_df.columns]
            display_ma50   = ma50_df[show_ma50_cols].copy()
            styled_ma50 = (
                display_ma50.style
                .map(ma50_signal_style, subset=["Signal"])
                .map(change_style, subset=["Change 1D%"])
                .format({c: "₹{:.2f}" for c in ["Price", "EMA 20", "EMA 50", "EMA 200", "Monthly TC", "Monthly Pivot", "Monthly BC"] if c in display_ma50.columns})
                .format({c: "{:+.2f}%" for c in ["Change 1D%", "% Above EMA50", "% Above EMA200", "Touch%"] if c in display_ma50.columns})
                .format({c: "{:.2f}x" for c in ["Vol Ratio"] if c in display_ma50.columns})
            )

            ma50_selection = st.dataframe(
                styled_ma50,
                use_container_width=True,
                height=600,
                on_select="rerun",
                selection_mode="single-row",
                key="ma50_table",
            )

            csv_ma50 = ma50_df.to_csv(index=False).encode("utf-8")
            st.download_button("⬇️ Download CSV", csv_ma50, "ma50_support.csv", "text/csv")

            ma50_rows = ma50_selection.selection.get("rows", []) if ma50_selection else []
            if ma50_rows:
                ma50_row = ma50_df.iloc[ma50_rows[0]]
                cpr_levels = {}
                for lvl in ["Monthly TC", "Monthly Pivot", "Monthly BC"]:
                    if lvl in ma50_row:
                        cpr_levels[lvl] = float(ma50_row[lvl])
                chart_modal(ma50_row["Symbol"] + ".NS", ma50_row["Company"],
                            cached_ma50_tf, extra_levels=cpr_levels)
    else:
        st.info("👈 Click **Run 50 MA Support Scan** in the sidebar to start.")
