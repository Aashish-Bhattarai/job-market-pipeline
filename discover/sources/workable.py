"""Workable job-board source. See pipeline/CONTRACTS.md §5."""
from __future__ import annotations

from ..util import polite_get, html_to_text, date_only

API = "https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true"


def fetch(config: dict) -> list[dict]:
    slug = config["slug"]
    company = config.get("name", slug)
    resp = polite_get(API.format(slug=slug))
    resp.raise_for_status()
    data = resp.json()
    jobs = []
    for item in data.get("jobs", []):
        loc = item.get("locations") or [{}]
        loc0 = loc[0] if loc else {}
        city = item.get("city") or loc0.get("city") or ""
        country = item.get("country") or loc0.get("country") or ""
        location = ", ".join(p for p in [city, country] if p)
        url = item.get("url") or item.get("shortlink", "")
        jobs.append({
            "source": "workable",
            "company": company,
            "title": (item.get("title") or "").strip(),
            "location": location,
            "remote": bool(item.get("telecommuting")) or "remote" in location.lower(),
            "url": url,
            "apply_url": item.get("application_url") or url,
            "ats": "workable",
            "posted_at": date_only(item.get("published_on") or item.get("created_at")),
            "description": html_to_text(item.get("description")),
        })
    return jobs
