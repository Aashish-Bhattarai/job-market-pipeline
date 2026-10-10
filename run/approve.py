"""Approve a `ready` job for submission. Owner: BankRecords.

Usage:
    python pipeline/run/approve.py <job_id>
    python pipeline/run/approve.py --all-ready

For each `ready` job: records event `submit.approved` on the job with
{application_id of its latest dry-run attempt, answers: [{label, value}]
taken from that attempt's form_answers where source_kind != 'untouched'},
then sets `jobs.status='approved'` (the next batch submits it).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ledger import core


def approve_job(conn, job_id: str) -> dict:
    job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if job is None:
        raise SystemExit(f"job_id {job_id!r} not found")
    if job["status"] != "ready":
        raise SystemExit(f"job {job_id!r} is not 'ready' (status={job['status']!r})")

    app = conn.execute(
        "SELECT * FROM applications WHERE job_id=? AND mode='dry_run' ORDER BY attempt DESC LIMIT 1",
        (job_id,)).fetchone()
    if app is None:
        raise SystemExit(f"job {job_id!r}: no dry_run application recorded")
    app = dict(app)

    rows = conn.execute(
        "SELECT * FROM form_answers WHERE application_id=? AND source_kind != 'untouched' ORDER BY idx",
        (app["id"],)).fetchall()
    answers = []
    for r in rows:
        try:
            value = json.loads(r["value"]) if r["value"] is not None else None
        except (ValueError, TypeError):
            value = r["value"]
        answers.append({"label": r["label"], "value": value})

    with core.tx(conn):
        core.append_event(conn, "human", "submit.approved",
                           {"application_id": app["id"], "answers": answers}, job_id=job_id)
        conn.execute("UPDATE jobs SET status='approved' WHERE id=?", (job_id,))
        core.append_event(conn, "human", "job.status", {"from": "ready", "to": "approved", "reason": None},
                           job_id=job_id)

    return {"job_id": job_id, "application_id": app["id"], "n_answers": len(answers)}


def main() -> int:
    ap = argparse.ArgumentParser(description="Approve ready job(s) for submission")
    ap.add_argument("job_id", nargs="?")
    ap.add_argument("--all-ready", action="store_true")
    ap.add_argument("--db", default=None)
    args = ap.parse_args()

    if not args.job_id and not args.all_ready:
        ap.error("give a job_id or --all-ready")

    conn = core.connect(args.db) if args.db else core.connect()
    job_ids = [args.job_id] if args.job_id else \
        [r["id"] for r in conn.execute("SELECT id FROM jobs WHERE status='ready'")]

    if not job_ids:
        print("No ready jobs.")
        return 0

    for jid in job_ids:
        result = approve_job(conn, jid)
        print(f"{jid}: approved (application {result['application_id']}, "
              f"{result['n_answers']} answers carried forward)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
