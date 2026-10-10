"""Reed.co.uk jobs API discovery source.

Reed is one of the largest UK job boards with a free API for developers
(https://www.reed.co.uk/developers). Register for a key (instant approval),
then put it in pipeline/config.yaml:

    sources:
      reed: {api_key: "..."}

Without a key the source skips with a printed reason. Keyword searches, London
and UK-wide, two pages each; Reed's API has no date filter, so filter.py applies
the age rule. Only titles filter.py would keep cost a details call, and a job
whose details call fails is skipped -- never ingested as the ~450-char search
snippet, because FIT must see the whole posting.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from discover.filter import title_reason
from discover.sources._settings import keys_missing, source_settings
from discover.util import html_to_text

API = "https://www.reed.co.uk/api/1.0/search"
DETAILS = "https://www.reed.co.uk/api/1.0/jobs/{job_id}"
SEARCHES = [
    "machine learning engineer", "data scientist", "ai engineer",
    "junior data scientist", "junior machine learning engineer", "nlp engineer",
    "computer vision engineer", "mlops engineer", "data engineer", "applied scientist",
    "llm engineer", "generative ai engineer", "research engineer machine learning",
]
# Reed's own "graduate" flag: entry-level roles whose titles carry no "junior"/"graduate" word.
GRADUATE_SEARCHES = ["data science", "machine learning", "artificial intelligence", "data engineer",
                     "python", "software engineer"]
LOCATIONS = ["London", None]   # None = UK-wide, distance filter off
MAX_PAGES_PER_SEARCH = 2
RESULTS_PER_PAGE = 100

_ATS_HOSTS = (("greenhouse.io", "greenhouse"), ("lever.co", "lever"), ("ashbyhq.com", "ashby"),
              ("workable.com", "workable"), ("smartrecruiters.com", "smartrecruiters"),
              ("myworkdayjobs.com", "workday"), ("icims.com", "icims"), ("taleo.net", "taleo"),
              ("successfactors", "successfactors"), ("teamtailor.com", "teamtailor"),
              ("recruitee.com", "recruitee"), ("bamboohr.com", "bamboohr"), ("jobvite.com", "jobvite"))


def _employer_apply(details: dict) -> tuple[str | None, str | None]:
    """Reed's details API returns `externalUrl` -- the employer's own application page -- for jobs
    that are not applied for through Reed itself. Without it the posting can only be applied for
    inside Reed (login-gated), which this pipeline never does."""
    ext = (details.get("externalUrl") or "").strip()
    if not ext.lower().startswith("http"):
        return None, None
    host = ext.lower().split("/")[2] if ext.count("/") >= 2 else ""
    return ext, next((ats for frag, ats in _ATS_HOSTS if frag in host), "other")


def _row(job: dict, details: dict) -> dict:
    location = (job.get("locationName") or "").strip()
    description = html_to_text(details.get("jobDescription"))
    return {
        "source": "reed",
        "company": job.get("employerName") or "",
        "title": job.get("jobTitle") or "",
        "location": location,
        "remote": True if "remote" in location.lower() or "remote" in description.lower()[:500] else None,
        "url": job.get("jobUrl") or "",
        "apply_url": _employer_apply(details)[0],
        "ats": _employer_apply(details)[1],
        # Reed gives a posted date without a year; the API docs expose it as
        # part of `date`. Keep it parseable for filter.py's age check.
        "posted_at": _posted_at(job),
        "description": description,
    }


def _posted_at(job: dict) -> str | None:
    import datetime as dt
    raw = (job.get("date") or "").strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%dT%H:%M:%S", "%d/%m/%Y %H:%M:%S"):
        try:
            return dt.datetime.strptime(raw[:len(fmt) + 6].strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _details(job_id, auth) -> tuple[dict | None, bool]:
    """(details, rate_limited); details is None on any failure."""
    try:
        resp = requests.get(DETAILS.format(job_id=job_id), auth=auth, timeout=30)
        if resp.status_code == 429:
            return None, True
        return (resp.json() if resp.ok else None), False
    except (requests.RequestException, ValueError):
        return None, False


def fetch(config: dict) -> list[dict]:
    missing = keys_missing("reed", ["api_key"])
    if missing:
        print(f"    ! reed: skipping, missing config.yaml sources.reed {missing} "
              "(free key at https://www.reed.co.uk/developers)")
        return []
    auth = (source_settings("reed")["api_key"], "")
    out, seen_ids, no_description = [], set(), 0
    jobs_to_run = [(t, False) for t in SEARCHES] + [(t, True) for t in GRADUATE_SEARCHES]
    for term, graduate in jobs_to_run:
        for location in LOCATIONS:
            for page in range(1, MAX_PAGES_PER_SEARCH + 1):
                params = {"keywords": term, "resultsToTake": RESULTS_PER_PAGE,
                          "resultsToSkip": (page - 1) * RESULTS_PER_PAGE,
                          "fullDay": "false"}
                if graduate:
                    params["graduate"] = "true"
                if location:
                    params.update({"locationName": location, "distanceFromLocation": 100})
                try:
                    resp = requests.get(API, params=params, auth=auth, timeout=30)
                    if resp.status_code == 429:
                        print("    ! reed: rate limited, stopping this source")
                        return out
                    resp.raise_for_status()
                    results = (resp.json() or {}).get("results") or []
                except (requests.RequestException, ValueError) as exc:
                    print(f"    ! reed {term!r} {location!r} page {page}: {exc}")
                    break
                if not results:
                    break
                for job in results:
                    jid = job.get("jobId")
                    if not jid or jid in seen_ids:
                        continue
                    seen_ids.add(jid)
                    if title_reason(job.get("jobTitle") or ""):
                        continue  # filter.py would drop it anyway; skip the details call
                    details, limited = _details(jid, auth)
                    if limited:
                        print("    ! reed: rate limited, stopping this source")
                        return out
                    time.sleep(0.5)
                    if not (details or {}).get("jobDescription"):
                        no_description += 1  # never ingest the search snippet as the posting
                        continue
                    out.append(_row(job, details))
                time.sleep(1)  # polite pacing between pages
    if no_description:
        print(f"    ! reed: {no_description} job(s) skipped, details call returned no description")
    return out
