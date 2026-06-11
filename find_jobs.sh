#!/bin/bash
# Usage: ./find_jobs.sh <greenhouse_company_slug>   e.g. ./find_jobs.sh cresta
curl -s "https://boards-api.greenhouse.io/v1/boards/$1/jobs" | python3 -c "
import json,sys
for j in json.load(sys.stdin)['jobs']:
    print(j['title'],'|',j.get('location',{}).get('name',''),'|',j['absolute_url'])"
