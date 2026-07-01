"""Capture job details from a logged-in Simplify browser session.

This does not apply to jobs. It opens Simplify in a persistent Chromium profile,
waits while you log in if needed, searches Simplify, opens result details, then
writes read-only captures to simplify_jobs.csv for dedupe and application.

Usage:
    python simplify_capture.py
    python simplify_capture.py --query "Forward Deployed Engineer"
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import re
from dataclasses import dataclass
from pathlib import Path

from playwright.async_api import Page, async_playwright

from main import (
    company_from_url,
    has_blocking_email_history,
    job_key,
    load_applied,
    load_applied_company_history,
    load_email_company_history,
)


ROOT = Path(__file__).resolve().parent
PROFILE_DIR = ROOT / ".simplify-browser-profile"
OUTPUT = ROOT / "simplify_jobs.csv"
DEFAULT_QUERIES = [
    "Forward Deployed Engineer",
    "Applied AI Engineer",
    "AI Solutions Engineer",
    "Solutions Engineer AI",
    "Customer Engineer AI",
    "Deployment Engineer AI",
    "Implementation Engineer AI",
    "Python LLM API integrations",
    "AI agents workflow automation",
    "Enterprise AI customer integrations",
]

BLOCKED_URL_RE = re.compile(
    r"/(dashboard|matches|tracker|documents|services|preferences|lists|profile|"
    r"applications|builder|cover-letter|question-response|login|signup)(\?|/|$)"
)
JOB_URL_RE = re.compile(
    r"(simplify\.jobs/(jobs|job|p|company)/|greenhouse\.io|ashbyhq\.com|lever\.co|"
    r"workdayjobs\.com|wellfound\.com|ycombinator\.com/companies/.*/jobs|/careers/|/jobs/)"
)
JOB_TEXT_MARKERS = {
    "about the role",
    "about this role",
    "responsibilities",
    "requirements",
    "qualifications",
    "what you'll do",
    "what you will do",
    "what we're looking for",
    "full-time",
    "remote",
    "hybrid",
    "posted",
    "salary",
    "compensation",
}


@dataclass
class CapturedJob:
    title: str
    company: str
    url: str
    text: str
    duplicate_reason: str


def merge_company_history() -> dict[str, set[str]]:
    history = load_email_company_history()
    for company, statuses in load_applied_company_history().items():
        history.setdefault(company, set()).update(statuses)
    return history


def clean(text: str) -> str:
    return " ".join((text or "").split())


def clean_multiline(text: str) -> str:
    lines = [clean(line) for line in (text or "").splitlines()]
    return "\n".join(line for line in lines if line)


def is_job_url(url: str) -> bool:
    return bool(url and JOB_URL_RE.search(url) and not BLOCKED_URL_RE.search(url))


def likely_title(line: str) -> bool:
    lower = line.lower()
    if len(line) < 4 or len(line) > 140:
        return False
    if lower in {"full-time", "part-time", "contract", "remote", "hybrid", "onsite"}:
        return False
    if any(word in lower for word in ["engineer", "architect", "developer", "consultant", "specialist", "lead"]):
        return True
    return False


def title_company_from_text(text: str, fallback_url: str) -> tuple[str, str]:
    lines = [clean(line) for line in text.splitlines() if clean(line)]
    title = next((line for line in lines[:20] if likely_title(line)), lines[0] if lines else "unknown")
    try:
        title_index = lines.index(title)
    except ValueError:
        title_index = 0
    company = ""
    for line in lines[max(0, title_index - 3):title_index] + lines[title_index + 1:title_index + 5]:
        lower = line.lower()
        if line != title and 1 < len(line) <= 80 and not any(marker in lower for marker in JOB_TEXT_MARKERS):
            company = line
            break
    return title[:120], (company or company_from_url(fallback_url))[:80]


def extract_detail_tail(text: str, fallback_text: str) -> str:
    lines = [clean(line) for line in text.splitlines() if clean(line)]
    if not lines:
        return fallback_text
    lower_lines = [line.lower() for line in lines]
    marker_indexes = [
        i for i, line in enumerate(lower_lines)
        if line in {"overview", "responsibilities", "requirements", "qualifications"}
        or line.startswith("about the role")
        or line.startswith("about this role")
        or line.startswith("what you'll do")
        or line.startswith("what you will do")
    ]
    if marker_indexes and any(line.startswith("showing ") and " jobs" in line for line in lower_lines[:8]):
        tail = "\n".join(lines[min(marker_indexes):])
        return clean_multiline(f"{fallback_text}\n{tail}")
    return clean_multiline(text)


async def best_detail_text(page: Page) -> str:
    detail = await page.evaluate(
        """
        () => {
            const skip = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'SVG']);
            const markers = [
                'about the role', 'about this role', 'responsibilities', 'requirements',
                'qualifications', "what you'll do", 'what you will do',
                "what we're looking for", 'full-time', 'remote', 'hybrid',
                'posted', 'salary', 'compensation'
            ];
            const visible = el => {
                if (skip.has(el.tagName)) return false;
                const style = getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.visibility !== 'hidden' && style.display !== 'none' &&
                    rect.width > 20 && rect.height > 20;
            };
            const candidates = Array.from(document.querySelectorAll(
                'main, article, section, [role=main], [data-testid], [class*=job], [class*=Job], [class*=detail], [class*=Detail], div'
            )).filter(visible).map(el => {
                const text = (el.innerText || '').trim();
                const lower = text.toLowerCase();
                const markerCount = markers.filter(marker => lower.includes(marker)).length;
                const linkCount = el.querySelectorAll('a[href]').length;
                const buttonCount = el.querySelectorAll('button').length;
                const score = Math.min(text.length, 10000) + markerCount * 1600 - linkCount * 25 - buttonCount * 15;
                return { text, score, markerCount };
            }).filter(x => x.text.length > 180);
            candidates.sort((a, b) => b.score - a.score);
            const marked = candidates.find(x => x.markerCount >= 2);
            return (marked || candidates[0] || { text: document.body.innerText || '' }).text;
        }
        """
    )
    return clean_multiline(detail)


async def best_job_url(page: Page, fallback_url: str) -> str:
    urls = await page.evaluate(
        """
        () => Array.from(document.querySelectorAll('a[href]'))
            .map(a => new URL(a.getAttribute('href'), location.href).toString().split('#')[0])
        """
    )
    for url in [page.url.split("#")[0], fallback_url, *urls]:
        if is_job_url(url):
            return url
    return page.url.split("#")[0] or fallback_url


async def extract_jobs(page: Page) -> list[CapturedJob]:
    applied = load_applied()
    company_history = merge_company_history()
    rows = await page.evaluate(
        """
        () => Array.from(document.querySelectorAll('a[href]')).map(a => {
            const href = new URL(a.getAttribute('href'), location.href).toString();
            const box = a.closest('[data-testid], article, li, tr, [role=listitem], div') || a;
            return {
                href,
                text: (box.innerText || a.innerText || '').trim()
            };
        }).filter(x => x.text.length > 20)
        """
    )
    out: list[CapturedJob] = []
    seen = set()
    for row in rows:
        url = row["href"].split("#")[0]
        if not is_job_url(url) or url in seen:
            continue
        seen.add(url)
        text = clean_multiline(row["text"])
        if len(text) < 8:
            continue
        title, company = title_company_from_text(text, url)
        duplicate = ""
        try:
            if job_key(url) in applied:
                duplicate = "applied.csv"
            else:
                duplicate = has_blocking_email_history(company_from_url(url), company_history)
        except Exception:
            duplicate = ""
        out.append(CapturedJob(title=title, company=company, url=url, text=text, duplicate_reason=duplicate))
    return out


def parse_result_list(body: str) -> list[tuple[str, str]]:
    lines = [clean(line) for line in body.splitlines() if clean(line)]
    try:
        start = next(i for i, line in enumerate(lines) if line.lower() == "most recent") + 1
    except StopIteration:
        return []
    try:
        end = next(i for i, line in enumerate(lines[start:], start) if line.lower() == "overview")
    except StopIteration:
        end = min(len(lines), start + 80)
    chunk = lines[start:end]
    jobs = []
    i = 0
    while i + 1 < len(chunk):
        company = chunk[i]
        title = chunk[i + 1]
        if title.lower() == "full-time" or company.lower() in {"open user menu", "save search"}:
            i += 1
            continue
        if i + 2 < len(chunk) and chunk[i + 2].lower() == "full-time":
            jobs.append((company, title))
            i += 5
        else:
            i += 1
    return jobs


async def discover_result_cards(page: Page, limit: int) -> list[dict[str, str]]:
    return await page.evaluate(
        """
        limit => {
            const selector = [
                'a[href]', 'article', 'li', '[role=listitem]', '[data-testid*=job i]',
                '[class*=job i]', '[class*=result i]'
            ].join(',');
            const markers = [
                'engineer', 'architect', 'developer', 'consultant', 'specialist',
                'full-time', 'remote', 'hybrid', 'posted', 'salary', 'compensation'
            ];
            const bad = [
                'open user menu', 'save search', 'privacy policy', 'terms of service',
                'sign in', 'sign up', 'upload resume'
            ];
            const jobHrefPattern = /(simplify\\.jobs\\/(jobs|job|p|company)\\/|greenhouse\\.io|ashbyhq\\.com|lever\\.co|workdayjobs\\.com|wellfound\\.com|ycombinator\\.com\\/companies\\/.*\\/jobs|\\/careers\\/|\\/jobs\\/)/;
            const visible = el => {
                const style = getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.visibility !== 'hidden' && style.display !== 'none' &&
                    rect.width > 30 && rect.height > 20;
            };
            const seen = new Set();
            const out = [];
            Array.from(document.querySelectorAll(selector)).forEach((el, i) => {
                if (!visible(el)) return;
                const text = (el.innerText || '').trim();
                const lower = text.toLowerCase();
                if (text.length < 12 || text.length > 1600) return;
                if (bad.some(x => lower.includes(x))) return;
                if (!markers.some(x => lower.includes(x))) return;
                const href = el.href || (el.querySelector('a[href]') || {}).href || '';
                const hasJobHref = jobHrefPattern.test(href);
                if (href && !hasJobHref) return;
                const key = `${href}|${text.slice(0, 220)}`;
                if (seen.has(key)) return;
                seen.add(key);
                const id = `simplify-capture-${i}`;
                el.setAttribute('data-simplify-capture-id', id);
                out.push({ id, href, text, hasJobHref });
            });
            out.sort((a, b) => Number(b.hasJobHref) - Number(a.hasJobHref) || b.text.length - a.text.length);
            return out.slice(0, limit);
        }
        """,
        limit,
    )


async def click_result_card(page: Page, card: dict[str, str]) -> bool:
    selector = f"[data-simplify-capture-id='{card['id']}']"
    try:
        await page.locator(selector).click(timeout=2500)
        await page.wait_for_load_state("domcontentloaded", timeout=2500)
    except Exception:
        try:
            await page.locator(selector).click(timeout=2500, force=True)
        except Exception:
            first_line = next((line for line in clean_multiline(card["text"]).splitlines() if line), "")
            if not first_line:
                return False
            try:
                await page.get_by_text(first_line, exact=True).first.click(timeout=2500)
            except Exception:
                return False
    await page.wait_for_timeout(1500)
    return True


async def capture_detail_page(page: Page, url: str, fallback_text: str) -> tuple[str, str]:
    detail_page = await page.context.new_page()
    try:
        await detail_page.goto(url, wait_until="domcontentloaded", timeout=8000)
        await detail_page.wait_for_timeout(1200)
        detail = await best_detail_text(detail_page)
        return await best_job_url(detail_page, url), extract_detail_tail(detail, fallback_text)
    finally:
        await detail_page.close()


async def extract_simplify_result_jobs(page: Page, max_jobs: int) -> list[CapturedJob]:
    applied = load_applied()
    company_history = merge_company_history()
    cards = await discover_result_cards(page, max(max_jobs * 5, max_jobs + 20))
    href_cards = [card for card in cards if is_job_url((card.get("href") or "").split("#")[0])]
    cards = href_cards or cards[:max_jobs]
    if not cards:
        body = await page.locator("body").inner_text(timeout=8000)
        cards = [{"id": "", "href": "", "text": f"{company}\n{title}"} for company, title in parse_result_list(body)[:max_jobs]]
    out: list[CapturedJob] = []
    seen = set()
    results_url = page.url
    for card in cards:
        if len(out) >= max_jobs:
            break
        fallback_text = clean_multiline(card["text"])
        fallback_url = (card.get("href") or page.url).split("#")[0]
        if is_job_url(fallback_url):
            url, detail = await capture_detail_page(page, fallback_url, fallback_text)
        elif card["id"]:
            await click_result_card(page, card)
            detail = extract_detail_tail(await best_detail_text(page), fallback_text)
            url = await best_job_url(page, fallback_url)
        else:
            fallback_lines = [line for line in fallback_text.splitlines() if line]
            title = fallback_lines[1] if len(fallback_lines) > 1 else fallback_text
            try:
                await page.get_by_text(title, exact=True).first.click(timeout=4000)
                await page.wait_for_timeout(1500)
            except Exception:
                pass
            detail = extract_detail_tail(await best_detail_text(page), fallback_text)
            url = await best_job_url(page, fallback_url)
        title, company = title_company_from_text(detail or fallback_text, url)
        key = url if is_job_url(url) else f"{company}|{title}"
        if not is_job_url(url) or key in seen or BLOCKED_URL_RE.search(url):
            continue
        seen.add(key)
        duplicate = ""
        try:
            if is_job_url(url) and job_key(url) in applied:
                duplicate = "applied.csv"
            else:
                duplicate = has_blocking_email_history(company_from_url(url) or company, company_history)
        except Exception:
            duplicate = ""
        out.append(CapturedJob(
            title=title,
            company=company,
            url=url,
            text=detail or fallback_text,
            duplicate_reason=duplicate,
        ))
        if page.url.split("#")[0] != results_url.split("#")[0]:
            try:
                await page.go_back(wait_until="domcontentloaded", timeout=8000)
            except Exception:
                await page.goto(results_url, wait_until="domcontentloaded")
            await page.wait_for_timeout(1000)
    return out


def rank(job: CapturedJob) -> int:
    blob = f"{job.title} {job.company} {job.text}".lower()
    score = 0
    for term, points in {
        "forward deployed": 20,
        "fde": 18,
        "solutions engineer": 14,
        "solution architect": 14,
        "customer engineer": 13,
        "field engineer": 13,
        "deployment": 12,
        "implementation": 10,
        "applied ai": 10,
        "ai engineer": 9,
        "agent": 8,
        "llm": 8,
        "voice": 8,
        "python": 4,
        "full stack": 4,
        "backend": 3,
        "enterprise": 5,
        "customer": 5,
        "integrations": 5,
        "api": 4,
        "workflow": 4,
        "stakeholder": 4,
    }.items():
        if term in blob:
            score += points
    for bad in ["intern", "manager", "director", "sales", "marketing", "recruiter", "robot", "hardware", "mechanical"]:
        if bad in blob:
            score -= 20
    if job.duplicate_reason:
        score -= 100
    return score


async def run_search(page: Page, query: str) -> None:
    await page.goto("https://simplify.jobs/jobs", wait_until="domcontentloaded")
    await page.wait_for_timeout(3000)
    selectors = [
        "input[type='search']",
        "input[placeholder*='Search']",
        "input[placeholder*='search']",
        "input",
    ]
    for selector in selectors:
        try:
            loc = page.locator(selector).first
            await loc.click(timeout=3000)
            await loc.fill("")
            await loc.fill(query)
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(5000)
            break
        except Exception:
            continue
    for _ in range(10):
        await page.mouse.wheel(0, 1800)
        await page.wait_for_timeout(800)


def write_jobs(jobs: list[CapturedJob]) -> None:
    jobs = sorted(jobs, key=rank, reverse=True)
    with OUTPUT.open("w", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["score", "title", "company", "url", "duplicate_reason", "text"]
        )
        writer.writeheader()
        for job in jobs:
            writer.writerow({
                "score": rank(job),
                "title": job.title,
                "company": job.company,
                "url": job.url,
                "duplicate_reason": job.duplicate_reason,
                "text": job.text,
            })
    fresh = [j for j in jobs if not j.duplicate_reason and rank(j) > 0]
    print(f"Captured {len(jobs)} visible links to {OUTPUT.name}")
    print(f"Fresh positive-score candidates: {len(fresh)}")
    for job in fresh[:20]:
        print(f"  [{rank(job)}] {job.title} — {job.company}\n    {job.url}")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", action="append", default=[])
    parser.add_argument("--manual-wait", type=int, default=15)
    parser.add_argument("--max-per-query", type=int, default=25)
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()
    queries = args.query or DEFAULT_QUERIES

    async with async_playwright() as pw:
        browser = await pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=args.headless,
        )
        page = await browser.new_page()
        await page.goto("https://simplify.jobs/jobs", wait_until="domcontentloaded")
        print("Simplify is open. Log in if needed.")
        print(f"I will start searching in {args.manual_wait} seconds.")
        await page.wait_for_timeout(args.manual_wait * 1000)
        jobs = []
        seen_urls = set()
        for query in queries:
            print(f"Searching Simplify: {query}")
            await run_search(page, query)
            captured = await extract_simplify_result_jobs(page, args.max_per_query)
            if not captured:
                captured = await extract_jobs(page)
            for job in captured:
                seen_key = job.url if is_job_url(job.url) else f"{job.company}|{job.title}"
                if seen_key not in seen_urls:
                    seen_urls.add(seen_key)
                    jobs.append(job)
        write_jobs(jobs)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
