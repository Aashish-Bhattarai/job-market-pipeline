"""Record a MANUAL application outcome (the operator submitted in their own tab, or a
skip decision) into the ledger. Owner: BankRecords.

Usage:
    python pipeline/run/record_result.py <job_id> --status applied|queued_manual|queued_account|skipped
        --note "..." [--answers answers.json] [--existing-row N] [--db path]

--answers, if given, is a JSON list of `{"label", "value", "source"}` --
exactly what the operator typed/confirmed, each recorded as a form_answers row
with source_kind='human' (source_ref = the given "source" free-text note).

Records an `applications` row (mode='manual'), transitions `jobs.status`
(applied_at stamped for `applied`), appends a Tracker row for
applied/queued outcomes (never for `skipped`, `--existing-row` stamps the
Job ID column on an already-logged row instead of appending), then
regenerates the DB views and the dashboard.
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ledger import core
from run import tracker, views

STATUSES = ("applied", "queued_manual", "queued_account", "skipped")


def _latest_document(conn, job_id: str, kind: str, fmt: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM documents WHERE job_id=? AND kind=? AND format=? ORDER BY id DESC LIMIT 1",
        (job_id, kind, fmt)).fetchone()
    return dict(row) if row else None


def record_manual(conn, job_id: str, status: str, note: str, answers: list[dict] | None) -> dict:
    if status not in STATUSES:
        raise ValueError(f"invalid status {status!r}")
    actor = "human"
    job_row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if job_row is None:
        raise SystemExit(f"job_id {job_id!r} not found in jobs.db")
    job_row = dict(job_row)

    answers = answers or []
    rows = []
    for i, a in enumerate(answers):
        rows.append({
            "idx": i, "label": a.get("label"), "type": None, "required": None,
            "value": core.canonical(a.get("value")), "readback_value": None,
            "source_kind": "human", "source_ref": a.get("source"), "verified": 1,
        })
    fa_sha = core.form_answers_hash(rows)
    today_iso = datetime.date.today().isoformat()
    now = core.now()

    with core.tx(conn):
        cv_doc = _latest_document(conn, job_id, "cv", "pdf")
        cover_doc = _latest_document(conn, job_id, "cover", "pdf")
        pv_id = core.profile_version(conn, actor=actor)

        prev = conn.execute(
            "SELECT COALESCE(MAX(attempt), 0) AS n FROM applications WHERE job_id=?", (job_id,)).fetchone()
        attempt_no = prev["n"] + 1

        app_id = core.record(conn, "applications", {
            "job_id": job_id, "attempt": attempt_no, "mode": "manual", "status": status,
            "apply_url": job_row.get("apply_url"), "final_url": None, "ats": job_row.get("ats"),
            "profile_version_id": pv_id, "git_commit": core.git_commit(),
            "bank_export_sha256": None,
            "cv_document_id": cv_doc["id"] if cv_doc else None,
            "cover_document_id": cover_doc["id"] if cover_doc else None,
            "attempt_json_sha256": None, "screenshot_prefill_sha256": None, "screenshot_confirm_sha256": None,
            "confirmation_text": None, "confirmation_url": None, "form_answers_sha256": fa_sha,
            "started_at": now, "finished_at": now, "note": note or None, "created_at": now,
        }, actor=actor, event_type="application.recorded", job_id=job_id)

        for r in rows:
            conn.execute(
                "INSERT INTO form_answers(application_id, idx, label, type, required, value, "
                "readback_value, source_kind, source_ref, verified) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (app_id, r["idx"], r["label"], r["type"], r["required"], r["value"],
                 r["readback_value"], r["source_kind"], r["source_ref"], r["verified"]))

        old_status = job_row["status"]
        if status == "applied":
            conn.execute("UPDATE jobs SET status=?, applied_at=? WHERE id=?", (status, today_iso, job_id))
        else:
            conn.execute("UPDATE jobs SET status=? WHERE id=?", (status, job_id))
        core.append_event(conn, actor, "job.status", {"from": old_status, "to": status, "reason": note},
                           job_id=job_id)

    return {"application_id": app_id, "attempt": attempt_no, "old_status": old_status, "new_status": status}


def main() -> int:
    ap = argparse.ArgumentParser(description="Record a MANUAL application result")
    ap.add_argument("job_id")
    ap.add_argument("--status", required=True, choices=list(STATUSES))
    ap.add_argument("--note", default="")
    ap.add_argument("--answers", default=None, help="path to a JSON list of {label,value,source}")
    ap.add_argument("--existing-row", type=int, default=None,
                     help="stamp Job ID onto an already-existing Tracker row instead of appending one")
    ap.add_argument("--db", default=None)
    ap.add_argument("--xlsx", default=str(tracker.DEFAULT_XLSX))
    args = ap.parse_args()

    conn = core.connect(args.db) if args.db else core.connect()
    answers = None
    if args.answers:
        answers = json.loads(Path(args.answers).read_text(encoding="utf-8"))

    result = record_manual(conn, args.job_id, args.status, args.note, answers)
    print(f"application {result['application_id']} (attempt {result['attempt']}) recorded for {args.job_id}: "
          f"jobs.status {result['old_status']} -> {result['new_status']}")

    if args.status in tracker.TRACKER_STATUS:
        job = conn.execute("SELECT * FROM jobs WHERE id=?", (args.job_id,)).fetchone()
        folder = str(core.PIPELINE_ROOT / "out" / args.job_id)
        if args.existing_row is not None:
            tracker.stamp_existing_row(Path(args.xlsx), args.existing_row, args.job_id)
            print(f"Tracker row {args.existing_row} stamped with Job ID in {args.xlsx}")
        else:
            row = tracker.write_tracker_row(
                Path(args.xlsx), job["company"], job["title"],
                tracker.channel_for(job["ats"] or job["source"]), tracker.TRACKER_STATUS[args.status],
                args.note, datetime.datetime.now(), folder, args.job_id)
            print(f"Tracker row {row} appended in {args.xlsx}")
    else:
        print("Tracker not touched (status=skipped)")

    views.regenerate(conn)
    print("Views regenerated (ANSWERS_LOG.md + out/<job_id>/ANSWERS.md).")
    tracker.run_build_dashboard()
    return 0


if __name__ == "__main__":
    sys.exit(main())
