from pathlib import Path
from unittest.mock import patch, MagicMock
from fetchers.rss_fetcher import RSSFetcher, build_rss_urls

def test_build_rss_urls_includes_enabled_sources():
    sources = {"104": True, "wellfound": True, "indeed_rss": True, "yourator": False}
    markets = ["tw", "sg"]
    urls = build_rss_urls(sources, markets, keyword="Backend Engineer")
    assert any("104" in u for u in urls)
    assert any("wellfound" in u for u in urls)
    assert not any("yourator" in u for u in urls)

def test_build_rss_urls_indeed_per_market():
    sources = {"indeed_rss": True}
    markets = ["tw", "jp", "sg"]
    urls = build_rss_urls(sources, markets, keyword="Backend Engineer")
    assert any("tw.indeed" in u for u in urls)
    assert any("jp.indeed" in u for u in urls)
    assert any("sg.indeed" in u for u in urls)

def test_parse_feed_from_fixture():
    from fetchers.rss_fetcher import parse_feed_entries
    import feedparser
    content = Path("tests/fixtures/104_rss.xml").read_bytes()
    feed = feedparser.parse(content)
    entries = parse_feed_entries(feed, source="104", market="tw")
    assert len(entries) == 1
    assert entries[0]["title"] == "後端工程師 (Backend Engineer)"
    assert entries[0]["source"] == "104"
    assert entries[0]["market"] == "tw"
