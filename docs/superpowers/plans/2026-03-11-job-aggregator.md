# Job Aggregator Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a local Python tool that aggregates job listings from LinkedIn (via Gmail), Indeed, 104, CakeResume, Yourator, and Wellfound, filters by relevance using Claude Haiku, and delivers a daily deduplicated summary email.

**Architecture:** Gmail API fetches LinkedIn/Indeed alerts; RSS clients fetch 104/Wellfound/Indeed feeds; Playwright scrapes CakeResume/Yourator. All sources normalize to a unified Job schema, get deduplicated, filtered by Claude Haiku (software industry + role match), enriched with company metadata, then sent as a daily email digest. Config and state are file-based; setup is an interactive CLI.

**Tech Stack:** Python 3.11+, `google-auth` + `google-api-python-client` (Gmail), `feedparser` (RSS), `playwright` (scraping), `anthropic` (Claude Haiku), `PyYAML` (config), `pytest` + `pytest-mock` (testing)

---

## Chunk 1: Foundation

### Task 1: Project structure and dependencies

**Files:**
- Create: `job-aggregator/requirements.txt`
- Create: `job-aggregator/.gitignore`
- Create: `job-aggregator/tests/__init__.py`
- Create: `job-aggregator/fetchers/__init__.py`
- Create: `job-aggregator/credentials/.gitignore`

- [ ] **Step 1: Create project root and directory structure**

```bash
mkdir -p job-aggregator/fetchers job-aggregator/tests/fixtures job-aggregator/credentials
touch job-aggregator/fetchers/__init__.py job-aggregator/tests/__init__.py
```

- [ ] **Step 2: Create requirements.txt**

```
google-auth==2.29.0
google-auth-oauthlib==1.2.0
google-api-python-client==2.127.0
feedparser==6.0.11
playwright==1.44.0
anthropic==0.27.0
PyYAML==6.0.1
pytest==8.2.0
pytest-mock==3.14.0
```

- [ ] **Step 3: Create .gitignore**

```
credentials/client_secret.json
credentials/token.json
state.json
config.yaml
__pycache__/
.pytest_cache/
*.pyc
.env
```

- [ ] **Step 4: Create credentials/.gitignore** (protect credentials even if parent is tracked)

```
*
!.gitignore
```

- [ ] **Step 5: Install dependencies**

```bash
cd job-aggregator
pip install -r requirements.txt
playwright install chromium
```

Expected: all packages install without errors.

- [ ] **Step 6: Commit**

```bash
git add job-aggregator/
git commit -m "feat: scaffold job-aggregator project structure"
```

---

### Task 2: Job model

**Files:**
- Create: `job-aggregator/models.py`
- Create: `job-aggregator/tests/test_models.py`

- [ ] **Step 1: Write failing test**

`tests/test_models.py`:
```python
import hashlib
from models import Job, make_job_id

def test_make_job_id_is_deterministic():
    assert make_job_id("Stripe", "Backend Engineer") == make_job_id("Stripe", "Backend Engineer")

def test_make_job_id_differs_by_company():
    assert make_job_id("Stripe", "Backend Engineer") != make_job_id("Shopify", "Backend Engineer")

def test_job_defaults():
    job = Job(
        id="abc",
        title="Backend Engineer",
        company="Stripe",
        location="Singapore",
        market="sg",
        url="https://example.com/job/1",
        description="We are hiring...",
        sources=["linkedin"],
    )
    assert job.industry is None
    assert job.stage is None
    assert job.fetched_at == ""

def test_job_sources_merged():
    job = Job(
        id="abc", title="SWE", company="Stripe", location="SG",
        market="sg", url="https://x.com", description="",
        sources=["linkedin", "indeed"],
    )
    assert "linkedin" in job.sources
    assert "indeed" in job.sources
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd job-aggregator && pytest tests/test_models.py -v
```

Expected: FAIL — `models` module not found.

- [ ] **Step 3: Implement models.py**

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/test_models.py -v
```

Expected: 4 PASSED.

- [ ] **Step 5: Commit**

```bash
git add models.py tests/test_models.py
git commit -m "feat: add Job model and make_job_id"
```

---

### Task 3: Config loader and default config

**Files:**
- Create: `job-aggregator/config_loader.py`
- Create: `job-aggregator/config.yaml.example`
- Create: `job-aggregator/tests/test_config_loader.py`

- [ ] **Step 1: Write failing test**

`tests/test_config_loader.py`:
```python
import pytest
from pathlib import Path
from config_loader import load_config, ConfigError

