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

# ── Helpers ───────────────────────────────────────────────────────────────────
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


def tv_url(symbol: str) -> str:
    return f"https://www.tradingview.com/chart/?symbol=NSE:{symbol}"


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


def render_chart(nse_symbol: str, company: str, extra_levels: dict | None = None):
    with st.spinner(f"Loading {nse_symbol}..."):
        df = yf.download(nse_symbol, period="6mo", interval="1d", progress=False, auto_adjust=True)
    if df is None or df.empty:
        st.warning("No chart data available.")
        return

    close = df["Close"].squeeze()
    volume = df["Volume"].squeeze()
    ema20 = _ema(close, 20)
    ema50 = _ema(close, 50)

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
    fig.add_trace(go.Scatter(x=df.index, y=ema20, name="EMA 20",
                             line=dict(color="#FFD600", width=1.5)))
    fig.add_trace(go.Scatter(x=df.index, y=ema50, name="EMA 50",
                             line=dict(color="#FF9100", width=1.5)))

    # Optional CPR levels
    if extra_levels:
        colors = {"CPR TC": "#AB47BC", "CPR Pivot": "#7E57C2", "CPR BC": "#5C6BC0"}
        for label, price in extra_levels.items():
            fig.add_hline(y=price, line_dash="dot", line_color=colors.get(label, "#888"),
                          annotation_text=label, annotation_position="right")

    fig.add_trace(go.Bar(
        x=df.index, y=volume, name="Volume",
        marker_color="rgba(100,150,255,0.3)", yaxis="y2",
    ))

    fig.update_layout(
        title=dict(text=f"{company} ({nse_symbol})", font=dict(size=14)),
        xaxis_rangeslider_visible=False,
        yaxis=dict(title="Price (₹)", side="left"),
        yaxis2=dict(overlaying="y", side="right", showgrid=False),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        height=480,
        margin=dict(t=60, b=20, l=10, r=10),
        template="plotly_dark",
    )
    st.plotly_chart(fig, use_container_width=True)
    st.link_button("📊 Open Full Chart on TradingView", tv_url(nse_symbol.replace(".NS", "")))


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
            trending = get_trending_stocks(symbols_df, days=news_days)

        if not trending:
            st.error("No trending stocks found. Try increasing the lookback period.")
        else:
            progress = st.progress(0, text="Analyzing stocks...")
            rows_done = []
            for i, item in enumerate(trending):
                tech = analyze_stock(item["NSE_Symbol"], breakout_pct=breakout_pct / 100)
                if tech:
                    rows_done.append({
                        "Symbol": item["Symbol"],
                        "Company": item["Company"],
                        "News Mentions": item["News_Mentions"],
                        "Top Headline": item["Headlines"][0] if item["Headlines"] else "",
                        **tech,
                    })
                progress.progress((i + 1) / len(trending), text=f"Analyzing {item['Symbol']}...")
            progress.empty()
            st.session_state["news_results"] = pd.DataFrame(rows_done)
            st.session_state["news_selected"] = None

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

            # Metrics
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Screened", len(full_df))
            m2.metric("Strong Buy", len(full_df[full_df["Signal"] == "STRONG BUY"]))
            m3.metric("Buy", len(full_df[full_df["Signal"] == "BUY"]))
            m4.metric("Watch", len(full_df[full_df["Signal"] == "WATCH"]))

            st.caption("👆 Click any row to view chart on the right")

            display = filtered[NEWS_COLS].copy()
            styled = (
                display.style
                .map(signal_style, subset=["Signal"])
                .map(change_style, subset=["Change 1D%", "Change 1W%"])
                .format({
                    "Price": "₹{:.2f}",
                    "Change 1D%": "{:+.2f}%",
                    "Change 1W%": "{:+.2f}%",
                    "RSI": "{:.1f}",
                    "% from 52W High": "{:.1f}%",
                    "Vol Ratio (5d/20d)": "{:.2f}x",
                })
            )

            left, right = st.columns([3, 2])

            with left:
                st.subheader(f"Results — {len(filtered)} stocks")
                selection = st.dataframe(
                    styled,
                    use_container_width=True,
                    height=520,
                    on_select="rerun",
                    selection_mode="single-row",
                    key="news_table",
                )
                csv = filtered.to_csv(index=False).encode("utf-8")
                st.download_button("⬇️ Download CSV", csv, "news_screener.csv", "text/csv")

            with right:
                rows_sel = selection.selection.get("rows", []) if selection else []
                if rows_sel:
                    idx = rows_sel[0]
                    row = filtered.iloc[idx]
                    st.subheader(f"{row['Symbol']} — {row['Company']}")
                    render_chart(row["NSE_Symbol"], row["Company"])
                    if row.get("Top Headline"):
                        st.caption(f"📰 {row['Top Headline']}")
                else:
                    st.info("👈 Click a row in the table to view the chart here.")
    else:
        st.info("👈 Click **Run News Screener** in the sidebar to start.")

