"""
Banks & financials in F&O — OI / PCR and price structure from the OI monitor's saved snapshots.
Read-only: no Kite calls, no orders, no pushes, no change to any bot.

Per stock (latest snapshot of today vs yesterday's last one; ATM ±10 strikes, monthly expiry):
  PCR        put OI ÷ call OI, all saved strikes · PCR yday = the same at yesterday's close
  ATM PCR    the same on ATM ±3 strikes
  chg-OI PCR put OI added ÷ call OI added since yesterday (only when both were added)
  calls/puts near-ATM OI change % (the monitor's bands: calls ATM−1…+3, puts ATM−3…+1)
  fut        futures buildup today (price vs OI of the saved contract)
  structure  from the snapshots' spot range per day (approximate H / L, 09:20–15:30):
             above / below the previous 3 days' high / low, inside, and higher-highs / lower-lows
  trend      today's spot vs the average of the last 5 daily closes
  CE / PE    the monitor's own 6-check scores
  view       BULL when PCR ≥ 1 and rising, puts added / calls unwinding, price up, fut long buildup /
             short covering and price above the 3-day high or in an up-trend — BEAR the mirror.
             A count of agreeing points (0–6) is shown.

Usage:  /root/venv/bin/python scripts/fin_view.py            (Subba: /root/stockAnalyser, Praveen: /root/oimon)
"""

import datetime
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).parent))

import pandas as pd

import oi_monitor_bot as mon
from screener import oi_monitor as om

FIN = {
    # banks
    "HDFCBANK", "ICICIBANK", "SBIN", "KOTAKBANK", "AXISBANK", "INDUSINDBK", "BANKBARODA", "PNB", "CANBK",
    "AUBANK", "IDFCFIRSTB", "FEDERALBNK", "BANDHANBNK", "UNIONBANK", "INDIANB", "BANKINDIA", "RBLBANK",
    "YESBANK", "CUB",
    # NBFC / lenders / housing
    "BAJFINANCE", "BAJAJFINSV", "SHRIRAMFIN", "CHOLAFIN", "MUTHOOTFIN", "MANAPPURAM", "LICHSGFIN",
    "PNBHOUSING", "PFC", "RECLTD", "IREDA", "IRFC", "HUDCO", "ABCAPITAL", "LTF", "M&MFIN", "POONAWALLA",
    "SBICARD", "JIOFIN", "SAMMAANCAP", "BAJAJHFL", "IIFL",
    # insurance / AMC / capital markets / fintech
    "HDFCLIFE", "SBILIFE", "ICICIGI", "ICICIPRULI", "ICICILIFE", "LICI", "HDFCAMC", "NAM-INDIA", "360ONE",
    "ANGELONE", "BSE", "MCX", "CDSL", "CAMS", "KFINTECH", "NUVAMA", "PAYTM", "POLICYBZR",
}


def day_files() -> dict:
    out = {}
    for d in sorted(p for p in mon.DATA.iterdir() if p.is_dir() and p.name[:2] == "20"):
        f = [x for x in mon.snap_files(datetime.date.fromisoformat(d.name)) if "0920" <= x.name[:4] <= "1531"]
        if f:
            out[datetime.date.fromisoformat(d.name)] = f
    return out


