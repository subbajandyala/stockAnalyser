"""
OI Buildup History CLI — pull daily OI per strike from Kite historical API.
Usage: python kite_oi_history.py [SYMBOL] [EXPIRY_DATE YYYY-MM-DD] [FROM] [TO]
"""
import sys, datetime, json
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

import pandas as pd
import requests
from io import StringIO
import kite_creds

BASE = "https://api.kite.trade"

TICK = {"NIFTY": 50, "BANKNIFTY": 100, "FINNIFTY": 50, "MIDCPNIFTY": 25, "SENSEX": 100}
EXCH = {"NIFTY": "NFO", "BANKNIFTY": "NFO", "FINNIFTY": "NFO", "MIDCPNIFTY": "NFO", "SENSEX": "BFO"}
SPOT = {"NIFTY": "NSE:NIFTY 50", "BANKNIFTY": "NSE:NIFTY BANK", "FINNIFTY": "NSE:NIFTY FIN SERVICE",
        "MIDCPNIFTY": "NSE:NIFTY MID SELECT", "SENSEX": "BSE:SENSEX"}


def run(symbol="NIFTY", expiry_date=None, from_date=None, to_date=None, strikes_around=18):
    api_key, access_token = kite_creds.load()
    hdrs = {"X-Kite-Version": "3", "Authorization": f"token {api_key}:{access_token}"}
    exch = EXCH.get(symbol, "NFO")

    # Instruments
    r = requests.get(f"{BASE}/instruments/{exch}", headers=hdrs, timeout=30)
    r.raise_for_status()
    instr = pd.read_csv(StringIO(r.text))
    instr["expiry_dt"] = pd.to_datetime(instr["expiry"], errors="coerce")

    nifty_opts = instr[(instr["name"] == symbol) & (instr["instrument_type"].isin(["CE", "PE"]))]

    # Resolve expiry
    if expiry_date:
        target = pd.Timestamp(expiry_date)
    else:
        today = datetime.date.today()
        future = nifty_opts[nifty_opts["expiry_dt"].dt.date >= today]
        target = future["expiry_dt"].min()

    opts = nifty_opts[nifty_opts["expiry_dt"] == target].copy()
    if opts.empty:
        print(f"No options found for {symbol} expiry {target.date()}")
        return

    # Spot
    ltp_r = requests.get(f"{BASE}/quote/ltp", headers=hdrs, params={"i": SPOT[symbol]}, timeout=10)
    ltp_r.raise_for_status()
    spot = float(ltp_r.json()["data"][SPOT[symbol]]["last_price"])
    tick = TICK[symbol]
    atm  = round(spot / tick) * tick

    # Filter strikes around ATM
    opts = opts[abs(opts["strike"] - atm) <= strikes_around * tick].copy()

    # Date range
    to_dt   = pd.Timestamp(to_date) if to_date else pd.Timestamp.now()
    from_dt = pd.Timestamp(from_date) if from_date else to_dt - pd.Timedelta(days=14)
    from_str, to_str = from_dt.strftime("%Y-%m-%d"), to_dt.strftime("%Y-%m-%d")

    print(f"\n{'='*60}")
    print(f"  {symbol} OI BUILDUP HISTORY  |  Expiry: {target.strftime('%d %b %Y')}")
    print(f"  Spot: {spot:,.0f}  |  ATM: {atm}  |  Range: {from_str} → {to_str}")
    print(f"{'='*60}\n")

    meta = opts.set_index("instrument_token")[["strike", "instrument_type", "tradingsymbol"]].to_dict("index")
    records = []
    tokens  = opts["instrument_token"].tolist()

    for token in tokens:
        m = meta[token]
        try:
            hr = requests.get(
                f"{BASE}/instruments/historical/{token}/day",
                headers=hdrs, params={"from": from_str, "to": to_str, "oi": "1"}, timeout=20,
            )
            if not hr.ok:
                continue
            for c in hr.json().get("data", {}).get("candles", []):
                if len(c) >= 7:
                    records.append({
                        "date":   pd.Timestamp(c[0]).date(),
                        "strike": int(m["strike"]),
                        "type":   m["instrument_type"],
                        "ltp":    float(c[4]),
                        "volume": int(c[5]),
                        "oi":     int(c[6]),
                    })
        except Exception:
            continue

    if not records:
        print("No data returned. Check credentials / date range.")
        return

    daily = pd.DataFrame(records)
    dates = sorted(daily["date"].unique())
    first_dt, last_dt = dates[0], dates[-1]

    rows = []
    for (strike, otype), grp in daily.groupby(["strike", "type"]):
        grp = grp.sort_values("date")
        oi_s = int(grp[grp["date"] == first_dt]["oi"].values[0]) if first_dt in grp["date"].values else 0
        oi_e = int(grp[grp["date"] == last_dt]["oi"].values[-1]) if last_dt in grp["date"].values else 0
        ltp_s = float(grp[grp["date"] == first_dt]["ltp"].values[0]) if first_dt in grp["date"].values else 0
        ltp_e = float(grp[grp["date"] == last_dt]["ltp"].values[-1]) if last_dt in grp["date"].values else 0
        oi_chg = oi_e - oi_s
        ltp_chg = ltp_e - ltp_s

        if oi_chg > 0 and ltp_chg > 0:    sig = "📈 Long Buildup"
        elif oi_chg > 0 and ltp_chg < 0:  sig = "🐻 Short Buildup"
        elif oi_chg < 0 and ltp_chg > 0:  sig = "🔼 Short Covering"
        elif oi_chg < 0 and ltp_chg < 0:  sig = "🔽 Long Unwinding"
        else:                               sig = "➖ Neutral"

        rows.append({"Strike": strike, "Type": otype, "OI Start": oi_s, "OI End": oi_e,
                     "Net OI Change": oi_chg, "LTP Start": round(ltp_s, 1), "LTP End": round(ltp_e, 1),
                     "Signal": sig})

    df = pd.DataFrame(rows).sort_values(["Type", "Net OI Change"], ascending=[True, False])

    # --- Top CE writers (resistance)
    ce = df[df["Type"] == "CE"].copy()
    pe = df[df["Type"] == "PE"].copy()

    print("🔴 TOP CE OI BUILD (RESISTANCE WALLS)")
    print("-" * 60)
    ce_top = ce[ce["Net OI Change"] > 0].head(10)
    for _, r in ce_top.iterrows():
        bar = "█" * min(20, max(1, int(r["Net OI Change"] / 100000)))
        print(f"  {r['Strike']:>6}CE  OI+{r['Net OI Change']/1e6:.2f}M  LTP {r['LTP Start']}→{r['LTP End']}  {bar}")

    print(f"\n🟢 TOP PE OI BUILD (SUPPORT WALLS)")
    print("-" * 60)
    pe_top = pe[pe["Net OI Change"] > 0].head(10)
    for _, r in pe_top.iterrows():
        bar = "█" * min(20, max(1, int(r["Net OI Change"] / 100000)))
        print(f"  {r['Strike']:>6}PE  OI+{r['Net OI Change']/1e6:.2f}M  LTP {r['LTP Start']}→{r['LTP End']}  {bar}")

    # Signals summary
    print(f"\n📊 SIGNAL SUMMARY")
    print("-" * 60)
    for sig_type in ["📈 Long Buildup", "🐻 Short Buildup", "🔼 Short Covering", "🔽 Long Unwinding"]:
        matches = df[df["Signal"] == sig_type][["Strike", "Type"]].values.tolist()
        if matches:
            names = ", ".join(f"{s}{t}" for s, t in matches[:8])
            print(f"  {sig_type}: {names}")

    ce_added = ce[ce["Net OI Change"] > 0]["Net OI Change"].sum()
    pe_added = pe[pe["Net OI Change"] > 0]["Net OI Change"].sum()
    bias = "🐂 BULLISH" if pe_added > ce_added else "🐻 BEARISH"
    print(f"\n  CE OI Added: {ce_added/1e6:.2f}M  |  PE OI Added: {pe_added/1e6:.2f}M  |  Bias: {bias}")

    # Max pain estimate
    ce_oi = daily[daily["type"] == "CE"].groupby("strike")["oi"].last()
    pe_oi = daily[daily["type"] == "PE"].groupby("strike")["oi"].last()
    all_strikes = sorted(set(ce_oi.index) | set(pe_oi.index))
    pain = []
    for s in all_strikes:
        c = sum(ce_oi.get(k, 0) * max(0, k - s) for k in all_strikes)
        p = sum(pe_oi.get(k, 0) * max(0, s - k) for k in all_strikes)
        pain.append(c + p)
    if pain:
        max_pain_strike = all_strikes[pain.index(min(pain))]
        print(f"  Max Pain: {max_pain_strike}")

    print(f"\n  Range likely: {int(min(pe_top['Strike'].max() if not pe_top.empty else atm, atm))} → "
          f"{int(max(ce_top['Strike'].min() if not ce_top.empty else atm, atm))}")
    print()


if __name__ == "__main__":
    args = sys.argv[1:]
    run(
        symbol=args[0].upper() if len(args) > 0 else "NIFTY",
        expiry_date=args[1] if len(args) > 1 else None,
        from_date=args[2] if len(args) > 2 else None,
        to_date=args[3] if len(args) > 3 else None,
    )