def test_load_config_valid(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("""
markets: [tw, jp]
targets:
  titles: ["Backend Engineer"]
  experience_years: "3-5"
  exclude_keywords: []
sources:
  linkedin_gmail: true
  104: true
notification:
  to: test@example.com
  from: test@example.com
""")
    cfg = load_config(cfg_file)
    assert cfg["markets"] == ["tw", "jp"]
    assert cfg["targets"]["titles"] == ["Backend Engineer"]

def test_load_config_missing_file():
    with pytest.raises(ConfigError, match="not found"):
        load_config(Path("nonexistent.yaml"))

def test_load_config_missing_required_key(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("markets: [tw]")
    with pytest.raises(ConfigError, match="targets"):
        load_config(cfg_file)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_config_loader.py -v
```

Expected: FAIL — `config_loader` not found.

- [ ] **Step 3: Implement config_loader.py**

```python
from pathlib import Path
import yaml


REQUIRED_KEYS = ["markets", "targets", "sources", "notification"]


class ConfigError(Exception):
    pass


def load_config(path: Path | str = "config.yaml") -> dict:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    with open(path) as f:
        cfg = yaml.safe_load(f)
    for key in REQUIRED_KEYS:
        if key not in cfg:
            raise ConfigError(f"Missing required config key: {key}")
    return cfg
```

- [ ] **Step 4: Create config.yaml.example**

```yaml
markets:
  - tw
  - jp
  - sg

targets:
  titles:
    - "Backend Engineer"
    - "Software Engineer"
  experience_years: "3-5"
  exclude_keywords:
    - "outsourcing"
    - "派遣"

sources:
  linkedin_gmail: true
  indeed_gmail: true
  indeed_rss: true
  104: true
  cakeresume: true
  yourator: true
  wellfound: true

notification:
  to: "your@email.com"
  from: "your@email.com"
```

- [ ] **Step 5: Run test to verify it passes**

```bash
pytest tests/test_config_loader.py -v
```

Expected: 3 PASSED.

- [ ] **Step 6: Commit**

```bash
git add config_loader.py config.yaml.example tests/test_config_loader.py
git commit -m "feat: add config loader with validation"
```

---

### Task 4: State management

**Files:**
- Create: `job-aggregator/state.py`
- Create: `job-aggregator/tests/test_state.py`

- [ ] **Step 1: Write failing test**

`tests/test_state.py`:
```python
from pathlib import Path
from state import load_seen_ids, save_seen_ids

def test_load_seen_ids_empty_when_no_file(tmp_path):
    result = load_seen_ids(tmp_path / "state.json")
    assert result == set()

def test_save_and_reload(tmp_path):
    path = tmp_path / "state.json"
    ids = {"abc123", "def456"}
    save_seen_ids(ids, path)
    assert load_seen_ids(path) == ids

def test_save_is_atomic(tmp_path):
    path = tmp_path / "state.json"
    save_seen_ids({"id1"}, path)
    save_seen_ids({"id2"}, path)
    assert load_seen_ids(path) == {"id2"}
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_state.py -v
```

Expected: FAIL — `state` not found.

- [ ] **Step 3: Implement state.py**

```python
import json
from pathlib import Path

DEFAULT_STATE_PATH = Path("state.json")


def load_seen_ids(path: Path = DEFAULT_STATE_PATH) -> set[str]:
    if not path.exists():
        return set()
    data = json.loads(path.read_text())
    return set(data.get("seen_ids", []))


def save_seen_ids(ids: set[str], path: Path = DEFAULT_STATE_PATH) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"seen_ids": list(ids)}, indent=2))
    tmp.replace(path)  # atomic on POSIX
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/test_state.py -v
```

Expected: 3 PASSED.

- [ ] **Step 5: Run full test suite to confirm no regressions**

```bash
pytest tests/ -v
```

Expected: all PASSED.

- [ ] **Step 6: Commit**

```bash
git add state.py tests/test_state.py
git commit -m "feat: add atomic state management for seen job IDs"
```

---

## Chunk 2: Fetchers and Parser

### Task 5: Gmail OAuth and fetcher

**Files:**
- Create: `job-aggregator/fetchers/gmail_fetcher.py`
- Create: `job-aggregator/tests/test_gmail_fetcher.py`
- Create: `job-aggregator/tests/fixtures/linkedin_alert.html`
- Create: `job-aggregator/tests/fixtures/indeed_alert.html`

- [ ] **Step 1: Create fixture files**

`tests/fixtures/linkedin_alert.html` — minimal LinkedIn job alert email body:
```html
<html><body>
<table>
  <tr><td><a href="https://www.linkedin.com/jobs/view/123">Backend Engineer at Stripe</a></td></tr>
  <tr><td>Stripe · Singapore · Full-time</td></tr>
</table>
</body></html>
```

`tests/fixtures/indeed_alert.html` — minimal Indeed job alert email body:
```html
<html><body>
<div class="job_seen_beacon">
  <h2><a href="https://sg.indeed.com/viewjob?jk=abc">Software Engineer</a></h2>
  <span class="companyName">Shopify</span>
  <span class="companyLocation">Singapore</span>
</div>
</body></html>
```

- [ ] **Step 2: Write failing test**

`tests/test_gmail_fetcher.py`:
```python
import base64
from unittest.mock import MagicMock, patch
from fetchers.gmail_fetcher import GmailFetcher, build_search_query

def test_build_search_query_linkedin():
    query = build_search_query(sources={"linkedin_gmail": True}, days_back=1)
    assert "jobs-noreply@linkedin.com" in query

def test_build_search_query_indeed():
    query = build_search_query(sources={"indeed_gmail": True}, days_back=1)
    assert "indeed.com" in query

def test_build_search_query_respects_disabled_source():
    query = build_search_query(
        sources={"linkedin_gmail": False, "indeed_gmail": True}, days_back=1
    )
    assert "linkedin" not in query

def _make_mock_message(html_body: str) -> dict:
    encoded = base64.urlsafe_b64encode(html_body.encode()).decode()
    return {
        "id": "msg123",
        "payload": {
            "mimeType": "text/html",
            "body": {"data": encoded},
        },
    }

def test_extract_raw_html():
    from fetchers.gmail_fetcher import extract_html_body
    from pathlib import Path
    html = Path("tests/fixtures/linkedin_alert.html").read_text()
    msg = _make_mock_message(html)
    assert "Backend Engineer" in extract_html_body(msg)
```

- [ ] **Step 3: Run test to verify it fails**

```bash
pytest tests/test_gmail_fetcher.py -v
```

Expected: FAIL — `fetchers.gmail_fetcher` not found.

- [ ] **Step 4: Implement fetchers/gmail_fetcher.py**

```python
import base64
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly",
          "https://www.googleapis.com/auth/gmail.send"]
CREDENTIALS_DIR = Path("credentials")

SENDER_MAP = {
    "linkedin_gmail": "from:jobs-noreply@linkedin.com",
    "indeed_gmail": "from:(jobalert@indeed.com OR alert@sg.indeed.com OR alert@jp.indeed.com OR alert@tw.indeed.com)",
}


def build_search_query(sources: dict, days_back: int = 1) -> str:
    parts = []
    for key, sender_query in SENDER_MAP.items():
        if sources.get(key):
            parts.append(sender_query)
    if not parts:
        return ""
    sender_part = " OR ".join(f"({p})" for p in parts)
    since = (datetime.now() - timedelta(days=days_back)).strftime("%Y/%m/%d")
    return f"({sender_part}) after:{since}"


def extract_html_body(message: dict) -> str:
    payload = message.get("payload", {})
    parts = payload.get("parts", [])
    if parts:
        for part in parts:
            if part.get("mimeType") == "text/html":
                data = part["body"].get("data", "")
                return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    data = payload.get("body", {}).get("data", "")
    if data:
        return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    return ""


