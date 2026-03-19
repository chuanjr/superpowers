import hashlib
import re
from dataclasses import dataclass, field
from typing import Optional


def _norm(s: str) -> str:
    """Normalize a string for dedup ID: lowercase, collapse whitespace, strip punctuation."""
    s = s.lower().strip()
    s = re.sub(r'[^\w\s\u3000-\u9fff\uac00-\ud7af\u3040-\u30ff]', ' ', s)  # keep CJK, strip punct
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def _strip_url_params(url: str) -> str:
    """Strip query params and fragments from a URL, keeping only scheme+host+path."""
    if not url:
        return url
    # Remove query string and fragment
    for sep in ("?", "#"):
        idx = url.find(sep)
        if idx != -1:
            url = url[:idx]
    return url.rstrip("/")


def make_job_id(company: str, title: str, url: str = "") -> str:
    if company:
        key = f"{_norm(company)}::{_norm(title)}"
    else:
        clean_url = _strip_url_params(url)
        key = clean_url or _norm(title)
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
    logo_url: Optional[str] = None
