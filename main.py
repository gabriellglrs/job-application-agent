"""Job application agent: fills ATS application forms in its own browser.

Usage:
    python main.py <job_url> [<job_url> ...]
    python main.py --queue jobs.txt        # one URL per line

For each URL it opens a page in a dedicated Chromium window, extracts the form,
fills what it can from profile.yaml (LLM drafts the open-ended answers), then
PAUSES so you review and click Submit yourself. It never submits on its own.
"""

import asyncio
import json
import os
import sys

import yaml
from openai import OpenAI
from playwright.async_api import async_playwright

PROFILE_PATH = os.path.join(os.path.dirname(__file__), "profile.yaml")

# Works with any OpenAI-compatible endpoint (OpenAI, Azure/Foundry /openai/v1, etc.)
llm = OpenAI(
    base_url=os.environ.get("LLM_BASE_URL") or None,
    api_key=os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY"),
)
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-5.4")


def load_profile() -> dict:
    with open(PROFILE_PATH) as f:
        return yaml.safe_load(f)


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
- For file-upload fields, value = "UPLOAD_RESUME".
- If the profile has no answer and it can't be drafted honestly, source = "skip".
- EEO questions: use profile.eeo if filled, else skip.

Return ONLY JSON: {{"<idx>": {{"value": "...", "source": "profile|generated|skip"}}}}"""
    resp = llm.chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
    )
    return json.loads(resp.choices[0].message.content)


async def fill_field(page, field: dict, value: str, resume_path: str):
    selector = None
    if field["id"]:
        # attribute form handles IDs that start with digits (Ashby uses UUID ids)
        selector = f"[id='{field['id']}']"
    elif field["name"]:
        selector = f"{field['tag']}[name=\"{field['name']}\"]"
    if not selector:
        return False
    try:
        if value == "UPLOAD_RESUME":
            await page.set_input_files(selector, resume_path)
        elif field["tag"] == "combobox":
            # Greenhouse/React custom dropdown: open, type to filter, pick first match
            loc = page.locator(selector).first
            await loc.click(timeout=5000)
            await loc.type(value, delay=30)
            await page.wait_for_timeout(800)
            await page.keyboard.press("Enter")
        elif field["tag"] == "select":
            await page.select_option(selector, label=value)
        elif field["type"] in ("checkbox", "radio"):
            if value.lower() in ("yes", "true", "1"):
                await page.check(selector)
        else:
            await page.fill(selector, value)
        return True
    except Exception as e:
        print(f"  ! could not fill '{field['label']}': {e}")
        return False


async def apply_to(page, url: str, profile: dict):
    print(f"\n=== {url}")
    await page.goto(url, wait_until="domcontentloaded")
    await page.wait_for_timeout(2000)

    fields = await extract_form_fields(page)
    if not fields or len(fields) < 3:
        # Posting pages usually hide the form behind an Apply button — click through.
        print("  no form yet — looking for an Apply button...")
        for sel in ["a:has-text('Apply')", "button:has-text('Apply')",
                    "a:has-text('apply now')", "button:has-text('Apply Now')"]:
            try:
                async with page.context.expect_page(timeout=4000) as popup_info:
                    await page.locator(sel).first.click(timeout=4000)
                page = await popup_info.value  # form opened in a new tab
                break
            except Exception:
                try:  # same-tab navigation case
                    await page.locator(sel).first.click(timeout=2000)
                    break
                except Exception:
                    continue
        await page.wait_for_timeout(4000)
        fields = await extract_form_fields(page)
        if not fields:
            print("  still no form found — leaving the page open so you can navigate"
                  " to the form manually; close the tab to continue")
            try:
                await page.wait_for_event("close", timeout=0)
            except Exception:
                pass
            return

    print(f"  {len(fields)} fields found; planning answers with {LLM_MODEL}...")
    plan = plan_answers(fields, profile, url)

    filled = skipped = 0
    resume = profile["personal"].get("resume_path", "")
    for f in fields:
        ans = plan.get(str(f["idx"]))
        if not ans or ans["source"] == "skip" or not ans.get("value"):
            skipped += 1
            continue
        ok = await fill_field(page, f, ans["value"], resume)
        filled += ok
    print(f"  filled {filled}, skipped {skipped}")
    print("  >>> REVIEW the form in the browser window, then click Submit yourself.")
    print("  >>> When you're done, CLOSE THE BROWSER TAB to move on.")
    try:
        await page.wait_for_event("close", timeout=0)
    except Exception:
        pass


async def main(urls: list[str]):
    profile = load_profile()
    async with async_playwright() as pw:
        # Persistent context: keeps cookies/logins between runs (Workday accounts etc.)
        browser = await pw.chromium.launch_persistent_context(
            user_data_dir=os.path.join(os.path.dirname(__file__), ".browser-profile"),
            headless=False,
        )
        for url in urls:
            # fresh tab per job — the previous one gets closed by the user after review
            page = await browser.new_page()
            try:
                await apply_to(page, url, profile)
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
    if not args:
        print(__doc__)
        sys.exit(1)
    if args[0] == "--queue":
        with open(args[1]) as f:
            urls = [l.strip() for l in f if l.strip() and not l.startswith("#")]
    else:
        urls = args
    asyncio.run(main(urls))
