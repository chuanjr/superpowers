#!/usr/bin/env python3
"""Daily job aggregator — run via cron.

Usage:
  python main.py            # fetch + store to jobs.db (no email)
  python main.py --email    # fetch + store + send email digest for new matches
  python main.py --debug    # verbose logging
"""
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone, timedelta

DEBUG = "--debug" in sys.argv or os.getenv("DEBUG") == "1"
SEND_EMAIL = "--email" in sys.argv


def _dbg_jobs(label: str, jobs):
    if not DEBUG:
        return
    print(f"\n[DEBUG] {label} ({len(jobs)} jobs):")
    for j in jobs:
        print(f"  [{j.sources[0]}] {j.title!r} | company={j.company!r} | posted_at={j.posted_at}")


from dotenv import load_dotenv
load_dotenv()

from config_loader import load_config, ConfigError
from store import init_db, upsert_jobs
from fetchers.gmail_fetcher import GmailFetcher, extract_html_body
from fetchers.gmail_parser import parse_gmail_message
from fetchers.rss_fetcher import RSSFetcher
from fetchers.scraper import WebScraper
from parser import parse_all
from deduplicator import deduplicate
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
    "growth product manager": ["成長產品經理", "グロースプロダクトマネージャ"],
}


def _rule_filter(jobs: list[Job], targets: dict) -> list[Job]:
    """Keep jobs whose title contains at least one keyword from targets.titles,
    and reject any job whose title or description contains an exclude_keyword."""
    en_phrases = [t.lower() for t in targets.get("titles", [])]
    zh_keywords: list[str] = []
    for phrase in en_phrases:
        zh_keywords.extend(_ZH_EXPAND.get(phrase, []))
        for word in phrase.split():
            zh_keywords.extend(_ZH_EXPAND.get(word, []))
    title_keywords = en_phrases + zh_keywords
    exclude = [kw.lower() for kw in targets.get("exclude_keywords", [])]
    if DEBUG:
        print(f"\n[DEBUG] rule_filter: en_phrases={en_phrases}, zh={zh_keywords[:6]}..., exclude={exclude}")

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
    "us": ["united states", "san francisco", "new york", "seattle", "austin", "boston", "chicago", "los angeles"],
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
        if "simplify.jobs" in sender:
            source = "simplify"
        elif "linkedin" in sender:
            source = "linkedin"
        else:
            source = "indeed"
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


def _check_source_health(
    sources: dict,
    gmail_raw: list, rss_raw: list, scraper_raw: list,
) -> None:
    """Warn when an enabled source returns zero entries."""
    gmail_enabled = sources.get("linkedin_gmail") or sources.get("indeed_gmail") or sources.get("simplify_gmail")
    if gmail_enabled and len(gmail_raw) == 0:
        print("[WARN] Gmail sources enabled but returned 0 jobs — check OAuth token or alert settings")

    rss_enabled = sources.get("indeed_rss") or sources.get("104_rss") or sources.get("wellfound")
    if rss_enabled and len(rss_raw) == 0:
        print("[WARN] RSS sources enabled but returned 0 jobs — check feed URLs in rss_fetcher.py")

    scraper_enabled = sources.get("cakeresume") or sources.get("yourator") or sources.get("104") or sources.get("teamblind")
    if scraper_enabled and len(scraper_raw) == 0:
        print("[WARN] Scraper sources enabled but returned 0 jobs — check selectors or run with --debug")


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
    gmail = None
    try:
        gmail = GmailFetcher()
    except Exception as _gmail_auth_err:
        print(f"[WARN] Gmail auth failed ({_gmail_auth_err}) — skipping Gmail sources. Re-run setup_cli.py to re-authenticate.")

    rss = RSSFetcher()
    scraper = WebScraper()

    with ThreadPoolExecutor(max_workers=3) as pool:
        gmail_fut = pool.submit(_fetch_gmail, gmail, sources, markets, days_back) if gmail else None
        rss_fut = pool.submit(rss.fetch_all, sources, markets, titles)
        scraper_fut = pool.submit(scraper.fetch_all, sources, titles, markets)

    gmail_raw = gmail_fut.result() if gmail_fut else []
    rss_raw = rss_fut.result()
    scraper_raw = scraper_fut.result()
    raw_entries = gmail_raw + rss_raw + scraper_raw

    # 3. Parse + deduplicate cross-platform
    print("[2/4] Parsing and normalizing...")
    print(f"  raw: gmail={len(gmail_raw)}, rss={len(rss_raw)}, scraper={len(scraper_raw)}")
    jobs = parse_all(raw_entries)
    after_parse = len(jobs)
    _dbg_jobs("after parse_all", jobs)

    jobs = deduplicate(jobs)
    after_dedup = len(jobs)
    print(f"  parse_all: {len(raw_entries)} → {after_parse}  dedup: {after_dedup}")
    _dbg_jobs("after dedup", jobs)

    # Source health warnings (after dedup so counts are post-normalization)
    _check_source_health(sources, gmail_raw, rss_raw, scraper_raw)

    # 4. Recency filter — drop jobs with a known old posted_at
    before = len(jobs)
    jobs = _recency_filter(jobs, hours=days_back * 24)
    print(f"[3/4] Filter: recency({days_back}d): {before} → {len(jobs)}")

    # 5. Title filter — only store jobs matching configured targets
    before = len(jobs)
    jobs = _rule_filter(jobs, targets)
    print(f"  title_filter: {before} → {len(jobs)}")

    # 6. Store matching jobs to SQLite (upsert — cross-day dedup via PRIMARY KEY)
    init_db()
    new_jobs = upsert_jobs(jobs)
    print(f"  stored: {len(jobs)} total, {len(new_jobs)} new → jobs.db")

    # 7. Optional email digest (--email flag)
    if SEND_EMAIL:
        matched = new_jobs  # already filtered above
        print(f"[4/4] Email: {len(matched)} new matches → sending...")
        today = date.today()
        subject = build_subject(matched, today)
        html = build_email_html(matched, today)
        gmail.send_email(
            to=notif["to"],
            from_=notif["from"],
            subject=subject,
            html_body=html,
        )
        print(f"Done. {len(matched)} matches emailed to {notif['to']}")
    else:
        print(f"Done. {len(new_jobs)} new jobs stored. Run: python server.py  then open http://localhost:8000")


if __name__ == "__main__":
    main()
