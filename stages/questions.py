"""Stage `questions`: resolve every OPEN `question_queue` row against the
answer bank -- exact/alias match, then a small-model mapper (code-checked
against the actual candidate list and, for choice fields, the actual
options), then a small-model classifier, and finally (fact_answerable, free
text, config-enabled) a mid-model essay audited by a different, stronger
model before it's stored. Anything that doesn't clear a gate stays open for
the operator.

Usage: python -m stages.questions [--db path] [--facts path]
"""
from __future__ import annotations

import argparse
import json
import sys

from stages.common import citable_facts, connect, get_job, load_bank
from ledger import bank, core, llm

CHOICE_TYPES = {"select", "radio", "checkbox", "checkbox-group", "combobox", "multiselect"}


def _options(row: dict) -> list[str]:
    raw = row.get("options")
    if not raw:
        return []
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return []


def _is_choice(row: dict) -> bool:
    return (row.get("type") or "").strip().lower() in CHOICE_TYPES


def _essay_eligible(row: dict) -> bool:
    """Prose goes only into a textarea. The essay path once wrote an education sentence into an email box ("university email")
    and a 100-word essay into a short text field ("How long have you been using React?")."""
    return (row.get("type") or "").strip().lower() == "textarea"


def _option_match(answer, options: list[str]) -> bool:
    if not isinstance(answer, str):
        return False
    a = answer.strip().lower()
    return any(a == (o or "").strip().lower() for o in options)


def _try_bank_lookup(conn, row: dict, company: str | None, actor: str) -> bool:
    hit = bank.lookup(conn, row["label"], row["job_id"], company)
    if hit is None:
        return False
    with core.tx(conn):
        bank.resolve_question(conn, row["id"], hit["id"], actor)
    return True


def _try_map_question(conn, row: dict, company: str | None, actor: str) -> bool:
    cands = bank.candidates(conn, row["job_id"], company)
    if not cands:
        return False
    options = _options(row)
    res = llm.call("map_question", "map_question", {
        "question": row["label"], "options": options,
        "candidates": [{"id": c["id"], "question": c["question"], "answer": c["answer"]} for c in cands],
    }, job_id=row["job_id"], actor="llm:map_question", conn=conn)
    if not res.ok:
        return False
    try:
        doc = res.json()
    except ValueError:
        return False
    if not isinstance(doc, dict):
        return False
    bank_id = doc.get("bank_id")
    if bank_id is None:
        return False
    cand_by_id = {c["id"]: c for c in cands}
    cand = cand_by_id.get(bank_id)
    if cand is None:
        return False  # model picked an id outside the offered candidates: rejected
    if _is_choice(row) and not _option_match(cand["answer"], options):
        return False  # choice answer must equal one of the actual options
    choice = _is_choice(row)
    if not choice and not bank.alias_plausible(row["question_norm"], cand["question_norm"]):
        # free text has no option check: the model once mapped "Start date year" to the date-of-birth entry
        return False
    with core.tx(conn):
        bank.add_alias(conn, row["question_norm"], bank_id,
                       mapped_by=f"model:{res.call_id}" + (":choice" if choice else ""), actor=actor)
        bank.resolve_question(conn, row["id"], bank_id, actor)
    return True


def _classify(conn, row: dict, job: dict, actor: str) -> str | None:
    res = llm.call("classify_question", "classify_question", {
        "question": row["label"], "options": _options(row),
        "title": job.get("title") or "", "description": job.get("description") or "",
    }, job_id=row["job_id"], actor="llm:classify_question", conn=conn)
    if not res.ok:
        return None
    try:
        doc = res.json()
    except ValueError:
        return None
    category = doc.get("category") if isinstance(doc, dict) else None
    if category not in ("personal", "fact_answerable"):
        return None
    with core.tx(conn):
        bank.set_category(conn, row["id"], category, actor)
    return category