def main() -> None:
    days = day_files()
    if not days:
        print(f"no snapshots in {mon.DATA}")
        return
    today = max(days)
    prev = [d for d in days if d < today]
    now = pd.read_csv(days[today][-1])
    base = pd.read_csv(days[prev[-1]][-1]) if prev else pd.read_csv(days[today][0])
    now, base = now[now["name"].isin(FIN)], base[base["name"].isin(FIN)]
    # approximate daily high / low / close of each stock from the snapshots' spot
    hist = {}
    for d in sorted(days)[-6:]:
        rows = []
        for f in days[d]:
            s = pd.read_csv(f, usecols=["name", "spot"])
            rows.append(s[s["name"].isin(FIN)].drop_duplicates("name"))
        g = pd.concat(rows).groupby("name")["spot"]
        hist[d] = pd.DataFrame({"hi": g.max(), "lo": g.min(), "cl": g.last()})
    an = {a["name"]: a for a in om.analyse_all(now, base, None) if "error" not in a}
    out = []
    for name, g in now.groupby("name"):
        a = an.get(name)
        if a is None:
            continue
        spot = float(g["spot"].iloc[0])
        b = base[base["name"] == name]
        c_oi, p_oi = g[g["type"] == "CE"]["oi"].sum(), g[g["type"] == "PE"]["oi"].sum()
        pcr = p_oi / c_oi if c_oi else None
        bc, bp = b[b["type"] == "CE"]["oi"].sum(), b[b["type"] == "PE"]["oi"].sum()
        pcr_y = bp / bc if bc else None
        strikes = sorted(g["strike"].unique())
        i = min(range(len(strikes)), key=lambda j: abs(strikes[j] - spot))
        near = g[g["strike"].isin(strikes[max(0, i - 3): i + 4])]
        nc, np_ = near[near["type"] == "CE"]["oi"].sum(), near[near["type"] == "PE"]["oi"].sum()
        m = g.merge(b[["strike", "type", "oi"]], on=["strike", "type"], how="left", suffixes=("", "_b"))
        m["d"] = m["oi"] - m["oi_b"].fillna(m["oi"])
        dc, dp = m[m["type"] == "CE"]["d"].sum(), m[m["type"] == "PE"]["d"].sum()
        coi = dp / dc if dc > 0 and dp > 0 else None
        past = [d for d in sorted(hist) if d < today][-3:]
        hh = max(hist[d].loc[name, "hi"] for d in past if name in hist[d].index) if past else None
        ll = min(hist[d].loc[name, "lo"] for d in past if name in hist[d].index) if past else None
        closes = [hist[d].loc[name, "cl"] for d in sorted(hist) if d < today and name in hist[d].index][-5:]
        his = [hist[d].loc[name, "hi"] for d in sorted(hist) if name in hist[d].index][-3:]
        los = [hist[d].loc[name, "lo"] for d in sorted(hist) if name in hist[d].index][-3:]
        if hh and spot > hh:
            struct = "above 3d high"
        elif ll and spot < ll:
            struct = "below 3d low"
        else:
            struct = "inside 3d range"
        if len(his) == 3 and his[0] < his[1] < his[2] and los[0] < los[1] < los[2]:
            struct += " · HH-HL"
        elif len(his) == 3 and his[0] > his[1] > his[2] and los[0] > los[1] > los[2]:
            struct += " · LH-LL"
        avg5 = sum(closes) / len(closes) if closes else None
        trend = None if not avg5 else (spot / avg5 - 1) * 100
        bull = [pcr is not None and pcr >= 1 and pcr_y is not None and pcr > pcr_y,
                a["put_pct"] > 0 and a["call_pct"] < 0,
                a["price_pct"] > 0,
                a["fut"] in ("long buildup", "short covering"),
                struct.startswith("above") or "HH-HL" in struct or (trend or 0) > 1,
                coi is not None and coi > 1]
        bear = [pcr is not None and pcr < 1 and pcr_y is not None and pcr < pcr_y,
                a["call_pct"] > 0 and a["put_pct"] < 0,
                a["price_pct"] < 0,
                a["fut"] in ("short buildup", "long unwinding"),
                struct.startswith("below") or "LH-LL" in struct or (trend or 0) < -1,
                coi is not None and coi < 1]
        nb, ns = sum(bull), sum(bear)
        view = f"BULL {nb}/6" if nb >= ns and nb >= 4 else f"BEAR {ns}/6" if ns > nb and ns >= 4 else f"mixed {nb}/{ns}"
        out.append({"stock": name, "spot": round(spot, 2), "day%": a["price_pct"],
                    "PCR": round(pcr, 2) if pcr else None, "PCR yday": round(pcr_y, 2) if pcr_y else None,
                    "ATM PCR": round(np_ / nc, 2) if nc else None, "chgOI PCR": round(coi, 2) if coi else None,
                    "calls%": a["call_pct"], "puts%": a["put_pct"], "fut": a["fut"],
                    "structure": struct, "vs 5d avg%": round(trend, 1) if trend is not None else None,
                    "CE": a["ce_score"], "PE": a["pe_score"], "view": view, "_rank": nb - ns})
    df = pd.DataFrame(out).sort_values(["_rank", "day%"], ascending=[False, False]).drop(columns="_rank")
    pd.set_option("display.width", 250)
    pd.set_option("display.max_rows", 200)
    print(f"Banks & financials · snapshot {today} {days[today][-1].name[:4]} vs {prev[-1] if prev else today} close · "
          f"{len(df)} stocks · history {', '.join(str(d)[5:] for d in sorted(hist))}\n")
    print(df.to_string(index=False))


if __name__ == "__main__":
    main()
