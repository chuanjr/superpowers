import re
from datetime import datetime, timezone
from typing import Optional
from models import Job, make_job_id

# ── Market inference from job content ─────────────────────────────────────────
# Correct for cases where the fetcher assigns the search market (e.g. "tw") but
# the actual job is in a different country (Amazon JP FBA, Randstad Japan, etc.)

_JP_RE = re.compile(
    r'japan|tokyo|日本|東京'
    r'|(?:^|[\s,(\[])jp(?:[\s,)\].]|$)',   # standalone "jp" abbreviation
    re.IGNORECASE,
)
_SG_RE = re.compile(r'singapore', re.IGNORECASE)
_TW_RE = re.compile(r'taiwan|taipei|台灣|台北', re.IGNORECASE)
_US_RE = re.compile(
    r'united states|new york|san francisco|seattle|austin|chicago|boston|los angeles',
    re.IGNORECASE,
)


def _infer_market(title: str, company: str, location: str, fallback: str) -> str:
    """Infer the actual job market from title/company/location content.

    The fetcher assigns a market based on the search context (e.g. "tw" for a
    LinkedIn TW search), but some jobs surface across borders. Correct that here.
    """
    text = f"{title} | {company} | {location}"
    if _JP_RE.search(text):
        return "jp"
    if _SG_RE.search(text):
        return "sg"
    if _TW_RE.search(text):
        return "tw"
    if _US_RE.search(text):
        return "us"
    return fallback


def parse_raw_entry(raw: dict) -> Optional[Job]:
    title = raw.get("title", "").strip()
    if not title:
        return None
    company = raw.get("company", "").strip()
    location = raw.get("location", "").strip()
    url = raw.get("url", "")
    raw_market = raw.get("market", "")
    market = _infer_market(title, company, location, raw_market)
    return Job(
        id=make_job_id(company, title, url),
        title=title,
        company=company,
        location=location,
        market=market,
        url=raw.get("url", ""),
        description=raw.get("description", ""),
        sources=[raw.get("source", "unknown")],
        fetched_at=datetime.now(timezone.utc).isoformat(),
        posted_at=raw.get("posted_at"),
        logo_url=raw.get("logo_url") or None,
    )


def parse_all(raw_entries: list[dict]) -> list[Job]:
    jobs = []
    for entry in raw_entries:
        job = parse_raw_entry(entry)
        if job:
            jobs.append(job)
    return jobs
