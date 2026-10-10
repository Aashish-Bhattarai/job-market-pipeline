"""The answer bank: reusable form answers, scoped job > company > global.

Active = not retired and not superseded by an (active) row. All mutating
functions here assume they run inside the caller's `core.tx(conn)` block,
except `export()` which manages its own transaction for its one event.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import core

def _export_path() -> Path:
    return core.PIPELINE_ROOT / "data" / "answer_bank.json"

_SCOPE_RANK = {"job": 0, "company": 1, "global": 2}


def _norm(text: str | None) -> str:
    return core.norm(text)


def _company_norm(company: str | None) -> str | None:
    return core.norm(company) if company else None


def _all_rows(conn) -> dict[int, dict]:
    return {r["id"]: dict(r) for r in conn.execute("SELECT * FROM answer_bank")}


def _active_rows(conn) -> list[dict]:
    """Every row that is not retired and not superseded by any row.

    Once superseded, always superseded — even if the row that superseded this
    one is later retired. Retiring a replacement must never resurrect the
    answer it replaced (the original rejection reason still applies); the
    auditable way to change an answer is always a new superseding row.
    """
    rows = _all_rows(conn)
    superseded = {r["supersedes_id"] for r in rows.values()
                  if r["supersedes_id"] is not None}
    return [r for r in rows.values() if r["retired"] == 0 and r["id"] not in superseded]


_ALIAS_STOP = {"what", "is", "are", "you", "your", "the", "an", "of", "to", "do", "does", "did", "have", "has", "for",
               "with", "and", "or", "please", "select", "enter", "this", "that", "any", "from", "how", "would", "describe",
               "tell", "about", "our", "can", "was", "were", "will", "why", "when", "which", "who", "whom", "there"}


def _alias_tokens(norm: str) -> set[str]:
    out = set()
    for w in (norm or "").split():
        if len(w) < 3 or w in _ALIAS_STOP:
            continue
        if w.endswith("ies") and len(w) > 4:
            w = w[:-3] + "y"
        elif w.endswith("ing") and len(w) > 5:
            w = w[:-3]
        elif w.endswith("es") and len(w) > 4:
            w = w[:-2]
        elif w.endswith("s") and len(w) > 3:
            w = w[:-1]
        out.add(w)
    return out


def alias_plausible(alias_norm: str, entry_question_norm: str) -> bool:
    """A MODEL-made alias is trusted only when the new question shares real wording with the bank entry's own
    question (Jaccard >= 0.5 over content words). The small mapping model once aliased "Start date year" to the
    DATE OF BIRTH entry, "A levels or equivalent" to the academic-background essay and "Have you used ElevenLabs?"
    to a generic AI-work essay (2026-10-02); a free-text mapping had no check at all. Human aliases are never gated.
    A false negative just falls back to the rules / the audited essay path / a human."""
    a, q = _alias_tokens(alias_norm), _alias_tokens(entry_question_norm)
    if not a or not q:
        return False
    return len(a & q) / len(a | q) >= 0.5


def _alias_trusted(mapped_by: str | None) -> bool:
    """Human aliases, and model aliases for CHOICE fields (the answer must equal one of the field's real options,
    checked by the mapper and again by bankLookup), need no lexical check; free-text model aliases do."""
    mb = str(mapped_by or "")
    return not mb.startswith("model:") or mb.endswith(":choice")


def _alias_norms(conn, bank_id: int) -> set[str]:
    entry = conn.execute("SELECT question_norm FROM answer_bank WHERE id=?", (bank_id,)).fetchone()
    entry_norm = entry["question_norm"] if entry else ""
    return {r["question_norm"] for r in
            conn.execute("SELECT question_norm, mapped_by FROM answer_aliases WHERE bank_id=?", (bank_id,))
            if _alias_trusted(r["mapped_by"]) or alias_plausible(r["question_norm"], entry_norm)}


def candidates(conn, job_id: str | None, company: str | None) -> list[dict]:
    """Active entries applicable to this job: its own job-scope entries, its
    company's company-scope entries, and every global entry."""
    company_norm = _company_norm(company)
    out = []
    for r in _active_rows(conn):
        if r["scope"] == "job" and job_id is not None and r["job_id"] == job_id:
            out.append(r)
        elif r["scope"] == "company" and company_norm is not None and r["company_norm"] == company_norm:
            out.append(r)
        elif r["scope"] == "global":
            out.append(r)
    return out


