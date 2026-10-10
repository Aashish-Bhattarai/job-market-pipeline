"""Gradcracker discovery source.

Access path: server-rendered HTML on Gradcracker's public category listing
pages (e.g. ``/search/computing-technology/ai-machine-learning-jobs``).
No login/JS rendering is required to read the listings themselves --
``requests.get`` returns the fully rendered job cards (Laravel/Livewire
server-side render). ``robots.txt`` (``https://www.gradcracker.com/robots.txt``)
allows ``/search/*`` except the ``?order=`` sort parameter, which this
module never uses.

The "Apply"/title link on each card (``/hub/<employer-id>/<slug>/...``) is
Gradcracker's own outbound-click redirect, not the employer's page, and is
itself behind a Cloudflare interactive challenge -- so it is not followed
for extra detail; ``apply_url``/``ats`` are left ``None`` since the listing
does not link directly to the employer.
"""
from __future__ import annotations

import html
import re
import time

import requests

BASE_URL = "https://www.gradcracker.com"
# Verified 2026-10-02: the slug after /search/computing-technology/ is ignored -- every slug returns
# the same paginated listing -- so fetching several slugs only re-downloads identical pages. One
# path, more pages; the keyword filter below does the real narrowing.
CATEGORY_PATHS = [
    "/search/computing-technology/ai-machine-learning-jobs",
]
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

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
    "data science",
    "machine learning",
    "artificial intelligence",
    # adjacent roles the CV evidences (filter.py's title family decides what survives)
    "software engineer",
    "software developer",
    "application developer",
    "python",
    "full stack",
    "full-stack",
    "backend",
    "front end",
    "frontend",
    "data engineer",
    "graduate developer",
]

CARD_RE = re.compile(
    r'href="(?P<url>https://www\.gradcracker\.com/hub/\d+/[^"]+)"'
    r'[^>]*aria-label="Apply for the (?P<title>[^"]+?) opportunity with (?P<company>[^"]+?)"',
    re.S,
)
SUBTITLE_RE = re.compile(r'<h3[^>]*>\s*(?P<subtitle>[^<]*?)\s*</h3>', re.S)
LOCATION_RE = re.compile(r"<dt>Location</dt>\s*<dd[^>]*>([^<]*)</dd>")
DEADLINE_RE = re.compile(r"<dt>Deadline</dt>\s*<dd[^>]*>([^<]*)</dd>")
TAIL_WINDOW = 3500


def _clean(text: str) -> str:
    return html.unescape(re.sub(r"\s+", " ", text or "")).strip()


def _matches_keywords(*parts: str) -> bool:
    haystack = " ".join(parts).lower()
    return any(kw in haystack for kw in KEYWORDS)


def _parse_page(html_text: str) -> list[dict]:
    jobs = []
    for match in CARD_RE.finditer(html_text):
        title = _clean(match.group("title"))
        company = _clean(match.group("company"))
        url = match.group("url")
        tail = html_text[match.end() : match.end() + TAIL_WINDOW]
        subtitle_match = SUBTITLE_RE.search(tail)
        subtitle = _clean(subtitle_match.group("subtitle")) if subtitle_match else ""
        if not _matches_keywords(title, subtitle):
            continue
        loc_match = LOCATION_RE.search(tail)
        location = _clean(loc_match.group(1)) if loc_match else ""
        deadline_match = DEADLINE_RE.search(tail)
        deadline = _clean(deadline_match.group(1)) if deadline_match else ""
        remote = "remote" in location.lower() if location else None
        description_parts = [p for p in [subtitle, f"Deadline: {deadline}" if deadline else ""] if p]
        jobs.append(
            {
                "source": "gradcracker",
                "company": company,
                "title": title,
                "location": location,
                "remote": remote,
                "url": url,
                "apply_url": None,
                "ats": None,
                "posted_at": None,
                "description": " | ".join(description_parts),
            }
        )
    return jobs


def fetch(config: dict) -> list[dict]:
    config = config or {}
    categories = config.get("categories") or CATEGORY_PATHS
    max_pages = config.get("max_pages", 6)
    delay = config.get("request_delay_seconds", 1.2)

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    seen: dict[str, dict] = {}
    first_request = True
    for path in categories:
        for page in range(1, max_pages + 1):
            if not first_request:
                time.sleep(delay)
            first_request = False
            url = f"{BASE_URL}{path}"
            params = {"page": page} if page > 1 else {}
            resp = session.get(url, params=params, timeout=20)
            if resp.status_code != 200:
                if page == 1 and not seen:
                    # Reported, never bypassed: a Cloudflare challenge (HTTP 403, cf-mitigated) is the
                    # site asking us to stop; discovery simply tries again next round. (2026-10-02:
                    # tripped by this session's own probing of category slugs -- ~16 quick requests.)
                    challenge = " (Cloudflare challenge, not bypassed)" if resp.headers.get("cf-mitigated") else ""
                    raise RuntimeError(f"gradcracker returned HTTP {resp.status_code}{challenge}; skipped this round")
                break
            page_jobs = _parse_page(resp.text)
            new_count = 0
            for job in page_jobs:
                if job["url"] not in seen:
                    seen[job["url"]] = job
                    new_count += 1
            if not page_jobs or f"page={page + 1}" not in resp.text:
                break

    return list(seen.values())
