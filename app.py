import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import yfinance as yf
from screener.stocks import get_nifty500_symbols
from screener.news import get_trending_stocks
from screener.technical import analyze_stocks, _ema

st.set_page_config(page_title="NIFTY 500 Stock Screener", layout="wide", page_icon="📈")

st.title("📈 NIFTY 500 Stock Screener")
st.markdown("**Trending in News · Uptrend · Breakout Ready**")

# ── Sidebar ──────────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("⚙️ Filters")
    news_days = st.slider("News lookback (days)", 7, 14, 10)
    min_mentions = st.slider("Min news mentions", 1, 10, 2)
    breakout_pct = st.slider("Max % below 52W High", 1, 20, 5)
    show_all = st.checkbox("Show SKIP signals too", value=False)

    st.divider()
    run_btn = st.button("🔍 Run Screener", type="primary", use_container_width=True)
    st.caption("First run may take 2–3 minutes while fetching data.")

# ── Signal badge colors ───────────────────────────────────────────────────────
SIGNAL_COLORS = {
    "STRONG BUY": "#00C853",
    "BUY":        "#64DD17",
    "WATCH":      "#FFD600",
    "SKIP":       "#FF5252",
}

DISPLAY_COLS = [
    "Symbol", "Company", "News Mentions",
    "Price", "Change 1D%", "Change 1W%",
    "RSI", "% from 52W High", "Vol Ratio (5d/20d)",
    "Signal", "Top Headline",
]


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


# ── Main ──────────────────────────────────────────────────────────────────────
if run_btn:
    with st.spinner("📰 Fetching news feeds..."):
        symbols_df = get_nifty500_symbols()
        trending = get_trending_stocks(symbols_df, days=news_days)

    if not trending:
        st.error("No trending stocks found in news feeds. Try increasing the lookback period.")
        st.stop()

    st.info(f"Found **{len(trending)}** stocks mentioned in news. Running technical analysis...")

    progress = st.progress(0, text="Analyzing stocks...")
    results_placeholder = st.empty()
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
    st.session_state["results"] = pd.DataFrame(rows_done)
    st.session_state["filters"] = (min_mentions, show_all, breakout_pct)

if "results" in st.session_state:
    full_df: pd.DataFrame = st.session_state["results"]

    if full_df.empty:
        st.warning("No results. Try relaxing the filters.")
        st.stop()

    # Sort
    signal_order = {"STRONG BUY": 0, "BUY": 1, "WATCH": 2, "SKIP": 3}
    full_df["_rank"] = full_df["Signal"].map(signal_order)
    full_df = full_df.sort_values(["_rank", "News Mentions"], ascending=[True, False])
    full_df = full_df.drop(columns=["_rank"])

    filtered = full_df[full_df["News Mentions"] >= min_mentions]
    if not show_all:
        filtered = filtered[filtered["Signal"] != "SKIP"]

    # ── Summary metrics ───────────────────────────────────────────────────────
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Stocks Screened", len(full_df))
    col2.metric("Strong Buy", len(full_df[full_df["Signal"] == "STRONG BUY"]))
    col3.metric("Buy", len(full_df[full_df["Signal"] == "BUY"]))
    col4.metric("Watch", len(full_df[full_df["Signal"] == "WATCH"]))

    st.subheader(f"Results — {len(filtered)} stocks")

    display = filtered[DISPLAY_COLS].copy()
    styled = (
        display.style
        .applymap(signal_style, subset=["Signal"])
        .applymap(change_style, subset=["Change 1D%", "Change 1W%"])
        .format({
            "Price": "₹{:.2f}",
            "Change 1D%": "{:+.2f}%",
            "Change 1W%": "{:+.2f}%",
            "RSI": "{:.1f}",
            "% from 52W High": "{:.1f}%",
            "Vol Ratio (5d/20d)": "{:.2f}x",
        })
    )
    st.dataframe(styled, use_container_width=True, height=450)

    # ── Download ──────────────────────────────────────────────────────────────
    csv = filtered.to_csv(index=False).encode("utf-8")
    st.download_button("⬇️ Download CSV", csv, "screener_results.csv", "text/csv")

    st.divider()

    # ── Stock detail chart ────────────────────────────────────────────────────
    st.subheader("📊 Stock Chart")
    choices = filtered["Symbol"].tolist()
    if choices:
        selected_sym = st.selectbox("Select a stock to view chart", choices)
        if selected_sym:
            row = filtered[filtered["Symbol"] == selected_sym].iloc[0]
            show_chart(row["NSE_Symbol"], row["Company"])

            with st.expander("📰 Recent Headlines"):
                idx = full_df[full_df["Symbol"] == selected_sym].index
                if len(idx) > 0:
                    headline = full_df.loc[idx[0], "Top Headline"]
                    st.write(f"- {headline}")
else:
    st.info("👈 Configure filters in the sidebar and click **Run Screener** to start.")
