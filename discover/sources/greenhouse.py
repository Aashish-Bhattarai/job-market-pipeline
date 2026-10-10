"""Greenhouse job-board source. See pipeline/CONTRACTS.md §5."""
from __future__ import annotations

from ..util import polite_get, html_to_text, date_only

API = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"


def fetch(config: dict) -> list[dict]:
    slug = config["slug"]
    company = config.get("name", slug)
    resp = polite_get(API.format(slug=slug))
    resp.raise_for_status()
    data = resp.json()
    jobs = []
    for item in data.get("jobs", []):
        location = (item.get("location") or {}).get("name", "") or ""
        url = item.get("absolute_url", "")
        jobs.append({
            "source": "greenhouse",
            "company": company,
            "title": item.get("title", "").strip(),
            "location": location.strip(),
            "remote": "remote" in location.lower() if location else None,
            "url": url,
            "apply_url": url,
            "ats": "greenhouse",
            "posted_at": date_only(item.get("updated_at") or item.get("first_published")),
            "description": html_to_text(item.get("content")),
        })
    return jobs
