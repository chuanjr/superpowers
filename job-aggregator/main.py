#!/usr/bin/env python3
"""Daily job aggregator — run via cron."""
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from dotenv import load_dotenv
load_dotenv()

from config_loader import load_config, ConfigError
from state import load_seen_ids, save_seen_ids
from fetchers.gmail_fetcher import GmailFetcher, extract_html_body
from fetchers.gmail_parser import parse_gmail_message
from fetchers.rss_fetcher import RSSFetcher
from fetchers.scraper import WebScraper
from parser import parse_all
from deduplicator import deduplicate, remove_seen
from notifier import build_email_html, build_subject
from models import Job


def _rule_filter(jobs: list[Job], targets: dict) -> list[Job]:
    """Keep jobs whose title contains at least one keyword from targets.titles,
    and reject any job whose title or description contains an exclude_keyword."""
    title_keywords = [kw.lower() for t in targets.get("titles", []) for kw in t.split()]
    exclude = [kw.lower() for kw in targets.get("exclude_keywords", [])]

    kept = []
    for job in jobs:
        t = job.title.lower()
        d = job.description.lower()
        if any(ex in t or ex in d for ex in exclude):
            continue
        if any(kw in t for kw in title_keywords):
            kept.append(job)
    return kept


def _fetch_gmail(gmail: GmailFetcher, sources: dict, markets: list[str]) -> list[dict]:
    raw = []
    for msg in gmail.fetch_alert_messages(sources, days_back=1):
        html = extract_html_body(msg)
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        sender = headers.get("From", "")
        source = "linkedin" if "linkedin" in sender else "indeed"
        market = next((m for m in markets if m in headers.get("Subject", "").lower()), markets[0])
        raw.extend(parse_gmail_message(html, source=source, market=market))
    return raw


def main():
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

    # 2. Fetch all sources concurrently
    print("[1/4] Fetching all sources in parallel (Gmail / RSS / Scraper)...")
    gmail = GmailFetcher()
    rss = RSSFetcher()
    scraper = WebScraper()

    with ThreadPoolExecutor(max_workers=3) as pool:
        gmail_fut = pool.submit(_fetch_gmail, gmail, sources, markets)
        rss_fut = pool.submit(rss.fetch_all, sources, markets, titles)
        scraper_fut = pool.submit(scraper.fetch_all, sources, titles)

    raw_entries = gmail_fut.result() + rss_fut.result() + scraper_fut.result()

    # 3. Parse
    print("[2/4] Parsing and normalizing...")
    jobs = parse_all(raw_entries)

    # 4. Deduplicate (cross-platform + cross-day)
    jobs = deduplicate(jobs)
    seen_ids = load_seen_ids()
    jobs = remove_seen(jobs, seen_ids)

    # 5. Rule-based filter
    before = len(jobs)
    jobs = _rule_filter(jobs, targets)
    print(f"[3/4] Rule filter: {before} → {len(jobs)} jobs kept")

    # 6. Send email
    print(f"[4/4] Sending digest ({len(jobs)} matches)...")
    today = date.today()
    subject = build_subject(jobs, today)
    html = build_email_html(jobs, today)
    gmail.send_email(
        to=notif["to"],
        from_=notif["from"],
        subject=subject,
        html_body=html,
    )

    # 7. Update state
    new_seen = seen_ids | {j.id for j in jobs}
    save_seen_ids(new_seen)

    print(f"Done. {len(jobs)} matches sent to {notif['to']}")


if __name__ == "__main__":
    main()