def lookup(conn, label: str, job_id: str | None, company: str | None) -> dict | None:
    """Best matching active bank entry for a form field label: job scope wins
    over company scope wins over global; match on question_norm or any alias."""
    label_norm = _norm(label)
    best = None
    for r in candidates(conn, job_id, company):
        norms = {r["question_norm"]} | _alias_norms(conn, r["id"])
        if label_norm not in norms:
            continue
        if best is None:
            best = r
            continue
        rank, best_rank = _SCOPE_RANK[r["scope"]], _SCOPE_RANK[best["scope"]]
        if rank < best_rank or (rank == best_rank and r["id"] > best["id"]):
            best = r
    return best


def add_answer(conn, question: str, answer: str, scope: str, *, company: str | None = None,
               job_id: str | None = None, created_by: str, approved_by: str,
               fact_ids=(), llm_call_ids=(), supersedes_id: int | None = None, actor: str) -> int:
    if scope not in ("global", "company", "job"):
        raise ValueError(f"invalid scope {scope!r}")
    company_norm = _company_norm(company) if scope == "company" else None
    jid = job_id if scope == "job" else None
    row = {
        "question": question, "question_norm": _norm(question), "answer": answer,
        "scope": scope, "company_norm": company_norm, "job_id": jid,
        "created_by": created_by, "approved_by": approved_by,
        "fact_ids": core.canonical(list(fact_ids)), "llm_call_ids": core.canonical(list(llm_call_ids)),
        "supersedes_id": supersedes_id, "retired": 0, "created_at": core.now(),
    }
    new_id = core.record(conn, "answer_bank", row, actor=actor, event_type="bank.answer_added", job_id=jid)
    if supersedes_id is not None:
        core.append_event(conn, actor, "bank.answer_superseded",
                           {"old_id": supersedes_id, "new_id": new_id}, job_id=jid)
    return new_id


def add_alias(conn, question_norm: str, bank_id: int, mapped_by: str, actor: str) -> None:
    row = {"question_norm": _norm(question_norm), "bank_id": bank_id,
           "mapped_by": mapped_by, "created_at": core.now()}
    core.record(conn, "answer_aliases", row, actor=actor, event_type="bank.alias_added")


def retire(conn, bank_id: int, actor: str, reason: str) -> None:
    row = conn.execute("SELECT id FROM answer_bank WHERE id=?", (bank_id,)).fetchone()
    if row is None:
        raise KeyError(f"no answer_bank row {bank_id}")
    conn.execute("UPDATE answer_bank SET retired=1 WHERE id=?", (bank_id,))
    core.append_event(conn, actor, "bank.answer_retired", {"id": bank_id, "reason": reason})


def open_questions(conn) -> list[dict]:
    return [dict(r) for r in
            conn.execute("SELECT * FROM question_queue WHERE status='open' ORDER BY created_at, id")]


def resolve_question(conn, queue_id: int, bank_id: int | None, actor: str) -> None:
    row = conn.execute("SELECT * FROM question_queue WHERE id=?", (queue_id,)).fetchone()
    if row is None:
        raise KeyError(f"no question_queue row {queue_id}")
    now = core.now()
    conn.execute("UPDATE question_queue SET status='resolved', bank_id=?, resolved_at=? WHERE id=?",
                 (bank_id, now, queue_id))
    core.append_event(conn, actor, "queue.resolved", {"queue_id": queue_id, "bank_id": bank_id},
                       job_id=row["job_id"])


