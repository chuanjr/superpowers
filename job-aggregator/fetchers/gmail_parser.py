"""Parse HTML body of Gmail job alert emails into raw job dicts."""
from html.parser import HTMLParser
import re


class _LinkExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.jobs: list[dict] = []
        self._current_href = ""

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            attrs_dict = dict(attrs)
            self._current_href = attrs_dict.get("href", "")

    def handle_data(self, data):
        text = data.strip()
        if not text or not self._current_href:
            return
        href = self._current_href
        # LinkedIn: links containing /jobs/view/ with "at CompanyName" pattern
        if "/jobs/view/" in href and " at " in text:
            parts = text.split(" at ", 1)
            self.jobs.append({
                "title": parts[0].strip(),
                "company": parts[1].strip() if len(parts) > 1 else "",
                "url": href,
            })
        # Indeed: links containing viewjob or /rc/clk with non-trivial text
        elif ("viewjob" in href or "/rc/clk" in href) and len(text) > 5:
            self.jobs.append({
                "title": text,
                "company": "",
                "url": href,
            })

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


def parse_gmail_message(html: str, source: str, market: str) -> list[dict]:
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
            "description": "",
            "location": "",
            "source": source,
            "market": market,
        })
    return results
