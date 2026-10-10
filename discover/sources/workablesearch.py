"""Workable job search (jobs.workable.com) discovery source -- keyless; the public JSON API the site itself uses.

Covers every Workable-hosted employer, not just the boards listed in companies.yaml. robots.txt (read 2026-10-03)
disallows only /search*?*, /profile* and /company-login-unauthorized; /api/v1/jobs is not covered, and no login, token
or challenge is involved. The API names employer and role but not the apply.workable.com shortcode, so rows go in
without an apply_url and run/resolve_apply_url.py maps employer + title to the real application page, exactly as it
does for every other aggregator.
"""
from __future__ import annotations

import requests

from ..util import HostCoolingDown, date_only, html_to_text, polite_get

API = "https://jobs.workable.com/api/v1/jobs"
LOCATION = "United Kingdom"
DAY_RANGE = 30        # the API honours it; filter.py drops anything older than 30 days anyway
MAX_PAGES = 6         # 20 jobs a page
QUERIES = [
    "machine learning", "data scientist", "AI engineer", "applied scientist", "research scientist",
    "research engineer", "NLP", "LLM", "computer vision", "deep learning", "MLOps", "generative AI",
    "data engineer", "python developer", "software engineer", "backend engineer", "full stack engineer",
    "graduate software engineer", "junior developer",
]


def _row(j: dict) -> dict:
    loc = j.get("location") or {}
    return {
        "source": "workablesearch",
        "company": ((j.get("company") or {}).get("title") or j.get("department") or "").strip(),
        "title": (j.get("title") or "").strip(),
        "location": ", ".join(p for p in (loc.get("city"), loc.get("countryName")) if p),
        "remote": j.get("workplace") == "remote",
        "url": j.get("url"),
        "apply_url": None,
        "ats": None,
        "posted_at": date_only(j.get("created")),
        "description": html_to_text((j.get("description") or "") + "\n" + (j.get("requirementsSection") or "")),
    }


def fetch(config: dict) -> list[dict]:
    rows, seen = [], set()
    for query in QUERIES:
        token = None
        for _ in range(MAX_PAGES):
            params = {"query": query, "location": LOCATION, "day_range": DAY_RANGE}
            if token:
                params["pageToken"] = token
            try:
                resp = polite_get(API, params=params)
                resp.raise_for_status()
                data = resp.json()
            except HostCoolingDown as exc:
                print(f"    ! workablesearch: {exc}")
                return rows
            except (requests.RequestException, ValueError) as exc:
                print(f"    ! workablesearch '{query}': {exc}")
                break
            for j in data.get("jobs") or []:
                if j.get("state") not in (None, "published") or j.get("id") in seen:
                    continue
                seen.add(j.get("id"))
                rows.append(_row(j))
            token = data.get("nextPageToken")
            if not token:
                break
    return rows
