from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote_plus
import calendar
import re
import time
import feedparser


INDEED_DOMAINS = {"tw": "tw.indeed.com", "jp": "jp.indeed.com", "sg": "sg.indeed.com", "us": "www.indeed.com"}

_WELLFOUND_LOCATIONS: dict[str, list[str]] = {
    "tw": ["taiwan", "taipei", "remote"],
    "sg": ["singapore", "remote"],
    "jp": ["japan", "tokyo", "remote"],
    "us": ["united+states", "san+francisco", "new+york", "remote"],
}


def build_rss_urls(sources: dict, markets: list[str], keyword: str) -> list[tuple[str, str, str]]:
    """Returns list of (url, source_name, market)."""
    kw = quote_plus(keyword)
    results = []
    if sources.get("104"):
        results.append((
            f"https://www.104.com.tw/jobs/search/rss?keyword={kw}&jobsource=2018indexpoc",
            "104", "tw"
        ))
    wellfound_markets = [m for m in markets if m in _WELLFOUND_LOCATIONS]
    if sources.get("wellfound") and wellfound_markets:
        for market in wellfound_markets:
            locs = _WELLFOUND_LOCATIONS[market]
            loc_params = "&".join(f"locations[]={loc}" for loc in locs)
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


def _clean_description(raw: str) -> str:
    """Strip HTML tags and collapse whitespace."""
    text = re.sub(r"<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", text).strip()


def _parse_published(entry) -> str | None:
    """Return ISO-8601 UTC string from feedparser's published_parsed / updated_parsed, or None."""
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    if t:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", t)
    return None


def parse_feed_entries(feed, source: str, market: str) -> list[dict]:
    entries = []
    for entry in feed.entries:
        entries.append({
            "title": entry.get("title", "").strip(),
            "url": entry.get("link", ""),
            "description": _clean_description(entry.get("summary", "")),
            "source": source,
            "market": market,
            "company": entry.get("author", "").strip(),
            "location": "",
            "posted_at": _parse_published(entry),
        })
    return entries


def _fetch_one(args: tuple) -> list[dict]:
    url, source, market = args
    try:
        feed = feedparser.parse(url)
        return parse_feed_entries(feed, source, market)
    except Exception as e:
        print(f"[WARN] RSS fetch failed ({source}/{market}): {e}")
        return []


class RSSFetcher:
    def fetch_all(self, sources: dict, markets: list[str], titles: list[str]) -> list[dict]:
        all_urls = []
        for title in titles:
            all_urls.extend(build_rss_urls(sources, markets, title))

        with ThreadPoolExecutor(max_workers=8) as pool:
            batches = pool.map(_fetch_one, all_urls)

        return [item for batch in batches for item in batch]
