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


_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)


async def _scrape_cakeresume(keyword: str) -> list[dict]:
    results = []
    kw = quote_plus(keyword)
    url = f"https://www.cakeresume.com/jobs?q={kw}&locale=tw"
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=_UA)
        page = await context.new_page()
        await page.goto(url, timeout=30000, wait_until="networkidle")
        # Use partial class match to avoid brittle CSS-module hashes
        try:
            await page.wait_for_selector("[class*='JobSearchResult_jobItem']", timeout=15000)
        except Exception:
            title_tag = await page.title()
            print(f"[DEBUG] CakeResume page title: {title_tag!r} — selector not found, skipping")
            await browser.close()
            return results
        items = await page.query_selector_all("[class*='JobSearchResult_jobItem']")
        for item in items[:20]:
            title_el = await item.query_selector("h3")
            company_el = await item.query_selector("[class*='JobSearchResult_companyName']")
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
        context = await browser.new_context(user_agent=_UA)
        page = await context.new_page()
        await page.goto(url, timeout=30000, wait_until="networkidle")
        # Try multiple known selectors for Yourator job cards
        selector = None
        for candidate in [".job-list-item", "[class*='job-list-item']", "article.job", "[class*='JobCard']", "li[class*='job']"]:
            try:
                await page.wait_for_selector(candidate, timeout=5000)
                selector = candidate
                break
            except Exception:
                continue
        if not selector:
            title_tag = await page.title()
            print(f"[DEBUG] Yourator page title: {title_tag!r} — no selector matched, skipping")
            await browser.close()
            return results
        items = await page.query_selector_all(selector)
        for item in items[:20]:
            title_el = await item.query_selector(".job-title, h3, h2, [class*='job-title'], [class*='JobTitle']")
            company_el = await item.query_selector(".company-name, [class*='company-name'], [class*='CompanyName']")
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
