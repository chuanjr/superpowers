"""Company culture research via DuckDuckGo HTML scraping + Claude Haiku parsing.

Flow:
  1. Normalize company name → cache key
  2. Check company_culture_cache — return if hit
  3. On cache miss: search DDG HTML, extract snippets
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
    """Normalize company name to a stable cache key."""
    key = company.lower().strip()
    key = re.sub(r"[^a-z0-9]+", "-", key)
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


def search_company_culture_snippets(company: str) -> list[str]:
    """Search DuckDuckGo for company culture snippets. Returns up to 10 text snippets."""
    query = f'"{company}" work culture values employees glassdoor'
    params = urlencode({"q": query})
    url = f"https://html.duckduckgo.com/html/?{params}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
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


# ── Claude Haiku parsing ───────────────────────────────────────────────────────

def parse_company_culture_sync(company: str, snippets: list[str]) -> dict:
    """Use Claude Haiku to structure culture signals from search snippets."""
    client = anthropic.Anthropic()
    snippets_text = "\n\n".join(f"[{i+1}] {s}" for i, s in enumerate(snippets))

    prompt = f"""You are analyzing company culture for a job candidate.
Based on these search snippets about {company}, extract structured culture signals.
Return ONLY valid JSON:
{{
  "values": ["<core value or principle>"],
  "work_style": "<1-2 sentences: pace, autonomy, collaboration style>",
  "green_flags": ["<positive culture signal worth noting>"],
  "red_flags": ["<potential concern or negative pattern>"],
  "summary": "<2-3 sentences: honest overall culture picture>"
}}

If snippets are insufficient or not about company culture, return minimal info with what's available.
Keep each list to 3-5 items max.

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

def get_or_research_company(company: str, force: bool = False) -> dict | None:
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

    snippets = search_company_culture_snippets(company)
    if not snippets:
        return None

    parsed = parse_company_culture_sync(company, snippets)
    upsert_company_culture_cache(
        company_key, company,
        json.dumps(snippets), json.dumps(parsed),
    )
    return parsed
