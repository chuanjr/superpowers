import asyncio
from playwright.async_api import async_playwright, Browser
from urllib.parse import quote_plus

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)

# Max concurrent browser contexts per site (avoid bot detection)
_CONCURRENCY = 3

_CAKE_SELECTORS = [
    "[class*='JobSearchResult_jobItem']",
    "[class*='JobItem']",
    "[class*='job-item']",
    "[class*='JobCard']",
    "[class*='job-card']",
    "[class*='SearchResultItem']",
    "article[class*='job']",
    "li[class*='job']",
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
    path = f"/tmp/debug_{name}.html"
    html = await page.content()
    with open(path, "w") as f:
        f.write(html)
    print(f"[DEBUG] Saved rendered HTML to {path} for selector diagnosis")


async def _extract_by_link_pattern(page, href_contains: str, base_url: str) -> list[dict]:
    links = await page.query_selector_all(f"a[href*='{href_contains}']")
    seen = set()
    results = []
    for link in links:
        href = await link.get_attribute("href") or ""
        if not href or href in seen:
            continue
        seen.add(href)
        text = (await link.inner_text()).strip()
        if not text or len(text) < 3:
            continue
        full_url = href if href.startswith("http") else f"{base_url}{href}"
        parent = await link.evaluate_handle("el => el.closest('li, article, div[class]') || el.parentElement")
        company = ""
        if parent:
            try:
                company_el = await parent.query_selector(
                    "[class*='company'], [class*='Company'], [class*='brand'], [class*='Brand']"
                )
                if company_el:
                    company = (await company_el.inner_text()).strip()
            except Exception:
                pass
        results.append({"title": text, "company": company, "url": full_url})
    return results


async def _scrape_cakeresume_one(keyword: str, browser: Browser, sem: asyncio.Semaphore) -> list[dict]:
    async with sem:
        results = []
        kw = quote_plus(keyword)
        url = f"https://www.cake.me/jobs/{kw}"
        context = await browser.new_context(user_agent=_UA)
        page = await context.new_page()
        try:
            await page.goto(url, timeout=30000, wait_until="networkidle")
            selector = await _wait_for_any(page, _CAKE_SELECTORS)
            if not selector:
                items_via_links = await _extract_by_link_pattern(page, "/jobs/", "https://www.cake.me")
                if items_via_links:
                    print(f"[DEBUG] CakeResume '{keyword}': link-pattern fallback, {len(items_via_links)} items")
                    for r in items_via_links[:20]:
                        results.append(normalize_cakeresume_item(r, market="tw"))
                else:
                    print(f"[DEBUG] CakeResume '{keyword}': {await page.title()!r} — no selector matched")
                    await _dump_html(page, f"cakeresume_{keyword}")
                return results

            items = await page.query_selector_all(selector)
            for item in items[:20]:
                title_el = await item.query_selector("h3, h2, [class*='title'], [class*='Title']")
                company_el = await item.query_selector(
                    "[class*='companyName'], [class*='company-name'], [class*='CompanyName'], [class*='company']"
                )
                link_el = await item.query_selector("a")
                title = await title_el.inner_text() if title_el else ""
                company = await company_el.inner_text() if company_el else ""
                href = await link_el.get_attribute("href") if link_el else ""
                full_url = href if (not href or href.startswith("http")) else f"https://www.cake.me{href}"
                if title:
                    results.append(normalize_cakeresume_item(
                        {"title": title, "company": company, "url": full_url}, market="tw"
                    ))
        except Exception as e:
            print(f"[WARN] CakeResume '{keyword}': {e}")
        finally:
            await context.close()
        return results


async def _scrape_yourator_one(keyword: str, browser: Browser, sem: asyncio.Semaphore) -> list[dict]:
    async with sem:
        results = []
        kw = quote_plus(keyword)
        url = f"https://www.yourator.co/search?s={kw}"
        context = await browser.new_context(user_agent=_UA)
        page = await context.new_page()
        try:
            await page.goto(url, timeout=30000, wait_until="networkidle")
            selector = await _wait_for_any(page, _YOURATOR_SELECTORS)
            if not selector:
                items_via_links = await _extract_by_link_pattern(page, "/jobs/", "https://www.yourator.co")
                if items_via_links:
                    print(f"[DEBUG] Yourator '{keyword}': link-pattern fallback, {len(items_via_links)} items")
                    for r in items_via_links[:20]:
                        results.append(normalize_yourator_item(
                            {"job_title": r.get("title"), "company_name": r.get("company"), "job_url": r.get("url")},
                            market="tw"
                        ))
                else:
                    print(f"[DEBUG] Yourator '{keyword}': {await page.title()!r} — no selector matched")
                    await _dump_html(page, f"yourator_{keyword}")
                return results

            items = await page.query_selector_all(selector)
            for item in items[:20]:
                title_el = await item.query_selector(
                    ".job-title, h3, h2, [class*='job-title'], [class*='JobTitle'], [class*='title'], [class*='Title']"
                )
                company_el = await item.query_selector(
                    ".company-name, [class*='company-name'], [class*='CompanyName'], [class*='company']"
                )
                link_el = await item.query_selector("a")
                title = await title_el.inner_text() if title_el else ""
                company = await company_el.inner_text() if company_el else ""
                href = await link_el.get_attribute("href") if link_el else ""
                full_url = href if (not href or href.startswith("http")) else f"https://www.yourator.co{href}"
                if title:
                    results.append(normalize_yourator_item(
                        {"job_title": title, "company_name": company, "job_url": full_url}, market="tw"
                    ))
        except Exception as e:
            print(f"[WARN] Yourator '{keyword}': {e}")
        finally:
            await context.close()
        return results


async def _scrape_all(sources: dict, titles: list[str]) -> list[dict]:
    """Run all scrapes concurrently with shared browser instances."""
    async with async_playwright() as p:
        browsers = {}
        try:
            if sources.get("cakeresume"):
                browsers["cake"] = await p.chromium.launch(headless=True)
            if sources.get("yourator"):
                browsers["yourator"] = await p.chromium.launch(headless=True)

            cake_sem = asyncio.Semaphore(_CONCURRENCY)
            yourator_sem = asyncio.Semaphore(_CONCURRENCY)
            tasks = []

            for title in titles:
                if "cake" in browsers:
                    tasks.append(_scrape_cakeresume_one(title, browsers["cake"], cake_sem))
                if "yourator" in browsers:
                    tasks.append(_scrape_yourator_one(title, browsers["yourator"], yourator_sem))

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
    def fetch_all(self, sources: dict, titles: list[str]) -> list[dict]:
        return asyncio.run(_scrape_all(sources, titles))
