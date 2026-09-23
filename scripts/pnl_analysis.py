"""Run on droplet: /root/venv/bin/python scripts/pnl_analysis.py"""
import json, datetime, pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
from kiteconnect import KiteConnect
import pandas as pd

creds = json.loads(pathlib.Path.home().joinpath(".kite_creds.json").read_text())
kite = KiteConnect(api_key=creds["api_key"])
kite.set_access_token(creds["access_token"])

# Today's trades (from order book screenshots)
TRADES = [
    # label, exchange, name, strike, opt_type, entry, entry_time, qty
    ("NIFTY 23400 CE",  "NFO", "NIFTY",  23400.0, "CE", 125.05, "09:45", 65),
    ("SENSEX 74700 CE", "BFO", "SENSEX", 74700.0, "CE", 238.90, "10:00", 20),
]

today = datetime.date.today()
ist   = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
from_dt = datetime.datetime.combine(today, datetime.time(9, 15), tzinfo=ist)
to_dt   = datetime.datetime.combine(today, datetime.time(15, 30), tzinfo=ist)

print(f"\n{'='*65}")
print(f"P&L ANALYSIS — {today}  (SL=-30%  Target=+60%)")
print(f"{'='*65}")

total_pnl = 0

for label, exch, name, strike, opt_type, entry, entry_time, qty in TRADES:
    # Resolve instrument token
    instr = pd.DataFrame(kite.instruments(exch))
    row = instr[
        (instr["name"] == name) &
        (instr["strike"].astype(float) == float(strike)) &
        (instr["instrument_type"] == opt_type) &
        (pd.to_datetime(instr["expiry"]).dt.date >= today)
    ].sort_values("expiry")

    if row.empty:
        print(f"\n{label}: instrument not found on {exch}")
        continue

    token = int(row.iloc[0]["instrument_token"])
    ts    = row.iloc[0]["tradingsymbol"]

    candles = kite.historical_data(token, from_dt, to_dt, "5minute")
    df = pd.DataFrame(candles)
    if df.empty:
        print(f"\n{label}: no historical data")
        continue

    df["time"] = pd.to_datetime(df["date"]).dt.strftime("%H:%M")

    sl     = round(entry * 0.70, 2)
    target = round(entry * 1.60, 2)

    # Only look at candles from entry time onwards
    entry_idx = df[df["time"] >= entry_time].index
    if entry_idx.empty:
        df_after = df
    else:
        df_after = df.loc[entry_idx[0]:]

    hit_sl  = df_after[df_after["low"]  <= sl]
    hit_tgt = df_after[df_after["high"] >= target]
    close   = df.iloc[-1]["close"]

    if not hit_sl.empty and not hit_tgt.empty:
        outcome = "SL HIT" if hit_sl.index[0] <= hit_tgt.index[0] else "TARGET HIT"
    elif not hit_sl.empty:
        outcome = "SL HIT"
    elif not hit_tgt.empty:
        outcome = "TARGET HIT"
    else:
        outcome = "OPEN (closed at market)"

    exit_price = sl if "SL" in outcome else (target if "TARGET" in outcome else close)
    pnl = round((exit_price - entry) * qty, 2)
    total_pnl += pnl

    sl_time  = hit_sl.iloc[0]["time"]  if not hit_sl.empty  else "—"
    tgt_time = hit_tgt.iloc[0]["time"] if not hit_tgt.empty else "—"

    print(f"\n{label} ({ts})")
    print(f"  Entry : ₹{entry} @ {entry_time}  |  Qty: {qty}")
    print(f"  SL    : ₹{sl}  ({sl_time})")
    print(f"  Target: ₹{target}  ({tgt_time})")
    print(f"  Close : ₹{close}")
    print(f"  Result: {outcome}")
    print(f"  P&L   : ₹{pnl:+.2f}")
    print()
    print(df[["time","open","high","low","close"]].to_string(index=False))

print(f"\n{'='*65}")
print(f"TOTAL P&L TODAY: ₹{total_pnl:+.2f}")
print(f"{'='*65}")
