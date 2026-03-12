import asyncio
import re
from pathlib import Path
from playwright.async_api import async_playwright, Browser
from urllib.parse import quote, quote_plus

_CAKE_AUTH_STATE = Path("credentials/cakeresume_state.json")

# 104 company names come in the form "BrandName_LegalName業種" or just "LegalName業種".
# Strip industry classification suffixes so we show clean company names.
_104_INDUSTRY_SUFFIXES = re.compile(
    r"(電腦軟體服務業|電腦系統整合服務業|網際網路相關業|多媒體傳播相關業|數位內容產業"
    r"|其它軟體及網路相關業|資訊服務業|電子商務業|FinTech|金融業|保險業"
    r"|軟體及網路相關業|電腦及週邊設備業|半導體業|光電及光學相關業"
    r"|人力資源服務業|顧問服務業|行銷/市場調查業)$"
)


def _clean_104_company(raw: str) -> str:
    """Normalize 104 company names.

    104 formats:
    - "BrandName_LegalName業種"  → "BrandName"
    - "LegalName業種"            → "LegalName"
    """
    name = raw.strip()
    # If there is an underscore, the part before it is the display brand name
    if "_" in name:
        name = name.split("_")[0].strip()
    else:
        # Strip industry classification suffix
        name = _104_INDUSTRY_SUFFIXES.sub("", name).strip()
    return name

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)

# Max concurrent browser contexts per site (avoid bot detection)
_CONCURRENCY = 3

_CAKE_LOCATION = {
    "tw": "Taipei City-Taiwan",
    "sg": "Republic of Singapore",
    "jp": "Tokyo Metropolis-Japan",
    "us": "United States",
}

_CAKE_SELECTORS = [
    # New CakeResume DOM (2025+): JobSearchHits list → li children
    "[class*='JobSearchHits'] li",
    "[class*='JobSearchHits'] > *",
    # Legacy / fallback selectors
    "[class*='JobSearchResult_jobItem']",
    "[class*='JobItem']",
    "[class*='job-item']",
    "[class*='JobCard']",
    "[class*='job-card']",
    "[class*='SearchResultItem']",
    "article[class*='job']",
    "li[class*='job']",
]

_104_SELECTORS = [
    "article.js-job-item",
    "li.list-group-item",
    "[class*='job-list-item']",
    "[class*='JobListItem']",
    "article[class*='job']",
    "li[class*='job']",
    "[class*='job-card']",
]

_YOURATOR_SELECTORS = [
    # /search page
    "[class*='JobSearchItem']",
    "[class*='job-search-item']",
    "[class*='SearchResult']",
    "[class*='search-result']",
    # /jobs page (legacy fallback)
    ".job-list-item",
    "[class*='job-list-item']",
    "article.job",
    "[class*='JobCard']",
    "li[class*='job']",
    "[class*='position-item']",
    "[class*='PositionItem']",
    "[class*='position']",
    "[class*='Position']",
    ".job",
    "[class*='job']",
]


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


async def _wait_for_any(page, candidates: list[str], timeout_each: int = 4000) -> str | None:
    for sel in candidates:
        try:
            await page.wait_for_selector(sel, timeout=timeout_each)
            return sel
        except Exception:
            continue
    return None


async def _dump_html(page, name: str) -> None:
    safe_name = name.replace(" ", "_").replace("/", "_")
    path = f"/tmp/debug_{safe_name}.html"
    html = await page.content()
    with open(path, "w") as f:
        f.write(html)
    # Print job-related class names inline so we can diagnose without reading the file
    import re
    classes: set[str] = set()
    for m in re.finditer(r'class="([^"]+)"', html):
        for c in m.group(1).split():
            if re.search(r'job|Job|card|Card|item|Item|result|Result|search|Search|position|Position|list|List', c):
                classes.add(c)
    print(f"[DEBUG] Saved HTML to {path}. Job-related classes: {sorted(classes)[:30]}")


_ARTICLE_SUFFIXES = ("jobs", "roles", "positions", "openings", "opportunities", "careers")

