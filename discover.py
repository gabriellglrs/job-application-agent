"""Daily job discovery: sweep Greenhouse/Ashby/Lever boards for the companies in
companies.yaml, keep US/remote roles matching Kevin's skill set, drop anything
already in applied.csv (via job_key), and write the top N to jobs.txt.

Usage:
    python discover.py                   # write top 20 to jobs.txt
    python discover.py -n 30             # different batch size
    python discover.py --per-company 3   # allow up to 3 roles per company
    python discover.py --per-company 0   # no per-company cap (old behavior)
    python discover.py --dry-run         # print matches, don't touch jobs.txt
"""

import argparse
import datetime
import json
import os
import re
import urllib.request

import yaml

from main import (
    company_from_url,
    has_blocking_email_history,
    job_key,
    load_applied,
    load_applied_company_history,
    load_email_company_history,
)

HERE = os.path.dirname(__file__)
DATA_DIR = os.path.join(HERE, "data")
COMPANIES = os.path.join(DATA_DIR, "companies.yaml")
QUEUE = os.path.join(DATA_DIR, "jobs.txt")

# Companies that have repeatedly rejected (different roles each time) — skip
# entirely so discovery stops re-queuing their new postings. URL-level dedup
# can't catch these because each rejection is a distinct posting. Revisit only
# with a referral. Slugs match the keys under each board in companies.yaml.
SKIP_COMPANIES = {
    "deepgram",   # rejected 3x (see memory: deepgram-repeated-rejections)
    "cresta",     # rejected 3+x (see memory: cresta-repeated-rejections)
    "scaleai",    # active pipeline / recruiter call already happened
    "cohere",     # upcoming recruiter call
    "databricks", # FDE already submitted; avoid duplicate business-unit variants
    "ramp",       # active after CodeSignal; avoid duplicates for now
    "decagon",    # application-limit history
    "openai",     # recent hard application limit
    "cognition",  # application-limit history
}

TITLE_EXCLUDE = [
    "intern", "new grad", "manager", "director", "recruiter", "sales",
    "marketing", "marketer", "designer", "phd", "head of", "vp ",
    "physical design", "design verification", "hardware",  # OpenAI chip FDE roles
    "federal", "ts required", "clearance", "government",
]
ROLE_SIGNALS = {
    # Customer-facing / FDE signals: strongest because Kevin's interviews convert here.
    "forward deployed": 10,
    "customer-facing": 9,
    "customer facing": 9,
    "embedded with customers": 9,
    "work directly with customers": 9,
    "client-facing": 8,
    "client facing": 8,
    "solutions engineer": 8,
    "solution architect": 7,
    "implementation": 7,
    "deployment": 7,
    "field engineer": 7,
    "technical advisor": 6,
    "stakeholder": 5,
    "ambiguous": 5,
    "enterprise customer": 5,

    # AI/agent work: proves modern relevance.
    "agentic": 8,
    "ai agent": 8,
    "agents": 6,
    "llm": 7,
    "large language model": 7,
    "genai": 7,
    "generative ai": 7,
    "rag": 6,
    "tool calling": 6,
    "eval": 5,
    "guardrail": 5,
    "fine-tun": 4,
    "prompt": 4,
    "inference": 4,
    "machine learning": 4,
    "ml engineer": 4,

    # Kevin's concrete differentiators.
    "voice": 7,
    "speech": 6,
    "twilio": 5,
    "deepgram": 5,
    "cartesia": 5,
    "pipecat": 5,
    "browser": 4,
    "playwright": 4,
    "workflow automation": 4,

    # Core software/integration base.
    "python": 3,
    "java": 3,
    "typescript": 3,
    "react": 3,
    "full-stack": 4,
    "full stack": 4,
    "fastapi": 3,
    "django": 3,
    "spring": 3,
    "api": 3,
    "integration": 5,
    "distributed systems": 4,
    "kubernetes": 3,
    "terraform": 3,
    "aws": 3,
    "azure": 3,
    "databricks": 3,
}

NEGATIVE_SIGNALS = {
    "research scientist": 8,
    "phd": 8,
    "ads bidding": 6,
    "payment intelligence": 5,
    "infrastructure only": 4,
    "security clearance": 8,
    "ts/sci": 8,
    "federal": 7,
    "government": 6,
}

MIN_SKILL_SCORE = 14
# Empty/ambiguous locations pass (better to over-queue than miss remote roles).
LOCATION_INCLUDE = [
    "remote", "united states", "usa", " us", "us)", "seattle", "san francisco",
    "new york", "anywhere", "north america", "bay area", "austin", "boston",
]
LOCATION_EXCLUDE = [
    "london", "berlin", "paris", "munich", "dublin", "amsterdam", "zurich",
    "bangalore", "bengaluru", "hyderabad", "india", "singapore", "tokyo",
    "sydney", "tel aviv", "warsaw", "emea", "europe", "united kingdom", " uk", "uk)",  # not bare "uk": milwaukee
    "germany", "netherlands", "spain", "france", "denmark", "sweden", "norway",
    "finland", "belgium", "austria", "poland", "portugal", "italy", "ireland",
    "switzerland", "taiwan", "australia", "japan", "korea", "china", "brazil",
    "mexico", "israel", "czech", "romania", "philippines", "vietnam",
]
# FDE first (Kevin's strongest angle), then voice (his project is the stack).
PRIORITY_WEIGHTS = {
    "forward deployed": 5, "deployed engineer": 5,
    "voice": 3, "conversational": 3,
    "agent": 1, "applied ai": 1,
}


