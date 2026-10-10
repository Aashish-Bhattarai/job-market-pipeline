"""Discovery CLI. Owner: DiscoveryCore.

Usage:
    python pipeline/discover/discover.py [--companies companies.yaml] [--sources a,b|all] [--db path]

For each ATS source (greenhouse, lever, ashby, workable, smartrecruiters) the
CLI iterates rows of --companies whose `ats` field matches and calls
fetch(company_row). For site sources (himalayas, jobsacuk, gradcracker,
hiringcafe, wttj) it calls fetch({}) once each, since those crawl whole job
sites rather than a single company's board. Never crashes the whole run on
one source's failure; errors are collected and printed per source.
"""
from __future__ import annotations

import argparse
import datetime
import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from discover import db as dbmod
from discover.util import HostCoolingDown
from discover.sources import REGISTRY, _ATS_MODULES, _SITE_MODULES

HERE = Path(__file__).resolve().parent
RETRY_PAUSE_S = 20  # before the one retry of a failed board / empty site source


def load_companies(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or []
    return data


def _no_retry(exc: Exception) -> bool:
    """404 = no such board (permanent); 429 / HostCoolingDown = the host said stop."""
    text = str(exc)
    return isinstance(exc, HostCoolingDown) or "404" in text or "429" in text


def run(companies_path: Path, source_names: list[str], db_path: Path, verbose: bool = False) -> dict:
    companies = load_companies(companies_path)
    discovered_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = dbmod.connect(db_path)

    if source_names == ["all"]:
        source_names = sorted(REGISTRY.keys())

    results = {}
    for name in source_names:
        mod = REGISTRY.get(name)
        if mod is None:
            results[name] = {"error": "source module not available", "fetched": 0,
                              "inserted": 0, "updated": 0}
            print(f"[{name}] SKIP: source module not available")
            continue

        fetched = 0
        errors = []
        all_jobs = []
        if name in _ATS_MODULES:
            rows = [c for c in companies if (c.get("ats") or "").lower() == name]
            failed = []
            for row in rows:
                try:
                    jobs = mod.fetch(row)
                    fetched += len(jobs)
                    all_jobs.extend(jobs)
                    if verbose:
                        print(f"    - {row.get('name', row.get('slug'))} ({row.get('slug')}): {len(jobs)} jobs")
                except Exception as exc:  # never crash the whole run
                    failed.append((row, exc))
            if failed:
                # A DNS/network blip (2026-10-02: 120+ boards failed with NameResolutionError and the
                # round carried on with a silently shrunken funnel) must not cost a whole round of
                # jobs: retry each failed board once after a pause. A 404 is a permanent answer, and a
                # 429 / cooling-down host has said STOP -- retrying seconds later is exactly what earned
                # Workable's 16-hour Cloudflare ban (util.polite_get now remembers it across runs).
                retry = [(r, e) for r, e in failed if not _no_retry(e)]
                still = [(r, e) for r, e in failed if _no_retry(e)]
                if retry:
                    time.sleep(RETRY_PAUSE_S)
                for row, exc in retry:
                    try:
                        jobs = mod.fetch(row)
                        fetched += len(jobs)
                        all_jobs.extend(jobs)
                    except Exception as exc2:
                        still.append((row, exc2))
                errors.extend(f"{row.get('name', row.get('slug'))}: {e}" for row, e in still)
        else:
            try:
                jobs = mod.fetch({})
                if not jobs:  # site sources swallow network errors and return []: look once more
                    time.sleep(RETRY_PAUSE_S)
                    jobs = mod.fetch({})
                fetched += len(jobs)
                all_jobs.extend(jobs)
            except Exception as exc:
                errors.append(str(exc))

        counts = dbmod.upsert_many(conn, all_jobs, discovered_at)
        results[name] = {"fetched": fetched, "inserted": counts["inserted"],
                          "updated": counts["updated"], "errors": errors}
        err_note = f", {len(errors)} error(s)" if errors else ""
        print(f"[{name}] fetched={fetched} inserted={counts['inserted']} "
              f"updated={counts['updated']}{err_note}")
        for e in errors:
            print(f"    ! {e}")

    total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    conn.close()
    results["_total_rows"] = total
    print(f"total rows in db: {total}")
    from ledger import core
    lconn = core.connect(db_path)
    try:
        with core.tx(lconn):
            core.append_event(lconn, "code:discover", "discover.run",
                              {"discovered_at": discovered_at, "sources": results})
    finally:
        lconn.close()
    return results


def main():
    ap = argparse.ArgumentParser(description="Fetch job postings into jobs.db")
    ap.add_argument("--companies", default=str(HERE / "companies.yaml"))
    ap.add_argument("--sources", default="all", help="comma-separated source names, or 'all'")
    ap.add_argument("--db", default=str(dbmod.DB_PATH))
    ap.add_argument("--verbose", action="store_true", help="print per-company fetched counts for ATS sources")
    args = ap.parse_args()

    source_names = ["all"] if args.sources.strip().lower() == "all" else \
        [s.strip() for s in args.sources.split(",") if s.strip()]

    run(Path(args.companies), source_names, Path(args.db), verbose=args.verbose)


if __name__ == "__main__":
    main()