# Generic path segments that indicate a listing page, not a specific job posting
_LISTING_SEGMENTS = frozenset({"jobs", "positions", "openings", "careers", "roles"})

# Known UI/nav texts to skip (language switcher, nav tabs, etc.)
_UI_SKIP_LOWER = frozenset({
    "english", "tiếng việt", "bahasa indonesia", "日本語powered by ai",
    "中文（繁體）", "中文（简体）", "日本語",
    "職缺", "公司", "專欄", "jobs", "companies",
    "jobs similar to", "apply now",
})

# Location suffixes — text ending with these is a place, not a job title
_LOCATION_SUFFIXES = ("台灣", ", taiwan", ", japan", ", singapore", "metropolis, japan",
                      "metropolis, korea", "city, taiwan", ", 台灣", "japan", "singapore")


def _is_location_text(text: str) -> bool:
    t = text.lower().strip()
    return any(t.endswith(suf) for suf in _LOCATION_SUFFIXES)


async def _extract_by_link_pattern(page, href_contains: str, base_url: str, min_path_depth: int = 2) -> list[dict]:
    # Prefer scoping to main content area to exclude nav/footer links
    scope = await page.query_selector("main, [role='main'], #content, #main")
    if scope:
        links = await scope.query_selector_all(f"a[href*='{href_contains}']")
    else:
        links = await page.query_selector_all(f"a[href*='{href_contains}']")
    seen = set()
    results = []
    for link in links:
        href = await link.get_attribute("href") or ""
        if not href or href in seen:
            continue
        # URL depth check: job detail pages have more path segments than nav/listing links
        path = href.split("?")[0].rstrip("/")
        segments = [s for s in path.split("/") if s]
        if len(segments) < min_path_depth:
            continue
        # Skip listing pages where the last segment is a generic word (not a job ID)
        if segments[-1].lower() in _LISTING_SEGMENTS:
            continue
        seen.add(href)
        text = (await link.inner_text()).strip()
        # Multi-line inner_text means the <a> wraps a card/banner, not a simple job title
        if "\n" in text:
            text = text.split("\n")[0].strip()
        if not text or len(text) < 4 or len(text) > 120:
            continue
        if text.lower() in _UI_SKIP_LOWER:
            continue
        # Skip location-only strings like "中正區, 台北市, 台灣" or "Tokyo Metropolis, Japan"
        if _is_location_text(text):
            continue
        # Skip category/navigation links like "Product Manager Jobs in Taiwan"
        if text.lower().rstrip(".").endswith(_ARTICLE_SUFFIXES):
            continue
        full_url = href if href.startswith("http") else f"{base_url}{href}"
        # Walk up until we find a container that holds both the job link AND a company link
        # (closest('div[class]') stops at the first inner div, missing sibling company links)
        parent = await link.evaluate_handle("""el => {
            let p = el.parentElement;
            while (p && p !== document.body) {
                if (p.tagName === 'LI' || p.tagName === 'ARTICLE') return p;
                if (p.querySelectorAll('a[href]').length >= 2) return p;
                p = p.parentElement;
            }
            return el.parentElement;
        }""")
        company = ""
        if parent:
            try:
                # 1. Try class-based company selector first
                company_el = await parent.query_selector(
                    "[class*='company'], [class*='Company'], [class*='brand'], [class*='Brand']"
                )
                if company_el:
                    company = (await company_el.inner_text()).strip().split("\n")[0]
                else:
                    # 2. Fallback: find any <a> in parent that does NOT link to the same job URL
                    #    (e.g. 104 company links go to /company/ path)
                    sibling_links = await parent.query_selector_all("a[href]")
                    for sl in sibling_links:
                        sl_href = await sl.get_attribute("href") or ""
                        if sl_href == href or not sl_href or sl_href.startswith("#"):
                            continue
                        # Skip if it's another job listing link
                        sl_path = sl_href.split("?")[0].rstrip("/")
                        if href_contains in sl_path:
                            continue
                        sl_text = (await sl.inner_text()).strip().split("\n")[0]
                        if sl_text and 2 <= len(sl_text) <= 60 and sl_text.lower() not in _UI_SKIP_LOWER:
                            company = _clean_104_company(sl_text)
                            break
            except Exception:
                pass
        results.append({"title": text, "company": company, "url": full_url})
    return results


