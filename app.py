import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import yfinance as yf
from screener.stocks import get_nifty500_symbols
from screener.news import get_trending_stocks
from screener.technical import analyze_stocks, _ema
from screener.ma_retracement import run_ma_retracement_scan

st.set_page_config(page_title="NIFTY 500 Stock Screener", layout="wide", page_icon="📈")

st.title("📈 NIFTY 500 Stock Screener")

tab1, tab2 = st.tabs(["📰 News + Breakout", "🔁 20 MA Retracement"])

# ════════════════════════════════════════════════════════════════════════════
# TAB 1 — News + Breakout Screener
# ════════════════════════════════════════════════════════════════════════════
with tab1:
    st.markdown("**Trending in News · Uptrend · Breakout Ready**")

    with st.sidebar:
        st.header("⚙️ Filters")

        st.subheader("📰 News Screener")
        news_days = st.slider("News lookback (days)", 7, 14, 10)
        min_mentions = st.slider("Min news mentions", 1, 10, 2)
        breakout_pct = st.slider("Max % below 52W High", 1, 20, 5)
        show_all = st.checkbox("Show SKIP signals too", value=False)
        st.divider()
        run_btn = st.button("🔍 Run News Screener", type="primary", use_container_width=True)

        st.subheader("🔁 MA Retracement")
        touch_pct = st.slider("20 MA touch tolerance (%)", 1, 3, 1)
        run_ma_btn = st.button("🔍 Run MA Screener", type="primary", use_container_width=True)

        st.caption("First run may take 2–3 minutes.")

# ── Signal badge colors ───────────────────────────────────────────────────────
SIGNAL_COLORS = {
    "STRONG BUY": "#00C853",
    "BUY":        "#64DD17",
    "WATCH":      "#FFD600",
    "SKIP":       "#FF5252",
}

DISPLAY_COLS = [
    "Kite Chart", "Symbol", "Company", "News Mentions",
    "Price", "Change 1D%", "Change 1W%",
    "RSI", "% from 52W High", "Vol Ratio (5d/20d)",
    "Signal", "Top Headline",
]


def kite_chart_url(symbol: str) -> str:
    return f"https://www.tradingview.com/chart/?symbol=NSE:{symbol}"


def signal_style(val):
    color = SIGNAL_COLORS.get(val, "#ffffff")
    return f"background-color: {color}; color: black; font-weight: bold; border-radius: 4px;"


def change_style(val):
    try:
        v = float(val)
        return "color: #00C853;" if v > 0 else "color: #FF5252;"
    except Exception:
        return ""


# ── Stock detail chart ────────────────────────────────────────────────────────
def show_chart(nse_symbol: str, company: str):
    df = yf.download(nse_symbol, period="6mo", interval="1d", progress=False, auto_adjust=True)
    if df is None or df.empty:
        st.warning("No chart data available.")
        return

    close = df["Close"].squeeze()
    volume = df["Volume"].squeeze()
    ema20 = _ema(close, 20)
    ema50 = _ema(close, 50)

    fig = go.Figure()

    # Candlestick
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

    # Volume bars
    fig.add_trace(go.Bar(
        x=df.index, y=volume,
        name="Volume",
        marker_color="rgba(100,150,255,0.3)",
        yaxis="y2",
    ))

    fig.update_layout(
        title=f"{company} ({nse_symbol}) — 6 Month Chart",
        xaxis_rangeslider_visible=False,
        yaxis=dict(title="Price (₹)", side="left"),
        yaxis2=dict(title="Volume", overlaying="y", side="right", showgrid=False),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        height=500,
        template="plotly_dark",
    )
    st.plotly_chart(fig, use_container_width=True)


    # ── News Screener logic ───────────────────────────────────────────────────
    if run_btn:
        with st.spinner("📰 Fetching news feeds..."):
            symbols_df = get_nifty500_symbols()
            trending = get_trending_stocks(symbols_df, days=news_days)

        if not trending:
            st.error("No trending stocks found. Try increasing the lookback period.")
            st.stop()

        st.info(f"Found **{len(trending)}** stocks in news. Running technical analysis...")
        progress = st.progress(0, text="Analyzing stocks...")
        rows_done = []

        from screener.technical import analyze_stock
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

            col1, col2, col3, col4 = st.columns(4)
            col1.metric("Stocks Screened", len(full_df))
            col2.metric("Strong Buy", len(full_df[full_df["Signal"] == "STRONG BUY"]))
            col3.metric("Buy", len(full_df[full_df["Signal"] == "BUY"]))
            col4.metric("Watch", len(full_df[full_df["Signal"] == "WATCH"]))

            st.subheader(f"Results — {len(filtered)} stocks")

            display = filtered.copy()
            display["Kite Chart"] = display["Symbol"].apply(kite_chart_url)
            display = display[DISPLAY_COLS]

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
            st.dataframe(
                styled,
                use_container_width=True,
                height=450,
                column_config={
                    "Kite Chart": st.column_config.LinkColumn(
                        "Chart",
                        display_text="📊 View Chart",
                        help="Opens TradingView chart in a new tab",
                    ),
                },
            )

            csv = filtered.to_csv(index=False).encode("utf-8")
            st.download_button("⬇️ Download CSV", csv, "news_screener.csv", "text/csv")

            st.divider()
            st.subheader("📊 Stock Chart")
            choices = filtered["Symbol"].tolist()
            if choices:
                selected_sym = st.selectbox("Select a stock", choices, key="news_chart_select")
                if selected_sym:
                    row = filtered[filtered["Symbol"] == selected_sym].iloc[0]
                    show_chart(row["NSE_Symbol"], row["Company"])
                    with st.expander("📰 Recent Headlines"):
                        st.write(f"- {row['Top Headline']}")
    else:
        st.info("👈 Click **Run News Screener** in the sidebar to start.")


