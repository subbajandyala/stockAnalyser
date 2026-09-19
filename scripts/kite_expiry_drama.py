"""
Expiry Drama CLI — find 10x-30x potential options on expiry day.
Usage: python kite_expiry_drama.py [SYMBOL]
"""
import sys, datetime
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

import kite_creds
from screener.expiry_drama import run_expiry_drama, get_today_expiry_instruments, ALL_INSTRUMENTS


def run(symbol=None):
    api_key, access_token = kite_creds.load()

    if symbol:
        symbols = [symbol.upper()]
    else:
        symbols = get_today_expiry_instruments()
        if not symbols:
            symbols = ALL_INSTRUMENTS[:1]

    for sym in symbols:
        try:
            res = run_expiry_drama(api_key, access_token, sym)
        except Exception as e:
            print(f"❌ {sym}: {e}")
            continue

        spot      = res["spot"]
        expiry    = res["expiry"]
        max_pain  = res["max_pain"]
        oi_magnet = res["oi_magnet"]
        pcr       = res["pcr"]
        bias      = res["bias"]
        gap       = res["gap_pts"]
        direction = res["direction"]
        cands     = res["candidates_df"]
        is_today  = res["is_expiry_day"]

        print(f"\n{'='*65}")
        print(f"  🎭 EXPIRY DRAMA — {sym}  |  Expiry: {expiry}")
        print(f"{'='*65}")
        print(f"  Spot: {spot:,.0f}  |  ATM: {res['atm']:.0f}  |  Max Pain: {max_pain:.0f}")
        print(f"  PCR: {pcr}  |  Bias: {bias}  |  Gap to Pain: {gap:+.0f} pts  {direction}")
        print(f"  OI Magnet: {oi_magnet:.0f}  |  CE Wall: {res['ce_wall']:.0f}  |  PE Wall: {res['pe_wall']:.0f}")
        if not is_today:
            print(f"  ⚠️  NOT expiry day — scores still valid but premium decay effect lower")

        if cands.empty:
            print("\n  No drama candidates found (no cheap OTM options scoring ≥35).")
        else:
            print(f"\n  {'Strike':>7}  {'Type':4}  {'LTP':>6}  {'Score':>5}  {'Label':16}  {'Pain Mult':>9}  Tags")
            print(f"  {'-'*80}")
            for _, row in cands.head(15).iterrows():
                print(f"  {row['Strike']:>7}  {row['Type']:4}  {row['LTP']:>6.2f}  "
                      f"{row['Score']:>5}  {row['Label']:16}  "
                      f"  {row['Pain Mult']:>5.1f}x  {row['Tags']}")

        print()


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else None)