class GmailFetcher:
    def __init__(self, credentials_dir: Path = CREDENTIALS_DIR):
        self.credentials_dir = credentials_dir
        self.service = self._authenticate()

    def _authenticate(self):
        token_path = self.credentials_dir / "token.json"
        secret_path = self.credentials_dir / "client_secret.json"
        creds = None
        if token_path.exists():
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(str(secret_path), SCOPES)
                creds = flow.run_local_server(port=0)
            token_path.write_text(creds.to_json())
        return build("gmail", "v1", credentials=creds)

    def fetch_alert_messages(self, sources: dict, days_back: int = 1) -> Iterator[dict]:
        query = build_search_query(sources, days_back)
        if not query:
            return
        response = self.service.users().messages().list(userId="me", q=query).execute()
        messages = response.get("messages", [])
        for msg_ref in messages:
            msg = self.service.users().messages().get(
                userId="me", id=msg_ref["id"], format="full"
            ).execute()
            yield msg

    def send_email(self, to: str, from_: str, subject: str, html_body: str) -> None:
        import base64
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = from_
        msg["To"] = to
        msg.attach(MIMEText(html_body, "html"))
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        self.service.users().messages().send(
            userId="me", body={"raw": raw}
        ).execute()
```

- [ ] **Step 5: Run test to verify it passes**

```bash
pytest tests/test_gmail_fetcher.py -v
```

Expected: 4 PASSED.

- [ ] **Step 6: Commit**

```bash
git add fetchers/gmail_fetcher.py tests/test_gmail_fetcher.py tests/fixtures/
git commit -m "feat: add Gmail fetcher with OAuth and search query builder"
```

---

### Task 6: RSS fetcher

**Files:**
- Create: `job-aggregator/fetchers/rss_fetcher.py`
- Create: `job-aggregator/tests/test_rss_fetcher.py`
- Create: `job-aggregator/tests/fixtures/104_rss.xml`

- [ ] **Step 1: Create RSS fixture**

`tests/fixtures/104_rss.xml`:
```xml
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>104人力銀行 - Backend Engineer</title>
    <item>
      <title>後端工程師 (Backend Engineer)</title>
      <link>https://www.104.com.tw/job/abc123</link>
      <description>負責後端系統開發，3年以上經驗。公司：Appier</description>
    </item>
  </channel>
</rss>
```

- [ ] **Step 2: Write failing test**

`tests/test_rss_fetcher.py`:
```python
from pathlib import Path
from unittest.mock import patch, MagicMock
from fetchers.rss_fetcher import RSSFetcher, build_rss_urls

def test_build_rss_urls_includes_enabled_sources():
    sources = {"104": True, "wellfound": True, "indeed_rss": True, "yourator": False}
    markets = ["tw", "sg"]
    urls = build_rss_urls(sources, markets, keyword="Backend Engineer")
    assert any("104" in u for u in urls)
    assert any("wellfound" in u for u in urls)
    assert not any("yourator" in u for u in urls)

def test_build_rss_urls_indeed_per_market():
    sources = {"indeed_rss": True}
    markets = ["tw", "jp", "sg"]
    urls = build_rss_urls(sources, markets, keyword="Backend Engineer")
    assert any("tw.indeed" in u for u in urls)
    assert any("jp.indeed" in u for u in urls)
    assert any("sg.indeed" in u for u in urls)

def test_parse_feed_from_fixture():
    from fetchers.rss_fetcher import parse_feed_entries
    import feedparser
    content = Path("tests/fixtures/104_rss.xml").read_bytes()
    feed = feedparser.parse(content)
    entries = parse_feed_entries(feed, source="104", market="tw")
    assert len(entries) == 1
    assert entries[0]["title"] == "後端工程師 (Backend Engineer)"
    assert entries[0]["source"] == "104"
    assert entries[0]["market"] == "tw"
```

- [ ] **Step 3: Run test to verify it fails**

```bash
pytest tests/test_rss_fetcher.py -v
```

Expected: FAIL — `fetchers.rss_fetcher` not found.

- [ ] **Step 4: Implement fetchers/rss_fetcher.py**

```python
from urllib.parse import quote_plus
import feedparser


INDEED_DOMAINS = {"tw": "tw.indeed.com", "jp": "jp.indeed.com", "sg": "sg.indeed.com"}


def build_rss_urls(sources: dict, markets: list[str], keyword: str) -> list[tuple[str, str, str]]:
    """Returns list of (url, source_name, market)."""
    kw = quote_plus(keyword)
    results = []
    if sources.get("104"):
        results.append((
            f"https://www.104.com.tw/jobs/search/rss?keyword={kw}&jobsource=2018indexpoc",
            "104", "tw"
        ))
    wellfound_markets = [m for m in markets if m in ("tw", "sg")]
    if sources.get("wellfound") and wellfound_markets:
        for market in wellfound_markets:
            results.append((
                f"https://wellfound.com/jobs.rss?keywords={kw}",
                "wellfound", market
            ))
    if sources.get("indeed_rss"):
        for market in markets:
            domain = INDEED_DOMAINS.get(market)
            if domain:
                results.append((
                    f"https://{domain}/rss?q={kw}&sort=date",
                    "indeed", market
                ))
    return results


def parse_feed_entries(feed, source: str, market: str) -> list[dict]:
    entries = []
    for entry in feed.entries:
        entries.append({
            "title": entry.get("title", "").strip(),
            "url": entry.get("link", ""),
            "description": entry.get("summary", ""),
            "source": source,
            "market": market,
            "company": "",  # extracted by parser from description
            "location": "",
        })
    return entries


class RSSFetcher:
    def fetch_all(self, sources: dict, markets: list[str], titles: list[str]) -> list[dict]:
        raw = []
        for title in titles:
            for url, source, market in build_rss_urls(sources, markets, title):
                feed = feedparser.parse(url)
                raw.extend(parse_feed_entries(feed, source, market))
        return raw
```

- [ ] **Step 5: Run test to verify it passes**

```bash
pytest tests/test_rss_fetcher.py -v
```

Expected: 3 PASSED.

- [ ] **Step 6: Commit**

```bash
git add fetchers/rss_fetcher.py tests/test_rss_fetcher.py tests/fixtures/104_rss.xml
git commit -m "feat: add RSS fetcher for 104, Wellfound, Indeed feeds"
```

---

### Task 7: Web scraper (CakeResume + Yourator)

**Files:**
- Create: `job-aggregator/fetchers/scraper.py`
- Create: `job-aggregator/tests/test_scraper.py`

- [ ] **Step 1: Write failing test**

`tests/test_scraper.py`:
```python
from unittest.mock import AsyncMock, patch, MagicMock
from fetchers.scraper import normalize_cakeresume_item, normalize_yourator_item

