"""One-time login helper.

Opens a site in the agent's PERSISTENT browser profile so your login/cookies
survive into future `main.py` runs (same profile the agent uses to fill forms).

Usage:
    python login.py                       # default: workatastartup.com
    python login.py https://some.site/    # any login-gated site

Log in, then CLOSE the browser window to save the session.
"""

import asyncio
import os
import sys

from playwright.async_api import async_playwright

PROFILE_DIR = os.path.join(os.path.dirname(__file__), ".browser-profile")


async def main(url: str):
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR, headless=False)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(url)
        print(f"Opened {url}")
        print(">>> Log in, then CLOSE the browser window to save the session.")
        try:
            await page.wait_for_event("close", timeout=0)
        except Exception:
            pass
        try:
            await ctx.close()
        except Exception:
            pass


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "https://www.workatastartup.com/"
    asyncio.run(main(url))
