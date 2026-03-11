from urllib.parse import quote_plus
import feedparser


INDEED_DOMAINS = {"tw": "tw.indeed.com", "jp": "jp.indeed.com", "sg": "sg.indeed.com"}


def build_rss_urls(sources: dict, markets: list[str], keyword: str) -> list[tuple[str, str, str]]:
    """Returns list of (url, source_name, market)."""
    kw = quote_plus(keyword)
    results = []
    if sources.get("104"):
        results.append((
            f"https://www.104.com.tw/jobs/search/rss?keyword={kw}&jobsource=2018indexpoc",
            "104", "tw"
        ))
    wellfound_markets = [m for m in markets if m in ("tw", "sg")]
    wellfound_locations = ["new-york", "san-francisco", "los-angeles", "remote"]
    if sources.get("wellfound") and wellfound_markets:
        loc_params = "&".join(f"locations[]={loc}" for loc in wellfound_locations)
        for market in wellfound_markets:
            results.append((
                f"https://wellfound.com/jobs.rss?keywords={kw}&{loc_params}",
                "wellfound", market
            ))
    if sources.get("indeed_rss"):
        for market in markets:
            domain = INDEED_DOMAINS.get(market)
            if domain:
                results.append((
                    f"https://{domain}/rss?q={kw}&sort=date",
                    "indeed", market
                ))
    return results


def parse_feed_entries(feed, source: str, market: str) -> list[dict]:
    entries = []
    for entry in feed.entries:
        entries.append({
            "title": entry.get("title", "").strip(),
            "url": entry.get("link", ""),
            "description": entry.get("summary", ""),
            "source": source,
            "market": market,
            "company": "",  # extracted by parser from description
            "location": "",
        })
    return entries


class RSSFetcher:
    def fetch_all(self, sources: dict, markets: list[str], titles: list[str]) -> list[dict]:
        raw = []
        for title in titles:
            for url, source, market in build_rss_urls(sources, markets, title):
                feed = feedparser.parse(url)
                raw.extend(parse_feed_entries(feed, source, market))
        return raw
