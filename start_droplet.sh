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

echo "=== MarketPulse — Starting services ==="

# ── Kill old processes ──────────────────────────────────────────────────────
pkill -f ticker_daemon.py  2>/dev/null || true
pkill -f "streamlit run"   2>/dev/null || true
sleep 1

# ── Start ticker daemon ─────────────────────────────────────────────────────
echo "Starting ticker_daemon.py..."
nohup python ticker_daemon.py >> ticker.log 2>&1 &
TICKER_PID=$!
echo "  ticker_daemon PID: $TICKER_PID"

# Give the WebSocket 3 seconds to connect
sleep 3

# ── Start Streamlit ─────────────────────────────────────────────────────────
echo "Starting Streamlit on port 8501..."
nohup streamlit run app.py \
    --server.port 8501 \
    --server.address 0.0.0.0 \
    --server.headless true \
    >> streamlit.log 2>&1 &
ST_PID=$!
echo "  Streamlit PID: $ST_PID"

echo ""
echo "=== Done ==="
echo "  App:    http://<your-droplet-ip>:8501"
echo "  Ticker: tail -f $SCRIPT_DIR/ticker.log"
echo "  App:    tail -f $SCRIPT_DIR/streamlit.log"
echo ""
echo "To stop:  bash stop_droplet.sh"
