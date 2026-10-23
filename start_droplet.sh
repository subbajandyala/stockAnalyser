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
rm -f .watchdog_off   # re-enable the chart_bot watchdog (paused by stop_droplet.sh)

PYTHON="${PYTHON:-/root/venv/bin/python}"
STREAMLIT="${STREAMLIT:-/root/venv/bin/streamlit}"

# Force Python stdout/stderr to be unbuffered so logs appear in real time
export PYTHONUNBUFFERED=1

# Export KITE_AUTO_TRADE from .env so bots can see it even if not in shell env
_auto_trade_val=$(grep '^KITE_AUTO_TRADE=' .env 2>/dev/null | head -1 | cut -d= -f2 | tr -d '"'"'" | tr -d '[:space:]' || true)
if [ "$_auto_trade_val" = "1" ]; then
    export KITE_AUTO_TRADE=1
    echo "  KITE_AUTO_TRADE=1 (from .env)"
else
    export KITE_AUTO_TRADE=0
    echo "  KITE_AUTO_TRADE=0 (auto-trade disabled)"
fi

# TRADE_ONLY=1 in .env (second trading droplet): only the live feed + chart_bot run —
# no dashboard, no alert bots, so the shared ntfy topic gets no duplicate alerts.
_trade_only=$(grep '^TRADE_ONLY=' .env 2>/dev/null | head -1 | cut -d= -f2 | tr -d '"'"'" | tr -d '[:space:]' || true)

echo "=== MarketPulse — Starting services ==="
[ "$_trade_only" = "1" ] && echo "  TRADE_ONLY=1 — live feed + chart_bot only"

# ── Kill old processes ──────────────────────────────────────────────────────
pkill -f ticker_daemon.py  2>/dev/null || true
pkill -f "streamlit run"   2>/dev/null || true
sleep 1

# ── Start ticker daemon ─────────────────────────────────────────────────────
echo "Starting ticker_daemon.py..."
nohup "$PYTHON" -u ticker_daemon.py >> ticker.log 2>&1 &
TICKER_PID=$!
echo "  ticker_daemon PID: $TICKER_PID"

# Give the WebSocket 3 seconds to connect
sleep 3

# ── Start Streamlit ─────────────────────────────────────────────────────────
if [ "$_trade_only" != "1" ]; then
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
nohup "$PYTHON" -u scripts/cpr_adx_bot.py >> cpr_adx.log 2>&1 &
CPR_PID=$!
echo "  cpr_adx_bot PID: $CPR_PID"
fi

# ── Start Chart Analysis bot (15m+5m confirm, every 5 min) ──────────────────
echo "Starting Chart Analysis bot..."
pkill -f chart_bot.py 2>/dev/null || true
nohup "$PYTHON" -u scripts/chart_bot.py --loop --interval 30 >> chart_bot.log 2>&1 &
CHART_PID=$!
echo "  chart_bot PID: $CHART_PID"

# ── Start Agent Flow bot (OI + momentum, every 5 min) ────────────────────────
if [ "$_trade_only" != "1" ]; then
echo "Starting Agent Flow bot..."
pkill -f agent_flow_bot.py 2>/dev/null || true
nohup "$PYTHON" -u scripts/agent_flow_bot.py NIFTY BANKNIFTY SENSEX --loop >> agent_flow.log 2>&1 &
AF_PID=$!
echo "  agent_flow_bot PID: $AF_PID"
fi

echo ""
echo "=== Done ==="
echo "  App:       http://<your-droplet-ip>:8501"
echo "  Ticker:    tail -f $SCRIPT_DIR/ticker.log"
echo "  App:       tail -f $SCRIPT_DIR/streamlit.log"
echo "  CPR+ADX:   tail -f $SCRIPT_DIR/cpr_adx.log"
echo "  Chart Bot: tail -f $SCRIPT_DIR/chart_bot.log"
echo "  Agent Flow: tail -f $SCRIPT_DIR/agent_flow.log"
echo ""
echo "To stop:  bash stop_droplet.sh"