def test_normalize_cakeresume_item():
    raw = {
        "title": "Backend Engineer",
        "company": {"name": "Appier"},
        "location": "台北市",
        "url": "https://www.cakeresume.com/jobs/backend-eng",
        "description": "We are hiring a backend engineer...",
    }
    result = normalize_cakeresume_item(raw, market="tw")
    assert result["title"] == "Backend Engineer"
    assert result["company"] == "Appier"
    assert result["source"] == "cakeresume"
    assert result["market"] == "tw"

def test_normalize_yourator_item():
    raw = {
        "job_title": "Software Engineer",
        "company_name": "Appier",
        "location": "Taipei",
        "job_url": "https://www.yourator.co/companies/appier/jobs/123",
        "job_description": "We need an SWE...",
    }
    result = normalize_yourator_item(raw, market="tw")
    assert result["title"] == "Software Engineer"
    assert result["company"] == "Appier"
    assert result["source"] == "yourator"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_scraper.py -v
```

Expected: FAIL — `fetchers.scraper` not found.

- [ ] **Step 3: Implement fetchers/scraper.py**

```python
import asyncio
from playwright.async_api import async_playwright
from urllib.parse import quote_plus


def normalize_cakeresume_item(raw: dict, market: str) -> dict:
    return {
        "title": raw.get("title", ""),
        "company": raw.get("company", {}).get("name", "") if isinstance(raw.get("company"), dict) else raw.get("company", ""),
        "location": raw.get("location", ""),
        "url": raw.get("url", ""),
        "description": raw.get("description", ""),
        "source": "cakeresume",
        "market": market,
    }


def normalize_yourator_item(raw: dict, market: str) -> dict:
    return {
        "title": raw.get("job_title", ""),
        "company": raw.get("company_name", ""),
        "location": raw.get("location", ""),
        "url": raw.get("job_url", ""),
        "description": raw.get("job_description", ""),
        "source": "yourator",
        "market": market,
    }


async def _scrape_cakeresume(keyword: str) -> list[dict]:
    results = []
    kw = quote_plus(keyword)
    url = f"https://www.cakeresume.com/jobs?q={kw}&locale=tw"
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(url, timeout=30000)
        await page.wait_for_selector(".JobSearchResult_jobItem__KPTmH", timeout=10000)
        items = await page.query_selector_all(".JobSearchResult_jobItem__KPTmH")
        for item in items[:20]:
            title_el = await item.query_selector("h3")
            company_el = await item.query_selector(".JobSearchResult_companyName__9_jMZ")
            link_el = await item.query_selector("a")
            title = await title_el.inner_text() if title_el else ""
            company = await company_el.inner_text() if company_el else ""
            href = await link_el.get_attribute("href") if link_el else ""
            if title:
                results.append(normalize_cakeresume_item(
                    {"title": title, "company": company, "url": f"https://www.cakeresume.com{href}"},
                    market="tw"
                ))
        await browser.close()
    return results


async def _scrape_yourator(keyword: str) -> list[dict]:
    results = []
    kw = quote_plus(keyword)
    url = f"https://www.yourator.co/jobs?term={kw}"
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(url, timeout=30000)
        await page.wait_for_selector(".job-list-item", timeout=10000)
        items = await page.query_selector_all(".job-list-item")
        for item in items[:20]:
            title_el = await item.query_selector(".job-title")
            company_el = await item.query_selector(".company-name")
            link_el = await item.query_selector("a")
            title = await title_el.inner_text() if title_el else ""
            company = await company_el.inner_text() if company_el else ""
            href = await link_el.get_attribute("href") if link_el else ""
            if title:
                results.append(normalize_yourator_item(
                    {"job_title": title, "company_name": company, "job_url": f"https://www.yourator.co{href}"},
                    market="tw"
                ))
        await browser.close()
    return results


class WebScraper:
    def fetch_all(self, sources: dict, titles: list[str]) -> list[dict]:
        results = []
        for title in titles:
            if sources.get("cakeresume"):
                try:
                    results.extend(asyncio.run(_scrape_cakeresume(title)))
                except Exception as e:
                    print(f"[WARN] CakeResume scrape failed for '{title}': {e}")
            if sources.get("yourator"):
                try:
                    results.extend(asyncio.run(_scrape_yourator(title)))
                except Exception as e:
                    print(f"[WARN] Yourator scrape failed for '{title}': {e}")
        return results
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/test_scraper.py -v
```

Expected: 2 PASSED.

- [ ] **Step 5: Commit**

```bash
git add fetchers/scraper.py tests/test_scraper.py
git commit -m "feat: add Playwright scraper for CakeResume and Yourator"
```

---

### Task 7b: Gmail message parser — extract job listings from alert emails

**Files:**
- Create: `job-aggregator/fetchers/gmail_parser.py`
- Create: `job-aggregator/tests/test_gmail_parser.py`

- [ ] **Step 1: Write failing test**

`tests/test_gmail_parser.py`:
```python
from pathlib import Path
from fetchers.gmail_parser import parse_gmail_message

def test_parse_linkedin_alert():
    html = Path("tests/fixtures/linkedin_alert.html").read_text()
    entries = parse_gmail_message(html, source="linkedin", market="sg")
    assert len(entries) == 1
    assert entries[0]["title"] == "Backend Engineer at Stripe"
    assert entries[0]["source"] == "linkedin"
    assert entries[0]["market"] == "sg"

def test_parse_indeed_alert():
    html = Path("tests/fixtures/indeed_alert.html").read_text()
    entries = parse_gmail_message(html, source="indeed", market="sg")
    assert len(entries) == 1
    assert "Software Engineer" in entries[0]["title"]
    assert entries[0]["company"] == "Shopify"

def test_parse_returns_empty_for_blank_html():
    entries = parse_gmail_message("", source="linkedin", market="tw")
    assert entries == []
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_gmail_parser.py -v
```

Expected: FAIL — `fetchers.gmail_parser` not found.

- [ ] **Step 3: Implement fetchers/gmail_parser.py**

```python
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
    pattern = re.escape(title) + r".{0,200}?<span[^>]*class="[^"]*company[^"]*"[^>]*>([^<]+)<"
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
```

- [ ] **Step 4: Update fixtures to match parser expectations**

Update `tests/fixtures/linkedin_alert.html`:
```html
<html><body>
<table>
  <tr><td><a href="https://www.linkedin.com/jobs/view/123">Backend Engineer at Stripe</a></td></tr>
