"""Answer bank workflow CLI (the human side of the digest). Owner: BankRecords.

Usage:
    python pipeline/run/answer_question.py list
    python pipeline/run/answer_question.py answer <queue_id> --text "..." --scope global|company|job
    python pipeline/run/answer_question.py retire <bank_id> --reason "..."
    python pipeline/run/answer_question.py supersede <bank_id> --text "..."
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ledger import bank, core


def cmd_list(conn) -> int:
    rows = bank.open_questions(conn)
    if not rows:
        print("No open questions.")
        return 0
    for r in rows:
        job = conn.execute("SELECT company, title FROM jobs WHERE id=?", (r["job_id"],)).fetchone()
        company = job["company"] if job else "?"
        title = job["title"] if job else "?"
        options = json.loads(r["options"]) if r["options"] else None
        print(f"[{r['id']}] {company} / {title} — {r['label']!r}")
        print(f"    required={bool(r['required'])} category={r['category'] or '-'} created={r['created_at']}")
        if options:
            print(f"    options: {options}")
    return 0


def _requeue_matching_jobs(conn, actor: str) -> list[str]:
    """After resolving queue rows, move any job whose required questions are
    now all resolved back from queued_manual to tailored."""
    moved = []
    for job in conn.execute("SELECT id, status FROM jobs WHERE status='queued_manual'"):
        remaining = conn.execute(
            "SELECT COUNT(*) AS n FROM question_queue WHERE job_id=? AND required=1 AND status='open'",
            (job["id"],)).fetchone()["n"]
        has_any_required = conn.execute(
            "SELECT COUNT(*) AS n FROM question_queue WHERE job_id=? AND required=1",
            (job["id"],)).fetchone()["n"]
        if has_any_required and remaining == 0:
            conn.execute("UPDATE jobs SET status='tailored' WHERE id=?", (job["id"],))
            core.append_event(conn, actor, "job.status",
                               {"from": "queued_manual", "to": "tailored",
                                "reason": "all required questions resolved"}, job_id=job["id"])
            moved.append(job["id"])
    return moved


def cmd_answer(conn, queue_id: int, text: str, scope: str) -> int:
    actor = "human"
    row = conn.execute("SELECT * FROM question_queue WHERE id=?", (queue_id,)).fetchone()
    if row is None:
        print(f"no open question {queue_id}", file=sys.stderr)
        return 1
    row = dict(row)
    job = conn.execute("SELECT company FROM jobs WHERE id=?", (row["job_id"],)).fetchone()
    company = job["company"] if (job and scope == "company") else None
    job_id = row["job_id"] if scope == "job" else None

    with core.tx(conn):
        bank_id = bank.add_answer(conn, row["label"], text, scope, company=company, job_id=job_id,
                                   created_by="human", approved_by="human", actor=actor)
        bank.add_alias(conn, row["question_norm"], bank_id, mapped_by="human", actor=actor)
        resolved = [queue_id]
        bank.resolve_question(conn, queue_id, bank_id, actor)
        # resolve every other open question whose question_norm now matches this entry
        candidates_norms = {row["question_norm"]}
        for other in conn.execute("SELECT * FROM question_queue WHERE status='open' AND id != ?", (queue_id,)):
            if scope == "job" and other["job_id"] != row["job_id"]:
                continue
            if scope == "company":
                ojob = conn.execute("SELECT company FROM jobs WHERE id=?", (other["job_id"],)).fetchone()
                if not ojob or core.norm(ojob["company"]) != core.norm(company):
                    continue
            if other["question_norm"] in candidates_norms:
                bank.resolve_question(conn, other["id"], bank_id, actor)
                resolved.append(other["id"])
        moved = _requeue_matching_jobs(conn, actor)

    bank.export(conn)
    print(f"bank entry {bank_id} added (scope={scope}); resolved queue rows: {resolved}")
    if moved:
        print(f"jobs moved queued_manual -> tailored: {moved}")
    return 0


def cmd_retire(conn, bank_id: int, reason: str) -> int:
    with core.tx(conn):
        bank.retire(conn, bank_id, "human", reason)
    bank.export(conn)
    print(f"bank entry {bank_id} retired: {reason}")
    return 0


def cmd_supersede(conn, bank_id: int, text: str) -> int:
    old = conn.execute("SELECT * FROM answer_bank WHERE id=?", (bank_id,)).fetchone()
    if old is None:
        print(f"no answer_bank row {bank_id}", file=sys.stderr)
        return 1
    old = dict(old)
    with core.tx(conn):
        new_id = bank.add_answer(
            conn, old["question"], text, old["scope"], company=old["company_norm"], job_id=old["job_id"],
            created_by="human", approved_by="human", supersedes_id=bank_id, actor="human")
    bank.export(conn)
    print(f"bank entry {bank_id} superseded by {new_id}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Answer bank workflow")
    ap.add_argument("--db", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    p_answer = sub.add_parser("answer")
    p_answer.add_argument("queue_id", type=int)
    p_answer.add_argument("--text", required=True)
    p_answer.add_argument("--scope", required=True, choices=["global", "company", "job"])
    p_retire = sub.add_parser("retire")
    p_retire.add_argument("bank_id", type=int)
    p_retire.add_argument("--reason", required=True)
    p_supersede = sub.add_parser("supersede")
    p_supersede.add_argument("bank_id", type=int)
    p_supersede.add_argument("--text", required=True)
    p_dismiss = sub.add_parser("dismiss")
    p_dismiss.add_argument("queue_id", type=int)
    p_dismiss.add_argument("--reason", required=True)
    args = ap.parse_args()

    conn = core.connect(args.db) if args.db else core.connect()
    if args.cmd == "list":
        return cmd_list(conn)
    if args.cmd == "answer":
        return cmd_answer(conn, args.queue_id, args.text, args.scope)
    if args.cmd == "retire":
        return cmd_retire(conn, args.bank_id, args.reason)
    if args.cmd == "supersede":
        return cmd_supersede(conn, args.bank_id, args.text)
    if args.cmd == "dismiss":
        with core.tx(conn):
            bank.dismiss_question(conn, args.queue_id, "human", args.reason)
        print(f"dismissed question {args.queue_id}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
