#!/usr/bin/env bash
# One-screen health check for a trading machine. Prints no secrets.
#   cd /root/stockAnalyser && git pull origin claude/sensex-option-moves-analysis-ma3tr7 && bash scripts/health_check.sh

cd "$(dirname "$0")/.." || exit 1
PYTHON="${PYTHON:-/root/venv/bin/python}"
BRANCH=claude/sensex-option-moves-analysis-ma3tr7
fails=0; warns=0
ok()   { printf "  OK    %-14s %s\n" "$1" "$2"; }
warn() { printf "  WARN  %-14s %s\n" "$1" "$2"; warns=$((warns+1)); }
bad()  { printf "  FAIL  %-14s %s\n" "$1" "$2"; fails=$((fails+1)); }
envv() {
    grep -E "^[[:space:]]*(export[[:space:]]+)?$1[[:space:]]*=" .env 2>/dev/null | tail -1 \
        | cut -d= -f2- | tr -d '"'"'" | tr -d '[:space:]'
}

name=$(envv KITE_NAME); acct=$(envv NOTION_ACCOUNT); trade_only=$(envv TRADE_ONLY)
echo "=== $(hostname) — $(curl -s -m 5 https://api.ipify.org) — ${acct:-${name:-(no KITE_NAME)}} — $(TZ=Asia/Kolkata date '+%d-%b %H:%M IST') ==="

# Right after a reboot, @reboot start_droplet.sh (30 s delay) is still starting things and
# kills any chart_bot process — including our preflight. Wait until it has finished.
up=$(cut -d. -f1 /proc/uptime)
if [ "${up:-999}" -lt 120 ]; then
    echo "  (machine booted ${up}s ago — waiting $((120 - up))s for the services to start)"
    sleep $((120 - up))
fi
[ -z "$name" ] && [ -z "$acct" ] && warn name "no KITE_NAME in .env — Notion rows and pushes won't say whose account"

# Code version
GIT_TERMINAL_PROMPT=0 timeout 20 git fetch -q origin "$BRANCH" 2>/dev/null
here=$(git rev-parse --short HEAD 2>/dev/null); there=$(git rev-parse --short "origin/$BRANCH" 2>/dev/null)
if [ -n "$there" ] && [ "$here" = "$there" ]; then ok code "up to date ($here)"
else warn code "at $here, latest is ${there:-?} — run git pull + bash start_droplet.sh"; fi

# Settings
[ -f .env ] || bad .env "missing"
[ "$(stat -c %a .env 2>/dev/null)" = "600" ] && ok ".env perms" "600" || warn ".env perms" "run: chmod 600 .env"
for k in KITE_USER_ID KITE_API_KEY KITE_API_SECRET KITE_PASSWORD KITE_TOTP_SECRET KITE_ACCESS_TOKEN; do
    [ -n "$(envv $k)" ] || bad "$k" "missing in .env"
done
[ "$(envv KITE_AUTO_TRADE)" = "1" ] && ok auto-trade "ON" || warn auto-trade "KITE_AUTO_TRADE is not 1 — no orders"
[ -n "$(envv NTFY_TOPIC)" ] && ok ntfy "topic set" || warn ntfy "NTFY_TOPIC missing"
[ -n "$(envv NOTION_TOKEN)" ] && ok notion "token set, rows tagged '${acct:-${name:-?}}'" || warn notion "NOTION_TOKEN missing"
ln=$(envv CHART_MAX_LOTS_NIFTY); ls=$(envv CHART_MAX_LOTS_SENSEX)
ok "lot cap" "NIFTY ${ln:-default 2}, SENSEX ${ls:-default 3} (0 = 95% of balance)"
if [ "$trade_only" = "1" ]; then ok mode "TRADE_ONLY (feed + chart_bot)"
else ok mode "full (dashboard + alert bots) — expected only on the main machine"; fi
if grep -qE "^[[:space:]]*KITE2_(API_KEY|USER_ID)=" .env 2>/dev/null; then
    warn KITE2 "second account ($(envv KITE2_NAME)) also trades from this machine — remove the KITE2_ lines if it has its own machine"
fi
if [ -n "$acct" ] && [ -n "$name" ] && [ "$acct" != "$name" ]; then
    warn names "NOTION_ACCOUNT=$acct but KITE_NAME=$name — copied .env?"
fi

# Processes
pgrep -f "ticker_daemon.py --worker" >/dev/null && ok ticker "running" || bad ticker "not running — bash start_droplet.sh"
n=$(pgrep -fc "scripts/chart_bot.py --loop")
[ "$n" = "1" ] && ok chart_bot "running" || { [ "$n" = "0" ] && bad chart_bot "not running — bash start_droplet.sh" || bad chart_bot "$n copies running"; }
if [ "$trade_only" = "1" ]; then
    extra=$(pgrep -fa "streamlit run|cpr_adx_bot|agent_flow_bot|rsi_scalp_bot" | wc -l)
    [ "$extra" = "0" ] && ok extras "none" || warn extras "$extra dashboard/alert processes running — bash stop_droplet.sh && bash start_droplet.sh"
fi
mem=$(free -m | awk '/^Mem:/{printf "%d/%d MB used", $3, $2}'); ok memory "$mem"

# Crontab
cron=$(crontab -l 2>/dev/null | grep -v '^[[:space:]]*#' | grep -v '^[[:space:]]*$')
for job in auto_token.py watchdog.sh "chart_bot.py --preflight" notion_daily_log.py "@reboot.*start_droplet.sh"; do
    echo "$cron" | grep -qE -- "$job" && ok cron "$job" || bad cron "missing: $job"
done
if [ "$trade_only" = "1" ]; then
    stray=$(echo "$cron" | grep -E "^[A-Z_]+=|^@[A-Z]|eod_report|auto_sk_token|token_server|red_flag|cpr_scan|agent_flow")
    [ -z "$stray" ] && ok cron "no main-machine jobs" || { warn cron "main-machine lines present (remove):"; echo "$stray" | sed 's/^/          /' | cut -c1-110; }
fi

# Token, balance, positions (no orders)
echo "--- preflight ---"
pf=$("$PYTHON" scripts/chart_bot.py --preflight 2>&1)
lines=$(echo "$pf" | grep -E "kite token|balance|positions|orphan|READY|FAIL|WARN")
if [ -n "$lines" ]; then
    echo "$lines" | sed 's/^/  /'
    echo "$pf" | grep -q "NOT READY" && fails=$((fails+1))
else
    bad preflight "no report — last lines of its output:"
    echo "$pf" | tail -8 | sed 's/^/          /'
fi

echo "=== result: $fails fail, $warns warn ==="
