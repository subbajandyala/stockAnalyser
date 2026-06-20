import requests
import pandas as pd
import io

NSE_NIFTY500_URL = "https://archives.nseindia.com/content/indices/ind_nifty500list.csv"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": "https://www.nseindia.com/",
}

# Fallback: top 100 NIFTY stocks if NSE fetch fails
FALLBACK_SYMBOLS = [
    ("RELIANCE", "Reliance Industries Limited"),
    ("TCS", "Tata Consultancy Services Limited"),
    ("HDFCBANK", "HDFC Bank Limited"),
    ("INFY", "Infosys Limited"),
    ("ICICIBANK", "ICICI Bank Limited"),
    ("HINDUNILVR", "Hindustan Unilever Limited"),
    ("ITC", "ITC Limited"),
    ("SBIN", "State Bank of India"),
    ("BHARTIARTL", "Bharti Airtel Limited"),
    ("KOTAKBANK", "Kotak Mahindra Bank Limited"),
    ("LT", "Larsen & Toubro Limited"),
    ("AXISBANK", "Axis Bank Limited"),
    ("ASIANPAINT", "Asian Paints Limited"),
    ("MARUTI", "Maruti Suzuki India Limited"),
    ("SUNPHARMA", "Sun Pharmaceutical Industries Limited"),
    ("WIPRO", "Wipro Limited"),
    ("ULTRACEMCO", "UltraTech Cement Limited"),
    ("TITAN", "Titan Company Limited"),
    ("BAJFINANCE", "Bajaj Finance Limited"),
    ("NESTLEIND", "Nestle India Limited"),
    ("TECHM", "Tech Mahindra Limited"),
    ("NTPC", "NTPC Limited"),
    ("POWERGRID", "Power Grid Corporation of India Limited"),
    ("ONGC", "Oil & Natural Gas Corporation Limited"),
    ("HCLTECH", "HCL Technologies Limited"),
    ("BAJAJFINSV", "Bajaj Finserv Limited"),
    ("M&M", "Mahindra & Mahindra Limited"),
    ("ADANIENT", "Adani Enterprises Limited"),
    ("ADANIPORTS", "Adani Ports and Special Economic Zone Limited"),
    ("COALINDIA", "Coal India Limited"),
    ("JSWSTEEL", "JSW Steel Limited"),
    ("TATAMOTORS", "Tata Motors Limited"),
    ("TATASTEEL", "Tata Steel Limited"),
    ("INDUSINDBK", "IndusInd Bank Limited"),
    ("GRASIM", "Grasim Industries Limited"),
    ("CIPLA", "Cipla Limited"),
    ("DRREDDY", "Dr. Reddy's Laboratories Limited"),
    ("EICHERMOT", "Eicher Motors Limited"),
    ("BRITANNIA", "Britannia Industries Limited"),
    ("DIVISLAB", "Divi's Laboratories Limited"),
    ("HEROMOTOCO", "Hero MotoCorp Limited"),
    ("TATACONSUM", "Tata Consumer Products Limited"),
    ("APOLLOHOSP", "Apollo Hospitals Enterprise Limited"),
    ("BPCL", "Bharat Petroleum Corporation Limited"),
    ("IOC", "Indian Oil Corporation Limited"),
    ("HINDALCO", "Hindalco Industries Limited"),
    ("UPL", "UPL Limited"),
    ("BAJAJ-AUTO", "Bajaj Auto Limited"),
    ("SHREECEM", "Shree Cement Limited"),
    ("SBILIFE", "SBI Life Insurance Company Limited"),
    ("HDFCLIFE", "HDFC Life Insurance Company Limited"),
    ("VEDL", "Vedanta Limited"),
    ("AMBUJACEM", "Ambuja Cements Limited"),
    ("BANKBARODA", "Bank of Baroda"),
    ("PNB", "Punjab National Bank"),
    ("CANBK", "Canara Bank"),
    ("INDIGO", "InterGlobe Aviation Limited"),
    ("ZOMATO", "Zomato Limited"),
    ("PAYTM", "One97 Communications Limited"),
    ("NYKAA", "FSN E-Commerce Ventures Limited"),
    ("DMART", "Avenue Supermarts Limited"),
    ("PIDILITIND", "Pidilite Industries Limited"),
    ("BERGEPAINT", "Berger Paints India Limited"),
    ("DABUR", "Dabur India Limited"),
    ("MARICO", "Marico Limited"),
    ("COLPAL", "Colgate-Palmolive (India) Limited"),
    ("GODREJCP", "Godrej Consumer Products Limited"),
    ("MCDOWELL-N", "United Spirits Limited"),
    ("HAVELLS", "Havells India Limited"),
    ("VOLTAS", "Voltas Limited"),
    ("WHIRLPOOL", "Whirlpool of India Limited"),
    ("SIEMENS", "Siemens Limited"),
    ("ABB", "ABB India Limited"),
    ("CUMMINSIND", "Cummins India Limited"),
    ("BOSCHLTD", "Bosch Limited"),
    ("ESCORTS", "Escorts Kubota Limited"),
    ("TVSMOTOR", "TVS Motor Company Limited"),
    ("BALKRISIND", "Balkrishna Industries Limited"),
    ("MRF", "MRF Limited"),
    ("APOLLOTYRE", "Apollo Tyres Limited"),
    ("EXIDEIND", "Exide Industries Limited"),
    ("AUROPHARMA", "Aurobindo Pharma Limited"),
    ("BIOCON", "Biocon Limited"),
    ("TORNTPHARM", "Torrent Pharmaceuticals Limited"),
    ("ALKEM", "Alkem Laboratories Limited"),
    ("LUPIN", "Lupin Limited"),
    ("GLENMARK", "Glenmark Pharmaceuticals Limited"),
    ("IPCALAB", "IPCA Laboratories Limited"),
    ("LICHSGFIN", "LIC Housing Finance Limited"),
    ("MUTHOOTFIN", "Muthoot Finance Limited"),
    ("CHOLAFIN", "Cholamandalam Investment and Finance Company Limited"),
    ("RECLTD", "REC Limited"),
    ("PFC", "Power Finance Corporation Limited"),
    ("IRCTC", "Indian Railway Catering and Tourism Corporation Limited"),
    ("HAL", "Hindustan Aeronautics Limited"),
    ("BEL", "Bharat Electronics Limited"),
    ("BHEL", "Bharat Heavy Electricals Limited"),
    ("SAIL", "Steel Authority of India Limited"),
    ("NMDC", "NMDC Limited"),
]


def get_nifty500_symbols() -> pd.DataFrame:
    try:
        resp = requests.get(NSE_NIFTY500_URL, headers=HEADERS, timeout=10)
        resp.raise_for_status()
        df = pd.read_csv(io.StringIO(resp.text))
        df = df[["Symbol", "Company Name"]].dropna()
        df.columns = ["Symbol", "Company"]
        df["NSE_Symbol"] = df["Symbol"].str.strip() + ".NS"
        return df.reset_index(drop=True)
    except Exception:
        df = pd.DataFrame(FALLBACK_SYMBOLS, columns=["Symbol", "Company"])
        df["NSE_Symbol"] = df["Symbol"].str.strip() + ".NS"
        return df
