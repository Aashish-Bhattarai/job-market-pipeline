"""Hacker News "Ask HN: Who is hiring?" discovery source (keyless).

The monthly Who-is-hiring thread is fetched through Algolia's free, public
HN API — no credentials, no scraping, fully ToS-clean:

  1. find the most recent thread whose title is "Ask HN: Who is hiring?"
  2. fetch its full comment tree via /api/v1/items/{id}
  3. keep only top-level comments (each is one job), strip HTML
  4. keep only roles plausibly UK: the location segment or body mentions
     London / UK / United Kingdom / England / remote (UK/Europe), and US-only
     sponsorship markers ("must be in the US", "US citizens") are rejected

Yield is modest but quality is high: this is where well-funded AI startups
post first. filter.py re-applies the full UK/title/age rules afterwards.
"""
from __future__ import annotations

import datetime as dt
import re
from html import unescape

import requests

ALGOLIA = "https://hn.algolia.com/api/v1"
THREAD_TITLE = "ask hn: who is hiring?"
_UK_MARKERS = re.compile(r"\b(london|uk|united kingdom|england|scotland|wales|"
                         r"europe/london|remote \(uk|remotely \(uk|gb)\b", re.I)
_US_ONLY = re.compile(r"\b(must (be|reside) in the us|us citizens? only|"
                      r"authorized to work in the us)\b", re.I)
_TAG = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    return re.sub(r"\s+", " ", unescape(_TAG.sub(" ", text or ""))).strip()


def _latest_thread(recent_days: int = 45) -> dict | None:
    since = int((dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=recent_days)).timestamp())
    resp = requests.get(f"{ALGOLIA}/search_by_date",
                        params={"query": THREAD_TITLE, "tags": "story",
                                "numericFilters": f"created_at_i>{since}"},
                        timeout=30)
    resp.raise_for_status()
    for hit in resp.json().get("hits", []):
        title = (hit.get("title") or "").lower()
        if THREAD_TITLE in title:
            return hit
    return None


def _parse_comment(comment: dict) -> dict | None:
    text = _strip_html(comment.get("text") or "")
    if len(text) < 40:
        return None
    if _US_ONLY.search(text):
        return None
    head, _, rest = text.partition("|")
    if not rest:                       # no pipe layout: not a structured post
        return None
    parts = [p.strip() for p in text.split("|")]
    company = parts[0] if parts else ""
    title = parts[1] if len(parts) > 1 else ""
    if not company or not title:
        return None
    if not _UK_MARKERS.search(text):
        return None
    return {
        "source": "hnhiring",
        "company": company,
        "title": title,
        "location": parts[2] if len(parts) > 2 else "",
        "remote": True if "remote" in text.lower() else None,
        "url": f"https://news.ycombinator.com/item?id={comment.get('id')}",
        "apply_url": None,
        "ats": None,
        "posted_at": dt.datetime.utcfromtimestamp(
            comment.get("created_at_i", 0)).date().isoformat(),
        "description": text,
    }


def fetch(config: dict) -> list[dict]:
    try:
        thread = _latest_thread()
        if not thread:
            print("    ! hnhiring: no Who-is-hiring thread found in the last 45 days")
            return []
        tree = requests.get(f"{ALGOLIA}/items/{thread['objectID']}", timeout=60).json()
    except (requests.RequestException, ValueError) as exc:
        print(f"    ! hnhiring: {exc}")
        return []
    out = []
    for comment in tree.get("children") or []:
        row = _parse_comment(comment)
        if row:
            out.append(row)
    return out
