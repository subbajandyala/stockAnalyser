"""
Backfill today's signal JSON logs from bot log files.
Run once after-hours when JSON logs in /tmp/ are missing:
    /root/venv/bin/python scripts/backfill_today_logs.py
Then re-run eod_report.py to get the updated report.
"""
import datetime
import json
import pathlib
import re
import sys

_IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

# ── Chart Bot log parser ──────────────────────────────────────────────────────

def _parse_chart_bot_log(log_path: str, date_str: str) -> list:
    p = pathlib.Path(log_path)
    if not p.exists():
        print(f"  Not found: {log_path}")
        return []

    text = p.read_text(errors="replace")
    SEP = "=" * 55

    # Split on separators — signal blocks appear between pairs of ===
    parts = text.split(SEP)
    signals = []
    seen_keys = set()

    for idx, block in enumerate(parts):
        if "CHART SIGNAL" not in block:
            continue

        entry = {
            "auto_trade": "off", "tradingsymbol": "", "strike": "",
            "qty": "", "entry_premium": "", "sl_premium": "",
            "target_premium": "", "order_id": "",
        }

        for line in block.splitlines():
            line = line.strip()
            if not line:
                continue

            if "CHART SIGNAL —" in line:
                m = re.search(r"CHART SIGNAL\s*[—-]\s*(\w+)", line)
                if m:
                    entry["symbol"] = m.group(1)

            elif line.startswith("Time"):
                m = re.search(r"(\d{1,2}:\d{2})\s*IST", line)
                if m:
                    entry["time"] = m.group(1) + " IST"

            elif line.startswith("Verdict"):
                m = re.search(r"Verdict\s*:\s*([\w ]+?)(?:\s+★+)?\s*$", line)
                if m:
                    entry["verdict"] = m.group(1).strip()

            elif line.startswith("Direction"):
                if "CALL" in line or "CE" in line:
                    entry["direction"] = "CE"
                elif "PUT" in line or "PE" in line:
                    entry["direction"] = "PE"

            elif line.startswith("Score"):
                m = re.search(r"([+-]\d+)\s*/\s*\d+", line)
                if m:
                    entry["score"] = int(m.group(1))
                m2 = re.search(r"Confidence:\s*(\d+)%", line)
                if m2:
                    entry["conf"] = int(m2.group(1))

            elif line.startswith("Spot"):
                m = re.search(r"Spot\s*:\s*([\d,]+\.?\d*)", line)
                if m:
                    entry["spot"] = float(m.group(1).replace(",", ""))

            elif line.startswith("Entry"):
                nums = re.findall(r"[\d,]+\.?\d*", line)
                nums = [float(n.replace(",", "")) for n in nums]
                if len(nums) >= 2:
                    entry["entry_low"]  = nums[0]
                    entry["entry_high"] = nums[1]

            elif line.startswith("Stop Loss"):
                m = re.search(r"([\d,]+\.?\d*)", line)
                if m:
                    entry["stop_loss"] = float(m.group(1).replace(",", ""))

            elif line.startswith("Targets"):
                tgts = re.findall(r"[\d,]+\.?\d*", line)
                entry["targets"] = " / ".join(t.replace(",", "") for t in tgts)

            elif line.startswith("R:R"):
                m = re.search(r"([\d.]+)x?", line)
                if m:
                    entry["rr"] = m.group(1)

            elif re.match(r"15m\s*:", line):
                entry["tf_15m"] = line.split(":", 1)[1].strip()

            elif re.match(r"5m\s*:", line):
                entry["tf_5m"] = line.split(":", 1)[1].strip()

        if not (entry.get("symbol") and entry.get("time") and entry.get("direction")):
            continue

        # Look for auto-trade info in the chunk AFTER this block
        next_chunk = parts[idx + 1] if idx + 1 < len(parts) else ""
        m = re.search(
            r"\[AUTO-TRADE\] ORDER — (\S+) \| Entry ₹([\d.]+) \| SL ₹([\d.]+) \| Target ₹([\d.]+)",
            next_chunk,
        )
        if m:
            entry["auto_trade"]      = "placed"
            entry["tradingsymbol"]   = m.group(1)
            entry["entry_premium"]   = float(m.group(2))
            entry["sl_premium"]      = float(m.group(3))
            entry["target_premium"]  = float(m.group(4))
            # Extract order id if present
            m2 = re.search(r"order[_\s]id[:\s]+(\d+)", next_chunk, re.I)
            if m2:
                entry["order_id"] = m2.group(1)
        elif re.search(r"\[AUTO-TRADE\].*SKIPPED", next_chunk):
            m3 = re.search(r"SKIPPED\s*[—-]\s*(.+)", next_chunk)
            entry["auto_trade"] = f"skipped: {m3.group(1).strip()}" if m3 else "skipped"

        # Build unique key: date + symbol + HHMM
        time_raw = entry["time"].replace(" IST", "").replace(":", "")
        date_key  = date_str.replace("-", "")
        key = f"{date_key}:{entry['symbol']}:{time_raw}"
        if key in seen_keys:
            continue
        seen_keys.add(key)

        entry["_key"] = key
        entry.setdefault("tf_15m", "")
        entry.setdefault("tf_5m", "")
        signals.append(entry)

    return signals