def fetch_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def sweep_greenhouse(slug: str) -> list[dict]:
    data = fetch_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    return [{"company": slug, "title": j["title"],
             "location": (j.get("location") or {}).get("name", ""),
             "url": j["absolute_url"],
             "text": j.get("content", "")} for j in data.get("jobs", [])]


def sweep_ashby(slug: str) -> list[dict]:
    data = fetch_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    return [{"company": slug, "title": j["title"],
             "location": j.get("location", ""), "url": j["jobUrl"],
             "text": j.get("descriptionHtml", "") or j.get("descriptionPlain", "")}
            for j in data.get("jobs", []) if j.get("isListed", True)]


def sweep_lever(slug: str) -> list[dict]:
    data = fetch_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    return [{"company": slug, "title": j["text"],
             "location": (j.get("categories") or {}).get("location", ""),
             "url": j["hostedUrl"],
             "text": j.get("descriptionPlain", "") or j.get("description", "")}
            for j in data]


SWEEPERS = {"greenhouse": sweep_greenhouse, "ashby": sweep_ashby, "lever": sweep_lever}


# City/geo phrases stripped before title dedup, so "FDE - NYC" and "FDE - SF"
# collapse to one role per company (longest phrases first so they match before
# their single-word fragments).
_CITY_PHRASES = sorted([
    "new york city", "new york", "san francisco", "los angeles", "bay area",
    "north america", "mountain view", "redwood city", "washington d.c.",
    "washington dc", "west coast", "east coast", "united states", "remote us",
], key=len, reverse=True)
_CITY_SINGLE = {
    "nyc", "sf", "la", "seattle", "boston", "austin", "redwood", "remote",
    "hybrid", "onsite", "usa", "us", "dc", "ca", "ny", "wa", "tx", "ma",
}


def norm_title(title: str) -> str:
    """Title with parentheticals and location tokens stripped, for dedup."""
    t = re.sub(r"\(.*?\)", " ", title.lower())
    for p in _CITY_PHRASES:
        t = t.replace(p, " ")
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    return " ".join(w for w in t.split() if w not in _CITY_SINGLE)


def title_ok(title: str) -> bool:
    t = title.lower()
    return not any(k in t for k in TITLE_EXCLUDE)


def location_ok(loc: str) -> bool:
    l = loc.lower()
    if any(k in l for k in LOCATION_EXCLUDE):
        return False
    return not l or any(k in l for k in LOCATION_INCLUDE)


def plain_text(value: str) -> str:
    return re.sub(r"<[^>]+>", " ", value or "")


def score(job: dict) -> int:
    blob = f"{job['title']} {plain_text(job.get('text', ''))}".lower()
    positive = sum(w for k, w in ROLE_SIGNALS.items() if k in blob)
    negative = sum(w for k, w in NEGATIVE_SIGNALS.items() if k in blob)
    # Keep exact-title FDE roles at the top, but let skill-rich adjacent roles in.
    title_boost = sum(w for k, w in PRIORITY_WEIGHTS.items()
                      if k in job["title"].lower())
    return positive + title_boost - negative


def skill_ok(job: dict) -> bool:
    return score(job) >= MIN_SKILL_SCORE


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=20)
    ap.add_argument("--per-company", type=int, default=2,
                    help="max roles kept per company (0 = unlimited)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with open(COMPANIES) as f:
        targets = yaml.safe_load(f)

    applied = load_applied()
    email_history = load_email_company_history()
    for company, statuses in load_applied_company_history().items():
        email_history.setdefault(company, set()).update(statuses)
    seen, picked = set(applied), []
    seen_titles = set()  # same role posted per-geo (Cresta FDE US/Canada/UK) counts once
    for board, slugs in targets.items():
        for slug in slugs or []:
            if slug.lower() in SKIP_COMPANIES:
                continue  # repeat-rejector — don't re-queue new postings
            try:
                jobs = SWEEPERS[board](slug)
            except Exception:
                continue  # bad slug / board down — skip quietly
            n_before = len(picked)
            for j in jobs:
                if not title_ok(j["title"]) or not location_ok(j["location"]) or not skill_ok(j):
                    continue
                k = job_key(j["url"])
                tk = (j["company"], norm_title(j["title"]))
                if k in seen or tk in seen_titles:
                    continue
                if has_blocking_email_history(company_from_url(j["url"]), email_history):
                    continue
                seen.add(k)
                seen_titles.add(tk)
                picked.append(j)
            if len(picked) > n_before:
                print(f"  {board}/{slug}: +{len(picked) - n_before}")

    picked.sort(key=score, reverse=True)
    if args.per_company:
        # cap after sorting so each company keeps its highest-scoring roles,
        # stopping one flood-board (OpenAI posts 39) from eating every slot
        capped, counts = [], {}
        for j in picked:
            if counts.get(j["company"], 0) >= args.per_company:
                continue
            counts[j["company"]] = counts.get(j["company"], 0) + 1
            capped.append(j)
        picked = capped
    picked = picked[: args.n]

    print(f"\n{len(picked)} jobs queued:")
    for j in picked:
        print(f"  [{j['company']}] {j['title']} — {j['location']} "
              f"(score {score(j)})\n    {j['url']}")

    if not args.dry_run and picked:
        with open(QUEUE, "w") as f:
            f.write(f"# Queue {datetime.date.today()} — auto-discovered "
                    f"(previous batches in applied.csv)\n")
            for j in picked:
                f.write(j["url"] + "\n")
        print(f"\nwrote {len(picked)} URLs to data/jobs.txt — run ./apply.sh to fill them")


if __name__ == "__main__":
    main()
