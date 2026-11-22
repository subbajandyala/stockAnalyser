#!/usr/bin/env bash
# NIFTY Pulse — start the bot (it stops itself at 15:20).   Usage: bash start_pulse.sh
cd "$(cd "$(dirname "$0")" && pwd)"
PYTHON="${PYTHON:-/root/venv/bin/python}"
export PYTHONUNBUFFERED=1
pkill -f scripts/pulse_bot.py 2>/dev/null || true
sleep 1
nohup "$PYTHON" -u scripts/pulse_bot.py >> pulse_bot.log 2>&1 &
echo "NIFTY Pulse started — log: tail -f $(pwd)/pulse_bot.log"