def dismiss_question(conn, queue_id: int, actor: str, reason: str) -> None:
    """Close a queue row that needs no answer (e.g. its job was already
    submitted, or it is a file-upload slot, not a question). Inside tx()."""
    row = conn.execute("SELECT * FROM question_queue WHERE id=?", (queue_id,)).fetchone()
    if row is None:
        raise KeyError(f"no question_queue row {queue_id}")
    conn.execute("UPDATE question_queue SET status='dismissed', resolved_at=? WHERE id=?",
                 (core.now(), queue_id))
    core.append_event(conn, actor, "queue.dismissed", {"queue_id": queue_id, "reason": reason},
                      job_id=row["job_id"])


def export(conn) -> tuple[Path, str]:
    """Write pipeline/data/answer_bank.json (active entries only) and append a
    bank.exported event carrying its sha256. Manages its own transaction."""
    entries = []
    for r in sorted(_active_rows(conn), key=lambda r: r["id"]):
        aliases = sorted(_alias_norms(conn, r["id"]) | {r["question_norm"]})
        entries.append({
            "id": r["id"], "question": r["question"], "question_norm": r["question_norm"],
            "answer": r["answer"], "scope": r["scope"], "company_norm": r["company_norm"],
            "job_id": r["job_id"], "aliases": aliases,
        })
    sha = core.sha256_text(core.canonical(entries))
    doc = {"schema": 1, "exported_at": core.now(), "sha256": sha, "entries": entries}
    export_path = _export_path()
    export_path.parent.mkdir(parents=True, exist_ok=True)
    export_path.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
    with core.tx(conn):
        core.append_event(conn, "code:bank.export", "bank.exported", {"sha256": sha, "count": len(entries)})
    return export_path, sha


def enqueue(conn, job_id: str, application_id: int | None, label: str, type: str | None,
            options, required, actor: str) -> int:
    """Upsert on (job_id, question_norm). A dismissed row is left alone (a dismissal is a human decision, not
    something the next attempt should undo). A RESOLVED row is re-opened: a later attempt still could not answer this
    label, so whatever resolved it no longer serves it (its bank entry was superseded or retired, or its model alias
    stopped passing the plausibility gate); left alone, the job stays held on a row everyone believes is done
    (ElevenLabs Full-Stack, 2026-10-02). If the bank does still serve it, the questions stage re-resolves it for free."""
    qn = _norm(label)
    opts_json = core.canonical(list(options)) if options else None
    required_int = int(bool(required))
    existing = conn.execute("SELECT * FROM question_queue WHERE job_id=? AND question_norm=?",
                             (job_id, qn)).fetchone()
    if existing is None:
        cur = conn.execute(
            "INSERT INTO question_queue(job_id, application_id, label, question_norm, type, options, "
            "required, category, status, bank_id, created_at, resolved_at) "
            "VALUES(?,?,?,?,?,?,?,NULL,'open',NULL,?,NULL)",
            (job_id, application_id, label, qn, type, opts_json, required_int, core.now()))
        qid = cur.lastrowid
        core.append_event(conn, actor, "queue.enqueued",
                           {"queue_id": qid, "label": label, "question_norm": qn}, job_id=job_id)
        return qid
    if existing["status"] != "dismissed":
        reopen = existing["status"] == "resolved"
        conn.execute(
            "UPDATE question_queue SET application_id=?, label=?, type=?, options=?, required=?" +
            (", status='open', bank_id=NULL, resolved_at=NULL" if reopen else "") + " WHERE id=?",
            (application_id, label, type, opts_json, required_int, existing["id"]))
        core.append_event(conn, actor, "queue.reopened" if reopen else "queue.reenqueued",
                           {"queue_id": existing["id"], "label": label, "question_norm": qn}, job_id=job_id)
    return existing["id"]


def set_category(conn, queue_id: int, category: str, actor: str) -> None:
    row = conn.execute("SELECT job_id FROM question_queue WHERE id=?", (queue_id,)).fetchone()
    if row is None:
        raise KeyError(f"no question_queue row {queue_id}")
    conn.execute("UPDATE question_queue SET category=? WHERE id=?", (category, queue_id))
    core.append_event(conn, actor, "queue.category_set", {"queue_id": queue_id, "category": category},
                       job_id=row["job_id"])
