"""Regenerate the human-browsable views of the ledger. Never hand-edited.

    python pipeline/run/views.py

Writes:
  - ROOT/ANSWERS_LOG.md      -- every job with any application, every attempt,
                                every field label -> value -> source, screenshots,
                                confirmation.
  - pipeline/out/<job_id>/ANSWERS.md -- the same content for one job.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ledger import core

OUT_ROOT = core.PIPELINE_ROOT / "out"
JOB_ID_RE = re.compile(r"[0-9a-f]{40}")


def _fmt_value(v) -> str:
    if v is None:
        return ""
    try:
        val = json.loads(v) if isinstance(v, str) else v
    except (ValueError, TypeError):
        val = v
    if isinstance(val, (list, dict)):
        return json.dumps(val, ensure_ascii=False)
    s = str(val)
    return s.replace("\n", " ").replace("|", "\\|")


def render_application(conn, app: dict) -> list[str]:
    lines = [f"### Attempt {app['attempt']} — {app['mode']} — {app['status']}", "",
              f"- Started: {app['started_at'] or '-'}  Finished: {app['finished_at'] or '-'}",
              f"- Apply URL: {app['apply_url'] or '-'}"]
    if app["note"]:
        lines.append(f"- Note: {app['note']}")
    lines.append("")
    rows = conn.execute(
        "SELECT * FROM form_answers WHERE application_id=? ORDER BY idx", (app["id"],)).fetchall()
    if rows:
        lines += ["| Field | Value | Source | Verified |", "|---|---|---|---|"]
        for r in rows:
            src = r["source_kind"] + (f":{r['source_ref']}" if r["source_ref"] else "")
            verified = "yes" if r["verified"] else ("no" if r["verified"] is not None else "-")
            lines.append(f"| {r['label'] or ''} | {_fmt_value(r['value'])} | {src} | {verified} |")
        lines.append("")

    shots = []
    if app["screenshot_prefill_sha256"]:
        try:
            shots.append(("prefill", core.blob_path(conn, app["screenshot_prefill_sha256"])))
        except KeyError:
            pass
    if app["screenshot_confirm_sha256"]:
        try:
            shots.append(("confirm", core.blob_path(conn, app["screenshot_confirm_sha256"])))
        except KeyError:
            pass
    if shots:
        lines.append("Screenshots: " + ", ".join(f"{name}: `{path}`" for name, path in shots))
        lines.append("")
    if app["confirmation_text"]:
        lines += ["Confirmation:", "", "> " + app["confirmation_text"].replace("\n", "\n> "), ""]
    return lines


def render_job(conn, job_id: str) -> str:
    job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    company = job["company"] if job else "?"
    title = job["title"] if job else "?"
    apps = conn.execute(
        "SELECT * FROM applications WHERE job_id=? ORDER BY attempt", (job_id,)).fetchall()
    lines = [f"# {company} — {title}", "", f"**Job ID:** `{job_id}`", ""]
    for app in apps:
        lines += render_application(conn, dict(app))
    return "\n".join(lines)


def job_ids_with_applications(conn) -> list[str]:
    rows = conn.execute("SELECT DISTINCT job_id FROM applications ORDER BY job_id").fetchall()
    return [r["job_id"] for r in rows if JOB_ID_RE.fullmatch(r["job_id"] or "")]


def regenerate(conn) -> list[str]:
    job_ids = job_ids_with_applications(conn)
    docs = []
    for job_id in job_ids:
        md = render_job(conn, job_id)
        job_dir = OUT_ROOT / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "ANSWERS.md").write_text(md, encoding="utf-8")
        docs.append(md)

    out = ["# Answers log — every job, every answer given", "",
           "Regenerated automatically by `pipeline/run/views.py` from the ledger. Do not edit by hand.", ""]
    for md in docs:
        out.append(md)
        out.append("\n---\n")
    (core.TRACKER_ROOT / "ANSWERS_LOG.md").write_text("\n".join(out), encoding="utf-8")
    return job_ids


def main() -> int:
    ap = argparse.ArgumentParser(description="Regenerate ANSWERS_LOG.md and out/<job_id>/ANSWERS.md")
    ap.add_argument("--db", default=None)
    args = ap.parse_args()
    conn = core.connect(args.db) if args.db else core.connect()
    job_ids = regenerate(conn)
    print(f"Regenerated ANSWERS_LOG.md and {len(job_ids)} out/<job_id>/ANSWERS.md file(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
