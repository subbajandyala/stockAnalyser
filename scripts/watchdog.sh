#!/usr/bin/env bash
# Restart chart_bot if it has died (e.g. killed by Linux when memory runs out).
# Cron, every 2 minutes:
#   */2 * * * * bash /root/stockAnalyser/scripts/watchdog.sh
#
# Does nothing after stop_droplet.sh (it creates .watchdog_off) until
# start_droplet.sh runs again. chart_bot itself refuses to run twice, so a
# restart here can never create a duplicate instance.

cd "$(dirname "$0")/.." || exit 1
PYTHON="${PYTHON:-/root/venv/bin/python}"

[ -f .watchdog_off ] && exit 0
pgrep -f "scripts/chart_bot.py --loop" >/dev/null && exit 0

echo "$(date '+%F %T') chart_bot not running — restarting" >> watchdog.log
export PYTHONUNBUFFERED=1
nohup "$PYTHON" -u scripts/chart_bot.py --loop --interval 30 >> chart_bot.log 2>&1 &

topic=$(grep '^NTFY_TOPIC=' .env 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"'"'" | tr -d '[:space:]')
if [ -n "$topic" ]; then
    curl -s -m 10 -H "Title: Chart bot restarted by watchdog" -H "Priority: high" \
        -d "chart_bot had stopped (possibly out of memory) and was restarted at $(TZ=Asia/Kolkata date '+%H:%M IST')." \
        "https://ntfy.sh/$topic" >/dev/null
fi