# ════════════════════════════════════════════════════════════════════════════
# TAB 2 — 20 MA Retracement
# ════════════════════════════════════════════════════════════════════════════
with tab2:
    st.markdown("**Stocks retesting 20 EMA in uptrend with bullish continuation candle**")
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
            c1.metric("Stocks Found", len(ma_df))
            c2.metric("With CPR Support", len(ma_df[ma_df["CPR Support"] == "✅ Yes"]))
            c3.metric("Vol Surge (>1.2x)", len(ma_df[ma_df["Vol Ratio"] > 1.2]))

            st.caption("👆 Click any row to view chart on the right")

            display_ma = ma_df[[c for c in MA_COLS if c in ma_df.columns]].copy()
            styled_ma = (
                display_ma.style
                .map(ma_signal_style, subset=["Signal"])
                .map(change_style, subset=["Change 1D%"])
                .format({
                    "Price": "₹{:.2f}",
                    "EMA20": "₹{:.2f}",
                    "Change 1D%": "{:+.2f}%",
                    "% Above EMA20": "{:+.2f}%",
                    "Touch%": "{:+.2f}%",
                    "Vol Ratio": "{:.2f}x",
                    "CPR BC": "₹{:.2f}",
                    "CPR Pivot": "₹{:.2f}",
                    "CPR TC": "₹{:.2f}",
                })
            )

            left2, right2 = st.columns([3, 2])

            with left2:
                st.subheader(f"Setups — {len(ma_df)} stocks")
                ma_selection = st.dataframe(
                    styled_ma,
                    use_container_width=True,
                    height=520,
                    on_select="rerun",
                    selection_mode="single-row",
                    key="ma_table",
                )
                csv_ma = ma_df.to_csv(index=False).encode("utf-8")
                st.download_button("⬇️ Download CSV", csv_ma, "ma_retracement.csv", "text/csv")

            with right2:
                ma_rows_sel = ma_selection.selection.get("rows", []) if ma_selection else []
                if ma_rows_sel:
                    idx = ma_rows_sel[0]
                    ma_row = ma_df.iloc[idx]
                    nse_sym = ma_row["Symbol"] + ".NS"
                    st.subheader(f"{ma_row['Symbol']} — {ma_row['Company']}")
                    cpr_levels = {
                        "CPR TC": ma_row["CPR TC"],
                        "CPR Pivot": ma_row["CPR Pivot"],
                        "CPR BC": ma_row["CPR BC"],
                    }
                    render_chart(nse_sym, ma_row["Company"], extra_levels=cpr_levels)
                    st.markdown(f"""
                    | Level | Price |
                    |---|---|
                    | CPR TC | ₹{ma_row['CPR TC']:.2f} |
                    | Pivot  | ₹{ma_row['CPR Pivot']:.2f} |
                    | CPR BC | ₹{ma_row['CPR BC']:.2f} |
                    | EMA 20 | ₹{ma_row['EMA20']:.2f} |
                    """)
                else:
                    st.info("👈 Click a row in the table to view the chart here.")
    else:
        st.info("👈 Click **Run MA Screener** in the sidebar to scan.")
