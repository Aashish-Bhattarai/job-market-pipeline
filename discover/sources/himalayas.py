"""Himalayas discovery source.

Access path: Himalayas' public, unauthenticated Remote Jobs API
(``https://himalayas.app/jobs/api`` / ``/jobs/api/search``), documented at
https://himalayas.app/docs/remote-jobs-api. No login, JS rendering, or
CAPTCHA involved; ``robots.txt`` only disallows ``/apply`` and paginated
HTML listing pages, not the JSON API.

Every job on Himalayas is remote by definition, so ``remote`` is always
``True``. The API never exposes the employer's own application link (only
Himalayas' own tracking/apply page), so ``apply_url``/``ats`` are left
``None`` -- the listing does not link directly to the employer.
"""
from __future__ import annotations

import html
import re
import time
from datetime import datetime, timezone

import requests

SEARCH_URL = "https://himalayas.app/jobs/api/search"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# Queries broad enough that a full-text match on any one of them also
# surfaces "graduate"/"junior" variants of the role (Himalayas' `q` param
# does a substring/full-text search over title+description).
DEFAULT_QUERIES = [
    "machine learning engineer",
    "ai engineer",
    "data scientist",
    "applied scientist",
    "mlops",
    "llm",
    "graduate machine learning",
    "junior data scientist",
    # adjacent roles the CV evidences + AI roles with other titles (2026-10-02)
    "software engineer",
    "graduate software engineer",
    "junior software engineer",
    "backend engineer",
    "python developer",
    "full stack engineer",
    "data engineer",
    "research engineer",
    "forward deployed engineer",
]
PAGE_SIZE = 20   # the API's documented maximum
MAX_PAGES = 3    # per query; the API is cached for 24h, so deeper walks add nothing

KEYWORDS = [
    "machine learning engineer",
    "ml engineer",
    "ai engineer",
    "artificial intelligence engineer",
    "data scientist",
    "applied scientist",
    "mlops",
    "ml ops",
    "llm",
    "large language model",
    "graduate data scientist",
    "junior data scientist",
    "graduate machine learning",
    "junior machine learning",
    "graduate ml engineer",
    "junior ml engineer",
    "software engineer",
    "software developer",
    "backend",
    "full stack",
    "full-stack",
    "python",
    "data engineer",
    "research engineer",
    "research scientist",
    "forward deployed",
    "applied ai",
]

TAG_RE = re.compile(r"<[^>]+>")


def _plain_text(description: str) -> str:
    text = TAG_RE.sub(" ", description or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _location(job: dict) -> str:
    restrictions = job.get("locationRestrictions") or []
    names = []
    for item in restrictions:
        if isinstance(item, dict):
            names.append(item.get("name") or item.get("slug") or "")
        else:
            names.append(str(item))
    names = [n for n in names if n]
    if not names:
        return "Worldwide (Remote)"
    return ", ".join(names)


def _posted_at(job: dict) -> str | None:
    pub_date = job.get("pubDate")
    if not pub_date:
        return None
    try:
        ts = float(pub_date)
        if ts > 1e12:  # milliseconds
            ts /= 1000
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return None


def _matches_keywords(job: dict) -> bool:
    haystack = " ".join(
        [
            job.get("title") or "",
            " ".join(job.get("categories") or []),
            " ".join(job.get("parentCategories") or []),
        ]
    ).lower()
    return any(kw in haystack for kw in KEYWORDS)


def _normalize(job: dict) -> dict:
    return {
        "source": "himalayas",
        "company": job.get("companyName") or "",
        "title": job.get("title") or "",
        "location": _location(job),
        "remote": True,
        "url": job.get("applicationLink") or job.get("guid") or "",
        "apply_url": None,
        "ats": None,
        "posted_at": _posted_at(job),
        "description": _plain_text(job.get("description") or job.get("excerpt") or ""),
    }


def fetch(config: dict) -> list[dict]:
    config = config or {}
    queries = config.get("queries") or DEFAULT_QUERIES
    country = config.get("country", "GB")
    delay = config.get("request_delay_seconds", 1.1)
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    seen: dict[str, dict] = {}
    requests_made = 0
    for query in queries:
        for page in range(1, MAX_PAGES + 1):
            if requests_made:
                time.sleep(delay)
            params = {"q": query, "page": page}
            if country:
                params["country"] = country
            resp = session.get(SEARCH_URL, params=params, timeout=20)
            requests_made += 1
            if resp.status_code == 429:  # documented rate limit: stop, keep what we have
                return list(seen.values())
            resp.raise_for_status()
            jobs = resp.json().get("jobs", [])
            for job in jobs:
                if not _matches_keywords(job):
                    continue
                guid = job.get("guid") or job.get("applicationLink")
                if not guid or guid in seen:
                    continue
                seen[guid] = _normalize(job)
            if len(jobs) < PAGE_SIZE:
                break

    return list(seen.values())
