#!/usr/bin/env python3
"""One-time setup: log in to CakeResume and save browser session.

Run this script once before running main.py for the first time, or
whenever CakeResume logs you out (typically every few months).

    python setup_cakeresume_auth.py

A browser window will open. Log in to your CakeResume account, then
press Enter in this terminal. The session will be saved to
credentials/cakeresume_state.json and reused by main.py automatically.
"""
import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright


CREDS_DIR = Path("credentials")
STATE_PATH = CREDS_DIR / "cakeresume_state.json"


async def main():
    CREDS_DIR.mkdir(exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()

        print("Opening CakeResume login page...")
        await page.goto("https://www.cake.me/users/sign_in", wait_until="domcontentloaded")
        print()
        print("=" * 60)
        print("Please log in to CakeResume in the browser window.")
        print("After you are fully logged in (you can see job listings),")
        print("come back here and press Enter.")
        print("=" * 60)
        input()

        # Verify we're logged in by checking for a user-specific element
        try:
            await page.goto("https://www.cake.me/jobs/Product-Manager", wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)
            # Check if EmptyResults/GuestHint is shown (means not logged in)
            guest_hint = await page.query_selector("[class*='EmptyResults'], [class*='GuestHint']")
            if guest_hint:
                print("[WARN] It looks like you may not be logged in yet (guest hint detected).")
                print("Try logging in again and press Enter once more.")
                input()
        except Exception:
            pass

        await context.storage_state(path=str(STATE_PATH))
        await browser.close()

    print(f"\nSession saved to {STATE_PATH}")
    print("You can now run: python main.py")


if __name__ == "__main__":
    asyncio.run(main())
