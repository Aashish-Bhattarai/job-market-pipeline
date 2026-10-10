"""Resolve apply_url for jobs discovered via aggregators (Himalayas, Gradcracker,
jobs.ac.uk) that have no employer application URL. Owner: run pipeline.

Usage:
    python pipeline/run/resolve_apply_url.py --job-id ID [--job-id ID ...]
    python pipeline/run/resolve_apply_url.py --all-missing
    [--db path]

For each targeted job with jobs.apply_url IS NULL: builds candidate slugs
from the company name, checks pipeline/discover/companies.yaml for a known
board first, then probes the Greenhouse / Lever / Ashby / Workable public
job-board APIs directly (a 404 means "not that slug", not an error). Each
board's postings are matched against the job's title by normalized token
overlap (overlap coefficient >= 0.8) and, when the posting carries a
location, a UK/London/remote check.

On a unique confident match: sets jobs.apply_url / jobs.ats in jobs.db and
replaces jobs.description with the employer posting's full text when that is
longer (aggregator rows carry only a stub; a job that had already passed FIT
goes back to `new` so FIT re-runs on the real text), and adds apply_url / ats /
resolved_from to pipeline/out/<job_id>/job.json (if that folder exists).

On no match or an ambiguous (multiple >=0.8) match: leaves apply_url null,
sets jobs.reason = "apply_url unresolved: <why>" and jobs.status =
'queued_manual'.

Also useful straight after discover.py: a himalayas/gradcracker/jobsacuk row
with apply_url still null can be resolved before it ever reaches filter/FIT,
so the candidate the FIT reviewer sees already has a real employer link.
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
from urllib.parse import urlsplit
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from discover import db as dbmod
from ledger import core as ledger_core
from discover.util import polite_get, html_to_text

PIPELINE_ROOT = Path(__file__).resolve().parent.parent
OUT_ROOT = PIPELINE_ROOT / "out"
COMPANIES_YAML = PIPELINE_ROOT / "discover" / "companies.yaml"

GREENHOUSE_API = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
LEVER_API = "https://api.lever.co/v0/postings/{slug}?mode=json"
ASHBY_API = "https://api.ashbyhq.com/posting-api/job-board/{slug}"
WORKABLE_API = "https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true"

STOPWORDS = {
    "a", "an", "the", "and", "or", "of", "for", "in", "at", "to", "with",
    "&", "-", "–", "on", "is", "as",
}

_COMPANY_SUFFIXES = re.compile(
    r"\b(ltd|limited|plc|inc|incorporated|corporation|corp|co|company|"
    r"technologies|technology|group|holdings|llc)\b\.?", re.I,
)


def normalize_company(name: str) -> str:
    s = name.lower()
    s = s.replace(".com", "")
    s = _COMPANY_SUFFIXES.sub(" ", s)
    s = re.sub(r"[^a-z0-9\s-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def slug_candidates(name: str) -> list[str]:
    base = normalize_company(name)
    words = base.split()
    candidates: list[str] = []
    seen = set()

    def add(c: str) -> None:
        c = c.strip("-_")
        if c and c not in seen:
            seen.add(c)
            candidates.append(c)

    if base:
        add(base.replace(" ", ""))
        add(base.replace(" ", "-"))
        add(base.replace(" ", "_"))
    if words:
        add(words[0])
    add(re.sub(r"[^a-z0-9]", "", name.lower()))
    return candidates


def tokenize_title(t: str) -> set[str]:
    t = (t or "").lower()
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    return {w for w in t.split() if w not in STOPWORDS and len(w) > 1}


def title_overlap(a: str, b: str) -> float:
    ta, tb = tokenize_title(a), tokenize_title(b)
    if not ta or not tb:
        return 0.0
    inter = ta & tb
    return len(inter) / min(len(ta), len(tb))


UK_MARKERS = ("uk", "united kingdom", "london", "england", "britain")
NON_UK_MARKERS = (
    "netherlands", "europe", "emea", "apac", "usa", "united states",
    "us east", "us west", "canada", "india", "ireland", "germany", "france",
    "spain", "poland", "australia", "singapore", "berlin", "frankfurt",
)


def location_ok(posting_location: str | None, job_location: str | None) -> bool:
    if not posting_location:
        return True  # no location info on the posting -> don't disqualify
    loc = posting_location.lower()
    if any(m in loc for m in UK_MARKERS):
        return True
    # A region-wide posting ("Remote, EMEA/LATAM", "Europe") includes the UK; when the job we hold already lists
    # the UK (Himalayas lists the countries a remote role is open to), it is the same role, not another country's.
    if any(m in (job_location or "").lower() for m in UK_MARKERS) and any(m in loc for m in ("emea", "europe")):
        return True
    if any(m in loc for m in NON_UK_MARKERS):
        return False
    return "remote" in loc  # bare "Remote" with no country hint either way


def desc_overlap(a: str, b: str) -> float:
    """Rough token-overlap similarity between two descriptions (0..1)."""
    ta = {w for w in re.sub(r"[^a-z0-9\s]", " ", (a or "").lower()).split() if len(w) > 3}
    tb = {w for w in re.sub(r"[^a-z0-9\s]", " ", (b or "").lower()).split() if len(w) > 3}
    if not ta or not tb:
        return 1.0  # nothing to compare against -> don't flag
    inter = ta & tb
    return len(inter) / min(len(ta), len(tb))


def load_companies() -> list[dict]:
    if not COMPANIES_YAML.exists():
        return []
    with open(COMPANIES_YAML, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or []


def known_board_for(company: str, companies: list[dict]) -> dict | None:
    cname = company.lower()
    for c in companies:
        name = (c.get("name") or "").lower()
        if name and (name == cname or name in cname or cname in name):
            return c
    return None


def fetch_greenhouse(slug: str):
    resp = polite_get(GREENHOUSE_API.format(slug=slug))
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    data = resp.json()
    out = []
    for item in data.get("jobs", []):
        location = (item.get("location") or {}).get("name", "") or ""
        out.append({
            "title": item.get("title", "").strip(),
            "location": location.strip(),
            "apply_url": item.get("absolute_url", ""),
            "description": html_to_text(item.get("content")),
        })
    return out


def fetch_lever(slug: str):
    resp = polite_get(LEVER_API.format(slug=slug))
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        return None
    out = []
    for item in data:
        cats = item.get("categories", {}) or {}
        location = cats.get("location") or ", ".join(cats.get("allLocations", []) or [])
        hosted = item.get("hostedUrl") or ""
        apply_url = f"{hosted.rstrip('/')}/apply" if hosted else (item.get("applyUrl") or "")
        desc_html = "\n".join(filter(None, [
            item.get("descriptionHtml"),
            "".join(
                f"<h4>{lst.get('text', '')}</h4>{lst.get('content', '')}"
                for lst in (item.get("lists") or [])
            ),
        ]))
        out.append({
            "title": (item.get("text") or "").strip(),
            "location": (location or "").strip(),
            "apply_url": apply_url,
            "description": html_to_text(desc_html),
        })
    return out


def fetch_ashby(slug: str):
    resp = polite_get(ASHBY_API.format(slug=slug))
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    data = resp.json()
    jobs = data.get("jobs")
    if jobs is None:
        return None
    out = []
    for item in jobs:
        job_url = item.get("jobUrl") or ""
        apply_url = f"{job_url.rstrip('/')}/application" if job_url else (item.get("applyUrl") or "")
        out.append({
            "title": (item.get("title") or "").strip(),
            "location": (item.get("location") or "").strip(),
            "apply_url": apply_url,
            "description": html_to_text(item.get("descriptionHtml") or item.get("descriptionPlain")),
        })
    return out


def fetch_workable(slug: str):
    resp = polite_get(WORKABLE_API.format(slug=slug))
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    data = resp.json()
    jobs = data.get("jobs")
    if jobs is None:
        return None
    out = []
    for item in jobs:
        loc = item.get("locations") or [{}]
        loc0 = loc[0] if loc else {}
        city = item.get("city") or loc0.get("city") or ""
        country = item.get("country") or loc0.get("country") or ""
        location = ", ".join(p for p in [city, country] if p)
        base = item.get("shortlink") or item.get("url") or ""
        apply_url = f"{base.rstrip('/')}/apply" if base else ""
        out.append({
            "title": (item.get("title") or "").strip(),
            "location": location,
            "apply_url": apply_url,
            "description": html_to_text(item.get("description")),
        })
    return out


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "workable": fetch_workable,
}

_FETCH_CACHE: dict[tuple[str, str], object] = {}


def _cached_fetch(ats: str, slug: str):
    """One board fetch per (ats, slug) per process. Several jobs at one company
    (five Harnham postings) used to re-probe the same boards each, and the
    repeated probing drove Workable into 429 rate-limiting for discovery too."""
    key = (ats, slug)
    if key not in _FETCH_CACHE:
        try:
            _FETCH_CACHE[key] = FETCHERS[ats](slug)
        except Exception as exc:  # cached too: a rate-limited host must not be hammered again
            _FETCH_CACHE[key] = exc
    hit = _FETCH_CACHE[key]
    if isinstance(hit, Exception):
        raise hit
    return hit

# Aggregators whose own job pages (allowed by their robots.txt) carry both the
# full JD and an outbound apply redirect. The /out link is RECORDED, never
# fetched: gradcracker's robots.txt disallows /out; the approved apply step
# opens it in the browser, like a human clicking "Apply online now".
AGGREGATOR_PAGE_HOSTS = {"www.gradcracker.com"}
_OUT_LINK_RE = re.compile(r'<a[^>]+href="(?P<href>[^"]*/out/\d+/\d+/[^"]+)"[^>]*>')


def _inner_div(html_text: str, open_at: int) -> str:
    """The <div ...> at open_at through its matching </div> (depth-counted)."""
    depth = 0
    for m in re.finditer(r"<div\b|</div>", html_text[open_at:]):
        depth += 1 if m.group(0).startswith("<div") else -1
        if depth == 0:
            return html_text[open_at:open_at + m.end()]
    return html_text[open_at:]


def page_posting(row_url: str, title: str):
    """(posting|None, note): the aggregator's own job page as a posting."""
    host = urlsplit(row_url or "").netloc.lower()
    if host not in AGGREGATOR_PAGE_HOSTS:
        return None, None
    try:
        resp = polite_get(row_url)
    except Exception as exc:
        return None, f"{host}: job page fetch error ({exc})"
    if resp.status_code >= 400:
        return None, f"{host}: job page HTTP {resp.status_code}"
    link = _OUT_LINK_RE.search(resp.text)
    jd_at = resp.text.find('class="job-description')
    desc = ""
    if jd_at >= 0:
        open_at = resp.text.rfind("<div", 0, jd_at)
        if open_at >= 0:
            desc = html_to_text(_inner_div(resp.text, open_at))
    if not link:
        return None, (f"{host}: {'full JD on the job page' if desc else 'no JD'} but no apply link -- apply manually")
    return ({"title": title, "location": None, "apply_url": link["href"], "description": desc},
            f"{host}: apply redirect taken from the job's own page")


