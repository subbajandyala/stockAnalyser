"""
Fundamental screener — price-action quality filter via Kite batch quotes.

Kite does not provide fundamental ratios (ROE, OPM, P/E, D/E).
This screener uses Kite /quote to apply price-based quality filters:
  - Pulled back >20% from 52-week high (buying opportunity in a quality stock)
  - LTP above zero and liquid (volume_traded > 0)
"""

import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

from screener.kite_hist import batch_quote_nse, kite_hdrs
import requests

_KITE_BASE = "https://api.kite.trade"


def _load_creds() -> tuple[str, str]:
    import os
    from pathlib import Path
    env: dict[str, str] = {}
    for p in [Path(__file__).parent.parent / ".env", Path(".env")]:
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
            break
    for k in ("KITE_API_KEY", "KITE_ACCESS_TOKEN"):
        if k in os.environ:
            env[k] = os.environ[k]
    return env.get("KITE_API_KEY", ""), env.get("KITE_ACCESS_TOKEN", "")


def fetch_fundamental_stocks(
    symbols_df: pd.DataFrame,
    max_workers: int = 20,
    progress_cb: Callable[[float], None] | None = None,
) -> pd.DataFrame:
    """
    Screen NIFTY 500 stocks using Kite batch quotes.
    Filters: pulled back >20% from 52W high, liquid (volume > 0).
    """
    api_key, access_token = _load_creds()
    if not api_key or not access_token:
        return pd.DataFrame()

    plain_syms = list(symbols_df["Symbol"].str.upper())
    total = len(plain_syms)

    # Batch-fetch in chunks of 400
    quotes: dict = {}
    chunk_size = 400
    done = 0
    for i in range(0, total, chunk_size):
        chunk = plain_syms[i : i + chunk_size]
        try:
            q = batch_quote_nse(api_key, access_token, chunk)
            quotes.update(q)
        except Exception:
            pass
        done += len(chunk)
        if progress_cb:
            progress_cb(done / total * 0.8)

    results: list[dict] = []
    sym_map = {r["Symbol"].upper(): r for _, r in symbols_df.iterrows()}

    for sym, row in sym_map.items():
        q = quotes.get(f"NSE:{sym}", {})
        if not q:
            continue

        ltp        = float(q.get("last_price") or 0)
        volume     = int(q.get("volume_traded") or 0)
        week52high = float(q.get("week_52_high") or 0)
        week52low  = float(q.get("week_52_low") or 0)
        ohlc       = q.get("ohlc") or {}
        day_open   = float(ohlc.get("open") or ltp)

        if ltp <= 0 or volume <= 0:
            continue
        if week52high <= 0:
            continue

        down_pct = (week52high - ltp) / week52high * 100
        if down_pct < 20:
            continue

        up_from_low = (ltp - week52low) / week52low * 100 if week52low > 0 else 0
        change_day  = (ltp - day_open) / day_open * 100 if day_open > 0 else 0

        results.append({
            "Symbol":       sym,
            "Company":      row["Company"],
            "NSE_Symbol":   row["NSE_Symbol"],
            "Price":        round(ltp, 2),
            "↓ 52W High %": round(down_pct, 1),
            "↑ 52W Low %":  round(up_from_low, 1),
            "Day Change %": round(change_day, 2),
            "52W High":     round(week52high, 2),
            "52W Low":      round(week52low, 2),
            "Volume":       volume,
        })

    if progress_cb:
        progress_cb(1.0)

    if not results:
        return pd.DataFrame()

    df = pd.DataFrame(results)
    df = df.sort_values("↓ 52W High %", ascending=False).reset_index(drop=True)
    return df
