"""
Sharekhan SKAPI order placement.
Mirrors kite_order.place_order_for_signal() for dual-broker execution.
chart_bot calls this after a successful Kite order to mirror it on Sharekhan.
"""
import datetime
import json
import logging
import os
import pathlib
import re
import time

_LOG = logging.getLogger(__name__)

# Sharekhan exchange codes for F&O
_SK_EXCHANGE  = {"NIFTY": "NF", "BANKNIFTY": "NF", "SENSEX": "BF"}
_INSTR_TYPE   = {"NIFTY": "OI", "BANKNIFTY": "OI", "SENSEX": "OI"}  # OI = Option Index
_LOT_SIZE     = {"NIFTY": 75,   "BANKNIFTY": 35,   "SENSEX": 20}
_STRIKE_STEP  = {"NIFTY": 50,   "BANKNIFTY": 100,  "SENSEX": 100}

_MASTER_PATH  = pathlib.Path("/tmp/sk_scriptmaster_fno.json")
_MASTER_TTL   = 24 * 3600


# ── Credentials ───────────────────────────────────────────────────────────────

def _sk_creds() -> dict:
    keys = ["SHAREKHAN_API_KEY", "SHAREKHAN_SECURE_KEY",
            "SHAREKHAN_ACCESS_TOKEN", "SHAREKHAN_CUSTOMER_ID",
            "SHAREKHAN_LOGIN_ID"]
    vals = {k: os.environ.get(k, "") for k in keys}
    env_path = pathlib.Path(__file__).parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                k = k.strip()
                if k in vals and not vals[k]:
                    vals[k] = v.strip().strip('"').strip("'")
    return vals


def _update_env(key: str, value: str) -> None:
    env_path = pathlib.Path(__file__).parent.parent / ".env"
    text = env_path.read_text() if env_path.exists() else ""
    pattern = rf"^{key}=.*$"
    new_line = f"{key}={value}"
    if re.search(pattern, text, re.MULTILINE):
        text = re.sub(pattern, new_line, text, flags=re.MULTILINE)
    else:
        text = text.rstrip() + f"\n{new_line}\n"
    env_path.write_text(text)


# ── Client ────────────────────────────────────────────────────────────────────

def _sk_client(api_key: str, access_token: str):
    try:
        from SharekhanApi import SharekhanConnect
        return SharekhanConnect(api_key, access_token)
    except ImportError:
        _LOG.error("SharekhanApi not found — run: pip install shareconnect")
        return None


# ── Script master (instrument → scripCode lookup) ─────────────────────────────

def _load_master(client) -> list:
    if _MASTER_PATH.exists():
        if time.time() - _MASTER_PATH.stat().st_mtime < _MASTER_TTL:
            try:
                return json.loads(_MASTER_PATH.read_text())
            except Exception:
                pass
    try:
        master = client.getScriptMaster("FNO")
        if isinstance(master, list) and master:
            _MASTER_PATH.write_text(json.dumps(master))
            return master
    except Exception as e:
        _LOG.warning(f"Script master fetch failed: {e}")
    return []


def _find_scrip(master: list, symbol: str, strike: int,
                opt_type: str, expiry_dt: datetime.date) -> dict | None:
    expiry_variants = {
        expiry_dt.strftime("%d/%m/%Y"),
        expiry_dt.strftime("%d-%m-%Y"),
        expiry_dt.strftime("%Y-%m-%d"),
    }
    for s in master:
        ts  = str(s.get("tradingSymbol") or s.get("TradingSymbol") or "").upper()
        st  = str(s.get("strikePrice")   or s.get("StrikePrice")   or "")
        ot  = str(s.get("optionType")    or s.get("OptionType")     or "").upper()
        exp = str(s.get("expiry")        or s.get("Expiry")         or "")
        if (symbol in ts and str(strike) in st
                and ot == opt_type.upper()
                and exp in expiry_variants):
            return s
    return None


# ── Strike / expiry helpers ───────────────────────────────────────────────────

def _atm_strike(spot: float, symbol: str) -> int:
    step = _STRIKE_STEP.get(symbol, 50)
    return int(round(spot / step) * step)


