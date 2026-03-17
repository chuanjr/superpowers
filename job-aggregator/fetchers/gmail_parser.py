"""Parse HTML body of Gmail job alert emails into raw job dicts."""
from html.parser import HTMLParser
import re

_UI_EXACT = frozenset({
    "view", "apply", "apply now", "see job", "see more jobs", "view job",
    "see all jobs", "unsubscribe", "jobs similar to", "similar jobs",
    "sign in", "log in",
    # LinkedIn email tab navigation (Traditional Chinese / Simplified Chinese / Japanese)
    "職缺", "公司", "専欄", "专栏", "求人", "会社", "專欄",
})
_UI_PREFIX = ("jobs similar to", "similar to")


def _is_ui_text(text: str) -> bool:
    t = text.lower().strip()
    if t in _UI_EXACT:
        return True
    if any(t.startswith(p) for p in _UI_PREFIX):
        return True
    return False


def _normalize_url(href: str) -> str:
    """Strip tracking params and hash fragments to get a stable canonical job URL."""
    return href.split("?")[0].split("#")[0].rstrip("/")


def _clean_company(text: str) -> str:
    """Strip location suffix from 'Company · City, Country' text."""
    return text.split("·")[0].strip()


class _LinkExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.jobs: list[dict] = []
        self._current_href = ""
        # canonical_url → index in self.jobs, so later text nodes can update company
        self._seen_urls: dict[str, int] = {}
        # most recently seen company name from a /company/ link
        self._current_company = ""
        # most recently seen img src (company logo before job link)
        self._pending_logo = ""
        # index of the last job added — for free-text company fallback
        self._last_job_idx: int | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "img":
            src = dict(attrs).get("src", "")
            if src:
                self._pending_logo = src
        elif tag == "a":
            attrs_dict = dict(attrs)
            self._current_href = attrs_dict.get("href", "")

    def handle_data(self, data):
        text = data.strip()
        if not text:
            return

        # Free text node (outside any anchor) — may be company name after a job title link
        if not self._current_href:
            if (self._last_job_idx is not None
                    and not _is_ui_text(text)
                    and 2 < len(text) < 100):
                job = self.jobs[self._last_job_idx]
                if not job["company"] and text != job["title"]:
                    job["company"] = _clean_company(text)
            return

        href = self._current_href

        # LinkedIn company profile link → remember company name for upcoming jobs
        if "/company/" in href and "/jobs/view/" not in href and not _is_ui_text(text) and len(text) > 2:
            self._current_company = _clean_company(text)
            return

        # LinkedIn: any link containing /jobs/view/ (href may include /comm/ prefix)
        if "/jobs/view/" in href and len(text) > 3:
            if _is_ui_text(text):
                return
            # Old format: "Backend Engineer at Stripe" in a single link text
            if " at " in text:
                parts = text.split(" at ", 1)
                canonical = _normalize_url(href)
                if canonical not in self._seen_urls:
                    self._seen_urls[canonical] = len(self.jobs)
                    self._last_job_idx = len(self.jobs)
                    self.jobs.append({
                        "title": parts[0].strip(),
                        "company": parts[1].strip() if len(parts) > 1 else "",
                        "url": href,
                        "logo_url": self._pending_logo,
                    })
                    self._pending_logo = ""
            else:
                # New format: title-only link; assign current company if available
                canonical = _normalize_url(href)
                if canonical not in self._seen_urls:
                    self._seen_urls[canonical] = len(self.jobs)
                    self._last_job_idx = len(self.jobs)
                    self.jobs.append({
                        "title": text,
                        "company": self._current_company,
                        "url": href,
                        "logo_url": self._pending_logo,
                    })
                    self._pending_logo = ""
                else:
                    # Subsequent text for same URL — treat as company name if not yet set
                    idx = self._seen_urls[canonical]
                    if not self.jobs[idx]["company"] and text != self.jobs[idx]["title"]:
                        self.jobs[idx]["company"] = _clean_company(text)
        # Indeed: links containing viewjob or /rc/clk with non-trivial text
        elif ("viewjob" in href or "/rc/clk" in href) and len(text) > 5:
            canonical = _normalize_url(href)
            if canonical not in self._seen_urls:
                self._seen_urls[canonical] = len(self.jobs)
                self._last_job_idx = len(self.jobs)
                self.jobs.append({
                    "title": text,
                    "company": "",
                    "url": href,
                    "logo_url": self._pending_logo,
                })
                self._pending_logo = ""

    def handle_endtag(self, tag):
        if tag == "a":
            self._current_href = ""


def _extract_company_from_context(html: str, title: str) -> str:
    """Best-effort: find company name near a job title in raw HTML."""
    pattern = re.escape(title) + r'.{0,200}?<span[^>]*class="[^"]*company[^"]*"[^>]*>([^<]+)<'
    match = re.search(pattern, html, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return ""


def parse_gmail_message(html: str, source: str, market: str, debug: bool = False) -> list[dict]:
    if not html:
        return []
    extractor = _LinkExtractor()
    extractor.feed(html)
    results = []
    for job in extractor.jobs:
        if not job.get("company"):
            job["company"] = _extract_company_from_context(html, job["title"])
        results.append({
            "title": job.get("title", ""),
            "company": job.get("company", ""),
            "url": job.get("url", ""),
            "logo_url": job.get("logo_url", ""),
            "description": "",
            "location": "",
            "source": source,
            "market": market,
        })
    if debug:
        for r in results:
            print(f"    parsed: {r['title']!r} | company={r['company']!r} | logo={r['logo_url']!r} | url={r['url'][-50:]!r}")
    return results
