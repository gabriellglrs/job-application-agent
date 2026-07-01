#!/bin/bash
# Email outreach (SEPARATE from the form-filler). Drafts by default; --send to email.
# Usage:
#   ./outreach.sh                 - draft every contact in contacts.yaml
#   ./outreach.sh --send          - send them (needs OUTREACH_EMAIL/_APP_PASSWORD in .env)
#   ./outreach.sh --to a@b.com --name "Jane" --company "Acme" --role "FDE" --context "..."
cd "$(dirname "$0")"
set -a; source .env; set +a
.venv/bin/python -u outreach.py "$@"