def resolve(company: str, title: str, location: str | None, companies: list[dict], url: str | None = None):
    """Returns (result_dict_or_None, attempts_log_list)."""
    attempts: list[str] = []
    tried: set[tuple[str, str]] = set()

    def probe(ats: str, slug: str):
        key = (ats, slug)
        if key in tried:
            return None
        tried.add(key)
        try:
            postings = _cached_fetch(ats, slug)
        except Exception as exc:  # network / parse error, not a "not found"
            attempts.append(f"{ats}:{slug} -> error ({exc})")
            return None
        if postings is None:
            attempts.append(f"{ats}:{slug} -> 404 (no such board)")
            return None
        scored = [
            (title_overlap(title, p["title"]), location_ok(p.get("location"), location), p)
            for p in postings
        ]
        matches = [(s, p) for s, ok, p in scored if s >= 0.8 and ok]
        if not matches:
            best = max(scored, key=lambda x: x[0]) if scored else None
            if best:
                attempts.append(
                    f"{ats}:{slug} -> board found ({len(postings)} postings), "
                    f"best title match {best[0]:.2f} for '{best[2]['title']}'"
                )
            else:
                attempts.append(f"{ats}:{slug} -> board found but empty")
            return None
        if len(matches) > 1:
            attempts.append(
                f"{ats}:{slug} -> ambiguous, {len(matches)} postings scored >= 0.8"
            )
            return None
        score, posting = matches[0]
        return {"ats": ats, "slug": slug, "score": score, "posting": posting}

    known = known_board_for(company, companies)
    if known and known.get("ats") in FETCHERS:
        r = probe(known["ats"], known["slug"])
        if r:
            r["resolved_from"] = f"companies.yaml:{known['ats']}:{known['slug']}"
            return r, attempts

    for slug in slug_candidates(company):
        for ats in ("greenhouse", "lever", "ashby", "workable"):
            r = probe(ats, slug)
            if r:
                r["resolved_from"] = f"{ats}:{slug}"
                return r, attempts

    if url:
        posting, note = page_posting(url, title)
        if posting:
            return {"ats": "other", "slug": "", "score": 1.0, "posting": posting,
                    "resolved_from": f"aggregator-page:{urlsplit(url).netloc}"}, attempts
        if note:
            attempts.append(note)
    return None, attempts


