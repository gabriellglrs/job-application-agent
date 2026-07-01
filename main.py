"""Job application agent: fills ATS application forms in its own browser.

Usage:
    python main.py <job_url> [<job_url> ...]
    python main.py --queue jobs.txt        # one URL per line

For each URL it opens a page in a dedicated Chromium window, extracts the form,
fills what it can from profile.yaml (LLM drafts the open-ended answers), then
PAUSES so you review and click Submit yourself. It never submits on its own.
"""

import asyncio
import csv
import json
import os
import sys

import yaml
from openai import OpenAI
from playwright.async_api import async_playwright

from tailor import make_cover_letter, make_tailored_resume

ROOT_DIR = os.path.dirname(__file__)
DATA_DIR = os.path.join(ROOT_DIR, "data")
BROWSER_PROFILE_DIR = os.path.join(ROOT_DIR, ".browser-profile")

PROFILE_PATH = os.path.join(DATA_DIR, "profile.yaml")
APPLIED_LOG = os.path.join(DATA_DIR, "applied.csv")
GMAIL_WEB_LOG = os.path.join(DATA_DIR, "gmail_web_job_records.csv")
GMAIL_API_LOG = os.path.join(DATA_DIR, "gmail_job_records.csv")

LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-5.4")
_llm = None


def get_llm() -> OpenAI:
    """Lazy client so importing this module (e.g. from discover.py for job_key)
    doesn't require API credentials."""
    global _llm
    if _llm is None:
        # Works with any OpenAI-compatible endpoint (OpenAI, Azure/Foundry /openai/v1, etc.)
        _llm = OpenAI(
            base_url=os.environ.get("LLM_BASE_URL") or None,
            api_key=os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY"),
        )
    return _llm
NO_TAILOR = False  # set by --no-tailor: upload the master resume as-is
FORCE = False  # set by --force: revisit URLs even if they are in applied.csv


def load_profile() -> dict:
    with open(PROFILE_PATH) as f:
        return yaml.safe_load(f)


def pick_resume(profile: dict, text: str) -> str:
    """Pick the role-appropriate CV from the JD/URL: an FDE role gets the FDE CV,
    everything else gets the AI CV. Falls back to the default resume_path."""
    p = profile["personal"]
    t = text.lower()
    is_fde = ("forward deployed" in t or "forward-deployed" in t
              or " fde" in t or "fde " in t or "(fde)" in t)
    key = "resume_path_fde" if is_fde else "resume_path_ai"
    return p.get(key) or p.get("resume_path", "")


def job_key(url: str) -> str:
    """Canonical identity for a job so the same posting under different URLs
    (company site with ?gh_jid=, greenhouse embed, board URL) dedupes to one.
    Greenhouse job ids are globally unique, so the id alone is enough."""
    import re
    import urllib.parse
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or ""
    qs = urllib.parse.parse_qs(parsed.query)
    gh_id = (qs.get("gh_jid") or qs.get("token") or [None])[0]
    if not gh_id and "greenhouse" in host:
        m = re.search(r"/jobs/(\d+)", parsed.path)
        gh_id = m.group(1) if m else None
    if gh_id:
        return f"greenhouse:{gh_id}"
    # YC: the real app lives on workatastartup.com/jobs/<id>; the public
    # ycombinator.com/companies/<slug>/jobs/<token> listing points at the same role.
    if "workatastartup.com" in host:
        m = re.search(r"/jobs/(\d+)", parsed.path)
        if m:
            return f"workatastartup:{m.group(1)}"
    if "ycombinator.com" in host:
        m = re.search(r"/jobs/([\w-]+)", parsed.path)
        if m:
            return f"yc:{m.group(1)}"
    parts = [p for p in parsed.path.split("/") if p]
    if "ashbyhq" in host or "lever.co" in host:
        return f"{host}:{'/'.join(parts[:2])}"
    return f"{host}{parsed.path}".rstrip("/")


