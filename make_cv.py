"""One-off: tailor a CV to a single job URL without the apply browser.
Usage: python make_cv.py <job_url>
"""
import asyncio
import sys

from playwright.async_api import async_playwright

from main import LLM_MODEL, get_llm
from tailor import make_tailored_resume


async def run(url: str):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(url, wait_until="networkidle")
        jd_text = await page.evaluate("() => document.body.innerText")
        await browser.close()
        path = await make_tailored_resume(pw, get_llm(), LLM_MODEL, jd_text, url)
        print(path)


if __name__ == "__main__":
    asyncio.run(run(sys.argv[1]))