_cake_dumped: set[str] = set()


async def _scrape_cakeresume_one(keyword: str, market: str, browser: Browser, sem: asyncio.Semaphore) -> list[dict]:
    async with sem:
        results = []
        kw = quote(keyword)
        loc = _CAKE_LOCATION.get(market, "")
        loc_param = f"locations={quote(loc)}&" if loc else ""
        url = f"https://www.cake.me/jobs/{kw}?{loc_param}order=latest"
        # Load saved login session if available (run setup_cakeresume_auth.py first)
        ctx_kwargs = {"user_agent": _UA}
        if _CAKE_AUTH_STATE.exists():
            ctx_kwargs["storage_state"] = str(_CAKE_AUTH_STATE)
        context = await browser.new_context(**ctx_kwargs)
        page = await context.new_page()
        try:
            await page.goto(url, timeout=45000, wait_until="domcontentloaded")
            # Wait for either job listings or an empty/gate indicator to appear.
            # JobSearchHits renders asynchronously; 3 s is sometimes not enough.
            try:
                await page.wait_for_selector(
                    "[class*='JobSearchHits'], [class*='GuestHint'], [class*='EmptyResults']",
                    timeout=8000,
                )
            except Exception:
                pass  # fall through to selector loop below
            # Dump HTML once so we can inspect actual page structure
            if keyword not in _cake_dumped:
                _cake_dumped.add(keyword)
                await _dump_html(page, f"cakeresume_{kw}")
            # If the page requires login (GuestHint) or has no results (EmptyResults), bail early
            # GuestHint = login wall (no auth state), EmptyResults = no matching jobs
            gated = await page.query_selector("[class*='GuestHint']")
            if gated:
                if _CAKE_AUTH_STATE.exists():
                    print(f"[WARN] CakeResume '{keyword}': login may have expired — re-run setup_cakeresume_auth.py")
                else:
                    print(f"[DEBUG] CakeResume '{keyword}': not logged in — run setup_cakeresume_auth.py to enable")
                return results
            empty = await page.query_selector("[class*='EmptyResults']")
            if empty and await empty.is_visible():
                print(f"[DEBUG] CakeResume '{keyword}': no matching jobs")
                return results
            selector = await _wait_for_any(page, _CAKE_SELECTORS)
            if not selector:
                items_via_links = await _extract_by_link_pattern(page, "/jobs/", "https://www.cake.me", min_path_depth=3)
                for r in items_via_links[:20]:
                    results.append(normalize_cakeresume_item(r, market=market))
                return results

            items = await page.query_selector_all(selector)
            for item in items[:20]:
                title_el = await item.query_selector("h3, h2, [class*='title'], [class*='Title']")
                company_el = await item.query_selector(
                    "[class*='companyName'], [class*='company-name'], [class*='CompanyName'], [class*='company']"
                )
                link_el = await item.query_selector("a")
                title = (await title_el.inner_text() if title_el else "").strip()
                company = (await company_el.inner_text() if company_el else "").strip()
                href = await link_el.get_attribute("href") if link_el else ""
                full_url = href if (not href or href.startswith("http")) else f"https://www.cake.me{href}"
                if title and len(title) >= 5 and title.lower() not in _UI_SKIP_LOWER:
                    results.append(normalize_cakeresume_item(
                        {"title": title, "company": company, "url": full_url}, market=market
                    ))
        except Exception as e:
            print(f"[WARN] CakeResume '{keyword}': {e}")
        finally:
            await context.close()
        return results


