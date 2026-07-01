"""Generate follow-up tasks and draft messages from submitted applications.

This does not send anything. It reads application_records.csv / applied.csv,
creates a CSV of outreach targets to research, and writes ready-to-edit drafts.

Usage:
    python followup.py              # today's submitted applications
    python followup.py --days 7     # last 7 days
"""

import argparse
import csv
import datetime as dt
import os
import re
import urllib.parse


HERE = os.path.dirname(__file__)
APP_RECORDS = os.path.join(HERE, "application_records.csv")
APPLIED = os.path.join(HERE, "applied.csv")
OUT_DIR = os.path.join(HERE, "followup_drafts")
TASKS = os.path.join(HERE, "followup_tasks.csv")


COMPANY_HINTS = {
    "cartesia": "voice AI, realtime speech, agents, Cartesia TTS",
    "livekit": "realtime voice/video infra, agents, Pipecat-adjacent workflows",
    "serval": "applied AI for IT/service workflows, customer-facing automation",
    "snorkelai": "applied AI solutions, enterprise data-centric AI",
    "vapi": "voice agents, agent engineering, customer workflows",
    "sierra": "customer-facing agents, enterprise agent development",
    "harvey": "legal AI agents, production agent workflows",
}


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "draft"


def parse_date(value: str) -> dt.date | None:
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


def read_records(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def submitted_recent(rows: list[dict], days: int) -> list[dict]:
    cutoff = dt.date.today() - dt.timedelta(days=days - 1)
    out = []
    for row in rows:
        day = parse_date(row.get("date", ""))
        status = (row.get("status") or "").lower()
        if day and day >= cutoff and "submitted" in status:
            out.append(row)
    return out


def role_from_notes(row: dict) -> str:
    notes = row.get("notes") or row.get("status") or ""
    notes = notes.replace("submitted-", "").replace("_", " ").replace("-", " ")
    return " ".join(notes.split())[:90] or "applied AI / FDE role"


def search_urls(company: str, role: str) -> dict[str, str]:
    q_base = f'{company} {role}'
    return {
        "linkedin_recruiter": "https://www.google.com/search?q="
        + urllib.parse.quote(f'site:linkedin.com/in {company} recruiter talent acquisition AI'),
        "linkedin_hiring_manager": "https://www.google.com/search?q="
        + urllib.parse.quote(f'site:linkedin.com/in {company} head of ai engineering agents'),
        "linkedin_fde": "https://www.google.com/search?q="
        + urllib.parse.quote(f'site:linkedin.com/in {company} "forward deployed" OR "solutions engineer"'),
        "company_people": "https://www.google.com/search?q="
        + urllib.parse.quote(f'{q_base} hiring manager founder agent AI'),
    }


def draft_text(row: dict) -> str:
    company = row.get("company", "").strip()
    role = role_from_notes(row)
    hints = COMPANY_HINTS.get(company.lower(), "production agentic AI and customer workflows")
    url = row.get("url", "")
    resume = row.get("resume_path", "")

    return f"""Subject: Applied for {role} at {company}

Hi <Name>,

I applied for {role} at {company}. The role stood out because it connects directly to my recent work in {hints}.

Two relevant examples:
- Production-style voice agent: Pipecat + Twilio + Deepgram + Cartesia + GPT-4.1, with a technical walkthrough.
- Mind2Web computer-use agent: DeBERTa candidate ranker with 93% recall@10 on unseen domains.

GitHub: https://github.com/torontodeveloper/voice-agent-chitti
HuggingFace: https://huggingface.co/torontodeveloper
YouTube: https://youtube.com/@KevinKakolla

Application: {url}
Tailored resume used: {resume}

If useful, I would be happy to walk through the systems and how I would approach {company}'s agent/customer workflow problems.

Kevin
"""


def write_outputs(rows: list[dict]) -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(TASKS, "w", newline="") as f:
        fieldnames = [
            "date",
            "company",
            "role_hint",
            "application_url",
            "draft_path",
            "linkedin_recruiter",
            "linkedin_hiring_manager",
            "linkedin_fde",
            "company_people",
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            company = row.get("company", "").strip()
            role = role_from_notes(row)
            draft_path = os.path.join(OUT_DIR, f"{row.get('date')}-{slug(company)}-{slug(role)}.txt")
            with open(draft_path, "w") as d:
                d.write(draft_text(row))
            searches = search_urls(company, role)
            writer.writerow({
                "date": row.get("date", ""),
                "company": company,
                "role_hint": role,
                "application_url": row.get("url", ""),
                "draft_path": os.path.relpath(draft_path, HERE),
                **searches,
            })


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=1)
    args = parser.parse_args()

    rows = read_records(APP_RECORDS)
    if not rows:
        rows = read_records(APPLIED)
    rows = submitted_recent(rows, args.days)
    write_outputs(rows)
    print(f"generated {len(rows)} follow-up drafts")
    print(f"tasks: {TASKS}")
    print(f"drafts: {OUT_DIR}")


if __name__ == "__main__":
    main()
