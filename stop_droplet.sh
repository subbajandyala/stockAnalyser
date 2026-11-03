#!/usr/bin/env bash
touch "$(cd "$(dirname "$0")" && pwd)/.watchdog_off"
pkill -f chandu_bot.py    2>/dev/null && echo "chandu_bot stopped"    || echo "chandu_bot not running"
pkill -f ticker_daemon.py 2>/dev/null && echo "ticker_daemon stopped" || echo "ticker_daemon not running"