def _nearest_expiry(symbol: str) -> datetime.date:
    """Nearest weekly expiry: Thursday for all index options."""
    target_weekday = 3  # Thu=3
    today = datetime.date.today()
    days  = (target_weekday - today.weekday()) % 7
    if days == 0 and datetime.datetime.now().hour >= 15:
        days = 7
    return today + datetime.timedelta(days=days)


# ── Main order function ───────────────────────────────────────────────────────

def place_sk_order(
    symbol: str,
    direction: str,
    spot: float,
    option_ltp: float = 0.0,
) -> dict:
    """
    Place ATM option BUY on Sharekhan.
    direction: "CE" or "PE" (or "CALL"/"PUT")
    option_ltp: Kite LTP of the option — used as limit price if available.
    Returns dict with order_id and details, or {"error": "..."}.
    """
    creds = _sk_creds()
    api_key      = creds["SHAREKHAN_API_KEY"]
    access_token = creds["SHAREKHAN_ACCESS_TOKEN"]
    customer_id  = creds["SHAREKHAN_CUSTOMER_ID"]
    login_id     = creds["SHAREKHAN_LOGIN_ID"] or customer_id

    if not (api_key and access_token):
        return {"error": "Sharekhan creds not set (SHAREKHAN_API_KEY / SHAREKHAN_ACCESS_TOKEN in .env)"}

    if symbol not in _SK_EXCHANGE:
        return {"error": f"Sharekhan: unsupported symbol {symbol}"}

    client = _sk_client(api_key, access_token)
    if client is None:
        return {"error": "shareconnect not installed"}

    opt_type    = "CE" if any(x in direction.upper() for x in ("CE", "CALL")) else "PE"
    strike      = _atm_strike(spot, symbol)
    expiry_dt   = _nearest_expiry(symbol)
    expiry_str  = expiry_dt.strftime("%d/%m/%Y")
    exchange    = _SK_EXCHANGE[symbol]
    lot         = _LOT_SIZE[symbol]
    instr_type  = _INSTR_TYPE[symbol]

    # Look up scripCode from master
    master      = _load_master(client)
    scrip_obj   = _find_scrip(master, symbol, strike, opt_type, expiry_dt)

    if scrip_obj:
        scrip_code     = scrip_obj.get("scripCode") or scrip_obj.get("ScripCode") or 0
        trading_symbol = scrip_obj.get("tradingSymbol") or scrip_obj.get("TradingSymbol") or ""
    else:
        scrip_code     = 0
        trading_symbol = f"{symbol}{expiry_dt.strftime('%d%b%y').upper()}{strike}{opt_type}"
        _LOG.warning(
            f"[Sharekhan] scripCode not found for {symbol} {strike} {opt_type} {expiry_str} "
            f"— attempting with scripCode=0"
        )

    # Limit price: use Kite LTP + 2% buffer; fall back to market if LTP unknown
    if option_ltp > 0:
        price = str(round(round(option_ltp * 1.02 / 0.05) * 0.05, 2))
    else:
        price = "0"

    order_params = {
        "customerId":      customer_id,
        "scripCode":       scrip_code,
        "tradingSymbol":   trading_symbol,
        "exchange":        exchange,
        "transactionType": "B",
        "quantity":        lot,
        "disclosedQty":    0,
        "price":           price,
        "triggerPrice":    "0",
        "rmsCode":         "ANY",
        "afterHour":       "N",
        "orderType":       "NORMAL",
        "channelUser":     login_id,
        "validity":        "GFD",
        "requestType":     "NEW",
        "productType":     "INVESTMENT",
        "instrumentType":  instr_type,
        "strikePrice":     str(strike),
        "optionType":      opt_type,
        "expiry":          expiry_str,
    }

    _LOG.info(f"[Sharekhan] Placing: BUY {lot} x {trading_symbol} @ {price}")

    try:
        result   = client.placeOrder(order_params)
        order_id = (result.get("orderId") or result.get("OrderId")
                    or result.get("order_id") or result.get("BrokerOrderId") or "")
        _LOG.info(f"[Sharekhan] Order placed — ID: {order_id}")
        return {
            "order_id":      str(order_id),
            "tradingsymbol": trading_symbol,
            "strike":        strike,
            "opt_type":      opt_type,
            "expiry":        expiry_str,
            "qty":           lot,
            "price":         price,
        }
    except Exception as e:
        _LOG.error(f"[Sharekhan] placeOrder failed: {e}")
        return {"error": str(e)}