def load_job_json(job_id: str) -> dict | None:
    path = OUT_ROOT / job_id / "job.json"
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_job_json(job_id: str, doc: dict) -> None:
    path = OUT_ROOT / job_id / "job.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2, ensure_ascii=False)


def fetch_smartrecruiters_text(url: str) -> str:
    """SmartRecruiters hosts the full JD inside the posting page itself
    ("jobDescription": {"text": ...}); polite GET is fine, the DataDome wall
    guards only the application wizard."""
    if not re.match(r"https://jobs\.smartrecruiters\.com/[^/]+/\d+", url):
        raise RuntimeError("not a smartrecruiters posting url")
    resp = polite_get(url)
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}")
    key = '"jobDescription":'
    i = resp.text.find(key)
    if i < 0:
        raise RuntimeError("no jobDescription blob on the page")
    obj, _ = json.JSONDecoder().raw_decode(resp.text[i + len(key):])
    text = html_to_text(obj.get("text") or "")
    if len(text) < 300:
        raise RuntimeError("description too short")
    return text


def refresh_smartrecruiters_descriptions(conn, lconn) -> list[dict]:
    rows = conn.execute(
        "SELECT id, company, title, apply_url, description FROM jobs "
        "WHERE ats = 'smartrecruiters' AND apply_url LIKE 'https://jobs.smartrecruiters.com/%' "
        "AND status IN ('new', 'fit_fail') AND LENGTH(COALESCE(description, '')) < 300"
    ).fetchall()
    out = []
    for row in rows:
        try:
            text = fetch_smartrecruiters_text(row["apply_url"])
        except RuntimeError as exc:
            out.append({"company": row["company"], "status": "unavailable", "why": str(exc)})
            continue
        # one connection for both writes: the ledger and discover connections
        # point at the same jobs.db file and deadlock on each other's write lock
        with ledger_core.tx(lconn):
            lconn.execute("UPDATE jobs SET description = ?, reason = NULL WHERE id = ?", (text, row["id"]))
            ledger_core.append_event(lconn, "code:resolve_apply_url", "job.description_refreshed",
                                     {"ats": "smartrecruiters", "chars": len(text)}, job_id=row["id"])
        out.append({"company": row["company"], "status": "refreshed", "chars": len(text)})
    return out


