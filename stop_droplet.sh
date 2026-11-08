#!/usr/bin/env bash
touch "$(cd "$(dirname "$0")" && pwd)/.watchdog_off"
pkill -f praveen_bot.py    2>/dev/null && echo "praveen_bot stopped"    || echo "praveen_bot not running"
pkill -f ticker_daemon.py 2>/dev/null && echo "ticker_daemon stopped" || echo "ticker_daemon not running"
