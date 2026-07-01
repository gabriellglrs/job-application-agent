"""Inspect Simplify page structure for the capture script."""

from __future__ import annotations

import asyncio
from pathlib import Path

from playwright.async_api import async_playwright


ROOT = Path(__file__).resolve().parent
PROFILE_DIR = ROOT / ".simplify-browser-profile"


async def main() -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
        )
        page = await browser.new_page()
        await page.goto("https://simplify.jobs/jobs", wait_until="domcontentloaded")
        await page.wait_for_timeout(5000)
        print("URL:", page.url)
        print("TITLE:", await page.title())
        text = await page.locator("body").inner_text(timeout=5000)
        print("BODY:")
        print(text[:4000])
        inputs = await page.locator("input, textarea, [role=combobox]").evaluate_all(
            """els => els.map((el, i) => ({
                i,
                tag: el.tagName,
                type: el.getAttribute('type'),
                role: el.getAttribute('role'),
                placeholder: el.getAttribute('placeholder'),
                aria: el.getAttribute('aria-label'),
                value: el.value
            }))"""
        )
        print("INPUTS:", inputs)
        links = await page.locator("a[href]").evaluate_all(
            """els => els.slice(0, 80).map(a => ({
                href: a.href,
                text: (a.innerText || '').trim().slice(0, 160)
            }))"""
        )
        print("LINKS:", links)
        await page.screenshot(path=str(ROOT / "simplify_debug.png"), full_page=True)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
