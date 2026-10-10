"""SmartRecruiters job-board source. See pipeline/CONTRACTS.md §5."""
from __future__ import annotations

from ..util import polite_get, html_to_text, date_only

LIST_API = "https://api.smartrecruiters.com/v1/companies/{slug}/postings"
DETAIL_API = "https://api.smartrecruiters.com/v1/companies/{slug}/postings/{pid}"

# Cost control: SmartRecruiters boards can list thousands of postings for a
# single (often non-tech) company. Only spend a detail request (needed for
# the full description) on titles that plausibly belong to the AI/ML/DS
# family filter.py cares about; filter.py still re-validates fully and is
# the source of truth for keep/drop.
_RELEVANT_HINTS = (
    "data", "machine learning", " ml ", "ml ", " ml", "artificial intelligence",
    " ai ", "ai ", " ai", "scientist", "nlp", "computer vision", "deep learning",
    "genai", "generative", "llm", "mlops", "analytics", "analyst",
)


def _looks_relevant(title: str) -> bool:
    t = f" {title.lower()} "
    return any(hint in t for hint in _RELEVANT_HINTS)


def _location(loc: dict) -> str:
    if not loc:
        return ""
    parts = [loc.get("city"), loc.get("region"), loc.get("country")]
    return ", ".join(p for p in parts if p)


def _description(pid: str, slug: str) -> str:
    resp = polite_get(DETAIL_API.format(slug=slug, pid=pid))
    if resp.status_code != 200:
        return ""
    sections = (resp.json().get("jobAd") or {}).get("sections") or {}
    texts = [sec.get("text", "") for sec in sections.values() if isinstance(sec, dict)]
    return html_to_text("\n".join(texts))


def fetch(config: dict) -> list[dict]:
    slug = config["slug"]
    company = config.get("name", slug)
    resp = polite_get(LIST_API.format(slug=slug), params={"limit": 100})
    resp.raise_for_status()
    data = resp.json()
    jobs = []
    for item in data.get("content", []):
        loc = item.get("location") or {}
        location = _location(loc)
        pid = item.get("id", "")
        title = (item.get("name") or "").strip()
        url = f"https://jobs.smartrecruiters.com/{slug}/{pid}"
        jobs.append({
            "source": "smartrecruiters",
            "company": company,
            "title": title,
            "location": location,
            "remote": bool(loc.get("remote")) or "remote" in location.lower(),
            "url": url,
            "apply_url": url,
            "ats": "smartrecruiters",
            "posted_at": date_only(item.get("releasedDate")),
            "description": _description(pid, slug) if pid and _looks_relevant(title) else "",
        })
    return jobs
