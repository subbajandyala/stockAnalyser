"""
Live Option Snapshot CLI — full option chain with OI, PCR, Max Pain.
Usage: python kite_option_snapshot.py [SYMBOL] [EXPIRY_YYYY-MM-DD]
"""
import sys, datetime
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

import pandas as pd, requests
from io import StringIO
import kite_creds

BASE = "https://api.kite.trade"
EXCH = {"NIFTY": "NFO", "BANKNIFTY": "NFO", "FINNIFTY": "NFO", "MIDCPNIFTY": "NFO", "SENSEX": "BFO"}
SPOT_SYM = {"NIFTY": "NSE:NIFTY 50", "BANKNIFTY": "NSE:NIFTY BANK", "FINNIFTY": "NSE:NIFTY FIN SERVICE",
            "MIDCPNIFTY": "NSE:NIFTY MID SELECT", "SENSEX": "BSE:SENSEX"}
TICK = {"NIFTY": 50, "BANKNIFTY": 100, "FINNIFTY": 50, "MIDCPNIFTY": 25, "SENSEX": 100}


def run(symbol="NIFTY", expiry_date=None, strikes_around=12):
    api_key, access_token = kite_creds.load()
    hdrs = {"X-Kite-Version": "3", "Authorization": f"token {api_key}:{access_token}"}
    exch = EXCH.get(symbol, "NFO")

    r = requests.get(f"{BASE}/instruments/{exch}", headers=hdrs, timeout=30)
    r.raise_for_status()
    instr = pd.read_csv(StringIO(r.text))
    instr["expiry_dt"] = pd.to_datetime(instr["expiry"], errors="coerce")
    opts = instr[(instr["name"] == symbol) & (instr["instrument_type"].isin(["CE", "PE"]))]

    if expiry_date:
        target = pd.Timestamp(expiry_date)
    else:
        target = opts["expiry_dt"].min()

    opts = opts[opts["expiry_dt"] == target].copy()

    # Spot
    ltp_r = requests.get(f"{BASE}/quote/ltp", headers=hdrs, params={"i": SPOT_SYM[symbol]}, timeout=10)
    ltp_r.raise_for_status()
    spot = float(ltp_r.json()["data"][SPOT_SYM[symbol]]["last_price"])
    tick = TICK[symbol]
    atm  = round(spot / tick) * tick

    # Filter strikes
    opts = opts[abs(opts["strike"] - atm) <= strikes_around * tick].copy()

    # Batch quotes
    syms = (exch + ":" + opts["tradingsymbol"]).tolist()
    quotes = {}
    for i in range(0, len(syms), 400):
        qr = requests.get(f"{BASE}/quote", headers=hdrs, params={"i": syms[i:i+400]}, timeout=30)
        if qr.ok:
            quotes.update(qr.json().get("data", {}))

    # Build chain
    rows = {}
    for _, row in opts.iterrows():
        s = float(row["strike"])
        t = row["instrument_type"]
        q = quotes.get(f"{exch}:{row['tradingsymbol']}", {})
        if s not in rows:
            rows[s] = {"Strike": s, "CE OI": 0, "CE ChgOI": 0, "CE Vol": 0, "CE LTP": 0.0,
                        "PE OI": 0, "PE ChgOI": 0, "PE Vol": 0, "PE LTP": 0.0}
        rows[s][f"{t} OI"]     = int(q.get("oi", 0))
        rows[s][f"{t} ChgOI"]  = int(q.get("oi_day_change", 0))
        rows[s][f"{t} Vol"]    = int(q.get("volume", 0))
        rows[s][f"{t} LTP"]    = float(q.get("last_price", 0.0))

    chain = pd.DataFrame(list(rows.values())).sort_values("Strike")

    # Max Pain
    s_arr = chain["Strike"].values
    ce_oi_arr = chain["CE OI"].values.astype(float)
    pe_oi_arr = chain["PE OI"].values.astype(float)
    import numpy as np
    pain = [(ce_oi_arr * np.maximum(0, s - s_arr)).sum() + (pe_oi_arr * np.maximum(0, s_arr - s)).sum() for s in s_arr]
    max_pain = float(s_arr[int(np.argmin(pain))])
    pcr = round(chain["PE OI"].sum() / max(1, chain["CE OI"].sum()), 2)
    ce_wall = float(chain.loc[chain["CE OI"].idxmax(), "Strike"])
    pe_wall = float(chain.loc[chain["PE OI"].idxmax(), "Strike"])

    print(f"\n{'='*80}")
    print(f"  📊 {symbol} LIVE OPTION CHAIN  |  Expiry: {target.strftime('%d %b %Y')}")
    print(f"{'='*80}")
    print(f"  Spot: {spot:,.0f}  ATM: {atm}  Max Pain: {max_pain:.0f}  PCR: {pcr}")
    print(f"  CE Wall: {ce_wall:.0f}  PE Wall: {pe_wall:.0f}  "
          f"Bias: {'🐂 Bullish' if pcr > 1.2 else '🐻 Bearish' if pcr < 0.8 else '↔ Neutral'}")
    print()
    print(f"  {'CE OI':>10}  {'CE ChOI':>8}  {'CE Vol':>8}  {'CE LTP':>7}  "
          f"{'Strike':>7}  {'PE LTP':>7}  {'PE Vol':>8}  {'PE ChOI':>8}  {'PE OI':>10}")
    print(f"  {'-'*88}")

    for _, row in chain.sort_values("Strike").iterrows():
        atm_marker = " ◀ ATM" if row["Strike"] == atm else ""
        print(f"  {row['CE OI']:>10,}  {row['CE ChgOI']:>+8,}  {row['CE Vol']:>8,}  {row['CE LTP']:>7.2f}  "
              f"{row['Strike']:>7.0f}  {row['PE LTP']:>7.2f}  {row['PE Vol']:>8,}  "
              f"{row['PE ChgOI']:>+8,}  {row['PE OI']:>10,}{atm_marker}")
    print()


if __name__ == "__main__":
    run(
        symbol=sys.argv[1].upper() if len(sys.argv) > 1 else "NIFTY",
        expiry_date=sys.argv[2] if len(sys.argv) > 2 else None,
    )