def company_from_url(url: str) -> str:
    import urllib.parse
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or ""
    qs = urllib.parse.parse_qs(parsed.query)
    parts = [p for p in parsed.path.split("/") if p]
    if qs.get("for"):
        return qs["for"][0].lower()
    if "greenhouse" in host and parts:
        return parts[0].lower()
    if "ashbyhq" in host and parts:
        return parts[0].lower()
    if "lever.co" in host and parts:
        return parts[0].lower()
    if "workatastartup.com" in host:
        return "workatastartup"
    return host.removeprefix("www.").split(".")[0].lower()


def normalize_company_key(company: str) -> str:
    import re
    key = re.sub(r"[^a-z0-9]+", "", (company or "").lower())
    aliases = {
        "abnormal": "abnormalsecurity",
        "abnormalai": "abnormalsecurity",
        "abnormalkevin": "abnormalsecurity",
        "sigmacomputinghellokevin": "sigmacomputing",
        "sigmacomputingyourapplicationh": "sigmacomputing",
        "samsaradearkevin": "samsara",
        "sukikevin": "suki",
        "sukiyourapplicationhasbeenrec": "suki",
        "ripplekevin": "ripple",
        "palletkevin": "pallet",
        "airtableunfortunately": "airtable",
        "gitlabhikevin": "gitlab",
        "adobehinageswar": "adobe",
        "adoberesultinginaverycomp": "adobe",
        "enchargeaihikevin": "enchargeai",
        "glean": "gleanwork",
        "snorkel": "snorkelai",
        "together": "togetherai",
    }
    return aliases.get(key, key)


def load_applied() -> set[str]:
    if not os.path.exists(APPLIED_LOG):
        return set()
    with open(APPLIED_LOG) as f:
        next(f, None)  # header
        return {job_key(line.split(",")[2].strip())
                for line in f if line.count(",") >= 3}


def load_applied_company_history() -> dict[str, set[str]]:
    history: dict[str, set[str]] = {}
    if not os.path.exists(APPLIED_LOG):
        return history
    with open(APPLIED_LOG, newline="") as f:
        for row in csv.DictReader(f):
            company = normalize_company_key(row.get("company", ""))
            status = (row.get("status") or "").strip().lower()
            if company and status:
                history.setdefault(company, set()).add(status)
    return history


def load_email_company_history() -> dict[str, set[str]]:
    history: dict[str, set[str]] = {}
    for path in (GMAIL_WEB_LOG, GMAIL_API_LOG):
        if not os.path.exists(path):
            continue
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                company = normalize_company_key(row.get("company") or "")
                status = (row.get("status") or "").strip().lower()
                if not company or company == "unknown" or not status:
                    continue
                history.setdefault(company, set()).add(status)
    return history


def has_blocking_email_history(company: str, history: dict[str, set[str]]) -> str:
    key = normalize_company_key(company)
    statuses = history.get(key, set())
    if not statuses:
        return ""
    strong = [
        s for s in statuses
        if s in {"submitted-confirmed", "rejected", "assessment", "interview"}
        or "submitted" in s
        or "rejected" in s
        or "assessment" in s
        or "interview" in s
        or "codesignal" in s
        or "blocked" in s
    ]
    if not strong:
        return ""
    return ",".join(sorted(strong))


def record_applied(url: str, status: str = "reviewed"):
    import datetime
    import urllib.parse
    parsed = urllib.parse.urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    host = parsed.hostname or ""
    qs = urllib.parse.parse_qs(parsed.query)
    # company: greenhouse embed ?for=, else path slug, else the domain
    if qs.get("for"):
        company = qs["for"][0]
    elif ("greenhouse" in host or "ashbyhq" in host) and parts and parts[0] != "embed":
        company = parts[0]
    else:
        company = host.removeprefix("www.").split(".")[0]
    with open(APPLIED_LOG, "a") as f:
        f.write(f"{datetime.date.today()},{company},{url},{status}\n")