async def _scrape_104_one(keyword: str, browser: Browser, sem: asyncio.Semaphore) -> list[dict]:
    async with sem:
        results = []
        kw = quote_plus(keyword)
        url = (
            f"https://www.104.com.tw/jobs/search/?area=6001001000"
            f"&jobcat=2004003009&jobsource=joblist_search&keyword={kw}"
            f"&mode=s&page=1&order=16&searchJobs=1&isnew=0&indcat=1001001000"
        )
        context = await browser.new_context(user_agent=_UA)
        page = await context.new_page()
        try:
            await page.goto(url, timeout=45000, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)
            selector = await _wait_for_any(page, _104_SELECTORS)
            if not selector:
                items_via_links = await _extract_by_link_pattern(page, "/job/", "https://www.104.com.tw", min_path_depth=2)
                for r in items_via_links[:20]:
                    if r.get("title"):
                        results.append({"title": r["title"], "company": r.get("company", ""),
                                        "url": r["url"], "description": "", "location": "Taiwan",
                                        "source": "104", "market": "tw"})
                return results
            items = await page.query_selector_all(selector)
            for item in items[:25]:
                # Skip ad/adsmart items — they have irrelevant content
                item_class = await item.get_attribute("class") or ""
                if "adsmart" in item_class or "ad-" in item_class:
                    continue
                title_el = await item.query_selector(
                    "h2, h3, [class*='title'], [class*='Title'], .job-name, [class*='job-name'], p.b2"
                )
                link_el = await item.query_selector("a[href*='/job/'], a[href]")
                title = (await title_el.inner_text() if title_el else "").strip()
                # Strip embedded newlines (ad/banner text); take first line only
                title = title.split("\n")[0].strip()
                # Try company: class-based first, then sibling <a> not going to /job/
                company = ""
                company_el = await item.query_selector(
                    "[class*='company'], [class*='Company'], .b-block--company"
                )
                if company_el:
                    company = _clean_104_company((await company_el.inner_text()).strip().split("\n")[0])
                else:
                    all_links = await item.query_selector_all("a[href]")
                    for al in all_links:
                        al_href = await al.get_attribute("href") or ""
                        if not al_href or "/job/" in al_href or al_href.startswith("#"):
                            continue
                        al_text = (await al.inner_text()).strip().split("\n")[0]
                        if al_text and 2 <= len(al_text) <= 60 and al_text.lower() not in _UI_SKIP_LOWER:
                            company = _clean_104_company(al_text)
                            break
                href = await link_el.get_attribute("href") if link_el else ""
                full_url = href if (not href or href.startswith("http")) else f"https://www.104.com.tw{href}"
                if title and 3 <= len(title) <= 100 and title.lower() not in _UI_SKIP_LOWER:
                    results.append({"title": title, "company": company, "url": full_url,
                                    "description": "", "location": "Taiwan",
                                    "source": "104", "market": "tw"})
        except Exception as e:
            print(f"[WARN] 104 '{keyword}': {e}")
        finally:
            if not results:
                try:
                    await _dump_html(page, f"104_{kw}")
                except Exception:
                    pass
            await context.close()
        return results


