"""Build a work order for the Node runner: pick the latest CV/cover documents
for a job, export them into a fresh attempt dir, export the answer bank, and
write `order.json` (shape: v2_contracts.md "Work order")."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import bank, core


def _latest_document(conn, job_id: str, kind: str, fmt: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM documents WHERE job_id=? AND kind=? AND format=? ORDER BY id DESC LIMIT 1",
        (job_id, kind, fmt)).fetchone()
    return dict(row) if row else None


def _previously_applied(conn, job_row) -> bool:
    """Has an application to a DIFFERENT posting at this company already gone out? (answers "have you
    applied to us before?" from the ledger instead of guessing.)"""
    key = re.sub(r"[^a-z0-9]+", "", (job_row.get("company") or "").lower())
    if not key:
        return False
    for r in conn.execute("SELECT j.company FROM applications a JOIN jobs j ON j.id = a.job_id "
                          "WHERE a.status IN ('applied', 'submitted') AND j.id != ?", (job_row["id"],)):
        if re.sub(r"[^a-z0-9]+", "", (r["company"] or "").lower()) == key:
            return True
    return False

def _candidate_filename_stem() -> str:
    """Derive the CV/cover filename stem from profile/facts.yaml's contact.name (falls back to
    'Candidate' if the file is missing or has no name yet -- e.g. a fresh clone before personalization)."""
    name = ""
    try:
        facts = yaml.safe_load(core.FACTS_PATH.read_text(encoding="utf-8")) or {}
        name = ((facts.get("contact") or {}).get("name") or "").strip()
    except OSError:
        pass
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_") or "Candidate"


def build_order(conn, job_id: str, mode: str, approved_answers: list[dict] | None = None) -> Path:
    if mode not in ("dry_run", "submit"):
        raise ValueError(f"invalid mode {mode!r}")

    job_row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if job_row is None:
        raise ValueError(f"job {job_id!r} not found in jobs table")
    job_row = dict(job_row)
    if mode == "submit" and (job_row["status"] == "applied" or conn.execute(
            "SELECT 1 FROM applications WHERE job_id=? AND status IN ('submitted','applied') LIMIT 1",
            (job_id,)).fetchone()):
        # 2026-10-02: Deliveroo's Full-Stack role received three applications in five minutes from diagnostic
        # re-runs (Greenhouse sent three confirmations). A job with a confirmed submission is never submitted again.
        raise ValueError(f"job {job_id!r} already has a confirmed submission; refusing to build another submit order")

    cv_doc = _latest_document(conn, job_id, "cv", "pdf")
    if cv_doc is None:
        raise ValueError(f"job {job_id!r}: no cv/pdf document recorded in the ledger")
    cover_pdf = _latest_document(conn, job_id, "cover", "pdf")
    if cover_pdf is None:
        raise ValueError(f"job {job_id!r}: no cover/pdf document recorded in the ledger")
    cover_txt = _latest_document(conn, job_id, "cover", "txt")
    if cover_txt is None:
        raise ValueError(f"job {job_id!r}: no cover/txt document recorded in the ledger")

    ts = datetime.now(timezone.utc).astimezone().strftime("%Y%m%dT%H%M%S")
    attempt_dir = core.PIPELINE_ROOT / "work" / "attempts" / job_id / ts
    attempt_dir.mkdir(parents=True, exist_ok=True)

    stem = _candidate_filename_stem()
    cv_pdf_path = core.export_blob(conn, cv_doc["sha256"], attempt_dir / f"CV_{stem}.pdf")
    cover_pdf_path = core.export_blob(conn, cover_pdf["sha256"], attempt_dir / f"Cover_Letter_{stem}.pdf")
    cover_txt_path = core.export_blob(conn, cover_txt["sha256"], attempt_dir / f"Cover_Letter_{stem}.txt")

    bank_path, bank_sha = bank.export(conn)

    order = {
        "schema": 1, "job_id": job_id, "mode": mode,
        "attempt_dir": str(attempt_dir),
        "job": {
            "job_id": job_id, "company": job_row.get("company"), "title": job_row.get("title"),
            "location": job_row.get("location"), "region": job_row.get("region"),
            "url": job_row.get("url"), "apply_url": job_row.get("apply_url"),
            "ats": job_row.get("ats"), "source": job_row.get("source"), "description": job_row.get("description"),
            "previously_applied": _previously_applied(conn, job_row),
        },
        "files": {
            "cv_pdf": str(cv_pdf_path), "cover_pdf": str(cover_pdf_path), "cover_txt": str(cover_txt_path),
        },
        "file_sha256": {
            "cv_pdf": cv_doc["sha256"], "cover_pdf": cover_pdf["sha256"], "cover_txt": cover_txt["sha256"],
        },
        "document_ids": {"cv": cv_doc["id"], "cover": cover_pdf["id"]},
        "answers_path": str(core.ANSWERS_PATH), "facts_path": str(core.FACTS_PATH),
        "bank_path": str(bank_path), "bank_sha256": bank_sha,
        "approved_answers": approved_answers,
    }
    order_path = attempt_dir / "order.json"
    order_path.write_text(json.dumps(order, indent=2, ensure_ascii=False), encoding="utf-8")

    with core.tx(conn):
        core.append_event(conn, "code:orders.build_order", "order.created",
                           {"job_id": job_id, "mode": mode, "attempt_dir": str(attempt_dir),
                            "document_ids": order["document_ids"], "bank_sha256": bank_sha},
                           job_id=job_id)
    return order_path