def process_job(row, companies, conn, lconn) -> dict:
    job_id = row["id"]
    company = row["company"] or ""
    title = row["title"] or ""
    location = row["location"] or ""
    description = row["description"] or ""

    if row["apply_url"]:
        return {"job_id": job_id, "company": company, "title": title,
                "status": "already-resolved", "apply_url": row["apply_url"],
                "ats": row["ats"]}

    result, attempts = resolve(company, title, location, companies, row["url"])

    if result is None:
        reason = "apply_url unresolved: " + ("; ".join(attempts) if attempts else "no candidate slugs matched")
        if row["status"] in ("fit_pass", "tailored"):
            # It was on the way to being applied: it genuinely needs a human
            # (or a later resolution) before it can go anywhere.
            conn.execute(
                "UPDATE jobs SET reason = ?, status = 'queued_manual' WHERE id = ?",
                (reason, job_id),
            )
        else:
            # Still a discovery-stage candidate: record why there is no URL,
            # but leave the status alone — filter.py owns its state, and
            # demoting 'new' jobs here once poisoned the queue.
            conn.execute("UPDATE jobs SET reason = ? WHERE id = ?", (reason, job_id))
        conn.commit()
        with ledger_core.tx(lconn):
            final_status = "queued_manual" if row["status"] in ("fit_pass", "tailored") else row["status"]
            ledger_core.append_event(lconn, "code:resolve_apply_url", "job.status",
                                      {"from": row["status"], "to": final_status, "reason": reason},
                                      job_id=job_id)
        return {"job_id": job_id, "company": company, "title": title,
                "status": "unresolved", "reason": reason, "attempts": attempts}

    posting = result["posting"]
    apply_url = posting["apply_url"]
    ats = result["ats"]
    score = result["score"]
    resolved_from = result["resolved_from"]

    diff_score = desc_overlap(description, posting.get("description", ""))
    desc_flag = None
    if diff_score < 0.3 and not resolved_from.startswith("aggregator-page:"):
        desc_flag = (
            f"description overlap only {diff_score:.2f} between aggregator text and "
            f"the {ats} posting -- verify it's the same role before applying"
        )

    # FIT reads jobs.description. An aggregator row carries only a stub
    # (Gradcracker ~65 chars, jobs.ac.uk none); the employer's own posting is
    # the real text, so keep it whenever it is longer.
    posting_text = (posting.get("description") or "").strip()
    new_description = posting_text if len(posting_text) > len(description) else None

    new_status = None
    if row["status"] == "queued_manual" and (row["reason"] or "").startswith("apply_url unresolved"):
        new_status = "tailored" if next((OUT_ROOT / job_id).glob("CV_*.pdf"), None) else "fit_pass"
    if new_description and (new_status or row["status"]) in ("fit_pass", "tailored"):
        new_status = "new"  # it passed FIT on the old text; FIT must run again on the real one
    status_sql, status_params = (", status = ?, reason = NULL", (new_status,)) if new_status else ("", ())
    conn.execute(
        f"UPDATE jobs SET apply_url = ?, ats = ?, description = COALESCE(?, description){status_sql} WHERE id = ?",
        (apply_url, ats, new_description, *status_params, job_id),
    )
    conn.commit()

    with ledger_core.tx(lconn):
        ledger_core.append_event(lconn, "code:resolve_apply_url", "job.apply_url_resolved",
                                  {"apply_url": apply_url, "ats": ats, "resolved_from": resolved_from,
                                   "match": {"title_score": round(score, 2),
                                             "posting_title": posting["title"],
                                             "posting_location": posting.get("location"),
                                             "desc_overlap": round(diff_score, 2), "desc_flag": desc_flag},
                                   "description_replaced": ({"chars_before": len(description),
                                                             "chars_after": len(new_description)}
                                                            if new_description else None)},
                                  job_id=job_id)
        if new_status is not None:
            ledger_core.append_event(lconn, "code:resolve_apply_url", "job.status",
                                      {"from": row["status"], "to": new_status,
                                       "reason": "apply_url resolved" + (
                                           "; posting text replaced the stub, FIT re-runs"
                                           if new_description else "")}, job_id=job_id)

    job_doc = load_job_json(job_id)
    if job_doc is not None:
        job_doc["apply_url"] = apply_url
        job_doc["ats"] = ats
        job_doc["resolved_from"] = resolved_from
        if new_description:
            job_doc["description"] = new_description
        save_job_json(job_id, job_doc)

    return {
        "job_id": job_id, "company": company, "title": title,
        "status": "resolved", "apply_url": apply_url, "ats": ats,
        "title_score": round(score, 2), "resolved_from": resolved_from,
        "posting_title": posting["title"], "posting_location": posting.get("location"),
        "desc_overlap": round(diff_score, 2), "desc_flag": desc_flag,
        "description_replaced": new_description is not None,
    }


