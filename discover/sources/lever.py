"""Lever job-board source. See pipeline/CONTRACTS.md §5."""
from __future__ import annotations

from ..util import polite_get, html_to_text, date_only

API = "https://api.lever.co/v0/postings/{slug}?mode=json"


def _location(posting: dict) -> str:
    cats = posting.get("categories", {}) or {}
    loc = cats.get("location") or ""
    if not loc:
        loc = ", ".join(cats.get("allLocations", []) or [])
    return loc.strip()


def fetch(config: dict) -> list[dict]:
    slug = config["slug"]
    company = config.get("name", slug)
    resp = polite_get(API.format(slug=slug))
    resp.raise_for_status()
    postings = resp.json()
    jobs = []
    for item in postings:
        location = _location(item)
        desc_html = "\n".join(filter(None, [
            item.get("descriptionHtml"),
            "".join(
                f"<h4>{lst.get('text','')}</h4>{lst.get('content','')}"
                for lst in (item.get("lists") or [])
            ),
        ]))
        url = item.get("hostedUrl") or item.get("applyUrl", "")
        posted_ms = item.get("createdAt")
        posted_at = None
        if posted_ms:
            import datetime
            posted_at = datetime.datetime.utcfromtimestamp(posted_ms / 1000).strftime("%Y-%m-%d")
        jobs.append({
            "source": "lever",
            "company": company,
            "title": item.get("text", "").strip(),
            "location": location,
            "remote": (item.get("workplaceType") == "remote") or ("remote" in location.lower()),
            "url": url,
            "apply_url": item.get("applyUrl") or url,
            "ats": "lever",
            "posted_at": date_only(posted_at),
            "description": html_to_text(desc_html),
        })
    return jobs
