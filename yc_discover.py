"""Discover LIVE workatastartup job URLs using the agent's logged-in browser.

Uses the same persistent profile as main.py/login.py (so it sees the logged-in
YC board), runs one or more searches, scrolls to load results, and writes the
unique job URLs it finds to yc_jobs.txt — ready to feed to ./apply.sh --queue.

Usage:
    python yc_discover.py                          # default FDE/AI/voice searches
    python yc_discover.py "forward deployed" "voice agent"
"""

import asyncio
import os
import re
import sys

from playwright.async_api import async_playwright

HERE = os.path.dirname(__file__)
PROFILE_DIR = os.path.join(HERE, ".browser-profile")
OUT_PATH = os.path.join(HERE, "yc_jobs.txt")

DEFAULT_QUERIES = [
    "forward deployed engineer",
    "member of technical staff",
    "machine learning engineer",
    "AI engineer",
]

# Titles vary company-to-company, so we match on SKILLS, not titles. Title is only
# used to drop obvious non-IC / non-eng roles up front.
TITLE_EXCLUDE = [
    "director", "manager", "head of", "vp ", "vice president", "chief",
    "intern", "designer", "sales", "recruit", "marketing", "account exec",
    "product manager", "data scientist", "researcher", "research scientist",
    "principal", "customer success", "solutions architect", "developer advocate",
]
# Kevin's skill set — a role's DESCRIPTION must show real AI/ML engineering signal.
SKILLS = [
    "llm", "large language model", "genai", "gen ai", "generative ai", "rag",
    "agent", "agentic", "fine-tun", "qlora", "lora", "dpo", "rlhf", "transformer",
    "pytorch", "tensorflow", "machine learning", "deep learning", "nlp",
    "embedding", "vector", "inference", "voice ai", "speech", "mlops",
    "prompt", "python",
]
# A role must have at least this many distinct skill hits to count as a match.
MIN_SKILL_HITS = 3


def title_ok(title: str) -> bool:
    t = title.lower()
    return not any(x in t for x in TITLE_EXCLUDE)


def skills_score(jd_text: str) -> int:
    t = jd_text.lower()
    return sum(1 for s in SKILLS if s in t)


async def search_urls(page, query: str) -> dict[str, str]:
    url = f"https://www.workatastartup.com/jobs?role=eng&query={query.replace(' ', '+')}"
    print(f"  searching: {query}")
    await page.goto(url, wait_until="domcontentloaded")
    await page.wait_for_timeout(3500)
    # scroll to pull in lazy-loaded results
    for _ in range(6):
        await page.mouse.wheel(0, 4000)
        await page.wait_for_timeout(1200)
    # capture each job link WITH its visible title text so we can filter by role
    items = await page.evaluate(
        "() => Array.from(document.querySelectorAll('a[href*=\"/jobs/\"]'))"
        ".map(a => ({href: a.href, text: (a.innerText||'').trim().replace(/\\s+/g,' ')}))")
    found: dict[str, str] = {}
    kept = 0
    for it in items:
        m = re.search(r"/jobs/(\d+)", it["href"])
        if not m:
            continue
        url = f"https://www.workatastartup.com/jobs/{m.group(1)}"
        title = it["text"][:120]
        # drop obvious non-IC roles by title; keep the rest for the skill deep-scan
        if title and not title_ok(title):
            continue
        found[url] = title or "(title TBD)"
        kept += 1
    print(f"    -> {kept} candidates (pre skill-scan)")
    return found


async def deep_skill_scan(page, url: str, title: str) -> tuple[bool, int, str]:
    """Open the job page, read its description, keep it only if it shows enough of
    Kevin's skill set. Returns (keep, score, real_title)."""
    await page.goto(url, wait_until="domcontentloaded")
    await page.wait_for_timeout(2000)
    text = await page.evaluate("() => document.body.innerText")
    # grab a better title from the page heading if the card text was thin
    try:
        h = (await page.locator("h1, h2").first.inner_text(timeout=1500)).strip()
        real_title = (h or title).replace("\n", " ")[:90]
    except Exception:
        real_title = title
    # respect the title exclusions on the real page title too
    if not title_ok(real_title):
        return (False, 0, real_title)
    score = skills_score(text)
    return (score >= MIN_SKILL_HITS, score, real_title)


async def main(queries: list[str]):
    candidates: dict[str, str] = {}
    matches: list[tuple[str, str, int]] = []
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR, headless=False)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        # confirm we're logged in
        await page.goto("https://www.workatastartup.com/", wait_until="domcontentloaded")
        await page.wait_for_timeout(2500)
        body = (await page.evaluate("() => document.body.innerText")).lower()
        if "log in" in body and "log out" not in body:
            print("!! Not logged in. Run: python login.py  (log in, close window) first.")
        for q in queries:
            try:
                candidates |= await search_urls(page, q)
            except Exception as e:
                print(f"    ! '{q}' failed ({type(e).__name__}: {e})")
        print(f"\n{len(candidates)} candidates — skill-scanning each...")
        for i, (url, title) in enumerate(sorted(candidates.items()), 1):
            try:
                keep, score, real_title = await deep_skill_scan(page, url, title)
            except Exception as e:
                print(f"  [{i}] {url} scan failed ({type(e).__name__}) — skip")
                continue
            mark = "KEEP" if keep else "drop"
            print(f"  [{i}] {mark} (skills:{score}) {real_title}")
            if keep:
                matches.append((url, real_title, score))
        await ctx.close()
    matches.sort(key=lambda x: -x[2])  # strongest skill match first
    with open(OUT_PATH, "w") as f:
        f.write("# Live workatastartup roles matching Kevin's skill set"
                " (FDE/MTS/ML/AI eng), strongest first.\n")
        for url, title, score in matches:
            f.write(f"# [skills:{score}] {title}\n{url}\n")
    print(f"\nKept {len(matches)} skill-matched roles -> {OUT_PATH}")


if __name__ == "__main__":
    qs = sys.argv[1:] or DEFAULT_QUERIES
    asyncio.run(main(qs))