def select_all_missing(conn, retry_after_days: float):
    """Live candidates still lacking an apply URL. Only live candidates need one:
    filtered-out jobs stay filtered out (resolving them -- and worse, demoting
    failures into queued_manual -- once flooded the human queue with 81 irrelevant
    jobs on 2026-09-30), and a queued_manual row whose reason is an unresolved
    apply URL is exactly the row waiting for this resolver. A job this resolver
    already tried within `retry_after_days` is skipped: nothing about an agency's
    missing ATS board changes overnight, and the repeated probing is what drove
    Workable into 429 rate-limiting (2026-10-02). A probe that failed on a TRANSIENT
    error (HTTP 429, a host cooling down, a network blip: the reason carries
    "-> error (") answered nothing and does not count as tried -- "skip it now, try
    it later" -- the host cooldown (discover.util) keeps that retry from hammering."""
    cutoff = (datetime.datetime.now().astimezone()
              - datetime.timedelta(days=retry_after_days)).isoformat(timespec="seconds")
    return conn.execute(
        "SELECT * FROM jobs WHERE (apply_url IS NULL OR apply_url = '') "
        "AND (status IN ('new', 'fit_pass', 'tailored') "
        "     OR (status = 'queued_manual' AND reason LIKE 'apply_url unresolved%')) "
        "AND NOT EXISTS (SELECT 1 FROM events e WHERE e.job_id = jobs.id "
        "                AND e.actor = 'code:resolve_apply_url' AND e.ts > ? "
        "                AND e.payload NOT LIKE '%-> error (%') "
        "ORDER BY posted_at DESC", (cutoff,)
    ).fetchall()