</table>
</body></html>
```

Update `tests/fixtures/indeed_alert.html`:
```html
<html><body>
<div>
  <a href="https://sg.indeed.com/viewjob?jk=abc">Software Engineer</a>
  <span class="companyName">Shopify</span>
</div>
</body></html>
```

- [ ] **Step 5: Run test to verify it passes**

```bash
pytest tests/test_gmail_parser.py -v
```

Expected: 3 PASSED.

- [ ] **Step 6: Commit**

```bash
git add fetchers/gmail_parser.py tests/test_gmail_parser.py tests/fixtures/
git commit -m "feat: add Gmail alert HTML parser for LinkedIn and Indeed emails"
```

---

### Task 8: Parser — normalize all sources to Job schema

**Files:**
- Create: `job-aggregator/parser.py`
- Create: `job-aggregator/tests/test_parser.py`

- [ ] **Step 1: Write failing test**

`tests/test_parser.py`:
```python
from parser import parse_raw_entry
from models import Job

def test_parse_raw_entry_rss():
    raw = {
        "title": "Backend Engineer",
        "company": "Stripe",
        "location": "Singapore",
        "market": "sg",
        "url": "https://sg.indeed.com/job/123",
        "description": "We need a backend engineer with 3+ years.",
        "source": "indeed",
    }
    job = parse_raw_entry(raw)
    assert isinstance(job, Job)
    assert job.title == "Backend Engineer"
    assert job.company == "Stripe"
    assert job.market == "sg"
    assert job.sources == ["indeed"]
    assert job.id == job.id  # deterministic

def test_parse_raw_entry_strips_whitespace():
    raw = {
        "title": "  Software Engineer  ",
        "company": " Shopify ",
        "location": "Taiwan",
        "market": "tw",
        "url": "https://example.com",
        "description": "desc",
        "source": "104",
    }
    job = parse_raw_entry(raw)
    assert job.title == "Software Engineer"
    assert job.company == "Shopify"

def test_parse_raw_entry_empty_company_returns_none():
    raw = {
        "title": "Engineer",
        "company": "",
        "location": "", "market": "tw",
        "url": "https://x.com", "description": "", "source": "cakeresume",
    }
    job = parse_raw_entry(raw)
    assert job is None
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_parser.py -v
```

Expected: FAIL — `parser` not found.

- [ ] **Step 3: Implement parser.py**

```python
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
    )


def parse_all(raw_entries: list[dict]) -> list[Job]:
    jobs = []
    for entry in raw_entries:
        job = parse_raw_entry(entry)
        if job:
            jobs.append(job)
    return jobs
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/test_parser.py -v
```

Expected: 3 PASSED.

- [ ] **Step 5: Run full test suite**

```bash
pytest tests/ -v
```

Expected: all PASSED.

- [ ] **Step 6: Commit**

```bash
git add parser.py tests/test_parser.py
git commit -m "feat: add parser to normalize raw entries into Job schema"
```

---

## Chunk 3: Processing Pipeline

### Task 9: Deduplicator

**Files:**
- Create: `job-aggregator/deduplicator.py`
- Create: `job-aggregator/tests/test_deduplicator.py`

- [ ] **Step 1: Write failing test**

`tests/test_deduplicator.py`:
```python
from models import Job, make_job_id
from deduplicator import deduplicate, remove_seen

def _job(title, company, source, market="tw"):
    return Job(
        id=make_job_id(company, title),
        title=title, company=company,
        location="Taipei", market=market,
        url="https://x.com", description="",
        sources=[source],
    )

def test_deduplicate_merges_same_job_across_sources():
    jobs = [
        _job("Backend Engineer", "Stripe", "linkedin"),
        _job("Backend Engineer", "Stripe", "indeed"),
    ]
    result = deduplicate(jobs)
    assert len(result) == 1
    assert set(result[0].sources) == {"linkedin", "indeed"}

def test_deduplicate_keeps_different_jobs():
    jobs = [
        _job("Backend Engineer", "Stripe", "linkedin"),
        _job("Frontend Engineer", "Stripe", "linkedin"),
    ]
    result = deduplicate(jobs)
    assert len(result) == 2

def test_remove_seen_filters_known_ids():
    jobs = [
        _job("Backend Engineer", "Stripe", "linkedin"),
        _job("Frontend Engineer", "Shopify", "indeed"),
    ]
    seen = {jobs[0].id}
    result = remove_seen(jobs, seen)
    assert len(result) == 1
    assert result[0].title == "Frontend Engineer"

def test_remove_seen_empty_seen_returns_all():
    jobs = [_job("SWE", "Stripe", "linkedin")]
    assert remove_seen(jobs, set()) == jobs
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_deduplicator.py -v
```

Expected: FAIL — `deduplicator` not found.

- [ ] **Step 3: Implement deduplicator.py**

```python
from models import Job


def deduplicate(jobs: list[Job]) -> list[Job]:
    seen: dict[str, Job] = {}
    for job in jobs:
        if job.id in seen:
            existing = seen[job.id]
            merged_sources = list(set(existing.sources + job.sources))
            seen[job.id] = Job(
                id=existing.id, title=existing.title, company=existing.company,
                location=existing.location, market=existing.market, url=existing.url,
                description=existing.description or job.description,
                sources=merged_sources,
                industry=existing.industry, stage=existing.stage,
                fetched_at=existing.fetched_at,
            )
        else:
            seen[job.id] = job
    return list(seen.values())


def remove_seen(jobs: list[Job], seen_ids: set[str]) -> list[Job]:
    return [j for j in jobs if j.id not in seen_ids]
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/test_deduplicator.py -v
```

Expected: 4 PASSED.

- [ ] **Step 5: Commit**

```bash
git add deduplicator.py tests/test_deduplicator.py
git commit -m "feat: add cross-platform and cross-day deduplicator"
```

---

### Task 10: Matcher — Claude Haiku filtering

**Files:**
- Create: `job-aggregator/matcher.py`
- Create: `job-aggregator/tests/test_matcher.py`

- [ ] **Step 1: Write failing test**

`tests/test_matcher.py`:
```python
import pytest
from unittest.mock import MagicMock, patch
from models import Job, make_job_id
from matcher import Matcher, build_prompt

