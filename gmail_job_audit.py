"""Read-only Gmail audit for job application duplicate avoidance.

This script scans job-search emails and writes gmail_job_records.csv. It uses
Gmail OAuth read-only scope and never sends, deletes, archives, or modifies mail.

Setup:
    pip install -r requirements.txt
    python gmail_job_audit.py --setup-help
    python gmail_job_audit.py
"""

from __future__ import annotations

import argparse
import base64
import csv
import datetime as dt
import os
import re
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Iterable

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from main import job_key


ROOT = Path(__file__).resolve().parent
CREDENTIALS_PATH = ROOT / "gmail_credentials.json"
TOKEN_PATH = ROOT / "gmail_token.json"
OUTPUT_PATH = ROOT / "gmail_job_records.csv"
APPLIED_LOG = ROOT / "applied.csv"
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

JOB_QUERY = (
    'newer_than:180d ('
    '"thank you for applying" OR '
    '"application has been received" OR '
    '"we received your application" OR '
    '"your application for" OR '
    '"decided not to move forward" OR '
    '"move forward with other candidates" OR '
    '"after careful consideration" OR '
    '"CodeSignal" OR '
    '"assessment" OR '
    '"recruiter call" OR '
    '"interview" OR '
    '"ashby" OR '
    '"greenhouse" OR '
    '"lever"'
    ")"
)

COMPANY_ALIASES = {
    "ashby": None,
    "greenhouse": None,
    "lever": None,
    "workatastartup": None,
    "scale ai": "scaleai",
    "scale": "scaleai",
    "cohere": "cohere",
    "vercel": "vercel",
    "decagon": "decagon",
    "sierra": "sierra",
    "ramp": "ramp",
    "databricks": "databricks",
    "glean": "gleanwork",
    "harvey": "harvey",
    "hubspot": "hubspot",
    "serval": "serval",
    "vapi": "vapi",
    "cartesia": "cartesia",
    "livekit": "livekit",
    "dataiku": "dataiku",
    "baseten": "baseten",
    "snorkel": "snorkelai",
    "palantir": "palantir",
    "langchain": "langchain",
    "openai": "openai",
    "anthropic": "anthropic",
    "mistral": "mistral",
    "together ai": "togetherai",
    "togetherai": "togetherai",
    "assemblyai": "assemblyai",
    "elevenlabs": "elevenlabs",
    "deepgram": "deepgram",
    "cresta": "cresta",
    "cognition": "cognition",
    "hebbia": "hebbia",
    "webflow": "webflow",
    "brex": "brex",
    "stripe": "stripe",
    "anyscale": "anyscale",
    "distyl": "distyl",
    "komodo": "komodohealth",
    "galileo": "galileo",
}


@dataclass
class EmailRecord:
    date: str
    company: str
    status: str
    subject: str
    sender: str
    snippet: str
    urls: str
    gmail_url: str


def get_service():
    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_PATH.exists():
                raise SystemExit(
                    f"Missing {CREDENTIALS_PATH.name}. Run with --setup-help for setup."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_PATH), SCOPES
            )
            creds = flow.run_local_server(port=0)
        TOKEN_PATH.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def setup_help() -> None:
    print(
        f"""
Create a Google OAuth Desktop client and save it here:
  {CREDENTIALS_PATH}

Required Gmail scope:
  {SCOPES[0]}

This script stores the local OAuth token at:
  {TOKEN_PATH}

It is read-only. It cannot send or modify email.
"""
    )


def get_header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def decode_part(part: dict) -> str:
    data = part.get("body", {}).get("data")
    if not data:
        return ""
    return base64.urlsafe_b64decode(data.encode()).decode("utf-8", "replace")


def message_text(payload: dict) -> str:
    chunks = []
    if payload.get("mimeType", "").startswith("text/"):
        chunks.append(decode_part(payload))
    for part in payload.get("parts", []) or []:
        chunks.append(message_text(part))
    return "\n".join(c for c in chunks if c)


def normalize_date(value: str) -> str:
    try:
        parsed = parsedate_to_datetime(value)
        return parsed.astimezone().date().isoformat()
    except Exception:
        return ""


def clean_company(value: str) -> str:
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value[:60] or "unknown"


def infer_company(subject: str, sender: str, text: str) -> str:
    haystack = f"{subject}\n{sender}\n{text}".lower()
    for raw, normalized in COMPANY_ALIASES.items():
        if raw in haystack and normalized:
            return normalized

    patterns = [
        r"application (?:for|to) (?:the )?(?:.+?) (?:role|position|opening) at ([A-Z][A-Za-z0-9 .&-]{2,40})",
        r"thanks for applying to ([A-Z][A-Za-z0-9 .&-]{2,40})",
        r"thank you for applying to ([A-Z][A-Za-z0-9 .&-]{2,40})",
        r"your application to ([A-Z][A-Za-z0-9 .&-]{2,40})",
        r"interest in ([A-Z][A-Za-z0-9 .&-]{2,40})",
    ]
    source = f"{subject}\n{text}"
    for pattern in patterns:
        match = re.search(pattern, source, re.IGNORECASE)
        if match:
            company = match.group(1)
            company = re.split(r"\s+(?:has|for|was|is|role|position)\b", company)[0]
            return clean_company(company)

    email_domain = re.search(r"@([A-Za-z0-9.-]+)", sender)
    if email_domain:
        domain = email_domain.group(1).lower()
        parts = domain.split(".")
        if len(parts) >= 2 and parts[-2] not in {"ashbyhq", "greenhouse", "lever"}:
            return clean_company(parts[-2])
    return "unknown"