async def _scrape_yourator_page(url: str, label: str, browser: Browser, sem: asyncio.Semaphore) -> list[dict]:
    async with sem:
        results = []
        context = await browser.new_context(user_agent=_UA)
        page = await context.new_page()
        try:
            await page.goto(url, timeout=45000, wait_until="networkidle")
            await page.wait_for_timeout(3000)

            # Use JS to extract all anchor data (works even if href="" or href="#")
            raw_links: list[dict] = await page.evaluate("""
                () => {
                    const scope = document.querySelector(
                        '.search-result__cards, .search-records-section, [class*="search-result"], main'
                    ) || document.body;
                    return Array.from(scope.querySelectorAll('a')).map(a => {
                        // Walk up to find a card container that might hold company info
                        let card = a.closest('li, article, [class*="card"], [class*="item"], [class*="result"]');
                        let company = '';
                        if (card) {
                            let companyEl = card.querySelector(
                                '[class*="company"], [class*="Company"], [class*="brand"], [class*="Brand"]'
                            );
                            if (!companyEl) {
                                // Try second <a> in the card as company link
                                let cardLinks = Array.from(card.querySelectorAll('a'));
                                if (cardLinks.length > 1 && cardLinks[1] !== a) {
                                    company = cardLinks[1].innerText.trim().split('\\n')[0];
                                }
                            } else {
                                company = companyEl.innerText.trim().split('\\n')[0];
                            }
                        }
                        return {
                            href: a.getAttribute('href') || '',
                            text: a.innerText.trim(),
                            company: company
                        };
                    });
                }
            """)

            seen: set[str] = set()
            for item in raw_links:
                href = item.get("href", "")
                text = item.get("text", "").strip()
                company = item.get("company", "").strip()

                if not href or href in seen or href.startswith("#") or href.startswith("javascript"):
                    continue
                # Yourator job detail pages: /companies/{slug}/jobs/{id} (depth=4)
                # Company pages: /companies/{slug} (depth=2) — skip those
                path = href.split("?")[0].rstrip("/")
                segments = [s for s in path.split("/") if s]
                if "jobs" not in segments:
                    continue
                if len(segments) < 3:
                    continue
                seen.add(href)

                # Strip multi-line text (company cards have location/industry on extra lines)
                if "\n" in text:
                    text = text.split("\n")[0].strip()
                if not text or len(text) < 3 or len(text) > 100:
                    continue
                if text.lower() in _UI_SKIP_LOWER or _is_location_text(text):
                    continue

                full_url = href if href.startswith("http") else f"https://www.yourator.co{href}"

                # Extract company slug from URL if format is /companies/{slug}/jobs/{id}
                if not company and "companies" in segments:
                    idx = segments.index("companies")
                    if idx + 1 < len(segments):
                        company = segments[idx + 1].replace("-", " ").title()

                results.append(normalize_yourator_item(
                    {"job_title": text, "company_name": company, "job_url": full_url},
                    market="tw"
                ))
                if len(results) >= 20:
                    break

            if not results:
                print(f"[DEBUG] Yourator '{label}': {await page.title()!r} — 0 jobs extracted")
                await _dump_html(page, f"yourator_{label}")
        except Exception as e:
            print(f"[WARN] Yourator '{label}': {e}")
        finally:
            await context.close()
        return results


async def _scrape_yourator_one(keyword: str, browser: Browser, sem: asyncio.Semaphore) -> list[dict]:
    kw = quote_plus(keyword)
    # /jobs?term= shows individual job listings; /search?s= shows company cards
    url = f"https://www.yourator.co/jobs?term={kw}"
    return await _scrape_yourator_page(url, keyword, browser, sem)


async def _scrape_all(sources: dict, titles: list[str], markets: list[str]) -> list[dict]:
    """Run all scrapes concurrently with shared browser instances."""
    async with async_playwright() as p:
        browsers = {}
        try:
            if sources.get("cakeresume"):
                browsers["cake"] = await p.chromium.launch(headless=True)
            if sources.get("yourator"):
                browsers["yourator"] = await p.chromium.launch(headless=True)
            if sources.get("104"):
                browsers["104"] = await p.chromium.launch(headless=True)

            cake_sem = asyncio.Semaphore(_CONCURRENCY)
            yourator_sem = asyncio.Semaphore(_CONCURRENCY)
            sem_104 = asyncio.Semaphore(_CONCURRENCY)
            tasks = []

            yourator_url = sources.get("yourator_url") if isinstance(sources, dict) else None
            if "yourator" in browsers and yourator_url:
                tasks.append(_scrape_yourator_page(yourator_url, "configured_url", browsers["yourator"], yourator_sem))

            for title in titles:
                if "cake" in browsers:
                    for market in markets:
                        tasks.append(_scrape_cakeresume_one(title, market, browsers["cake"], cake_sem))
                if "yourator" in browsers and not yourator_url:
                    tasks.append(_scrape_yourator_one(title, browsers["yourator"], yourator_sem))
                if "104" in browsers:
                    tasks.append(_scrape_104_one(title, browsers["104"], sem_104))

            batches = await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            for b in browsers.values():
                await b.close()

    flat = []
    for batch in batches:
        if isinstance(batch, Exception):
            print(f"[WARN] scrape task raised: {batch}")
        else:
            flat.extend(batch)
    return flat


class WebScraper:
    def fetch_all(self, sources: dict, titles: list[str], markets: list[str] | None = None) -> list[dict]:
        return asyncio.run(_scrape_all(sources, titles, markets or ["tw"]))
