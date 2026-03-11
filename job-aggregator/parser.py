from datetime import datetime, timezone
from typing import Optional
from models import Job, make_job_id


def parse_raw_entry(raw: dict) -> Optional[Job]:
    title = raw.get("title", "").strip()
    company = raw.get("company", "").strip()
    if not title or not company:
        return None
    return Job(
        id=make_job_id(company, title),
        title=title,
        company=company,
        location=raw.get("location", "").strip(),
        market=raw.get("market", ""),
        url=raw.get("url", ""),
        description=raw.get("description", ""),
        sources=[raw.get("source", "unknown")],
        fetched_at=datetime.now(timezone.utc).isoformat(),
        posted_at=raw.get("posted_at"),
    )


def parse_all(raw_entries: list[dict]) -> list[Job]:
    jobs = []
    for entry in raw_entries:
        job = parse_raw_entry(entry)
        if job:
            jobs.append(job)
    return jobs