def main() -> int:
    ap = argparse.ArgumentParser(description="Resolve apply_url for aggregator-sourced jobs")
    ap.add_argument("--job-id", action="append", default=[], help="specific job id(s)")
    ap.add_argument("--all-missing", action="store_true", help="process every job with apply_url IS NULL")
    ap.add_argument("--retry-after-days", type=float, default=3.0,
                    help="--all-missing skips jobs this resolver already tried within N days (0 = retry all)")
    ap.add_argument("--db", default=str(dbmod.DB_PATH))
    args = ap.parse_args()

    if not args.job_id and not args.all_missing:
        ap.error("give --job-id (repeatable) or --all-missing")

    conn = dbmod.connect(Path(args.db))
    lconn = ledger_core.connect(Path(args.db))
    companies = load_companies()

    if args.all_missing:
        rows = select_all_missing(conn, args.retry_after_days)
    else:
        rows = []
        for jid in args.job_id:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone()
            if row is None:
                print(f"[{jid}] not found in jobs.db")
                continue
            rows.append(row)

    results = refresh_smartrecruiters_descriptions(conn, lconn)
    for res in results:
        print(f"  [smartrecruiters] {res['company']}: {res['status']}"
              + (f" ({res.get('chars')} chars)" if res.get("chars") else f" -- {res.get('why')}"))

    results = []
    for row in rows:
        res = process_job(row, companies, conn, lconn)
        results.append(res)
        print(f"\n=== {res['company']} — {res['title']} ({res['job_id']}) ===")
        if res["status"] == "already-resolved":
            print(f"  already resolved: apply_url={res['apply_url']} ats={res['ats']}")
        elif res["status"] == "resolved":
            print(f"  MATCH  ats={res['ats']}  score={res['title_score']}  via {res['resolved_from']}")
            print(f"  apply_url = {res['apply_url']}")
            print(f"  posting title: {res['posting_title']!r}  location: {res['posting_location']!r}")
            print(f"  description overlap vs aggregator text: {res['desc_overlap']}")
            print(f"  description: {'replaced by the employer posting' if res['description_replaced'] else 'kept'}")
            if res["desc_flag"]:
                print(f"  ** FLAG ** {res['desc_flag']}")
        else:
            print("  UNRESOLVED")
            for a in res["attempts"]:
                print(f"    - {a}")
            print(f"  reason recorded: {res['reason']}")

    conn.close()
    lconn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
