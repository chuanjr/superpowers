import hashlib
from dataclasses import dataclass, field
from typing import Optional


def make_job_id(company: str, title: str) -> str:
    key = f"{company.lower().strip()}::{title.lower().strip()}"
    return hashlib.md5(key.encode()).hexdigest()


@dataclass
class Job:
    id: str
    title: str
    company: str
    location: str
    market: str          # "tw" | "jp" | "sg"
    url: str
    description: str
    sources: list[str]
    industry: Optional[str] = None
    stage: Optional[str] = None
    fetched_at: str = ""
    posted_at: Optional[str] = None  # ISO-8601 UTC, from RSS published date
