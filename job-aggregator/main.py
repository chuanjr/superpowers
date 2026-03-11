#!/usr/bin/env python3
"""Daily job aggregator — run via cron."""
import os
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()  # loads .env before any other imports that may read env vars

from config_loader import load_config, ConfigError
from state import load_seen_ids, save_seen_ids
from fetchers.gmail_fetcher import GmailFetcher, extract_html_body
from fetchers.gmail_parser import parse_gmail_message
from fetchers.rss_fetcher import RSSFetcher
from fetchers.scraper import WebScraper
from parser import parse_all
from deduplicator import deduplicate, remove_seen
from matcher import Matcher
from enricher import Enricher
from notifier import build_email_html, build_subject


def main():
    # 0. Validate required secrets are present (fail fast, never log values)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("[ERROR] ANTHROPIC_API_KEY not set.\nCopy .env.example to .env and add your key.")
        sys.exit(1)

    # 1. Load config
    try:
        cfg = load_config()
    except ConfigError as e:
        print(f"[ERROR] {e}\nRun: python setup_cli.py")
        sys.exit(1)

    sources = cfg["sources"]
    markets = cfg["markets"]
    targets = cfg["targets"]
    titles = targets["titles"]
    notif = cfg["notification"]

    # 2. Fetch
    raw_entries = []
    gmail = GmailFetcher()

    print("[1/6] Fetching Gmail alerts...")
    for msg in gmail.fetch_alert_messages(sources, days_back=1):
        html = extract_html_body(msg)
        # Determine source from sender header
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        sender = headers.get("From", "")
        source = "linkedin" if "linkedin" in sender else "indeed"
        market = next((m for m in markets if m in headers.get("Subject", "").lower()), markets[0])
        raw_entries.extend(parse_gmail_message(html, source=source, market=market))

    print("[2/6] Fetching RSS feeds...")
    rss = RSSFetcher()
    raw_entries.extend(rss.fetch_all(sources, markets, titles))

    print("[3/6] Scraping CakeResume / Yourator...")
    scraper = WebScraper()
    raw_entries.extend(scraper.fetch_all(sources, titles))

    # 3. Parse
    print("[4/6] Parsing and normalizing...")
    jobs = parse_all(raw_entries)

    # 4. Deduplicate (cross-platform + cross-day)
    jobs = deduplicate(jobs)
    seen_ids = load_seen_ids()
    jobs = remove_seen(jobs, seen_ids)

    # 5. Filter
    print(f"[5/6] Filtering {len(jobs)} new jobs with Claude Haiku...")
    matcher = Matcher()
    jobs = matcher.filter(jobs, targets)

    # 6. Enrich
    enricher = Enricher()
    jobs = enricher.enrich(jobs)

    # 7. Send email
    print(f"[6/6] Sending digest ({len(jobs)} matches)...")
    today = date.today()
    subject = build_subject(jobs, today)
    html = build_email_html(jobs, today)
    gmail.send_email(
        to=notif["to"],
        from_=notif["from"],
        subject=subject,
        html_body=html,
    )

    # 8. Update state
    new_seen = seen_ids | {j.id for j in jobs}
    save_seen_ids(new_seen)

    print(f"Done. {len(jobs)} matches sent to {notif['to']}")


if __name__ == "__main__":
    main()