async def extract_form_fields(page) -> list[dict]:
    """Collect visible form controls with their labels."""
    return await page.evaluate(
        """
        () => Array.from(document.querySelectorAll(
                'input, textarea, select, [role=combobox], [aria-haspopup=listbox]'))
            .filter(el => {
                const r = el.getBoundingClientRect();
                if (el.type === 'hidden' || r.width <= 0 || r.height <= 0) return false;
                return true;
            })
            .map((el, idx) => {
                let label = '';
                if (el.labels && el.labels.length) label = el.labels[0].innerText;
                if (!label && el.getAttribute('aria-label')) label = el.getAttribute('aria-label');
                if (!label && el.placeholder) label = el.placeholder;
                if (!label && el.getAttribute('aria-labelledby')) {
                    const ref = document.getElementById(el.getAttribute('aria-labelledby'));
                    if (ref) label = ref.innerText;
                }
                if (!label) {
                    const wrap = el.closest('div,fieldset');
                    const lab = wrap && wrap.querySelector('label');
                    if (lab) label = lab.innerText;
                }
                const isCombo = el.getAttribute('role') === 'combobox'
                                || el.getAttribute('aria-haspopup') === 'listbox';
                return {
                    idx,
                    tag: isCombo && el.tagName !== 'SELECT' ? 'combobox' : el.tagName.toLowerCase(),
                    type: el.type || '',
                    name: el.name || '',
                    id: el.id || '',
                    placeholder: el.placeholder || '',
                    label: (label || '').trim().slice(0, 200),
                    required: el.required || el.getAttribute('aria-required') === 'true' || false,
                    options: el.tagName === 'SELECT'
                        ? Array.from(el.options).map(o => o.text.trim()).slice(0, 50)
                        : null,
                };
            })
        """
    )


def plan_answers(fields: list[dict], profile: dict, job_url: str) -> dict:
    """Ask the LLM to map every form field to a value from the profile.

    Returns {field_idx: {"value": str, "source": "profile"|"generated"|"skip"}}.
    """
    prompt = f"""You fill job application forms. Map each form field to an answer.

APPLICANT PROFILE (authoritative — never invent facts not present here):
{yaml.safe_dump(profile)}

JOB URL: {job_url}

FORM FIELDS (JSON):
{json.dumps(fields, indent=1)}

Rules:
- Use profile values verbatim for factual fields (name, email, phone, links).
- For select fields, the value MUST be one of the given options (exact text).
- For open-ended questions (why us, cover letter), draft 2-4 sentences in the
  applicant's voice per voice_notes. Never fabricate experience.
- For resume/CV file-upload fields, value = "UPLOAD_RESUME".
- For cover-letter file-upload fields, value = "UPLOAD_COVER_LETTER".
- For open-ended pitch fields (cover letter text, "why us", "tell us about a
  project"), cite 1-2 work_examples from the profile WITH their links — concrete
  shipped work with real numbers, in the applicant's voice. Never invent links.
- If the profile has no answer and it can't be drafted honestly, source = "skip".
- EEO questions: use profile.eeo if filled, else skip.

Return ONLY JSON: {{"<idx>": {{"value": "...", "source": "profile|generated|skip"}}}}"""
    resp = get_llm().chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
    )
    plan = json.loads(resp.choices[0].message.content)
    return apply_deterministic_answers(plan, fields, profile)


def field_blob(field: dict) -> str:
    return " ".join(str(field.get(k, "")) for k in (
        "label", "name", "id", "placeholder", "type"
    )).lower()


