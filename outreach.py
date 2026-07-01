"""Email outreach — a SEPARATE tool from the form-filler (main.py).

Composes tailored cold/intro emails to recruiters or founders from your
profile.yaml identity + voice, SAVES each as a draft for review, and only
actually SENDS when you pass --send. Mirrors the form-filler's rule: it never
acts on your behalf without you. main.py is untouched; this only imports its
profile/LLM helpers so there's one source of truth, no duplicated config.

Usage:
    python outreach.py                       # draft every contact in contacts.yaml
    python outreach.py --send                # send them (needs Gmail creds, see below)
    python outreach.py --to jane@acme.com --name "Jane" --company "Acme" \
                       --role "Forward Deployed Engineer" --context "seed-stage, voice AI"

Sending (only needed for --send) — set in .env:
    OUTREACH_EMAIL=you@gmail.com
    OUTREACH_APP_PASSWORD=xxxx xxxx xxxx xxxx   # Gmail App Password (not your login pw)
"""

import argparse
import csv
import datetime
import json
import os
import re
import smtplib
import sys
from email.message import EmailMessage

import yaml

# Reuse the form-filler's single source of truth — without touching its logic.
from main import LLM_MODEL, get_llm, load_profile

HERE = os.path.dirname(__file__)
CONTACTS_PATH = os.path.join(HERE, "contacts.yaml")
DRAFTS_DIR = os.path.join(HERE, "outreach_drafts")
LOG_PATH = os.path.join(HERE, "outreach_log.csv")


def compose(profile: dict, contact: dict) -> dict:
    """Draft a tailored outreach email. Returns {"subject", "body"}."""
    p = profile["personal"]
    prompt = f"""Write a short, tailored cold outreach email for a job seeker.

SENDER (authoritative — never invent facts not present here):
name: {p['first_name']} {p['last_name']}
links: LinkedIn {p['linkedin']} | GitHub {p['github']} | {p['website']}
summary: {profile['experience']['summary']}
work_examples: {yaml.safe_dump(profile['work_examples'])}
voice: {profile['voice_notes']}

RECIPIENT:
name: {contact.get('name', '')}
company: {contact.get('company', '')}
role of interest: {contact.get('role', '')}
context/notes: {contact.get('context', '')}

Rules:
- 90-140 words, warm and direct, in the sender's voice.
- Open with why THIS company/role specifically (use the context) — not a generic intro.
- Cite ONE concrete work_example with its real link and real numbers.
- Exactly one clear ask: a quick chat, or to be considered for the role.
- No fluff, no overclaiming, no fabricated facts. Plain text, no markdown.
- Sign off with the sender's first name only.
Return ONLY JSON: {{"subject": "...", "body": "..."}}"""
    resp = get_llm().chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
    )
    return json.loads(resp.choices[0].message.content)


def save_draft(contact: dict, email: dict) -> str:
    os.makedirs(DRAFTS_DIR, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", (contact.get("company") or "draft").lower()).strip("-")
    path = os.path.join(DRAFTS_DIR, f"{slug or 'draft'}.txt")
    with open(path, "w") as f:
        f.write(f"To: {contact.get('email', '')}\n")
        f.write(f"Subject: {email['subject']}\n\n")
        f.write(email["body"].rstrip() + "\n")
    return path


def send_email(contact: dict, email: dict) -> None:
    sender = os.environ.get("OUTREACH_EMAIL")
    pw = os.environ.get("OUTREACH_APP_PASSWORD")
    if not sender or not pw:
        raise RuntimeError(
            "OUTREACH_EMAIL / OUTREACH_APP_PASSWORD not set in .env — can't --send")
    to = contact.get("email")
    if not to:
        raise RuntimeError(f"no email address for contact {contact.get('company')}")
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = email["subject"]
    msg.set_content(email["body"].rstrip() + "\n")
    with smtplib.SMTP("smtp.gmail.com", 587) as s:
        s.starttls()
        s.login(sender, pw.replace(" ", ""))
        s.send_message(msg)


def log(contact: dict, status: str) -> None:
    new = not os.path.exists(LOG_PATH)
    with open(LOG_PATH, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["date", "company", "email", "status"])
        w.writerow([datetime.date.today(), contact.get("company", ""),
                    contact.get("email", ""), status])


def load_contacts() -> list[dict]:
    if not os.path.exists(CONTACTS_PATH):
        return []
    with open(CONTACTS_PATH) as f:
        data = yaml.safe_load(f) or {}
    return data.get("contacts", [])


def main():
    ap = argparse.ArgumentParser(description="Tailored job-outreach emails (drafts by default).")
    ap.add_argument("--send", action="store_true", help="actually send (default: draft only)")
    ap.add_argument("--to", help="recipient email for a single one-off outreach")
    ap.add_argument("--name", default="", help="recipient name")
    ap.add_argument("--company", default="", help="company")
    ap.add_argument("--role", default="", help="role of interest")
    ap.add_argument("--context", default="", help="why-them notes for tailoring")
    args = ap.parse_args()

    if args.to:
        contacts = [{"email": args.to, "name": args.name, "company": args.company,
                     "role": args.role, "context": args.context}]
    else:
        contacts = load_contacts()
    if not contacts:
        print("No contacts. Add them to contacts.yaml or pass --to ...")
        sys.exit(1)

    profile = load_profile()
    for c in contacts:
        label = c.get("company") or c.get("email") or "contact"
        print(f"\n=== {label}")
        try:
            email = compose(profile, c)
        except Exception as e:
            print(f"  ! compose failed ({type(e).__name__}: {e}) — skipping")
            continue
        path = save_draft(c, email)
        print(f"  draft saved: {path}")
        print(f"  subject: {email['subject']}")
        if args.send:
            try:
                send_email(c, email)
                log(c, "sent")
                print("  >>> SENT ✓")
            except Exception as e:
                log(c, f"send-failed:{type(e).__name__}")
                print(f"  ! send failed ({type(e).__name__}: {e}) — draft kept for manual send")
        else:
            log(c, "drafted")
            print("  (draft only — review it, then re-run with --send to email it)")


if __name__ == "__main__":
    main()
