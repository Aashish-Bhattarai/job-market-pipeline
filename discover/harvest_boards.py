"""Find live public job boards for a list of company names and append the UK-relevant ones to
companies.yaml. Public board APIs only (Greenhouse, Lever, Ashby, SmartRecruiters); each host is
hit at most once per second (the project's politeness standard), hosts run in parallel.

Usage:
    python -m discover.harvest_boards [--names discover/candidates_uk.txt] [--dry-run]
    python -m discover.harvest_boards --slugs boards.json [--dry-run]   # [{"ats": "ashby", "slug": "trainline"}, ...]
                                                                          # exact slugs (e.g. from site: searches)

A board is added only if it exists, lists at least one posting whose location hints at the UK, and
its (ats, slug) is not already in companies.yaml. The ATS that answers wins per name; one name can
legitimately have boards on more than one ATS and all UK-hinting ones are kept.
"""
from __future__ import annotations

import argparse
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
import yaml

HERE = Path(__file__).resolve().parent
COMPANIES = HERE / "companies.yaml"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0 Safari/537.36"}
UK_WORDS = ("london", "united kingdom", "uk", "england", "scotland", "wales", "edinburgh", "manchester",
            "cambridge", "oxford", "bristol", "leeds", "birmingham", "glasgow", "brighton", "reading",
            "belfast", "cardiff", "sheffield", "newcastle", "nottingham", "liverpool", "gb")
_UK_RE = re.compile(r"(?<![a-z])(" + "|".join(re.escape(w) for w in UK_WORDS) + r")(?![a-z])", re.I)

_last: dict[str, float] = {}
_lock = threading.Lock()


def _wait(host: str, gap: float = 1.0) -> None:
    """At most one request per `gap` seconds per host, across threads."""
    with _lock:
        now = time.monotonic()
        at = max(now, _last.get(host, 0.0) + gap)
        _last[host] = at
    delay = at - now
    if delay > 0:
        time.sleep(delay)


def slug_variants(name: str) -> list[str]:
    base = re.sub(r"[^a-z0-9 \-]", "", name.lower().replace("&", " and ").replace(".", ""))
    words = base.replace("-", " ").split()
    out = []
    for v in ("".join(words), "-".join(words), words[0] if words else ""):
        if v and v not in out:
            out.append(v)
    return out


def _get(host: str, url: str):
    _wait(host)
    try:
        return requests.get(url, headers=UA, timeout=25)
    except requests.RequestException:
        return None


def _uk_count(locations) -> int:
    return sum(1 for loc in locations if loc and _UK_RE.search(str(loc)))


def probe_greenhouse(slug):
    r = _get("boards-api.greenhouse.io", f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    if r is None or r.status_code != 200:
        return None
    jobs = (r.json() or {}).get("jobs") or []
    return len(jobs), _uk_count((j.get("location") or {}).get("name") for j in jobs)


def probe_lever(slug):
    r = _get("api.lever.co", f"https://api.lever.co/v0/postings/{slug}?mode=json")
    if r is None or r.status_code != 200 or not isinstance(r.json(), list):
        return None
    jobs = r.json()
    return len(jobs), _uk_count((j.get("categories") or {}).get("location") for j in jobs)


def probe_ashby(slug):
    r = _get("api.ashbyhq.com", f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    if r is None or r.status_code != 200:
        return None
    jobs = (r.json() or {}).get("jobs")
    if jobs is None:
        return None
    return len(jobs), _uk_count(j.get("location") for j in jobs)


def probe_smartrecruiters(slug):
    r = _get("api.smartrecruiters.com", f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100")
    if r is None or r.status_code != 200:
        return None
    data = r.json() or {}
    items = data.get("content") or []
    if not data.get("totalFound"):
        return None
    locs = [((i.get("location") or {}).get("city") or "") + " " + ((i.get("location") or {}).get("country") or "")
            for i in items]
    return data["totalFound"], _uk_count(locs)


PROBES = {"greenhouse": probe_greenhouse, "lever": probe_lever, "ashby": probe_ashby,
          "smartrecruiters": probe_smartrecruiters}
CAREERS = {"greenhouse": "https://boards.greenhouse.io/{s}", "lever": "https://jobs.lever.co/{s}",
           "ashby": "https://jobs.ashbyhq.com/{s}", "smartrecruiters": "https://jobs.smartrecruiters.com/{s}"}


def run_ats(ats: str, names: list[str], known: set, found: list, log) -> None:
    probe = PROBES[ats]
    for name in names:
        for slug in slug_variants(name):
            if (ats, slug.lower()) in known:
                break
            res = probe(slug)
            if res is None:
                continue
            total, uk = res
            log(f"  {ats:15s} {slug:28s} {total:4d} postings, {uk:3d} UK-hinting" + ("  <-- ADD" if uk else ""))
            if uk:
                found.append({"name": name, "ats": ats, "slug": slug})
                known.add((ats, slug.lower()))
            break  # first slug that answers wins for this name on this ATS


def run_exact(ats: str, slugs: list[str], known: set, found: list, log) -> None:
    """Probe exact board slugs (no name guessing): the slug came from a real job URL."""
    probe = PROBES[ats]
    for slug in slugs:
        if (ats, slug.lower()) in known:
            continue
        res = probe(slug)
        if res is None:
            continue
        total, uk = res
        log(f"  {ats:15s} {slug:28s} {total:4d} postings, {uk:3d} UK-hinting" + ("  <-- ADD" if uk else ""))
        if uk:
            found.append({"name": slug.replace("-", " ").replace("_", " ").title(), "ats": ats, "slug": slug})
            known.add((ats, slug.lower()))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--slugs", default=None, help="JSON list of {ats, slug}: probe exact slugs instead of --names")
    ap.add_argument("--names", default=str(HERE / "candidates_uk.txt"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    names = []
    if not args.slugs:
        for line in Path(args.names).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and line not in names:
                names.append(line)
    existing = yaml.safe_load(COMPANIES.read_text(encoding="utf-8")) or []
    known = {(str(c.get("ats")), str(c.get("slug", "")).lower()) for c in existing}
    print(f"{len(names)} candidate names, {len(known)} boards already known", flush=True)

    found: list[dict] = []
    printed = threading.Lock()

    def log(msg):
        with printed:
            print(msg, flush=True)

    with ThreadPoolExecutor(max_workers=len(PROBES)) as pool:
        if args.slugs:
            import json
            pairs = json.loads(Path(args.slugs).read_text(encoding="utf-8"))
            by_ats = {ats: [p["slug"] for p in pairs if p.get("ats") == ats] for ats in PROBES}
            futures = [pool.submit(run_exact, ats, sl, known, found, log) for ats, sl in by_ats.items() if sl]
        else:
            futures = [pool.submit(run_ats, ats, names, known, found, log) for ats in PROBES]
        for f in futures:
            f.result()

    print(f"\n{len(found)} new UK-hinting boards", flush=True)
    if found and not args.dry_run:
        lines = [f'- {{name: "{b["name"]}", ats: {b["ats"]}, slug: "{b["slug"]}", '
                 f'careers_url: "{CAREERS[b["ats"]].format(s=b["slug"])}", region: london}}' for b in found]
        with open(COMPANIES, "a", encoding="utf-8") as fh:
            fh.write("\n# harvested by discover/harvest_boards.py\n" + "\n".join(lines) + "\n")
        print(f"appended {len(found)} boards to {COMPANIES.name}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
