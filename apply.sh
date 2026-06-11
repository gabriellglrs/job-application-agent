#!/bin/bash
# Usage:
#   ./apply.sh <job_url>          - apply to one job
#   ./apply.sh                    - run the queue in jobs.txt (one URL per line)
cd "$(dirname "$0")"
set -a; source .env; set +a
if [ -n "$1" ]; then
  .venv/bin/python -u main.py "$1"
else
  .venv/bin/python -u main.py --queue jobs.txt
fi
