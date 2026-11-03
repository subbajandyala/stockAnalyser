#!/usr/bin/env bash
# chandu-logic — start the live feed and the bot.   Usage: bash start_droplet.sh
set -e
cd "$(cd "$(dirname "$0")" && pwd)"
PYTHON="${PYTHON:-/root/venv/bin/python}"
export PYTHONUNBUFFERED=1
rm -f .watchdog_off

pkill -f ticker_daemon.py 2>/dev/null || true
pkill -f chandu_bot.py    2>/dev/null || true
sleep 1

echo "Starting ticker_daemon.py (live ticks -> Redis)..."
nohup "$PYTHON" -u ticker_daemon.py >> ticker.log 2>&1 &
sleep 3

if [ -f scripts/chandu_bot.py ]; then
    echo "Starting chandu_bot.py..."
    nohup "$PYTHON" -u scripts/chandu_bot.py >> chandu_bot.log 2>&1 &
    echo "  log: tail -f chandu_bot.log"
else
    echo "scripts/chandu_bot.py not written yet — only the live feed is running."
fi
echo "To stop: bash stop_droplet.sh"