def _job(title, company, description=""):
    return Job(
        id=make_job_id(company, title),
        title=title, company=company,
        location="Singapore", market="sg",
        url="https://x.com", description=description,
        sources=["linkedin"],
    )

def test_build_prompt_includes_job_info():
    job = _job("Backend Engineer", "Stripe", "We build payments infra")
    targets = {"titles": ["Backend Engineer"], "experience_years": "3-5", "exclude_keywords": []}
    prompt = build_prompt(job, targets)
    assert "Backend Engineer" in prompt
    assert "Stripe" in prompt
    assert "3-5" in prompt

def test_matcher_filters_passing_job(mocker):
    mock_client = MagicMock()
    mock_client.messages.create.return_value = MagicMock(
        content=[MagicMock(text='{"is_software_industry": true, "matches_target_role": true, "reason": "matches"}')]
    )
    matcher = Matcher(client=mock_client)
    job = _job("Backend Engineer", "Stripe")
    result = matcher.filter([job], targets={"titles": ["Backend Engineer"], "experience_years": "3-5", "exclude_keywords": []})
    assert len(result) == 1

def test_matcher_removes_failing_job(mocker):
    mock_client = MagicMock()
    mock_client.messages.create.return_value = MagicMock(
        content=[MagicMock(text='{"is_software_industry": false, "matches_target_role": false, "reason": "bank"}')]
    )
    matcher = Matcher(client=mock_client)
    job = _job("Backend Engineer", "Goldman Sachs")
    result = matcher.filter([job], targets={"titles": ["Backend Engineer"], "experience_years": "3-5", "exclude_keywords": []})
    assert len(result) == 0

def test_matcher_handles_malformed_json(mocker):
    mock_client = MagicMock()
    mock_client.messages.create.return_value = MagicMock(
        content=[MagicMock(text="Sorry, I can't help with that.")]
    )
    matcher = Matcher(client=mock_client)
    job = _job("SWE", "Unknown Corp")
    # Should not raise; malformed response = exclude
    result = matcher.filter([job], targets={"titles": ["SWE"], "experience_years": "3+", "exclude_keywords": []})
    assert len(result) == 0
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_matcher.py -v
```

Expected: FAIL — `matcher` not found.

- [ ] **Step 3: Implement matcher.py**

```python
import json
from anthropic import Anthropic
from models import Job

MODEL = "claude-haiku-4-5-20251001"


def build_prompt(job: Job, targets: dict) -> str:
    titles = ", ".join(targets.get("titles", []))
    experience = targets.get("experience_years", "")
    excludes = ", ".join(targets.get("exclude_keywords", []))
    return f"""You are a job relevance filter. Evaluate this job listing.

Job:
- Title: {job.title}
- Company: {job.company}
- Location: {job.location}
- Description: {job.description[:500]}

Target criteria:
- Target roles: {titles}
- Experience: {experience} years
- Exclude if contains: {excludes}

Respond with JSON only, no other text:
{{"is_software_industry": bool, "matches_target_role": bool, "reason": "one sentence"}}"""


class Matcher:
    def __init__(self, client: Anthropic = None):
        self.client = client or Anthropic()

    def _evaluate(self, job: Job, targets: dict) -> bool:
        prompt = build_prompt(job, targets)
        try:
            response = self.client.messages.create(
                model=MODEL,
                max_tokens=100,
                messages=[{"role": "user", "content": prompt}],
            )
            text = response.content[0].text.strip()
            result = json.loads(text)
            return result.get("is_software_industry", False) and result.get("matches_target_role", False)
        except (json.JSONDecodeError, KeyError, IndexError):
            return False

    def filter(self, jobs: list[Job], targets: dict) -> list[Job]:
        return [job for job in jobs if self._evaluate(job, targets)]
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/test_matcher.py -v
```

Expected: 4 PASSED.

- [ ] **Step 5: Commit**

```bash
git add matcher.py tests/test_matcher.py
git commit -m "feat: add Claude Haiku matcher for software industry + role filtering"
```

---

### Task 11: Enricher — company industry and stage

**Files:**
- Create: `job-aggregator/enricher.py`
- Create: `job-aggregator/tests/test_enricher.py`

- [ ] **Step 1: Write failing test**

`tests/test_enricher.py`:
```python
from unittest.mock import MagicMock
from models import Job, make_job_id
from enricher import Enricher

def _job(title, company, source="linkedin"):
    return Job(
        id=make_job_id(company, title),
        title=title, company=company,
        location="SG", market="sg",
        url="https://x.com", description="We build marketplace software.",
        sources=[source],
    )

def test_enricher_wellfound_uses_native_data():
    job = _job("SWE", "Stripe", source="wellfound")
    job.industry = "fintech"
    job.stage = "public"
    enricher = Enricher(client=MagicMock())
    result = enricher.enrich([job])
    # wellfound jobs already have data; enricher should not overwrite
    assert result[0].industry == "fintech"
    assert result[0].stage == "public"

def test_enricher_infers_for_non_wellfound(mocker):
    mock_client = MagicMock()
    mock_client.messages.create.return_value = MagicMock(
        content=[MagicMock(text='{"industry": "marketplace", "stage": "series_b"}')]
    )
    enricher = Enricher(client=mock_client)
    job = _job("SWE", "Shopee", source="indeed")
    result = enricher.enrich([job])
    assert result[0].industry == "marketplace"
    assert result[0].stage == "series_b"

def test_enricher_handles_null_gracefully(mocker):
    mock_client = MagicMock()
    mock_client.messages.create.return_value = MagicMock(
        content=[MagicMock(text='{"industry": null, "stage": null}')]
    )
    enricher = Enricher(client=mock_client)
    job = _job("SWE", "Unknown Corp", source="104")
    result = enricher.enrich([job])
    assert result[0].industry is None
    assert result[0].stage is None
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_enricher.py -v
```

Expected: FAIL — `enricher` not found.

- [ ] **Step 3: Implement enricher.py**

```python
import json
from anthropic import Anthropic
from models import Job

MODEL = "claude-haiku-4-5-20251001"


def _build_enrich_prompt(job: Job) -> str:
    return f"""Classify this company based on the job listing.

Company: {job.company}
Job title: {job.title}
Description excerpt: {job.description[:300]}

Respond with JSON only:
{{"industry": "one of: saas, marketplace, fintech, crypto, social, ecommerce, infra, healthtech, edtech, gaming, other, or null if unknown",
  "stage": "one of: pre-seed, seed, series_a, series_b, series_c, pre-ipo, public, or null if unknown"}}"""


