"""
Agent Flow CLI — institutional-grade option buying analysis via Kite.
Usage: python kite_agent_flow.py [SYMBOL]
"""
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

import kite_creds
from screener.agent_flow import run_agent_analysis, INSTRUMENTS


def run(symbol="NIFTY"):
    api_key, access_token = kite_creds.load()
    symbol = symbol.upper()
    if symbol not in INSTRUMENTS:
        print(f"Symbol must be one of: {INSTRUMENTS}")
        return

    print(f"\n⚡ Running Agent Flow analysis for {symbol}…\n")
    try:
        res = run_agent_analysis(api_key, access_token, symbol)
    except Exception as e:
        print(f"❌ Error: {e}")
        return

    spot      = res.get("spot", 0)
    bias      = res.get("bias", "")
    score     = res.get("score", 0)
    signal    = res.get("signal", "NO TRADE")
    trade     = res.get("trade", {})
    support   = res.get("support", [])
    resistance = res.get("resistance", [])
    reasons   = res.get("reasons", [])
    checklist = res.get("checklist", {})
    risk      = res.get("risk", "")
    theta     = res.get("theta_risk", "")

    print(f"{'='*65}")
    print(f"  🤖 AGENT FLOW — {symbol}  |  Spot: {spot:,.0f}")
    print(f"{'='*65}")
    print(f"\n  MARKET BIAS:  {bias}")
    print(f"  CONFIDENCE:   {score}%")
    print(f"  SIGNAL:       {signal}")

    if trade:
        print(f"\n  ── TRADE ──────────────────────────────────────────────")
        for k, v in trade.items():
            print(f"  {k:<20}: {v}")

    if reasons:
        print(f"\n  ── REASONS ─────────────────────────────────────────────")
        for r in reasons:
            print(f"    • {r}")

    if support:
        print(f"\n  Support levels:    {', '.join(str(s) for s in support)}")
    if resistance:
        print(f"  Resistance levels: {', '.join(str(r) for r in resistance)}")

    if checklist:
        print(f"\n  ── CHECKLIST ───────────────────────────────────────────")
        for k, v in checklist.items():
            icon = "✅" if v else "❌"
            print(f"    {icon} {k}")

    print(f"\n  Risk: {risk}  |  Theta Risk: {theta}")
    print()


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else "NIFTY")
