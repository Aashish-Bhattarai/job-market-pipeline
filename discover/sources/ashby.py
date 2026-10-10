"""Ashby job-board source. See pipeline/CONTRACTS.md §5."""
from __future__ import annotations

from ..util import polite_get, html_to_text, date_only

API = "https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"


def fetch(config: dict) -> list[dict]:
    slug = config["slug"]
    company = config.get("name", slug)
    resp = polite_get(API.format(slug=slug))
    resp.raise_for_status()
    data = resp.json()
    jobs = []
    for item in data.get("jobs", []):
        location = item.get("location", "") or ""
        url = item.get("jobUrl") or item.get("applyUrl", "")
        jobs.append({
            "source": "ashby",
            "company": company,
            "title": (item.get("title") or "").strip(),
            "location": location.strip(),
            "remote": bool(item.get("isRemote")) or "remote" in location.lower(),
            "url": url,
            "apply_url": item.get("applyUrl") or url,
            "ats": "ashby",
            "posted_at": date_only(item.get("publishedAt")),
            "description": html_to_text(item.get("descriptionHtml") or item.get("descriptionPlain")),
        })
    return jobs