def _try_essay(conn, row: dict, job: dict, facts: list[dict], citable_ids: set[str], actor: str) -> bool:
    def draft(errors_text=None):
        return llm.call("essay", "essay", {
            "question": row["label"], "title": job.get("title") or "",
            "description": job.get("description") or "", "facts": facts,
            "errors": errors_text or "(none: this is the first attempt)",
        }, job_id=row["job_id"], actor="llm:essay", conn=conn)
    res = draft()
    if not res.ok:
        return False
    try:
        doc = res.json()
    except ValueError:
        return False
    if not isinstance(doc, dict):
        return False
    answer = doc.get("answer")
    fact_ids = doc.get("fact_ids")
    if not isinstance(answer, str) or not answer.strip():
        return False
    if not isinstance(fact_ids, list) or not fact_ids:
        return False
    if any(fid not in citable_ids for fid in fact_ids):
        return False

    content = [{"where": "essay", "text": answer, "fact_ids": fact_ids}]
    audit_res = llm.call("auditor", "audit", {
        "facts": facts, "description": job.get("description") or "", "content": content,
    }, job_id=row["job_id"], actor="llm:auditor", conn=conn)
    if not audit_res.ok:
        return False
    try:
        audit_doc = audit_res.json()
    except ValueError:
        return False
    if not isinstance(audit_doc, dict):
        return False
    passed = audit_doc.get("pass") is True
    problems = audit_doc.get("problems") or []
    if problems:
        passed = False
    if audit_res.actual_model == res.actual_model:
        passed = False  # auditor must be a different model from the essay writer
    if not passed:
        # One redraft with the auditor's exact problems fed back (the tailor
        # pattern): q88's first draft failed on a single fact-mix sentence the
        # feedback names verbatim; without this the question waits on the operator
        # forever over a mechanical fix.
        if not problems:
            return False
        feedback = "; ".join(f"{p.get('where', '')}: {p.get('sentence', '')[:120]} -- {p.get('why', '')[:180]}"
                             for p in problems if isinstance(p, dict))[:1200]
        res = draft(feedback)
        if not res.ok:
            return False
        try:
            doc = res.json()
        except ValueError:
            return False
        if not isinstance(doc, dict):
            return False
        answer = doc.get("answer")
        fact_ids = doc.get("fact_ids")
        if not isinstance(answer, str) or not answer.strip():
            return False
        if not isinstance(fact_ids, list) or not fact_ids or any(fid not in citable_ids for fid in fact_ids):
            return False
        content = [{"where": "essay", "text": answer, "fact_ids": fact_ids}]
        audit_res = llm.call("auditor", "audit", {
            "facts": facts, "description": job.get("description") or "", "content": content,
        }, job_id=row["job_id"], actor="llm:auditor", conn=conn)
        if not audit_res.ok:
            return False
        try:
            audit_doc = audit_res.json()
        except ValueError:
            return False
        if not isinstance(audit_doc, dict) or audit_doc.get("pass") is not True:
            return False
        if (audit_doc.get("problems") or []) or audit_res.actual_model == res.actual_model:
            return False
        problems = []

    with core.tx(conn):
        new_id = bank.add_answer(conn, row["label"], answer, "job", job_id=row["job_id"],
                                  created_by="model", approved_by=f"audit:{audit_res.call_id}",
                                  fact_ids=fact_ids, llm_call_ids=[res.call_id, audit_res.call_id],
                                  actor=actor)
        bank.resolve_question(conn, row["id"], new_id, actor)
    return True


def _bank_grew_since_classified(conn, queue_id: int) -> bool:
    """True when the bank gained entries since this row was last classified OR last offered to the mapper."""
    last_cat = conn.execute(
        "SELECT MAX(seq) FROM events WHERE type IN ('queue.category_set', 'queue.mapping_tried') "
        "AND json_extract(payload, '$.queue_id')=?", (queue_id,)).fetchone()[0]
    if last_cat is None:
        return True
    last_bank = conn.execute(
        "SELECT MAX(seq) FROM events WHERE json_extract(payload, '$.table') IN "
        "('answer_bank', 'answer_aliases')").fetchone()[0]
    return last_bank is not None and last_bank > last_cat


def run(db_path=None, facts_path: str | None = None, actor: str = "code:questions") -> dict:
    conn = connect(db_path)
    try:
        cfg = llm.load_config()
        gen_essays = bool(cfg.get("generate_fact_grounded_answers"))
        bank_module_bank = load_bank(facts_path)
        facts = citable_facts(bank_module_bank)
        citable_ids = {f["id"] for f in facts}

        resolved, personal, essayed, unresolved, dismissed, waiting = 0, 0, 0, 0, 0, 0
        for row in bank.open_questions(conn):
            job = get_job(conn, row["job_id"]) or {}
            company = job.get("company")

            # A job that is no longer pursued needs no answer. Re-mapping its rows through an LLM on every bank change cost ~11
            # minutes per batch (94 open rows, 23 of them on skipped jobs).
            if job.get("status") in ("applied", "skipped", "filtered_out", "fit_fail"):
                reason = ("job already applied; question no longer needs an answer" if job["status"] == "applied"
                          else f"job is {job['status']}: not pursued, question needs no answer")
                with core.tx(conn):
                    bank.dismiss_question(conn, row["id"], actor, reason)
                dismissed += 1
                continue

            # Free, every batch: an exact/alias bank hit (e.g. the operator answered it).
            if _try_bank_lookup(conn, row, company, actor):
                resolved += 1
                continue

            # Paid steps run on first sight only; model mapping re-runs only
            # when the bank gained entries since this row was classified.
            first_pass = row["category"] is None
            if _bank_grew_since_classified(conn, row["id"]):
                if _try_map_question(conn, row, company, actor):
                    resolved += 1
                    continue
                # Not placeable on today's bank: leave a marker so the model is not asked again until the bank changes
                # (69 held rows cost 73 model calls and ~9 minutes in EVERY batch, three times a batch).
                with core.tx(conn):
                    core.append_event(conn, actor, "queue.mapping_tried", {"queue_id": row["id"]}, job_id=row["job_id"])
            if not first_pass:
                waiting += 1
                continue

            category = _classify(conn, row, job, actor)
            if category == "personal":
                personal += 1
                continue
            if category == "fact_answerable" and gen_essays and _essay_eligible(row):
                if _try_essay(conn, row, job, facts, citable_ids, actor):
                    essayed += 1
                    continue
                # Drafted answer failed validation/audit: never retried
                # automatically (it would re-spend mid+strong model calls
                # every batch); it waits for the operator.
                with core.tx(conn):
                    bank.set_category(conn, row["id"], "essay_failed", actor)
            unresolved += 1

        return {"resolved": resolved, "personal": personal, "essayed": essayed,
                "left_open": unresolved, "dismissed": dismissed, "waiting_for_human": waiting}
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="Resolve every OPEN question_queue row")
    ap.add_argument("--db", default=None)
    ap.add_argument("--facts", default=None)
    args = ap.parse_args()

    try:
        result = run(args.db, args.facts)
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 1

    print(f"OK: resolved={result['resolved']} personal={result['personal']} "
          f"essayed={result['essayed']} left_open={result['left_open']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
