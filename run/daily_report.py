"""Write the daily digest from the ledger. Owner: BankRecords.

Usage:
    python pipeline/run/daily_report.py [--db path]

Writes pipeline/work/<YYYY-MM-DD>/report.md and ROOT/MORNING_REPORT.md:
  - applied since the last digest
  - ready jobs awaiting approval (with a `show.py` hint)
  - open questions, grouped by company
  - needs_review jobs with reasons
  - today's LLM spend (cost, calls by actual model, fallback count)
  - `verify_audit` result

Appends a `digest.generated` event so the next run knows where "since last
digest" starts.
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ledger import bank, core

GENESIS_TS = "1970-01-01T00:00:00+00:00"


def _last_digest_ts(conn) -> str:
    row = conn.execute(
        "SELECT ts FROM events WHERE type='digest.generated' ORDER BY seq DESC LIMIT 1").fetchone()
    return row["ts"] if row else GENESIS_TS


def build_report(conn, today_iso: str) -> str:
    since = _last_digest_ts(conn)

    applied = []
    for e in conn.execute(
            "SELECT * FROM events WHERE type='job.status' AND ts > ? ORDER BY ts", (since,)):
        payload = json.loads(e["payload"])
        if payload.get("to") == "applied":
            job = conn.execute("SELECT company, title FROM jobs WHERE id=?", (e["job_id"],)).fetchone()
            applied.append((e["job_id"], job["company"] if job else "?", job["title"] if job else "?"))

    ready = conn.execute("SELECT id, company, title FROM jobs WHERE status='ready' ORDER BY company").fetchall()
    needs_review = conn.execute(
        "SELECT id, company, title, reason FROM jobs WHERE status='needs_review' ORDER BY company").fetchall()
    open_qs = bank.open_questions(conn)

    llm_rows = conn.execute(
        "SELECT actual_model, cost_usd, fallback_errors FROM llm_calls WHERE date(created_at)=date(?)",
        (today_iso,)).fetchall()
    total_cost = sum((r["cost_usd"] or 0.0) for r in llm_rows)
    by_model: dict[str, int] = {}
    fallback_count = 0
    for r in llm_rows:
        model = r["actual_model"] or "unknown"
        by_model[model] = by_model.get(model, 0) + 1
        try:
            if json.loads(r["fallback_errors"] or "[]"):
                fallback_count += 1
        except ValueError:
            pass

    problems = core.verify(conn)

    lines = [f"# Daily report — {today_iso}", ""]

    lines.append(f"## Applied since last digest ({len(applied)})")
    if applied:
        for jid, company, title in applied:
            lines.append(f"- {company} | {title} | `{jid}`")
    else:
        lines.append("- none")
    lines.append("")

    lines.append(f"## Ready — awaiting approval ({len(ready)})")
    if ready:
        for r in ready:
            lines.append(f"- {r['company']} | {r['title']} | `{r['id']}` "
                          f"— review: `python pipeline/run/show.py {r['id']}`, "
                          f"approve: `python pipeline/run/approve.py {r['id']}`")
    else:
        lines.append("- none")
    lines.append("")

    lines.append(f"## Open questions ({len(open_qs)})")
    if open_qs:
        by_company: dict[str, list] = {}
        for q in open_qs:
            job = conn.execute("SELECT company FROM jobs WHERE id=?", (q["job_id"],)).fetchone()
            company = job["company"] if job else "?"
            by_company.setdefault(company, []).append(q)
        for company, qs in sorted(by_company.items()):
            lines.append(f"- **{company}**")
            for q in qs:
                opts = f" options: {json.loads(q['options'])}" if q["options"] else ""
                lines.append(f"  - [{q['id']}] {q['label']!r} (required={bool(q['required'])}){opts}")
    else:
        lines.append("- none")
    lines.append("")

    lines.append(f"## Needs review ({len(needs_review)})")
    if needs_review:
        for r in needs_review:
            lines.append(f"- {r['company']} | {r['title']} | `{r['id']}` — {r['reason'] or '(no reason recorded)'}")
    else:
        lines.append("- none")
    lines.append("")

    lines.append("## LLM spend (today)")
    lines.append(f"- total cost: ${total_cost:.4f}")
    if by_model:
        for model, n in sorted(by_model.items()):
            lines.append(f"- {model}: {n} call(s)")
    else:
        lines.append("- no calls today")
    lines.append(f"- fallbacks: {fallback_count}")
    lines.append("")

    lines.append("## Ledger integrity")
    if problems:
        lines.append(f"- LEDGER PROBLEMS FOUND ({len(problems)}):")
        for p in problems:
            lines.append(f"  - {p}")
    else:
        n_events = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
        n_blobs = conn.execute("SELECT COUNT(*) AS n FROM blobs").fetchone()["n"]
        lines.append(f"- LEDGER OK ({n_events} events, {n_blobs} blobs)")
    lines.append("")

    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Write today's pipeline digest")
    ap.add_argument("--db", default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    conn = core.connect(args.db) if args.db else core.connect()
    today = datetime.date.today()
    today_iso = today.isoformat()

    report = build_report(conn, today_iso)

    out_dir = Path(args.out) if args.out else core.PIPELINE_ROOT / "work" / today_iso
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / "report.md"
    report_path.write_text(report, encoding="utf-8")

    morning_report = core.TRACKER_ROOT / "MORNING_REPORT.md"
    morning_report.write_text(report, encoding="utf-8")

    with core.tx(conn):
        core.append_event(conn, "code:daily_report", "digest.generated", {"date": today_iso})

    print(f"Wrote {report_path}")
    print(f"Wrote {morning_report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
