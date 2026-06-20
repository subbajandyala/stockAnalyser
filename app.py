import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import yfinance as yf
from screener.stocks import get_nifty500_symbols
from screener.news import get_trending_stocks
from screener.technical import analyze_stock, _ema
from screener.ma_retracement import run_ma_retracement_scan

st.set_page_config(page_title="NIFTY 500 Stock Screener", layout="wide", page_icon="📈")

# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("⚙️ Filters")

    st.subheader("📰 News Screener")
    news_days = st.slider("News lookback (days)", 7, 14, 10)
    min_mentions = st.slider("Min news mentions", 1, 10, 2)
    breakout_pct = st.slider("Max % below 52W High", 1, 20, 5)
    show_all = st.checkbox("Show SKIP signals too", value=False)
    run_btn = st.button("🔍 Run News Screener", type="primary", use_container_width=True)

    st.divider()

    st.subheader("🔁 MA Retracement")
    touch_pct = st.slider("20 MA touch tolerance (%)", 1, 3, 1)
    run_ma_btn = st.button("🔍 Run MA Screener", type="primary", use_container_width=True)

    st.caption("First run may take 2–3 minutes.")

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
    "Price", "Change 1D%", "Change 1W%",
    "RSI", "% from 52W High", "Vol Ratio (5d/20d)",
    "Signal", "Top Headline",
]
MA_COLS = [
    "Symbol", "Company",
    "Price", "Change 1D%", "EMA20", "% Above EMA20",
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
@st.dialog("📊 Stock Chart", width="large")
def chart_modal(nse_symbol: str, company: str, extra_levels: dict | None = None):
    with st.spinner(f"Loading {nse_symbol}..."):
        df = yf.download(nse_symbol, period="6mo", interval="1d", progress=False, auto_adjust=True)

    if df is None or df.empty:
        st.error("No data available for this symbol.")
        return

    close  = df["Close"].squeeze()
    volume = df["Volume"].squeeze()
    ema20  = _ema(close, 20)
    ema50  = _ema(close, 50)

    latest = float(close.iloc[-1])
    prev   = float(close.iloc[-2])
    chg    = (latest - prev) / prev * 100
    high52 = float(close.tail(252).max())
    low52  = float(close.tail(252).min())

    # Key metrics row inside modal
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Price",    f"₹{latest:.2f}", f"{chg:+.2f}%")
    m2.metric("EMA 20",   f"₹{float(ema20.iloc[-1]):.2f}")
    m3.metric("52W High", f"₹{high52:.2f}")
    m4.metric("52W Low",  f"₹{low52:.2f}")

    fig = go.Figure()

    fig.add_trace(go.Candlestick(
        x=df.index,
        open=df["Open"].squeeze(),
        high=df["High"].squeeze(),
        low=df["Low"].squeeze(),
        close=close,
        name="Price",
        increasing_line_color="#00C853",
        decreasing_line_color="#FF5252",
    ))
    fig.add_trace(go.Scatter(
        x=df.index, y=ema20, name="EMA 20",
        line=dict(color="#FFD600", width=1.8),
    ))
    fig.add_trace(go.Scatter(
        x=df.index, y=ema50, name="EMA 50",
        line=dict(color="#FF9100", width=1.8),
    ))

    if extra_levels:
        level_colors = {
            "CPR TC":    "#CE93D8",
            "CPR Pivot": "#9575CD",
            "CPR BC":    "#7986CB",
        }
        for label, price in extra_levels.items():
            fig.add_hline(
                y=price,
                line_dash="dot",
                line_color=level_colors.get(label, "#aaa"),
                line_width=1.5,
                annotation_text=f" {label}: ₹{price:.2f}",
                annotation_position="right",
                annotation_font_size=11,
            )

    fig.add_trace(go.Bar(
        x=df.index, y=volume,
        name="Volume",
        marker_color="rgba(100,150,255,0.25)",
        yaxis="y2",
    ))

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

    col_a, col_b = st.columns([1, 1])
    with col_a:
        if extra_levels:
            st.markdown("**CPR Levels**")
            for label, price in extra_levels.items():
                st.markdown(f"- **{label}:** ₹{price:.2f}")
    with col_b:
        sym = nse_symbol.replace(".NS", "")
        st.link_button(
            "🔗 Open on TradingView",
            f"https://www.tradingview.com/chart/?symbol=NSE:{sym}",
            use_container_width=True,
        )


# ── Tabs ──────────────────────────────────────────────────────────────────────
st.title("📈 NIFTY 500 Stock Screener")
tab1, tab2 = st.tabs(["📰 News + Breakout", "🔁 20 MA Retracement"])


# ════════════════════════════════════════════════════════════════════════════
# TAB 1 — News + Breakout
# ════════════════════════════════════════════════════════════════════════════
with tab1:
    if run_btn:
        with st.spinner("📰 Fetching news and running analysis..."):
            symbols_df = get_nifty500_symbols()
            trending   = get_trending_stocks(symbols_df, days=news_days)

        if not trending:
            st.error("No trending stocks found. Try increasing the lookback period.")
        else:
            progress  = st.progress(0, text="Analyzing stocks...")
            rows_done = []
            for i, item in enumerate(trending):
                tech = analyze_stock(item["NSE_Symbol"], breakout_pct=breakout_pct / 100)
                if tech:
                    rows_done.append({
                        "Symbol":        item["Symbol"],
                        "Company":       item["Company"],
                        "News Mentions": item["News_Mentions"],
                        "Top Headline":  item["Headlines"][0] if item["Headlines"] else "",
                        **tech,
                    })
                progress.progress((i + 1) / len(trending), text=f"Analyzing {item['Symbol']}...")
            progress.empty()
            st.session_state["news_results"] = pd.DataFrame(rows_done)

    if "news_results" in st.session_state:
        full_df: pd.DataFrame = st.session_state["news_results"]

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

            display = filtered[NEWS_COLS].copy()
            styled = (
                display.style
                .map(signal_style, subset=["Signal"])
                .map(change_style, subset=["Change 1D%", "Change 1W%"])
                .format({
                    "Price":              "₹{:.2f}",
                    "Change 1D%":         "{:+.2f}%",
                    "Change 1W%":         "{:+.2f}%",
                    "RSI":                "{:.1f}",
                    "% from 52W High":    "{:.1f}%",
                    "Vol Ratio (5d/20d)": "{:.2f}x",
                })
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
                chart_modal(row["NSE_Symbol"], row["Company"])
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
        - **Continuation:** Today's candle is bullish and closing higher
        - 🟢 **CPR Bonus:** Previous candle found support at daily CPR level
        """)

    if run_ma_btn:
        with st.spinner("Scanning NIFTY 500 for 20 MA retracements... (3–5 min)"):
            symbols_df = get_nifty500_symbols()
            ma_results = run_ma_retracement_scan(symbols_df, touch_pct=touch_pct / 100)
            st.session_state["ma_results"] = ma_results

    if "ma_results" in st.session_state:
        ma_df: pd.DataFrame = st.session_state["ma_results"]

        if ma_df.empty:
            st.warning("No stocks found. Try increasing touch tolerance.")
        else:
            c1, c2, c3 = st.columns(3)
            c1.metric("Stocks Found",      len(ma_df))
            c2.metric("With CPR Support",  len(ma_df[ma_df["CPR Support"] == "✅ Yes"]))
            c3.metric("Vol Surge (>1.2x)", len(ma_df[ma_df["Vol Ratio"] > 1.2]))

            st.caption("👆 Click any row to open chart  ·  Press Esc to close")

            display_ma = ma_df[[c for c in MA_COLS if c in ma_df.columns]].copy()
            styled_ma = (
                display_ma.style
                .map(ma_signal_style, subset=["Signal"])
                .map(change_style, subset=["Change 1D%"])
                .format({
                    "Price":         "₹{:.2f}",
                    "EMA20":         "₹{:.2f}",
                    "Change 1D%":    "{:+.2f}%",
                    "% Above EMA20": "{:+.2f}%",
                    "Touch%":        "{:+.2f}%",
                    "Vol Ratio":     "{:.2f}x",
                    "CPR BC":        "₹{:.2f}",
                    "CPR Pivot":     "₹{:.2f}",
                    "CPR TC":        "₹{:.2f}",
                })
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
                chart_modal(ma_row["Symbol"] + ".NS", ma_row["Company"], extra_levels=cpr_levels)
    else:
        st.info("👈 Click **Run MA Screener** in the sidebar to scan.")
