#!/usr/bin/env bash
# Run this on the DigitalOcean droplet to start everything.
# Usage:  bash start_droplet.sh
#
# What it does:
#   1. Kills any existing ticker_daemon / streamlit processes
#   2. Starts ticker_daemon.py in the background (logs to ticker.log)
#   3. Starts Streamlit on port 8501 in the background (logs to streamlit.log)
#
# To stop everything:  bash stop_droplet.sh

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON="${PYTHON:-/root/venv/bin/python}"
STREAMLIT="${STREAMLIT:-/root/venv/bin/streamlit}"

echo "=== MarketPulse — Starting services ==="

# ── Kill old processes ──────────────────────────────────────────────────────
pkill -f ticker_daemon.py  2>/dev/null || true
pkill -f "streamlit run"   2>/dev/null || true
sleep 1

# ── Start ticker daemon ─────────────────────────────────────────────────────
echo "Starting ticker_daemon.py..."
nohup "$PYTHON" ticker_daemon.py >> ticker.log 2>&1 &
TICKER_PID=$!
echo "  ticker_daemon PID: $TICKER_PID"

# Give the WebSocket 3 seconds to connect
sleep 3

# ── Start Streamlit ─────────────────────────────────────────────────────────
echo "Starting Streamlit on port 8501..."
nohup "$STREAMLIT" run app.py \
    --server.port 8501 \
    --server.address 0.0.0.0 \
    --server.headless true \
    >> streamlit.log 2>&1 &
ST_PID=$!
echo "  Streamlit PID: $ST_PID"

# ── Start CPR+ADX signal bot (1-min continuous loop) ───────────────────────
echo "Starting CPR+ADX bot supervisor..."
pkill -f cpr_adx_bot.py 2>/dev/null || true
nohup "$PYTHON" scripts/cpr_adx_bot.py >> cpr_adx.log 2>&1 &
CPR_PID=$!
echo "  cpr_adx_bot PID: $CPR_PID"

# ── Start RSI Scalp bot (5-min continuous loop) ─────────────────────────────
echo "Starting RSI Scalp bot supervisor..."
pkill -f rsi_scalp_bot.py 2>/dev/null || true
nohup "$PYTHON" scripts/rsi_scalp_bot.py >> rsi_scalp.log 2>&1 &
RSI_PID=$!
echo "  rsi_scalp_bot PID: $RSI_PID"

echo ""
echo "=== Done ==="
echo "  App:    http://<your-droplet-ip>:8501"
echo "  Ticker: tail -f $SCRIPT_DIR/ticker.log"
echo "  App:    tail -f $SCRIPT_DIR/streamlit.log"
echo "  CPR+ADX: tail -f $SCRIPT_DIR/cpr_adx.log
  RSI Scalp: tail -f $SCRIPT_DIR/rsi_scalp.log"
echo ""
echo "To stop:  bash stop_droplet.sh"
