#!/usr/bin/env python3
"""One-time setup: log in to CakeResume and save browser session.

Run this script once before running main.py for the first time, or
whenever CakeResume logs you out (typically every few months).

    python setup_cakeresume_auth.py

A browser window will open. Log in to your CakeResume account, then
press Enter in this terminal. The session will be saved to
credentials/cakeresume_state.json and reused by main.py automatically.

Pass --auto to use automatic login detection (no Enter required).
Used by the web UI re-authentication flow.
"""
import asyncio
import sys
from pathlib import Path
from playwright.async_api import async_playwright


CREDS_DIR = Path("credentials")
STATE_PATH = CREDS_DIR / "cakeresume_state.json"


async def _is_logged_in(page) -> bool:
    """Return True if the current page shows logged-in state."""
    try:
        current_url = page.url
        # Stay out of the way during any auth-related step (sign_in, sign_up,
        # OAuth callbacks, password reset, email confirmation, etc.)
        auth_paths = ("sign_in", "sign_up", "/users/", "password",
                      "confirmation", "unlock", "oauth", "callback")
        if any(p in current_url for p in auth_paths):
            return False
        # Only conclude logged-in if we're actually on cake.me
        if "cake.me" not in current_url:
            return False
        # Check current page DOM without navigating away
        await page.wait_for_timeout(500)
        logged_in = await page.query_selector(
            "[class*='Avatar'], [class*='UserAvatar'], [class*='UserMenu']"
        )
        if logged_in:
            return True
        guest_hint = await page.query_selector("[class*='EmptyResults'], [class*='GuestHint']")
        return guest_hint is None
    except Exception:
        return False


async def main(auto: bool = False):
    CREDS_DIR.mkdir(exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()

        print("Opening CakeResume login page...")
        await page.goto("https://www.cake.me/users/sign_in", wait_until="domcontentloaded")

        if auto:
            print("Auto mode: waiting for you to log in (polls every 3s, max 5 minutes)...")
            # Poll until logged in or timeout
            for _ in range(100):  # 100 × 3s = 5 minutes
                await asyncio.sleep(3)
                if await _is_logged_in(page):
                    break
            else:
                print("[ERROR] Timed out waiting for login.")
                await browser.close()
                return
        else:
            print()
            print("=" * 60)
            print("Please log in to CakeResume in the browser window.")
            print("After you are fully logged in (you can see job listings),")
            print("come back here and press Enter.")
            print("=" * 60)
            input()

            # Verify login
            try:
                if not await _is_logged_in(page):
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
    auto_mode = "--auto" in sys.argv
    asyncio.run(main(auto=auto_mode))
