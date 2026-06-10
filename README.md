# Job Application Agent

Fills ATS job application forms (Greenhouse, Lever, Ashby, ...) in its **own browser
window** so your main browser stays free — unlike extension-based autofill that
holds your tab hostage while it works.

## How it works
1. `profile.yaml` holds your facts + voice notes (single source of truth)
2. Playwright opens the job URL in a dedicated Chromium with a persistent profile
   (logins/cookies survive between runs)
3. The form is extracted from the DOM; an LLM maps every field to an answer —
   profile facts verbatim, open-ended questions drafted in your voice, unknowns skipped
4. The agent fills the form, then **pauses — you review and click Submit yourself.**
   It never submits on its own.

## Setup
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp profile.yaml profile.yaml   # fill in your details
export LLM_API_KEY=...         # and optionally LLM_BASE_URL / LLM_MODEL
```

## Run
```bash
python main.py https://boards.greenhouse.io/acme/jobs/12345
python main.py --queue jobs.txt    # one URL per line
```

## Notes
- Quality over volume: review every application before submit
- Don't point this at LinkedIn Easy Apply (ToS) or CAPTCHA-walled flows
- Workday: log in once in the agent's browser window; the persistent profile remembers