def apply_deterministic_answers(plan: dict, fields: list[dict], profile: dict) -> dict:
    """Hard-override factual identity/contact fields.

    LLMs are useful for open text, but identity fields must never receive a
    project pitch. This keeps names/contact info deterministic.
    """
    personal = profile.get("personal", {})
    overrides = []
    for field in fields:
        blob = field_blob(field)
        idx = str(field["idx"])
        value = None

        if "legal" in blob and "first" in blob and "name" in blob:
            value = personal.get("legal_first_name") or personal.get("first_name")
        elif "preferred" in blob and "first" in blob and "name" in blob:
            value = personal.get("first_name")
        elif "first" in blob and "name" in blob:
            value = personal.get("first_name")
        elif "last" in blob and "name" in blob:
            value = personal.get("last_name")
        elif ("full" in blob and "name" in blob) or "your name" in blob:
            value = f"{personal.get('first_name', '')} {personal.get('last_name', '')}".strip()
        elif "email" in blob:
            value = personal.get("email")
        elif "phone" in blob or "mobile" in blob:
            value = personal.get("phone")
        elif "linkedin" in blob:
            value = personal.get("linkedin")
        elif "github" in blob:
            value = personal.get("github")
        elif "website" in blob or "portfolio" in blob:
            value = personal.get("website")

        if value:
            plan[idx] = {"value": value, "source": "profile"}
            overrides.append((idx, value))
    if overrides:
        print(f"  deterministic profile overrides: {len(overrides)}")
    return plan


async def fill_field(page, field: dict, value: str, resume_path: str,
                     cover_path: str = ""):
    selector = None
    if field["id"]:
        # attribute form handles IDs that start with digits (Ashby uses UUID ids)
        selector = f"[id='{field['id']}']"
    elif field["name"]:
        selector = f"{field['tag']}[name=\"{field['name']}\"]"
    elif field.get("placeholder"):
        # workatastartup (and other React modals): the message textarea has no
        # id/name — only a placeholder. Target that.
        ph = field["placeholder"].replace('"', '\\"')
        tag = field["tag"] if field["tag"] in ("input", "textarea", "select") else "textarea"
        selector = f"{tag}[placeholder=\"{ph}\"]"
    elif field["tag"] in ("textarea", "input"):
        # last resort: the lone textarea/input on the page (use .first below)
        selector = field["tag"]
    if not selector:
        return False
    loc = page.locator(selector).first  # .first = tolerant of multi-match fallbacks
    try:
        if value == "UPLOAD_RESUME":
            await loc.set_input_files(resume_path)
        elif value == "UPLOAD_COVER_LETTER":
            if not cover_path:
                return False
            await loc.set_input_files(cover_path)
        elif field["tag"] == "combobox":
            # Greenhouse/React custom dropdown: open, type to filter, pick first match
            await loc.click(timeout=5000)
            await loc.type(value, delay=30)
            await page.wait_for_timeout(800)
            await page.keyboard.press("Enter")
        elif field["tag"] == "select":
            await loc.select_option(label=value)
        elif field["type"] in ("checkbox", "radio"):
            if value.lower() in ("yes", "true", "1"):
                await loc.check()
        else:
            await loc.fill(value)
        return True
    except Exception as e:
        print(f"  ! could not fill '{field['label']}': {e}")
        return False


