#!/usr/bin/env bash
pkill -f scripts/pulse_bot.py 2>/dev/null && echo "NIFTY Pulse stopped" || echo "NIFTY Pulse not running"