# ── CPR+ADX log parser ────────────────────────────────────────────────────────

def _parse_cpr_adx_log(log_path: str) -> list:
    p = pathlib.Path(log_path)
    if not p.exists():
        print(f"  Not found: {log_path}")
        return []

    text = p.read_text(errors="replace")
    signals = []
    seen = set()
    current_time = ""

    for line in text.splitlines():
        # Track scan time from lines like: [14:28:31 IST] Scanning 25 stocks…
        m = re.match(r"\[(\d{1,2}:\d{2})(?::\d{2})?\s*IST\]", line)
        if m:
            current_time = m.group(1) + " IST"

        # Alert line produced by cpr_adx_bot when score threshold is met
        # Format: "  ✅ ntfy sent: BAJAJFINSV SHORT ★★★★ (score -4)"
        # OR: "[ALERT] BAJAJFINSV: SHORT signal ★★★★ (score -4)"
        # OR log line: "  🚀 LT             | SHORT | score -5 | ADX   58 | ★★★★★"
        # We capture lines that fired an ntfy (bot only alerts on score >= threshold)
        m2 = re.search(
            r"ntfy.*?:\s*(\w+)\s+(BUY|SHORT)\s+(★+)\s+\(score ([+-]\d+)\)", line, re.I
        )
        if not m2:
            m2 = re.search(
                r"\[ALERT\]\s+(\w+):\s+(BUY|SHORT)\s+signal\s+(★+)\s+\(score ([+-]\d+)\)", line
            )
        if m2:
            sym, direction, stars, score = (
                m2.group(1), m2.group(2), m2.group(3), int(m2.group(4))
            )
            key = f"{sym}:{current_time}"
            if key not in seen:
                seen.add(key)
                signals.append({
                    "time":      current_time,
                    "symbol":    sym,
                    "direction": direction,
                    "signal":    f"{direction} {stars}",
                    "stars":     stars,
                    "score":     score,
                })

    return signals


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    now_ist  = datetime.datetime.now(_IST)
    date_str = now_ist.strftime("%Y-%m-%d")
    print(f"Backfilling signal logs for {date_str}\n")

    # Chart Bot
    chart_signals = _parse_chart_bot_log("/root/stockAnalyser/chart_bot.log", date_str)
    if chart_signals:
        out = {"date": date_str, "signals": chart_signals}
        dest = pathlib.Path(f"/tmp/chart_bot_log_{date_str}.json")
        dest.write_text(json.dumps(out, indent=2))
        print(f"  Chart Bot : {len(chart_signals)} signals written → {dest}")
        for s in chart_signals:
            print(f"    {s['time']} {s['symbol']} {s['direction']} {s.get('verdict','')} score={s.get('score','?')}")
    else:
        print("  Chart Bot : no signals found in chart_bot.log")

    # CPR+ADX
    cpr_signals = _parse_cpr_adx_log("/root/stockAnalyser/cpr_adx.log")
    if cpr_signals:
        out = {"date": date_str, "signals": cpr_signals}
        dest = pathlib.Path(f"/tmp/cpr_adx_log_{date_str}.json")
        dest.write_text(json.dumps(out, indent=2))
        print(f"  CPR+ADX   : {len(cpr_signals)} alerts written → {dest}")
        for s in cpr_signals:
            print(f"    {s['time']} {s['symbol']} {s['direction']} {s['stars']}")
    else:
        print("  CPR+ADX   : no alerts found in cpr_adx.log")

    print("\nNow run:")
    print("  /root/venv/bin/python scripts/eod_report.py")
