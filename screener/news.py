import feedparser
import pandas as pd
from datetime import datetime, timedelta, timezone
import re
import time

RSS_FEEDS = [
    ("Economic Times Markets", "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms"),
    ("Economic Times News", "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
    ("MoneyControl Buzzing", "https://www.moneycontrol.com/rss/buzzingstocks.xml"),
    ("MoneyControl News", "https://www.moneycontrol.com/rss/latestnews.xml"),
    ("LiveMint Markets", "https://www.livemint.com/rss/markets"),
    ("Business Standard Markets", "https://www.business-standard.com/rss/markets-106.rss"),
    ("Financial Express Markets", "https://www.financialexpress.com/market/feed/"),
]


def _parse_entry_date(entry) -> datetime:
    for attr in ("published_parsed", "updated_parsed"):
        t = getattr(entry, attr, None)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def fetch_news_items(days: int = 14) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    items = []
    for source, url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries:
                pub_date = _parse_entry_date(entry)
                if pub_date < cutoff:
                    continue
                title = getattr(entry, "title", "") or ""
                summary = getattr(entry, "summary", "") or ""
                items.append({
                    "source": source,
                    "title": title,
                    "summary": re.sub(r"<[^>]+>", " ", summary),
                    "date": pub_date,
                })
        except Exception:
            continue
    return items


def get_trending_stocks(symbols_df: pd.DataFrame, days: int = 14) -> list[dict]:
    news_items = fetch_news_items(days=days)
    if not news_items:
        return []

    # Build matching patterns: longer names first to avoid partial matches
    symbol_rows = symbols_df.to_dict("records")
    symbol_rows.sort(key=lambda r: len(r["Company"]), reverse=True)

    mention_count: dict[str, int] = {}
    mention_articles: dict[str, list[str]] = {}

    for item in news_items:
        text = (item["title"] + " " + item["summary"]).upper()
        matched_this_article: set[str] = set()

        for row in symbol_rows:
            sym = row["Symbol"]
            if sym in matched_this_article:
                continue

            company_upper = row["Company"].upper()
            # Match by company name (first two words for brevity) or raw symbol
            first_two = " ".join(company_upper.split()[:2])
            patterns = [company_upper, first_two, f" {sym} ", f"({sym})"]

            for pat in patterns:
                if pat in text:
                    matched_this_article.add(sym)
                    mention_count[sym] = mention_count.get(sym, 0) + 1
                    if sym not in mention_articles:
                        mention_articles[sym] = []
                    if item["title"] not in mention_articles[sym]:
                        mention_articles[sym].append(item["title"])
                    break

    if not mention_count:
        return []

    results = []
    for row in symbol_rows:
        sym = row["Symbol"]
        if sym in mention_count:
            results.append({
                "Symbol": sym,
                "NSE_Symbol": row["NSE_Symbol"],
                "Company": row["Company"],
                "News_Mentions": mention_count[sym],
                "Headlines": mention_articles.get(sym, [])[:3],
            })

    results.sort(key=lambda x: x["News_Mentions"], reverse=True)
    return results