class Enricher:
    def __init__(self, client: Anthropic = None):
        self.client = client or Anthropic()

    def _infer(self, job: Job) -> tuple[str | None, str | None]:
        try:
            response = self.client.messages.create(
                model=MODEL,
                max_tokens=80,
                messages=[{"role": "user", "content": _build_enrich_prompt(job)}],
            )
            data = json.loads(response.content[0].text.strip())
            return data.get("industry"), data.get("stage")
        except (json.JSONDecodeError, KeyError, IndexError):
            return None, None

    def enrich(self, jobs: list[Job]) -> list[Job]:
        enriched = []
        for job in jobs:
            if "wellfound" in job.sources and job.industry is not None:
                enriched.append(job)
                continue
            industry, stage = self._infer(job)
            job.industry = industry
            job.stage = stage
            enriched.append(job)
        return enriched
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/test_enricher.py -v
```

Expected: 3 PASSED.

- [ ] **Step 5: Run full test suite**

```bash
pytest tests/ -v
```

Expected: all PASSED.

- [ ] **Step 6: Commit**

```bash
git add enricher.py tests/test_enricher.py
git commit -m "feat: add enricher for company industry and stage via Haiku"
```

---

## Chunk 4: Output and Orchestration

### Task 12: Notifier — daily summary email

**Files:**
- Create: `job-aggregator/notifier.py`
- Create: `job-aggregator/tests/test_notifier.py`

- [ ] **Step 1: Write failing test**

`tests/test_notifier.py`:
```python
from datetime import date
from models import Job, make_job_id
from notifier import build_email_html, build_subject

def _job(title, company, market="tw", industry="saas", stage="series_b"):
    j = Job(
        id=make_job_id(company, title),
        title=title, company=company, location="Taipei",
        market=market, url="https://example.com/job/1",
        description="", sources=["linkedin"],
    )
    j.industry = industry
    j.stage = stage
    return j

def test_build_subject_with_matches():
    jobs = [_job("SWE", "Stripe", "sg"), _job("Backend", "Shopify", "tw")]
    subject = build_subject(jobs, date(2026, 3, 11))
    assert "2026-03-11" in subject
    assert "2" in subject

def test_build_subject_no_matches():
    subject = build_subject([], date(2026, 3, 11))
    assert "No new matches" in subject

def test_build_email_html_contains_job_info():
    jobs = [_job("Backend Engineer", "Stripe", "sg", "fintech", "public")]
    html = build_email_html(jobs, date(2026, 3, 11))
    assert "Backend Engineer" in html
    assert "Stripe" in html
    assert "fintech" in html
    assert "public" in html
    assert "https://example.com/job/1" in html

def test_build_email_html_empty():
    html = build_email_html([], date(2026, 3, 11))
    assert "No new matches" in html
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_notifier.py -v
```

Expected: FAIL — `notifier` not found.

- [ ] **Step 3: Implement notifier.py**

```python
from datetime import date
from models import Job


def build_subject(jobs: list[Job], run_date: date) -> str:
    if not jobs:
        return f"[Job Digest] {run_date} — No new matches today"
    by_market: dict[str, int] = {}
    for job in jobs:
        by_market[job.market] = by_market.get(job.market, 0) + 1
    market_str = ", ".join(f"{m.upper()}: {c}" for m, c in sorted(by_market.items()))
    return f"[Job Digest] {run_date} — {len(jobs)} new matches ({market_str})"


def build_email_html(jobs: list[Job], run_date: date) -> str:
    if not jobs:
        return f"<p>No new matches today ({run_date}). The tool ran successfully.</p>"

    rows = []
    for job in jobs:
        industry = job.industry or "—"
        stage = job.stage or "—"
        sources = ", ".join(job.sources)
        rows.append(f"""
<tr style="border-bottom:1px solid #eee">
  <td style="padding:12px 8px">
    <strong><a href="{job.url}" style="color:#1a73e8">{job.title}</a></strong>
    — {job.company} ({job.market.upper()})<br>
    <small>Industry: {industry} &nbsp;|&nbsp; Stage: {stage} &nbsp;|&nbsp; Sources: {sources}</small>
  </td>
</tr>""")

    rows_html = "\n".join(rows)
    return f"""<html><body style="font-family:sans-serif;max-width:700px;margin:auto">
<h2 style="color:#333">Job Digest — {run_date}</h2>
<p>{len(jobs)} new matches</p>
<table style="width:100%;border-collapse:collapse">
{rows_html}
</table>
<hr><p style="color:#999;font-size:12px">Generated by job-aggregator</p>
</body></html>"""
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/test_notifier.py -v
```

Expected: 4 PASSED.

- [ ] **Step 5: Commit**

```bash
git add notifier.py tests/test_notifier.py
git commit -m "feat: add email notifier with HTML digest builder"
```

---

### Task 13: Interactive setup CLI

**Files:**
- Create: `job-aggregator/setup_cli.py`

- [ ] **Step 1: Implement setup_cli.py**

No unit tests for interactive CLI — test manually.

```python
#!/usr/bin/env python3
"""Interactive CLI to configure job-aggregator."""
import sys
from pathlib import Path
import yaml

CONFIG_PATH = Path("config.yaml")


def prompt(question: str, default: str = "") -> str:
    display = f"{question} [{default}]: " if default else f"{question}: "
    answer = input(display).strip()
    return answer if answer else default


def prompt_list(question: str, default: list[str]) -> list[str]:
    print(f"{question} (comma-separated) [{', '.join(default)}]: ", end="")
    answer = input().strip()
    if not answer:
        return default
    return [item.strip() for item in answer.split(",") if item.strip()]


def prompt_bool(question: str, default: bool) -> bool:
    default_str = "Y/n" if default else "y/N"
    answer = input(f"{question} [{default_str}]: ").strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes")


