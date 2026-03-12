#!/usr/bin/env python3
"""Daily job aggregator — run via cron."""
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone, timedelta

DEBUG = "--debug" in sys.argv or os.getenv("DEBUG") == "1"


def _dbg_jobs(label: str, jobs):
    if not DEBUG:
        return
    print(f"\n[DEBUG] {label} ({len(jobs)} jobs):")
    for j in jobs:
        print(f"  [{j.sources[0]}] {j.title!r} | company={j.company!r} | posted_at={j.posted_at}")

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


def _recency_filter(jobs: list[Job], hours: int = 24) -> list[Job]:
    """Drop jobs with a known posted_at older than `hours` hours. Jobs with no
    posted_at (scraped sources) are kept."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    kept = []
    for job in jobs:
        if job.posted_at:
            try:
                posted = datetime.fromisoformat(job.posted_at.replace("Z", "+00:00"))
                if posted < cutoff:
                    continue
            except ValueError:
                pass
        kept.append(job)
    return kept


_ZH_EXPAND: dict[str, list[str]] = {
    "product manager": ["產品經理", "產品主管", "プロダクトマネージャ", "プロダクトマネジャー"],
    "pm":              ["產品經理", "產品主管", "プロダクトマネージャ", "プロダクトマネジャー"],
    "growth":          ["成長", "增長"],
    "product":         ["プロダクト"],
    "manager":         ["マネージャ", "マネジャー"],
    "engineer":        ["工程師", "エンジニア"],
    "designer":        ["設計師", "デザイナー"],
    "data":            ["數據", "資料"],
    "marketing":       ["行銷"],
    "sales":           ["業務", "銷售"],
    "project manager": ["專案經理", "項目經理"],
}


def _rule_filter(jobs: list[Job], targets: dict) -> list[Job]:
    """Keep jobs whose title contains at least one keyword from targets.titles,
    and reject any job whose title or description contains an exclude_keyword.

    English matching uses the FULL target phrase (not individual words) so that
    "product manager" does not accidentally match "project manager" or
    "program manager". CJK expansions are derived from both full phrases and
    individual words so that Chinese/Japanese titles are correctly matched.
    """
    en_phrases = [t.lower() for t in targets.get("titles", [])]
    zh_keywords: list[str] = []
    for phrase in en_phrases:
        # Expand the full phrase (e.g. "product manager" → 產品經理)
        zh_keywords.extend(_ZH_EXPAND.get(phrase, []))
        # Expand each word for CJK-only titles (e.g. "pm" → 產品經理)
        for word in phrase.split():
            zh_keywords.extend(_ZH_EXPAND.get(word, []))
    title_keywords = en_phrases + zh_keywords
    exclude = [kw.lower() for kw in targets.get("exclude_keywords", [])]
    if DEBUG:
        print(f"\n[DEBUG] rule_filter: title_keywords={title_keywords}, exclude={exclude}")

    kept = []
    for job in jobs:
        t = job.title.lower()
        d = job.description.lower()
        if any(ex in t or ex in d for ex in exclude):
            if DEBUG:
                print(f"  EXCLUDED (keyword hit): {job.title!r}")
            continue
        if any(kw in t for kw in title_keywords):
            if DEBUG:
                print(f"  KEPT: {job.title!r} | company={job.company!r}")
            kept.append(job)
        elif DEBUG:
            print(f"  DROPPED (no title match): {job.title!r}")
    return kept


_MARKET_SUBJECT_KEYWORDS: dict[str, list[str]] = {
    "tw": ["taiwan", "taipei", "台灣", "台北"],
    "sg": ["singapore"],
    "jp": ["japan", "tokyo", "日本", "東京"],
}


def _detect_market(sender: str, subject: str, markets: list[str]) -> str:
    """Detect market from sender domain (Indeed) or subject keywords (LinkedIn)."""
    sender_lower = sender.lower()
    for market in markets:
        if f"@{market}.indeed.com" in sender_lower:
            return market
    subject_lower = subject.lower()
    for market in markets:
        for kw in _MARKET_SUBJECT_KEYWORDS.get(market, []):
            if kw in subject_lower:
                return market
    return markets[0]


def _fetch_gmail(gmail: GmailFetcher, sources: dict, markets: list[str], days_back: int = 7) -> list[dict]:
    raw = []
    source_counts: dict[str, int] = {}
    for msg in gmail.fetch_alert_messages(sources, days_back=days_back):
        html = extract_html_body(msg)
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        sender = headers.get("From", "")
        subject = headers.get("Subject", "")
        source = "linkedin" if "linkedin" in sender else "indeed"
        market = _detect_market(sender, subject, markets)
        is_pm_email = "product manager" in subject.lower() or "growth" in subject.lower()
        jobs = parse_gmail_message(html, source=source, market=market, debug=DEBUG and is_pm_email)
        source_counts[source] = source_counts.get(source, 0) + len(jobs)
        if DEBUG:
            print(f"[DEBUG] Gmail: {subject[:70]!r} → {len(jobs)} jobs (html={len(html)}B)")
            if len(jobs) == 0 and is_pm_email:
                import pathlib
                dump_path = pathlib.Path(f"/tmp/debug_gmail_{subject[:40].replace('/', '_').replace(' ', '_')}.html")
                dump_path.write_text(html, encoding="utf-8")
                print(f"[DEBUG]   → dumped HTML to {dump_path}")
        raw.extend(jobs)
    if source_counts:
        breakdown = ", ".join(f"{s}={c}" for s, c in sorted(source_counts.items()))
        print(f"  gmail breakdown: {breakdown}")
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
    days_back = cfg.get("days_back", 3)

    # 2. Fetch all sources concurrently
    print("[1/4] Fetching all sources in parallel (Gmail / RSS / Scraper)...")
    gmail = GmailFetcher()
    rss = RSSFetcher()
    scraper = WebScraper()

    with ThreadPoolExecutor(max_workers=3) as pool:
        gmail_fut = pool.submit(_fetch_gmail, gmail, sources, markets, days_back)
        rss_fut = pool.submit(rss.fetch_all, sources, markets, titles)
        scraper_fut = pool.submit(scraper.fetch_all, sources, titles, markets)

    gmail_raw = gmail_fut.result()
    rss_raw = rss_fut.result()
    scraper_raw = scraper_fut.result()
    raw_entries = gmail_raw + rss_raw + scraper_raw

    # 3. Parse
    print("[2/4] Parsing and normalizing...")
    print(f"  raw: gmail={len(gmail_raw)}, rss={len(rss_raw)}, scraper={len(scraper_raw)}")
    jobs = parse_all(raw_entries)
    after_parse = len(jobs)
    _dbg_jobs("after parse_all", jobs)

    # 4. Deduplicate (cross-platform + cross-day)
    jobs = deduplicate(jobs)
    after_dedup = len(jobs)
    seen_ids = load_seen_ids()
    jobs = remove_seen(jobs, seen_ids)
    after_remove = len(jobs)
    print(f"  parse_all: {len(raw_entries)} → {after_parse}  dedup: {after_dedup}  remove_seen: {after_remove} (seen_ids={len(seen_ids)})")
    _dbg_jobs("after dedup+remove_seen", jobs)

    # 5. Recency + rule-based filter
    before = len(jobs)
    jobs = _recency_filter(jobs, hours=days_back * 24)
    after_recency = len(jobs)
    jobs_after_recency = jobs  # keep reference to mark all as seen later
    jobs = _rule_filter(jobs, targets)
    print(f"[3/4] Filter: {before} → recency({days_back}d): {after_recency} → title_match: {len(jobs)}")

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

    # 7. Update state — mark ALL recency-passed jobs as seen (not just title_match hits)
    # so non-PM / garbage entries don't re-appear on every run within the 3-day window
    new_seen = seen_ids | {j.id for j in jobs_after_recency}
    save_seen_ids(new_seen)

    print(f"Done. {len(jobs)} matches sent to {notif['to']}")


if __name__ == "__main__":
    main()