# ════════════════════════════════════════════════════════════════════════════
# TAB 2 — 20 MA Retracement Scanner
# ════════════════════════════════════════════════════════════════════════════
with tab2:
    st.markdown("**Stocks retesting 20 EMA in an uptrend with bullish continuation**")
    st.markdown("""
    **Criteria:**
    - Stock is in uptrend (Price > EMA20 > EMA50, positive slope)
    - Previous candle's low touched EMA20 within ±tolerance%
    - Previous candle closed above EMA20 (no breakdown)
    - Today's candle is bullish and closing higher (continuation confirmed)
    - 🟢 **Bonus:** Previous candle also found support at daily CPR level
    """)

    if run_ma_btn:
        with st.spinner("Scanning NIFTY 500 for 20 MA retracements... (this takes 3-5 min)"):
            symbols_df = get_nifty500_symbols()
            ma_results = run_ma_retracement_scan(symbols_df, touch_pct=touch_pct / 100)
            st.session_state["ma_results"] = ma_results

    if "ma_results" in st.session_state:
        ma_df: pd.DataFrame = st.session_state["ma_results"]

        if ma_df.empty:
            st.warning("No stocks found matching 20 MA retracement criteria today. Try increasing the touch tolerance.")
        else:
            c1, c2, c3 = st.columns(3)
            c1.metric("Stocks Found", len(ma_df))
            c2.metric("With CPR Support", len(ma_df[ma_df["CPR Support"] == "✅ Yes"]))
            c3.metric("With Volume Surge", len(ma_df[ma_df["Vol Ratio"] > 1.2]))

            st.subheader(f"20 MA Retracement Setups — {len(ma_df)} stocks")

            MA_DISPLAY_COLS = [
                "Chart", "Symbol", "Company",
                "Price", "Change 1D%", "EMA20", "% Above EMA20",
                "Touch%", "Vol Ratio", "CPR Support",
                "CPR BC", "CPR Pivot", "CPR TC", "Signal",
            ]

            def ma_signal_style(val):
                colors = {
                    "STRONG (20MA + CPR)": "#00C853",
                    "GOOD (20MA + CPR)":   "#64DD17",
                    "GOOD (20MA + Vol)":   "#FFD600",
                    "20MA Bounce":         "#80CBC4",
                }
                color = colors.get(val, "#ffffff")
                return f"background-color: {color}; color: black; font-weight: bold;"

            display_ma = ma_df.copy()
            display_ma["Chart"] = display_ma["Symbol"].apply(kite_chart_url)
            display_ma = display_ma.rename(columns={"Chart": "Chart"})

            cols_to_show = ["Chart"] + [c for c in MA_DISPLAY_COLS[1:] if c in display_ma.columns]

            styled_ma = (
                display_ma[cols_to_show].style
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
            st.dataframe(
                styled_ma,
                use_container_width=True,
                height=500,
                column_config={
                    "Chart": st.column_config.LinkColumn(
                        "Chart",
                        display_text="📊 View",
                        help="Opens TradingView chart in a new tab",
                    ),
                },
            )

            csv_ma = ma_df.to_csv(index=False).encode("utf-8")
            st.download_button("⬇️ Download CSV", csv_ma, "ma_retracement.csv", "text/csv")

            st.divider()
            st.subheader("📊 Stock Chart")
            ma_choices = ma_df["Symbol"].tolist()
            selected_ma = st.selectbox("Select a stock", ma_choices, key="ma_chart_select")
            if selected_ma:
                ma_row = ma_df[ma_df["Symbol"] == selected_ma].iloc[0]
                nse_sym = ma_row["Symbol"] + ".NS"
                show_chart(nse_sym, ma_row["Company"])

                # Show CPR levels on the chart info
                with st.expander("📐 CPR Levels"):
                    st.markdown(f"""
                    | Level | Price |
                    |-------|-------|
                    | TC (Top Central Pivot) | ₹{ma_row['CPR TC']:.2f} |
                    | Pivot | ₹{ma_row['CPR Pivot']:.2f} |
                    | BC (Bottom Central Pivot) | ₹{ma_row['CPR BC']:.2f} |
                    | EMA 20 | ₹{ma_row['EMA20']:.2f} |
                    """)
    else:
        st.info("👈 Click **Run MA Screener** in the sidebar to scan for retracement setups.")