def infer_status(subject: str, text: str) -> str:
    haystack = f"{subject}\n{text}".lower()
    rejection_terms = [
        "decided not to move forward",
        "move forward with other candidates",
        "not be moving forward",
        "will not be moving forward",
        "after careful consideration",
        "not selected",
        "position has been filled",
    ]
    if any(term in haystack for term in rejection_terms):
        return "rejected"
    if "codesignal" in haystack or "assessment" in haystack or "hackerrank" in haystack:
        return "assessment"
    if "recruiter call" in haystack or "interview" in haystack or "schedule" in haystack:
        return "interview"
    confirmation_terms = [
        "thank you for applying",
        "thanks for applying",
        "application has been received",
        "we received your application",
        "your application for",
    ]
    if any(term in haystack for term in confirmation_terms):
        return "submitted-confirmed"
    return "unknown"


def extract_urls(text: str) -> list[str]:
    urls = re.findall(r"https?://[^\s)>\"]+", text)
    cleaned = []
    for url in urls:
        url = url.rstrip(".,;]")
        if any(host in url for host in ("ashbyhq.com", "greenhouse.io", "lever.co", "workatastartup.com", "ycombinator.com")):
            cleaned.append(url)
    return sorted(set(cleaned))


def gmail_search(service, query: str, limit: int) -> Iterable[dict]:
    page_token = None
    seen = 0
    while True:
        resp = service.users().messages().list(
            userId="me",
            q=query,
            maxResults=min(100, limit - seen),
            pageToken=page_token,
        ).execute()
        for item in resp.get("messages", []):
            seen += 1
            yield item
            if seen >= limit:
                return
        page_token = resp.get("nextPageToken")
        if not page_token or seen >= limit:
            return


def fetch_records(service, query: str, limit: int) -> list[EmailRecord]:
    records = []
    for item in gmail_search(service, query, limit):
        msg = service.users().messages().get(
            userId="me", id=item["id"], format="full"
        ).execute()
        payload = msg.get("payload", {})
        headers = payload.get("headers", [])
        subject = get_header(headers, "Subject")
        sender = get_header(headers, "From")
        date = normalize_date(get_header(headers, "Date"))
        body = message_text(payload)
        snippet = msg.get("snippet", "")
        text = f"{snippet}\n{body}"
        urls = extract_urls(text)
        records.append(
            EmailRecord(
                date=date,
                company=infer_company(subject, sender, text),
                status=infer_status(subject, text),
                subject=subject,
                sender=sender,
                snippet=snippet.replace("\n", " ")[:300],
                urls=" ".join(urls),
                gmail_url=f"https://mail.google.com/mail/u/0/#inbox/{item['id']}",
            )
        )
    records.sort(key=lambda r: (r.date, r.company, r.subject), reverse=True)
    return records


def write_records(records: list[EmailRecord], path: Path) -> None:
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "date",
                "company",
                "status",
                "subject",
                "sender",
                "snippet",
                "urls",
                "gmail_url",
            ],
        )
        writer.writeheader()
        for record in records:
            writer.writerow(record.__dict__)


def load_applied_rows() -> list[dict]:
    if not APPLIED_LOG.exists():
        return []
    with APPLIED_LOG.open() as f:
        return list(csv.DictReader(f))


def print_summary(records: list[EmailRecord]) -> None:
    by_company: dict[str, set[str]] = {}
    for record in records:
        by_company.setdefault(record.company, set()).add(record.status)

    print(f"Wrote {len(records)} email records to {OUTPUT_PATH.name}")
    print("\nCompanies found in Gmail:")
    for company in sorted(by_company):
        statuses = ", ".join(sorted(by_company[company]))
        print(f"  {company}: {statuses}")

    applied_rows = load_applied_rows()
    applied_companies = {r.get("company", "").lower() for r in applied_rows}
    email_companies = set(by_company)
    only_email = sorted(c for c in email_companies - applied_companies if c != "unknown")
    if only_email:
        print("\nIn Gmail but not applied.csv company list:")
        for company in only_email[:40]:
            print(f"  {company}: {', '.join(sorted(by_company[company]))}")

    applied_keys = {
        job_key(r.get("url", ""))
        for r in applied_rows
        if r.get("url") and not r.get("url", "").startswith("unknown")
    }
    email_urls = []
    for record in records:
        for url in record.urls.split():
            email_urls.append((url, record.company, record.status))
    key_matches = [(u, c, s) for u, c, s in email_urls if job_key(u) in applied_keys]
    if key_matches:
        print("\nEmail URLs already present in applied.csv:")
        for url, company, status in key_matches[:30]:
            print(f"  {company} [{status}] {url}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--setup-help", action="store_true")
    parser.add_argument("--query", default=JOB_QUERY)
    parser.add_argument("--limit", type=int, default=500)
    args = parser.parse_args()

    if args.setup_help:
        setup_help()
        return

    service = get_service()
    records = fetch_records(service, args.query, args.limit)
    write_records(records, OUTPUT_PATH)
    print_summary(records)


if __name__ == "__main__":
    main()
