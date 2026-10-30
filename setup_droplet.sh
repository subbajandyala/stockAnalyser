#!/usr/bin/env bash
# One-shot setup: swap + code pull + restart everything.
# Run from anywhere: bash /root/stockAnalyser/setup_droplet.sh
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== 1. SWAP SETUP ==="
if free | grep -q "^Swap:.*0.*0.*0"; then
    echo "No active swap — creating /swapfile (1 GB)..."
    if [ ! -f /swapfile ]; then
        fallocate -l 1G /swapfile
    fi
    chmod 600 /swapfile
    blkid /swapfile | grep -q swap || mkswap /swapfile
    swapon /swapfile 2>/dev/null || true
    grep -q swapfile /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
    echo "Swap created."
else
    echo "Swap already active — skipping."
fi
free -h

echo ""
echo "=== 2. GIT PULL ==="
git pull origin claude/sensex-option-moves-analysis-ma3tr7 2>/dev/null || git pull 2>/dev/null || echo "  (git pull skipped — already up to date or no network)"

echo ""
echo "=== 3. VERIFY KITE TOKEN ==="
PYTHON="${PYTHON:-/root/venv/bin/python}"
"$PYTHON" - <<'PYEOF'
import sys, pathlib, json, os
sys.path.insert(0, "/root/stockAnalyser")
env = {}
env_path = pathlib.Path("/root/stockAnalyser/.env")
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
api_key = env.get("KITE_API_KEY", "")
token   = env.get("KITE_ACCESS_TOKEN", "")
if not (api_key and token):
    print("  ERROR: KITE_API_KEY or KITE_ACCESS_TOKEN missing from .env")
    sys.exit(0)
import requests
r = requests.get(
    "https://api.kite.trade/user/profile",
    headers={"X-Kite-Version": "3", "Authorization": f"token {api_key}:{token}"},
    timeout=10,
)
if r.ok:
    name = r.json().get("data", {}).get("user_name", "?")
    print(f"  Token OK — User: {name}")
else:
    print(f"  Token EXPIRED (HTTP {r.status_code}) — run: python auto_token.py")
PYEOF

echo ""
echo "=== 4. RESTART SERVICES ==="
export PYTHONUNBUFFERED=1
_auto=$(grep '^KITE_AUTO_TRADE=' .env 2>/dev/null | head -1 | cut -d= -f2 | tr -d '"'"'" | tr -d '[:space:]' || true)
if [ "$_auto" = "1" ]; then
    export KITE_AUTO_TRADE=1
    echo "  KITE_AUTO_TRADE=1"
else
    export KITE_AUTO_TRADE=0
    echo "  KITE_AUTO_TRADE=0"
fi

pkill -f ticker_daemon.py   2>/dev/null || true
pkill -f "streamlit run"    2>/dev/null || true
pkill -f cpr_adx_bot.py     2>/dev/null || true
pkill -f rsi_scalp_bot.py   2>/dev/null || true
sleep 2

PYTHON="${PYTHON:-/root/venv/bin/python}"
STREAMLIT="${STREAMLIT:-/root/venv/bin/streamlit}"

nohup "$PYTHON" -u ticker_daemon.py           >> ticker.log     2>&1 &
echo "  ticker_daemon  PID $!"
sleep 2

nohup "$STREAMLIT" run app.py \
    --server.port 8501 --server.address 0.0.0.0 --server.headless true \
    >> streamlit.log 2>&1 &
echo "  streamlit      PID $!"

nohup "$PYTHON" -u scripts/cpr_adx_bot.py     >> cpr_adx.log   2>&1 &
echo "  cpr_adx_bot    PID $!"

nohup "$PYTHON" -u scripts/rsi_scalp_bot.py   >> rsi_scalp.log 2>&1 &
echo "  rsi_scalp_bot  PID $!"

echo ""
echo "=== 5. STATUS (10 s) ==="
sleep 10
echo "--- Running bots ---"
ps aux | grep -E "rsi_scalp|cpr_adx|streamlit|ticker_daemon" | grep -v grep | awk '{print $1, $2, $11}' || true

echo ""
echo "--- Last 15 lines of rsi_scalp.log ---"
tail -15 rsi_scalp.log

echo ""
echo "=== DONE ==="
echo "Swap is active. Services restarted with latest code."
echo "Watch live: tail -f /root/stockAnalyser/rsi_scalp.log"
