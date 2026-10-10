"""Ingest one Node-runner attempt into the ledger. Owner: BankRecords.

Usage:
    python pipeline/run/ingest_attempt.py <attempt_dir>

Reads `<attempt_dir>/order.json` and `<attempt_dir>/attempt.json` (+ optional
`prefill.png`/`confirm.png`), and in one transaction: stores the attempt
record and screenshots as blobs, builds `form_answers` rows from `snapshot`
(every control) plus any `unanswered` entries not already covered, inserts
the `applications` row, enqueues every required unanswered question, and
transitions `jobs.status`. After commit it appends a Tracker row (applied/
queued outcomes only), regenerates the DB views, and refreshes the dashboard.
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ledger import bank, core
from run import tracker, views

# attempt.json status -> jobs.status
STATUS_MAP = {
    "submitted": "applied",
    "filled_not_submitted": "ready",
    "queued_manual": "queued_manual",
    "blocked_gate": "queued_manual",
    "submit_failed": "queued_manual",
    "queued_account": "queued_account",
}


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _source_kind(source: str | None, filled_by: str | None) -> tuple[str, str | None]:
    if filled_by == "ats_autofill":
        return "ats_autofill", source
    if filled_by == "untouched" or not source:
        return "untouched", source
    if source.startswith("bank:"):
        return "bank", source.split(":", 1)[1]
    if source.startswith("answers."):
        return "answers_yaml", source
    if source.startswith("facts"):
        return "fact", source
    if source.startswith("files."):
        return "file", source
    if source.startswith("cover."):
        return "cover", source
    # standing-answer / freeform policy text: still a documented standing rule.
    return "answers_yaml", source


def build_form_answers(attempt: dict) -> list[dict]:
    unanswered_by_label = {u["label"]: u for u in attempt.get("unanswered", []) if u.get("label")}
    verified_labels = {f["label"] for f in attempt.get("fields", []) if f.get("label")}
    rows: list[dict] = []
    seen_labels = set()
    for s in attempt.get("snapshot", []):
        label = s.get("label")
        u = unanswered_by_label.get(label) if label else None
        if u is not None:
            rows.append({
                "idx": s["idx"], "label": label, "type": u.get("type", s.get("type")),
                "required": int(bool(u.get("required"))), "value": None, "readback_value": None,
                "source_kind": "unanswered", "source_ref": None, "verified": None,
            })
        else:
            kind, ref = _source_kind(s.get("source"), s.get("filled_by"))
            verified = 1 if (s.get("filled_by") == "pipeline" and label in verified_labels) else 0
            rows.append({
                "idx": s["idx"], "label": label, "type": s.get("type"),
                "required": int(bool(s.get("required"))), "value": core.canonical(s.get("value")),
                "readback_value": core.canonical(s.get("value")) if s.get("filled_by") == "pipeline" else None,
                "source_kind": kind, "source_ref": ref, "verified": verified,
            })
        if label:
            seen_labels.add(label)
    next_idx = (max((r["idx"] for r in rows), default=-1)) + 1
    for label, u in unanswered_by_label.items():
        if label in seen_labels:
            continue
        rows.append({
            "idx": next_idx, "label": label, "type": u.get("type"),
            "required": int(bool(u.get("required"))), "value": None, "readback_value": None,
            "source_kind": "unanswered", "source_ref": None, "verified": None,
        })
        next_idx += 1
    return rows


def ingest(conn, attempt_dir: Path) -> dict:
    order = _load(attempt_dir / "order.json")
    attempt = _load(attempt_dir / "attempt.json")
    job_id = attempt["job_id"]
    actor = "code:ingest_attempt"

    rows = build_form_answers(attempt)
    fa_sha = core.form_answers_hash(rows)

    gate_reasons = attempt.get("gate_reasons") or []
    errors = attempt.get("errors") or []
    note = "; ".join(gate_reasons + errors) or None

    with core.tx(conn):
        attempt_json_sha = core.store_bytes(
            conn, json.dumps(attempt, sort_keys=True, ensure_ascii=False).encode("utf-8"),
            ".json", actor, job_id)

        shots = attempt.get("screenshots") or {}
        prefill_sha = None
        confirm_sha = None
        if shots.get("prefill") and (attempt_dir / shots["prefill"]).exists():
            prefill_sha = core.store_file(conn, attempt_dir / shots["prefill"], actor, job_id)
        if shots.get("confirm") and (attempt_dir / shots["confirm"]).exists():
            confirm_sha = core.store_file(conn, attempt_dir / shots["confirm"], actor, job_id)

        prev_attempt = conn.execute(
            "SELECT COALESCE(MAX(attempt), 0) AS n FROM applications WHERE job_id=?", (job_id,)).fetchone()
        attempt_no = prev_attempt["n"] + 1

        pv_id = core.profile_version(conn, actor=actor)
        doc_ids = order.get("document_ids") or {}

        app_id = core.record(conn, "applications", {
            "job_id": job_id, "attempt": attempt_no, "mode": attempt["mode"], "status": attempt["status"],
            "apply_url": attempt.get("apply_url") or order.get("job", {}).get("apply_url"),
            "final_url": attempt.get("final_url"), "ats": order.get("job", {}).get("ats"),
            "profile_version_id": pv_id, "git_commit": core.git_commit(),
            "bank_export_sha256": order.get("bank_sha256"),
            "cv_document_id": doc_ids.get("cv"), "cover_document_id": doc_ids.get("cover"),
            "attempt_json_sha256": attempt_json_sha,
            "screenshot_prefill_sha256": prefill_sha, "screenshot_confirm_sha256": confirm_sha,
            "confirmation_text": attempt.get("confirmation_text"),
            "confirmation_url": attempt.get("confirmation_url"),
            "form_answers_sha256": fa_sha,
            "started_at": attempt.get("started_at"), "finished_at": attempt.get("finished_at"),
            "note": note, "created_at": core.now(),
        }, actor=actor, event_type="application.recorded", job_id=job_id)

        for r in rows:
            conn.execute(
                "INSERT INTO form_answers(application_id, idx, label, type, required, value, "
                "readback_value, source_kind, source_ref, verified) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (app_id, r["idx"], r["label"], r["type"], r["required"], r["value"],
                 r["readback_value"], r["source_kind"], r["source_ref"], r["verified"]))

        for r in rows:
            if r["source_kind"] == "unanswered" and r["required"]:
                if r["type"] == "system":
                    continue  # apply.mjs diagnostic ("No form fields found..."), never a real question
                bank.enqueue(conn, job_id, app_id, r["label"], r["type"], None, True, actor)

        job_row = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        old_status = job_row["status"] if job_row else None
        new_status = STATUS_MAP.get(attempt["status"], "needs_review")
        today_iso = datetime.date.today().isoformat()
        if new_status == "applied":
            conn.execute("UPDATE jobs SET status=?, applied_at=? WHERE id=?", (new_status, today_iso, job_id))
        else:
            conn.execute("UPDATE jobs SET status=? WHERE id=?", (new_status, job_id))
        core.append_event(conn, actor, "job.status",
                           {"from": old_status, "to": new_status, "reason": note}, job_id=job_id)

    return {"application_id": app_id, "attempt": attempt_no, "job_id": job_id,
            "old_status": old_status, "new_status": new_status}


def main() -> int:
    ap = argparse.ArgumentParser(description="Ingest a Node runner attempt into the ledger")
    ap.add_argument("attempt_dir")
    args = ap.parse_args()
    attempt_dir = Path(args.attempt_dir).resolve()

    conn = core.connect()
    result = ingest(conn, attempt_dir)
    print(f"application {result['application_id']} (attempt {result['attempt']}) recorded for "
          f"{result['job_id']}: jobs.status {result['old_status']} -> {result['new_status']}")

    if result["new_status"] in ("applied", "queued_manual", "queued_account"):
        job = conn.execute("SELECT * FROM jobs WHERE id=?", (result["job_id"],)).fetchone()
        tracker_status = tracker.TRACKER_STATUS[result["new_status"]]
        folder = str((core.PIPELINE_ROOT / "out" / result["job_id"]))
        row = tracker.write_tracker_row(
            tracker.DEFAULT_XLSX, job["company"], job["title"],
            tracker.channel_for(job["ats"] or job["source"]), tracker_status,
            "" , datetime.datetime.now(), folder, result["job_id"])
        print(f"Tracker row {row} appended in {tracker.DEFAULT_XLSX}")

    views.regenerate(conn)
    print("Views regenerated (ANSWERS_LOG.md + out/<job_id>/ANSWERS.md).")
    tracker.run_build_dashboard()
    return 0


if __name__ == "__main__":
    sys.exit(main())
