"""jobs.ac.uk discovery source.

Access path: jobs.ac.uk's public search results page
(``https://www.jobs.ac.uk/search/?keywords=...``) is server-rendered HTML
returned to a plain GET request -- no login/JS rendering/CAPTCHA involved.
jobs.ac.uk discontinued its general RSS feeds years ago (only individual
institutions run their own RSS, which is out of scope here), so the search
HTML is the most stable public access path. ``robots.txt``
(``https://www.jobs.ac.uk/robots.txt``) only disallows ``/job/feedback/``
and ``/enhanced/fp/``; ``/search/`` is unrestricted.

The search result card only links to the jobs.ac.uk posting page, never
directly to the employer, so ``apply_url``/``ats`` are left ``None``.
"""
from __future__ import annotations

import html
import re
import time

import requests

SEARCH_URL = "https://www.jobs.ac.uk/search/"
BASE_URL = "https://www.jobs.ac.uk"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
PAGE_SIZE = 25

DEFAULT_KEYWORD_QUERIES = [
    "machine learning engineer",
    "ai engineer",
    "data scientist",
    "applied scientist",
    "mlops",
    "large language model",
    "graduate data scientist",
    "junior data scientist",
]

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
]

CARD_RE = re.compile(
    r'data-advert-id="(?P<id>\d+)">\s*'
    r'<div class="j-search-result__text">\s*'
    r'<a href="(?P<href>[^"]+)">\s*(?P<title>[^<]+?)\s*</a>.*?'
    r'<div class="j-search-result__employer">\s*<b>(?P<employer>[^<]*)</b>.*?'
    r'<div>Location:\s*(?P<location>[^<]*?)\s*</div>',
    re.S,
)
DATE_PLACED_RE = re.compile(r"Date Placed:\s*</strong>\s*([^<]+)</div>")

MONTHS = {
    "Jan": "01", "Feb": "02", "Mar": "03", "Apr": "04", "May": "05", "Jun": "06",
    "Jul": "07", "Aug": "08", "Sep": "09", "Oct": "10", "Nov": "11", "Dec": "12",
}


def _clean(text: str) -> str:
    return html.unescape(re.sub(r"\s+", " ", text or "")).strip()


def _matches_keywords(title: str) -> bool:
    lowered = title.lower()
    return any(kw in lowered for kw in KEYWORDS)


def _parse_date_placed(tail: str) -> str | None:
    match = DATE_PLACED_RE.search(tail)
    if not match:
        return None
    raw = _clean(match.group(1))  # e.g. "18 Sep"
    parts = raw.split()
    if len(parts) != 2 or parts[1] not in MONTHS:
        return None
    day, mon = parts
    year = time.strftime("%Y")
    return f"{year}-{MONTHS[mon]}-{int(day):02d}"


def _parse_page(html_text: str) -> list[dict]:
    jobs = []
    for match in CARD_RE.finditer(html_text):
        title = _clean(match.group("title"))
        if not _matches_keywords(title):
            continue
        employer = _clean(match.group("employer"))
        location = _clean(match.group("location"))
        href = match.group("href")
        url = href if href.startswith("http") else f"{BASE_URL}{href}"
        tail = html_text[match.end() : match.end() + 600]
        posted_at = _parse_date_placed(tail)
        remote = "remote" in location.lower() if location else None
        jobs.append(
            {
                "source": "jobsacuk",
                "company": employer,
                "title": title,
                "location": location,
                "remote": remote,
                "url": url,
                "apply_url": None,
                "ats": None,
                "posted_at": posted_at,
                "description": "",
            }
        )
    return jobs


def fetch(config: dict) -> list[dict]:
    config = config or {}
    queries = config.get("queries") or DEFAULT_KEYWORD_QUERIES
    max_pages = config.get("max_pages", 4)
    delay = config.get("request_delay_seconds", 1.1)

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    seen: dict[str, dict] = {}
    first_request = True
    for keywords in queries:
        for page in range(1, max_pages + 1):
            if not first_request:
                time.sleep(delay)
            first_request = False
            start_index = (page - 1) * PAGE_SIZE + 1
            params = {
                "keywords": keywords,
                "sortOrder": 0,
                "pageSize": PAGE_SIZE,
                "startIndex": start_index,
            }
            resp = session.get(SEARCH_URL, params=params, timeout=20)
            if resp.status_code != 200:
                break
            page_jobs = _parse_page(resp.text)
            for job in page_jobs:
                if job["url"] not in seen:
                    seen[job["url"]] = job
            if len(page_jobs) < PAGE_SIZE:
                break

    return list(seen.values())
