"""Company culture research via DuckDuckGo HTML scraping + Claude Haiku parsing.

Flow:
  1. Normalize company name → cache key
  2. Check company_culture_cache — return if hit
  3. On cache miss: search DDG (English + Chinese), optionally scrape 104 company page
  4. Claude Haiku parses snippets → structured culture profile
  5. Store in cache (never expires; manual refresh via force=True)
"""
import json
import re
import time
from html.parser import HTMLParser
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import anthropic


def normalize_company(company: str) -> str:
    """Normalize company name to a stable cache key. Preserves Unicode (e.g. Chinese)."""
    key = company.lower().strip()
    key = re.sub(r"[^\w]+", "-", key, flags=re.UNICODE)
    return key.strip("-")


# ── DuckDuckGo HTML scraping ───────────────────────────────────────────────────

class _SnippetParser(HTMLParser):
    """Extract result titles and snippets from DuckDuckGo HTML response."""

    def __init__(self):
        super().__init__()
        self._in_snippet = False
        self._in_title = False
        self._depth = 0
        self.snippets: list[str] = []
        self._buf = ""
        self._current_class = ""

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        cls = attrs_dict.get("class", "")
        if "result__snippet" in cls or "result__title" in cls:
            self._in_snippet = True
            self._buf = ""
        elif self._in_snippet:
            self._depth += 1

    def handle_endtag(self, tag):
        if self._in_snippet:
            if self._depth > 0:
                self._depth -= 1
            else:
                text = self._buf.strip()
                if text and len(text) > 20:
                    self.snippets.append(text)
                self._in_snippet = False
                self._buf = ""

    def handle_data(self, data):
        if self._in_snippet:
            self._buf += data

    def handle_entityref(self, name):
        if self._in_snippet:
            entities = {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'"}
            self._buf += entities.get(name, "")

    def handle_charref(self, name):
        if self._in_snippet:
            try:
                if name.startswith("x"):
                    self._buf += chr(int(name[1:], 16))
                else:
                    self._buf += chr(int(name))
            except (ValueError, OverflowError):
                pass


def _ddg_search(query: str) -> list[str]:
    """Fetch up to 10 snippets from DuckDuckGo HTML search."""
    params = urlencode({"q": query})
    url = f"https://html.duckduckgo.com/html/?{params}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    req = Request(url, headers=headers)
    try:
        with urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
    except URLError:
        return []
    parser = _SnippetParser()
    parser.feed(html)
    return parser.snippets[:10]


def search_company_culture_snippets(company: str) -> list[str]:
    """Search DuckDuckGo (English) for company culture snippets."""
    return _ddg_search(f'"{company}" work culture values employees glassdoor')


def search_company_culture_snippets_zh(company: str) -> list[str]:
    """Search DuckDuckGo (Chinese) for company culture snippets — 職場天眼通, Glassdoor, 104."""
    return _ddg_search(f'"{company}" 工作環境 員工評價 職場天眼通 glassdoor 104')


# ── 104.com.tw company info ───────────────────────────────────────────────────

_HEADERS_104 = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.104.com.tw/",
    "Accept": "application/json",
}


def _extract_104_job_id(url: str) -> str | None:
    """Extract job ID from a 104.com.tw job URL."""
    m = re.search(r"104\.com\.tw/job/([A-Za-z0-9]+)", url)
    return m.group(1) if m else None


def fetch_104_company_info(job_url: str) -> list[str]:
    """Fetch company description from 104's job content API. Returns text snippets."""
    job_id = _extract_104_job_id(job_url)
    if not job_id:
        return []
    api_url = f"https://www.104.com.tw/job/ajax/content/{job_id}"
    req = Request(api_url, headers=_HEADERS_104)
    try:
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))
    except Exception:
        return []

    snippets: list[str] = []
    company = data.get("data", {}).get("company", {})
    profile = company.get("profile", {})

    intro = profile.get("intro", "").strip()
    if intro:
        snippets.append(f"[104公司介紹] {intro[:800]}")

    product = profile.get("product", "").strip()
    if product:
        snippets.append(f"[104產品服務] {product[:400]}")

    welfare = data.get("data", {}).get("welfare", {}).get("welfare", "").strip()
    if welfare:
        snippets.append(f"[104福利制度] {welfare[:400]}")

    return snippets


# ── JD auto-fetch ──────────────────────────────────────────────────────────────

def _strip_html(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text).strip()


def _extract_ld_json_description(html: str) -> str:
    """Extract job description from application/ld+json script tags.

    LinkedIn and CakeResume both embed structured JobPosting data in ld+json.
    This works even when the visible page content is behind a login wall.
    """
    ld_matches = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.DOTALL | re.IGNORECASE
    )
    for ld_raw in ld_matches:
        try:
            ld = json.loads(ld_raw.strip())
            items = ld if isinstance(ld, list) else [ld]
            for item in items:
                if not isinstance(item, dict):
                    continue
                if item.get("@type") == "JobPosting":
                    desc = item.get("description", "")
                    if desc:
                        return _strip_html(str(desc))[:3000]
        except Exception:
            continue
    return ""


def _extract_cakeresume_jd(html: str) -> str:
    """Extract job description from CakeResume page.

    CakeResume renders job content in a <div class="job-description"> or
    embeds it in ld+json. Try ld+json first, then specific div classes.
    """
    # Try ld+json first
    ld_desc = _extract_ld_json_description(html)
    if ld_desc:
        return ld_desc

    # Fallback: look for job description div
    for pattern in (
        r'<div[^>]+class="[^"]*job-description[^"]*"[^>]*>(.*?)</div\s*>',
        r'<div[^>]+class="[^"]*description[^"]*"[^>]*>(.*?)</div\s*>',
        r'<section[^>]+class="[^"]*job-detail[^"]*"[^>]*>(.*?)</section\s*>',
    ):
        m = re.search(pattern, html, re.DOTALL | re.IGNORECASE)
        if m:
            return _strip_html(m.group(1))[:3000]

    return ""


