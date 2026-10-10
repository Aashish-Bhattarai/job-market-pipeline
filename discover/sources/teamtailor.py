"""Teamtailor job-board source (public RSS: https://<slug>.teamtailor.com/jobs.rss). See pipeline/CONTRACTS.md §5."""
from __future__ import annotations

import email.utils
import html
import re

from ..util import polite_get, html_to_text

FEED = "https://{slug}.teamtailor.com/jobs.rss"


def _tag(item: str, name: str) -> str:
    m = re.search(rf"<{name}>(.*?)</{name}>", item, re.S)
    if not m:
        return ""
    return html.unescape(re.sub(r"^\s*<!\[CDATA\[|\]\]>\s*$", "", m.group(1)).strip())


def _rfc822_date(value: str):
    try:
        return email.utils.parsedate_to_datetime(value).date().isoformat()
    except (TypeError, ValueError):
        return None


def fetch(config: dict) -> list[dict]:
    slug = config["slug"]
    company = config.get("name", slug)
    resp = polite_get(FEED.format(slug=slug))
    resp.raise_for_status()
    jobs = []
    for item in re.findall(r"<item>(.*?)</item>", resp.text, re.S):
        link = _tag(item, "link")
        city, country = _tag(item, "tt:city"), _tag(item, "tt:country")
        location = ", ".join(p for p in (city, country) if p)
        remote_status = _tag(item, "remoteStatus").lower()
        jobs.append({
            "source": "teamtailor",
            "company": company,
            "title": _tag(item, "title"),
            "location": location,
            "remote": remote_status in ("remote", "fully", "hybrid") or "remote" in location.lower(),
            "url": link,
            "apply_url": link.rstrip("/") + "/applications/new",
            "ats": "teamtailor",
            "posted_at": _rfc822_date(_tag(item, "pubDate")),
            "description": html_to_text(_tag(item, "description")),
        })
    return jobs