async def apply_to(pw, page, url: str, profile: dict):
    print(f"\n=== {url}")
    await page.goto(url, wait_until="domcontentloaded")
    await page.wait_for_timeout(2000)

    fields = await extract_form_fields(page)
    if not fields or len(fields) < 3:
        # Posting pages usually hide the form behind an Apply button — click through.
        print("  no form yet — looking for an Apply button...")
        for sel in ["a:has-text('Apply')", "button:has-text('Apply')",
                    "a:has-text('apply now')", "button:has-text('Apply Now')",
                    "button:has-text('Submit application')", "a:has-text('Apply to')"]:
            try:
                async with page.context.expect_page(timeout=4000) as popup_info:
                    await page.locator(sel).first.click(timeout=4000)
                page = await popup_info.value  # form opened in a new tab
                break
            except Exception:
                try:  # same-tab navigation / modal case (workatastartup)
                    await page.locator(sel).first.click(timeout=2000)
                    break
                except Exception:
                    continue
        # YC/workatastartup opens the application as a modal — give it a moment and
        # wait for the message textarea to mount before extracting.
        try:
            await page.wait_for_selector("textarea", timeout=6000)
        except Exception:
            pass
        await page.wait_for_timeout(2000)
        fields = await extract_form_fields(page)
        if not fields:
            print("  still no form found — if this is a YC/workatastartup role, make"
                  " sure you're logged in first (run: python login.py). Leaving the page"
                  " open so you can navigate to the form manually; close the tab to continue")
            try:
                await page.wait_for_event("close", timeout=0)
            except Exception:
                pass
            return

    print(f"  {len(fields)} fields found; planning answers with {LLM_MODEL}...")
    plan = plan_answers(fields, profile, url)

    jd_text = await page.evaluate("() => document.body.innerText")
    resume = pick_resume(profile, f"{url}\n{jd_text}")
    print(f"  role CV: {resume}")
    if not NO_TAILOR:
        try:
            print("  tailoring resume to this JD...")
            resume = await make_tailored_resume(pw, get_llm(), LLM_MODEL, jd_text, url)
            print(f"  tailored resume: {resume}")
        except Exception as e:
            print(f"  ! tailoring failed ({type(e).__name__}: {e}) — using role CV {resume}")

    cover = ""
    if any(a.get("value") == "UPLOAD_COVER_LETTER" for a in plan.values()):
        try:
            print("  writing cover letter with work examples...")
            jd_text = await page.evaluate("() => document.body.innerText")
            cover = await make_cover_letter(pw, get_llm(), LLM_MODEL, jd_text,
                                            url, profile)
            print(f"  cover letter: {cover}")
        except Exception as e:
            print(f"  ! cover letter failed ({type(e).__name__}: {e}) — skipping it")

    filled = skipped = 0
    for f in fields:
        ans = plan.get(str(f["idx"]))
        if not ans or ans["source"] == "skip" or not ans.get("value"):
            skipped += 1
            continue
        ok = await fill_field(page, f, ans["value"], resume, cover)
        filled += ok
    print(f"  filled {filled}, skipped {skipped}")
    print("  >>> REVIEW the form in the browser window, then click Submit yourself.")
    print("  >>> When you're done, CLOSE THE BROWSER TAB to move on.")
    try:
        await page.wait_for_event("close", timeout=0)
    except Exception:
        pass
    record_applied(url)


async def main(urls: list[str]):
    profile = load_profile()
    if not FORCE:
        applied = load_applied()
        email_history = load_email_company_history()
        company_history = load_applied_company_history()
        for company, statuses in company_history.items():
            email_history.setdefault(company, set()).update(statuses)
        fresh, seen = [], set(applied)
        for u in urls:
            k = job_key(u)
            if k in seen:
                print(f"already applied, skipping: {u}")
            elif email_statuses := has_blocking_email_history(company_from_url(u), email_history):
                print(f"email history found ({email_statuses}), skipping unless --force: {u}")
            else:
                seen.add(k)  # also dedupes repeats within the queue itself
                fresh.append(u)
        urls = fresh
    if not urls:
        print("nothing new to apply to — every URL is already known from applied.csv or Gmail history")
        return
    async with async_playwright() as pw:
        # Persistent context: keeps cookies/logins between runs (Workday accounts etc.)
        browser = await pw.chromium.launch_persistent_context(
            user_data_dir=BROWSER_PROFILE_DIR,
            headless=False,
        )
        for url in urls:
            # fresh tab per job — the previous one gets closed by the user after review
            page = await browser.new_page()
            try:
                await apply_to(pw, page, url, profile)
            except Exception as e:
                print(f"  ! {url} aborted ({type(e).__name__}) — moving to next job")
            if not page.is_closed():
                try:
                    await page.close()
                except Exception:
                    pass
        await browser.close()


if __name__ == "__main__":
    args = sys.argv[1:]
    FORCE = False
    if "--force" in args:
        FORCE = True
        args.remove("--force")
    if "--no-tailor" in args:
        NO_TAILOR = True
        args.remove("--no-tailor")
    if not args:
        print(__doc__)
        sys.exit(1)
    if args[0] == "--queue":
        with open(args[1]) as f:
            urls = [l.strip() for l in f if l.strip() and not l.startswith("#")]
    else:
        urls = args
    asyncio.run(main(urls))