def fetch_jd_from_url(url: str) -> str:
    """Try to fetch a job description from its URL. Returns plain text or empty string."""
    if not url:
        return ""

    # 104.com.tw: use the job content API which returns structured JSON
    job_id_104 = _extract_104_job_id(url)
    if job_id_104:
        api_url = f"https://www.104.com.tw/job/ajax/content/{job_id_104}"
        try:
            req = Request(api_url, headers=_HEADERS_104)
            with urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="ignore"))
            desc_html = data.get("data", {}).get("jobDetail", {}).get("jobDescription", "")
            if desc_html:
                return _strip_html(desc_html)[:3000]
        except Exception:
            pass

    _ua = {
        "User-Agent": _HEADERS_104["User-Agent"],
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    # LinkedIn: try ld+json extraction from the public SSR page.
    # LinkedIn returns a partial page even without login — often includes ld+json with the JD.
    if "linkedin.com" in url:
        try:
            req = Request(url, headers=_ua)
            with urlopen(req, timeout=15) as resp:
                html = resp.read().decode("utf-8", errors="ignore")
            ld_desc = _extract_ld_json_description(html)
            if ld_desc and len(ld_desc) > 100:
                return ld_desc
        except Exception:
            pass
        # If ld+json extraction didn't work, fall through to generic (or return empty)
        # Generic text from LinkedIn login wall is useless — return empty
        return ""

    # CakeResume: use targeted extraction
    if "cakeresume.com" in url:
        try:
            req = Request(url, headers=_ua)
            with urlopen(req, timeout=15) as resp:
                html = resp.read().decode("utf-8", errors="ignore")
            cake_desc = _extract_cakeresume_jd(html)
            if cake_desc and len(cake_desc) > 100:
                return cake_desc
            # Fallback to full text strip if targeted extraction failed
            return _strip_html(html)[:3000]
        except Exception:
            return ""

    # Generic fallback: fetch URL and extract text
    try:
        req = Request(url, headers=_ua)
        with urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
        # Try ld+json first for any site that embeds it
        ld_desc = _extract_ld_json_description(html)
        if ld_desc and len(ld_desc) > 100:
            return ld_desc
        return _strip_html(html)[:3000]
    except Exception:
        return ""


# ── Claude Haiku parsing ───────────────────────────────────────────────────────

def parse_company_culture_sync(company: str, snippets: list[str]) -> dict:
    """Use Claude Haiku to structure culture signals from search snippets."""
    client = anthropic.Anthropic()
    snippets_text = "\n\n".join(f"[{i+1}] {s}" for i, s in enumerate(snippets))

    prompt = f"""You are analyzing company culture for a job candidate.
Based on these search snippets about {company}, extract structured culture signals.
Snippets may be in English or Chinese (Traditional/Simplified) — handle both.
Return ONLY valid JSON:
{{
  "values": ["<core value or principle>"],
  "work_style": "<1-2 sentences: pace, autonomy, collaboration style>",
  "green_flags": ["<positive culture signal worth noting>"],
  "red_flags": ["<potential concern or negative pattern>"],
  "summary": "<2-3 sentences: honest overall culture picture>"
}}

If snippets are insufficient or not about company culture, return minimal info with what's available.
Keep each list to 3-5 items max. Write the output in the same language as the majority of the input snippets.

Company: {company}
Search snippets:
{snippets_text[:3000]}"""

    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = msg.content[0].text.strip()
    # Strip code fences if present
    if "```" in raw:
        parts = raw.split("```")
        raw = parts[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())


# ── Main entry point ───────────────────────────────────────────────────────────

def get_or_research_company(company: str, job_url: str = "", force: bool = False) -> dict | None:
    """Return cached company culture profile, fetching if not cached or force=True.

    Returns parsed dict with keys: values, work_style, green_flags, red_flags, summary.
    Returns None if company name is empty or search returns no results.
    """
    if not company or not company.strip():
        return None

    from store import get_company_culture_cache, upsert_company_culture_cache
    company_key = normalize_company(company)

    if not force:
        cached = get_company_culture_cache(company_key)
        if cached and cached.get("parsed_json"):
            try:
                return json.loads(cached["parsed_json"])
            except (json.JSONDecodeError, TypeError):
                pass

    # Gather snippets from multiple sources
    snippets: list[str] = []

    # English DDG search
    snippets += search_company_culture_snippets(company)

    # Chinese DDG search (covers 職場天眼通, Glassdoor TW, 104 reviews)
    zh_snippets = search_company_culture_snippets_zh(company)
    # Avoid exact duplicates
    existing = set(snippets)
    snippets += [s for s in zh_snippets if s not in existing]

    # 104-specific company page (if job URL is from 104)
    if job_url and "104.com.tw" in job_url:
        job104_snippets = fetch_104_company_info(job_url)
        snippets = job104_snippets + snippets  # 104 info goes first (most relevant)

    if not snippets:
        return None

    parsed = parse_company_culture_sync(company, snippets[:15])
    upsert_company_culture_cache(
        company_key, company,
        json.dumps(snippets), json.dumps(parsed),
    )
    return parsed