def run_setup():
    print("\n=== Job Aggregator Setup ===\n")

    existing = {}
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            existing = yaml.safe_load(f) or {}
        print("Updating existing config. Press Enter to keep current values.\n")

    # Markets
    markets = prompt_list(
        "Markets to search (tw/jp/sg)",
        existing.get("markets", ["tw", "jp", "sg"])
    )

    # Target roles
    targets = existing.get("targets", {})
    titles = prompt_list(
        "Target job titles",
        targets.get("titles", ["Backend Engineer", "Software Engineer"])
    )
    experience = prompt("Experience years (e.g. 3-5)", targets.get("experience_years", "3-5"))
    excludes = prompt_list(
        "Exclude keywords",
        targets.get("exclude_keywords", ["outsourcing", "派遣"])
    )

    # Sources
    src = existing.get("sources", {})
    sources = {
        "linkedin_gmail": prompt_bool("Enable LinkedIn (Gmail)", src.get("linkedin_gmail", True)),
        "indeed_gmail": prompt_bool("Enable Indeed (Gmail)", src.get("indeed_gmail", True)),
        "indeed_rss": prompt_bool("Enable Indeed (RSS)", src.get("indeed_rss", True)),
        "104": prompt_bool("Enable 104", src.get("104", True)),
        "cakeresume": prompt_bool("Enable CakeResume", src.get("cakeresume", True)),
        "yourator": prompt_bool("Enable Yourator", src.get("yourator", True)),
        "wellfound": prompt_bool("Enable Wellfound", src.get("wellfound", True)),
    }

    # Notification
    notif = existing.get("notification", {})
    email_to = prompt("Send digest to (email)", notif.get("to", ""))
    email_from = prompt("Send digest from (email)", notif.get("from", email_to))

    config = {
        "markets": markets,
        "targets": {
            "titles": titles,
            "experience_years": experience,
            "exclude_keywords": excludes,
        },
        "sources": sources,
        "notification": {"to": email_to, "from": email_from},
    }

    CONFIG_PATH.write_text(yaml.dump(config, allow_unicode=True, default_flow_style=False))
    print(f"\n✓ Config saved to {CONFIG_PATH}")
    print("\nNext: place your Google OAuth client_secret.json in credentials/")
    print("Then run: python main.py\n")


if __name__ == "__main__":
    run_setup()
```

- [ ] **Step 2: Manual test**

```bash
cd job-aggregator && python setup_cli.py
```

Expected: interactive prompts appear, config.yaml is created after answering.

- [ ] **Step 3: Commit**

```bash
git add setup_cli.py
git commit -m "feat: add interactive setup CLI"
```

---

### Task 14: Main orchestrator

**Files:**
- Create: `job-aggregator/main.py`

- [ ] **Step 1: Implement main.py**

```python
#!/usr/bin/env python3
"""Daily job aggregator — run via cron."""
import sys
from datetime import date
from pathlib import Path

from config_loader import load_config, ConfigError
from state import load_seen_ids, save_seen_ids
from fetchers.gmail_fetcher import GmailFetcher, extract_html_body
from fetchers.gmail_parser import parse_gmail_message
from fetchers.rss_fetcher import RSSFetcher
from fetchers.scraper import WebScraper
from parser import parse_all
from deduplicator import deduplicate, remove_seen
from matcher import Matcher
from enricher import Enricher
from notifier import build_email_html, build_subject


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

    # 2. Fetch
    raw_entries = []
    gmail = GmailFetcher()

    print("[1/6] Fetching Gmail alerts...")
    for msg in gmail.fetch_alert_messages(sources, days_back=1):
        html = extract_html_body(msg)
        # Determine source from sender header
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        sender = headers.get("From", "")
        source = "linkedin" if "linkedin" in sender else "indeed"
        market = next((m for m in markets if m in headers.get("Subject", "").lower()), markets[0])
        raw_entries.extend(parse_gmail_message(html, source=source, market=market))

    print("[2/6] Fetching RSS feeds...")
    rss = RSSFetcher()
    raw_entries.extend(rss.fetch_all(sources, markets, titles))

    print("[3/6] Scraping CakeResume / Yourator...")
    scraper = WebScraper()
    raw_entries.extend(scraper.fetch_all(sources, titles))

    # 3. Parse
    print("[4/6] Parsing and normalizing...")
    jobs = parse_all(raw_entries)

    # 4. Deduplicate (cross-platform + cross-day)
    jobs = deduplicate(jobs)
    seen_ids = load_seen_ids()
    jobs = remove_seen(jobs, seen_ids)

    # 5. Filter
    print(f"[5/6] Filtering {len(jobs)} new jobs with Claude Haiku...")
    matcher = Matcher()
    jobs = matcher.filter(jobs, targets)

    # 6. Enrich
    enricher = Enricher()
    jobs = enricher.enrich(jobs)

    # 7. Send email
    print(f"[6/6] Sending digest ({len(jobs)} matches)...")
    today = date.today()
    subject = build_subject(jobs, today)
    html = build_email_html(jobs, today)
    gmail.send_email(
        to=notif["to"],
        from_=notif["from"],
        subject=subject,
        html_body=html,
    )

    # 8. Update state
    new_seen = seen_ids | {j.id for j in jobs}
    save_seen_ids(new_seen)

    print(f"Done. {len(jobs)} matches sent to {notif['to']}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run full test suite one final time**

```bash
pytest tests/ -v
```

Expected: all PASSED.

- [ ] **Step 3: Commit**

```bash
git add main.py
git commit -m "feat: add main orchestrator wiring all pipeline stages"
```

---

### Task 15: Cron setup and README

**Files:**
- Create: `job-aggregator/README.md`

- [ ] **Step 1: Create README.md**

```markdown
# Job Aggregator

Daily job digest from LinkedIn, Indeed, 104, CakeResume, Yourator, Wellfound.

## Setup

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   playwright install chromium
   ```

2. Get Google OAuth credentials:
   - Go to https://console.cloud.google.com
   - Create project → Enable Gmail API → OAuth 2.0 Client ID (Desktop app)
   - Download as `credentials/client_secret.json`

3. Run interactive setup:
   ```bash
   python setup_cli.py
   ```

4. Test run:
   ```bash
   python main.py
   ```
   First run will open browser for Gmail OAuth. Token saved to `credentials/token.json`.

## Cron (daily at 8am)

```bash
crontab -e
```

Add:
```
0 8 * * * cd /path/to/job-aggregator && /usr/bin/python3 main.py >> logs/job-aggregator.log 2>&1
```

Create log dir:
```bash
mkdir -p /path/to/job-aggregator/logs
```

## Updating search criteria

```bash
python setup_cli.py
```

Or edit `config.yaml` directly — changes take effect on next run.
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: add setup and cron instructions"
```

---

*End of plan.*
