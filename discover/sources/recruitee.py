"""Recruitee job-board source (public JSON API: https://<slug>.recruitee.com/api/offers/). See pipeline/CONTRACTS.md §5."""
from __future__ import annotations

from ..util import polite_get, html_to_text, date_only

LIST_API = "https://{slug}.recruitee.com/api/offers/"


def fetch(config: dict) -> list[dict]:
    slug = config["slug"]
    company = config.get("name", slug)
    resp = polite_get(LIST_API.format(slug=slug))
    resp.raise_for_status()
    jobs = []
    for o in resp.json().get("offers", []):
        if o.get("status") not in (None, "published"):
            continue
        # Remote offers carry the literal UI label "Remote job" in `location`; the real geography is in city/country
        # (Tether's UK copies were all filtered as "not a UK location").
        location = ", ".join(p for p in (o.get("city"), o.get("country")) if p) or o.get("location") or ""
        url = o.get("careers_url") or f"https://{slug}.recruitee.com/o/{o.get('slug')}"
        jobs.append({
            "source": "recruitee",
            "company": company,
            "title": (o.get("title") or "").strip(),
            "location": location,
            "remote": bool(o.get("remote")) or "remote" in location.lower(),
            "url": url,
            "apply_url": o.get("careers_apply_url") or url.rstrip("/") + "/c/new",
            "ats": "recruitee",
            "posted_at": date_only(o.get("published_at") or o.get("created_at")),
            "description": html_to_text((o.get("description") or "") + "\n" + (o.get("requirements") or "")),
        })
    return jobs
