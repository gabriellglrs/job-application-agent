"""Read-only Gmail web audit for job application duplicate avoidance.

This fallback uses Gmail in a headed Chromium window instead of the Gmail API.
It searches Gmail for application confirmations/rejections and extracts visible
result rows into gmail_web_job_records.csv. It does not send, delete, archive, or
modify email.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from playwright.async_api import async_playwright


ROOT = Path(__file__).resolve().parent
OUTPUT_PATH = ROOT / "gmail_web_job_records.csv"
PROFILE_DIR = ROOT / ".gmail-browser-profile"

SEARCHES = [
    '"thank you for applying" OR "application has been received" OR "we received your application"',
    '"decided not to move forward" OR "move forward with other candidates" OR "after careful consideration"',
    'ashby OR greenhouse OR lever newer_than:180d',
    'CodeSignal OR assessment OR Hackerrank newer_than:180d',
    'interview OR "recruiter call" newer_than:180d',
]

COMPANY_HINTS = [
    "anyscale",
    "scale ai",
    "cohere",
    "vercel",
    "decagon",
    "sierra",
    "ramp",
    "databricks",
    "glean",
    "harvey",
    "hubspot",
    "serval",
    "vapi",
    "cartesia",
    "livekit",
    "dataiku",
    "baseten",
    "snorkel",
    "palantir",
    "langchain",
    "openai",
    "anthropic",
    "mistral",
    "together ai",
    "assemblyai",
    "elevenlabs",
    "deepgram",
    "cresta",
    "cognition",
    "hebbia",
    "webflow",
    "brex",
    "stripe",
    "anyscale",
    "distyl",
    "komodo",
    "galileo",
]


@dataclass(frozen=True)
class WebEmailRecord:
    search: str
    company: str
    status: str
    row_text: str
    gmail_url: str


def normalize_company(value: str) -> str:
    aliases = {
        "anyscale": "anyscale",
        "scale ai": "scaleai",
        "glean": "gleanwork",
        "together ai": "togetherai",
        "snorkel": "snorkelai",
        "komodo": "komodohealth",
    }
    return aliases.get(value, value).replace(" ", "-")


def infer_company(text: str) -> str:
    lower = text.lower()
    for hint in COMPANY_HINTS:
        if re.search(rf"(?<![a-z0-9]){re.escape(hint)}(?![a-z0-9])", lower):
            return normalize_company(hint)
    match = re.search(r"(?:at|to|from)\s+([A-Z][A-Za-z0-9& .-]{2,35})", text)
    if match:
        company = re.sub(r"[^a-z0-9]+", "-", match.group(1).lower()).strip("-")
        return company[:60] or "unknown"
    return "unknown"


def infer_status(text: str) -> str:
    lower = text.lower()
    if any(
        term in lower
        for term in [
            "decided not to move forward",
            "move forward with other candidates",
            "not be moving forward",
            "after careful consideration",
            "not selected",
            "position has been filled",
        ]
    ):
        return "rejected"
    if any(term in lower for term in ["codesignal", "assessment", "hackerrank"]):
        return "assessment"
    if any(term in lower for term in ["interview", "recruiter call", "schedule"]):
        return "interview"
    if any(
        term in lower
        for term in [
            "thank you for applying",
            "thanks for applying",
            "application has been received",
            "we received your application",
            "your application for",
        ]
    ):
        return "submitted-confirmed"
    return "unknown"


async def wait_for_gmail(page) -> None:
    await page.goto("https://mail.google.com/mail/u/0/#inbox", wait_until="domcontentloaded")
    for _ in range(900):
        url = page.url
        if "mail.google.com" in url and "#inbox" in url:
            try:
                await page.wait_for_selector("div[role='main']", timeout=1000)
                return
            except Exception:
                pass
        await page.wait_for_timeout(1000)
    raise RuntimeError("Gmail did not finish loading within 15 minutes.")


async def extract_rows(page, search: str, max_rows: int) -> list[WebEmailRecord]:
    url = f"https://mail.google.com/mail/u/0/#search/{quote(search)}"
    await page.goto(url, wait_until="domcontentloaded")
    await page.wait_for_timeout(5000)
    rows: set[str] = set()
    last_count = -1
    for _ in range(12):
        texts = await page.locator("div[role='main'] tr").evaluate_all(
            """els => els.map(el => el.innerText)
                .filter(t => t && t.length > 20 && !t.includes('Select all'))
                .slice(0, 80)"""
        )
        for text in texts:
            compact = " ".join(text.split())
            if compact:
                rows.add(compact)
        if len(rows) >= max_rows or len(rows) == last_count:
            break
        last_count = len(rows)
        await page.mouse.wheel(0, 1800)
        await page.wait_for_timeout(1000)

    records = []
    for text in sorted(rows):
        records.append(
            WebEmailRecord(
                search=search,
                company=infer_company(text),
                status=infer_status(text),
                row_text=text[:600],
                gmail_url=url,
            )
        )
    return records[:max_rows]


def write_records(records: list[WebEmailRecord]) -> None:
    with OUTPUT_PATH.open("w", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["search", "company", "status", "row_text", "gmail_url"]
        )
        writer.writeheader()
        for record in records:
            writer.writerow(record.__dict__)


def print_summary(records: list[WebEmailRecord]) -> None:
    by_company: dict[str, set[str]] = {}
    for record in records:
        by_company.setdefault(record.company, set()).add(record.status)
    print(f"Wrote {len(records)} Gmail web records to {OUTPUT_PATH.name}")
    for company in sorted(by_company):
        statuses = ", ".join(sorted(by_company[company]))
        print(f"  {company}: {statuses}")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-rows-per-search", type=int, default=80)
    args = parser.parse_args()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
        )
        page = await browser.new_page()
        print("Opening Gmail. If prompted, log in; the script will continue after Gmail loads.")
        await wait_for_gmail(page)
        all_records = []
        seen = set()
        for search in SEARCHES:
            print(f"Searching Gmail: {search}")
            for record in await extract_rows(page, search, args.max_rows_per_search):
                key = (record.company, record.status, record.row_text)
                if key not in seen:
                    seen.add(key)
                    all_records.append(record)
        write_records(all_records)
        print_summary(all_records)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
