#!/usr/bin/env bash
pkill -f ticker_daemon.py  2>/dev/null && echo "ticker_daemon stopped"  || echo "ticker_daemon not running"
pkill -f "streamlit run"   2>/dev/null && echo "Streamlit stopped"      || echo "Streamlit not running"
pkill -f cpr_adx_bot.py    2>/dev/null && echo "cpr_adx_bot stopped"    || echo "cpr_adx_bot not running"
pkill -f rsi_scalp_bot.py  2>/dev/null && echo "rsi_scalp_bot stopped"  || echo "rsi_scalp_bot not running"
pkill -f chart_bot.py      2>/dev/null && echo "chart_bot stopped"      || echo "chart_bot not running"
pkill -f agent_flow_bot.py 2>/dev/null && echo "agent_flow_bot stopped"  || echo "agent_flow_bot not running"
