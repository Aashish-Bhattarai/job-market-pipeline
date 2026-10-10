"""Print the complete ledger trace for one or more jobs. Owner: BankRecords.

Usage:
    python pipeline/run/show.py <job_id>
    python pipeline/run/show.py <company substring>

If the argument is a 40-hex job id it is looked up directly; otherwise it is
matched case-insensitively against company/title. Prints: the job and its JD
hash, requirements + fit, every document (kind, sha, llm calls with
requested vs actual model, prompt version, profile version, git commit),
every application attempt (all form answers + sources, screenshots,
confirmation), the bank entries it drew on, and the job's event timeline.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ledger import core

JOB_ID_RE = re.compile(r"[0-9a-f]{40}$")


def find_jobs(conn, query: str) -> list[dict]:
    if JOB_ID_RE.fullmatch(query):
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (query,)).fetchone()
        return [dict(row)] if row else []
    like = f"%{query.lower()}%"
    rows = conn.execute(
        "SELECT * FROM jobs WHERE lower(company) LIKE ? OR lower(title) LIKE ? ORDER BY discovered_at DESC",
        (like, like)).fetchall()
    return [dict(r) for r in rows]


def _fmt_value(v):
    if v is None:
        return ""
    try:
        val = json.loads(v) if isinstance(v, str) else v
    except (ValueError, TypeError):
        val = v
    return json.dumps(val, ensure_ascii=False) if isinstance(val, (list, dict)) else str(val)


def print_job(conn, job: dict) -> None:
    print("=" * 100)
    print(f"{job['company']} — {job['title']}   [{job['id']}]")
    print(f"status={job['status']}  reason={job['reason'] or '-'}  applied_at={job['applied_at'] or '-'}")
    print(f"url={job['url']}  apply_url={job['apply_url']}  ats={job['ats']}")

    jd_doc = conn.execute(
        "SELECT * FROM documents WHERE job_id=? AND kind='jd' ORDER BY id DESC LIMIT 1", (job["id"],)).fetchone()
    print(f"JD sha256: {jd_doc['sha256'] if jd_doc else '(no jd document recorded)'}")

    fit = None
    if job["fit_json"]:
        try:
            fit = json.loads(job["fit_json"])
            print(f"\nFIT: decision={fit.get('final_decision', fit.get('decision'))} score={fit.get('score')}")
            print(f"  reason: {fit.get('reason')}")
        except ValueError:
            fit = None

    reqs = conn.execute("SELECT * FROM requirements WHERE job_id=? ORDER BY idx", (job["id"],)).fetchall()
    # `met`/`blocks` are decided by fit_rules.py from the requirement's text
    # (not stored as a requirements column); fit_json's verdicts were built
    # by iterating this exact same query, so position lines them up.
    verdicts = (fit or {}).get("verdicts") or []
    if reqs:
        print(f"\nRequirements ({len(reqs)}):")
        for i, r in enumerate(reqs):
            v = verdicts[i] if i < len(verdicts) else {}
            print(f"  [{r['idx']}] required={bool(r['required'])} met={v.get('met', '?')} "
                  f"fact_ids={r['fact_ids']}")
            print(f"       {r['text']!r}")

    print("\nDocuments:")
    docs = conn.execute("SELECT * FROM documents WHERE job_id=? ORDER BY id", (job["id"],)).fetchall()
    if not docs:
        print("  (none)")
    for d in docs:
        print(f"  [{d['id']}] {d['kind']}/{d['format']}  sha256={d['sha256']}")
        print(f"       profile_version_id={d['profile_version_id']}  git_commit={d['git_commit']}"
              f"  note={d['note'] or '-'}")
        llm_ids = json.loads(d["llm_call_ids"] or "[]")
        for cid in llm_ids:
            c = conn.execute("SELECT * FROM llm_calls WHERE id=?", (cid,)).fetchone()
            if c is None:
                continue
            print(f"       llm_call {c['id']}: stage={c['stage']} requested={c['requested_model']} "
                  f"actual={c['actual_model']} prompt={c['prompt_name']}@{c['prompt_sha256'][:8]} "
                  f"ok={bool(c['ok'])} cost=${c['cost_usd'] or 0:.4f}")

    print("\nApplications:")
    apps = conn.execute("SELECT * FROM applications WHERE job_id=? ORDER BY attempt", (job["id"],)).fetchall()
    if not apps:
        print("  (none)")
    for app in apps:
        print(f"  Attempt {app['attempt']}: mode={app['mode']} status={app['status']} "
              f"started={app['started_at']} finished={app['finished_at']}")
        print(f"    profile_version_id={app['profile_version_id']} git_commit={app['git_commit']}"
              f" bank_export_sha256={app['bank_export_sha256']}")
        rows = conn.execute(
            "SELECT * FROM form_answers WHERE application_id=? ORDER BY idx", (app["id"],)).fetchall()
        for r in rows:
            src = r["source_kind"] + (f":{r['source_ref']}" if r["source_ref"] else "")
            print(f"      [{r['idx']}] {r['label']!r} = {_fmt_value(r['value'])!r}  ({src}, "
                  f"verified={r['verified']})")
        for label, sha in (("prefill", app["screenshot_prefill_sha256"]),
                            ("confirm", app["screenshot_confirm_sha256"])):
            if sha:
                try:
                    print(f"    screenshot {label}: {core.blob_path(conn, sha)}")
                except KeyError:
                    print(f"    screenshot {label}: sha256={sha} (blob missing)")
        if app["confirmation_text"]:
            print(f"    confirmation: {app['confirmation_text']!r}")

    print("\nBank entries used:")
    bank_ids = set()
    for app in apps:
        for r in conn.execute(
                "SELECT source_ref FROM form_answers WHERE application_id=? AND source_kind='bank'", (app["id"],)):
            if r["source_ref"]:
                bank_ids.add(r["source_ref"])
    if not bank_ids:
        print("  (none)")
    for bid in sorted(bank_ids, key=lambda x: str(x)):
        b = conn.execute("SELECT * FROM answer_bank WHERE id=?", (bid,)).fetchone()
        if b:
            print(f"  [{b['id']}] scope={b['scope']} question={b['question']!r} answer={b['answer']!r}")

    print("\nEvent timeline:")
    for e in conn.execute("SELECT * FROM events WHERE job_id=? ORDER BY seq", (job["id"],)):
        print(f"  #{e['seq']} {e['ts']} {e['actor']:<20} {e['type']:<24} {e['payload']}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Print the full ledger trace for a job")
    ap.add_argument("query", help="job_id (40-hex) or a company/title substring")
    ap.add_argument("--db", default=None)
    ap.add_argument("--store-root", default=None)
    args = ap.parse_args()
    if args.store_root:
        core.PIPELINE_ROOT = Path(args.store_root)

    conn = core.connect(args.db) if args.db else core.connect()
    jobs = find_jobs(conn, args.query)
    if not jobs:
        print(f"No job matches {args.query!r}")
        return 1
    for job in jobs:
        print_job(conn, job)
    return 0


if __name__ == "__main__":
    sys.exit(main())
