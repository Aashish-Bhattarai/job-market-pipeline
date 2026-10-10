"""Stage `requirements`: extract structured requirements from a job
description with a small model, verified in code before anything is stored.

Usage: python -m stages.requirements <job_id> [--db path] [--facts path]

CODE verification (every violation fails the attempt): every `quote` must be
a verbatim substring of the description (whitespace/case normalised); every
`fact_id` must be a real citable id in facts.yaml; `kind` must be one of the
fixed enum values; `required` must be boolean. On any violation the stage
retries ONCE with the specific errors appended to the prompt; if still
invalid, the job is set to `needs_review` (reason lists the violations) and
nothing is stored. On success every requirement is inserted as an attested
`requirements` row (extraction = the winning llm_calls.id) via core.record,
and the requirements are NOT re-derived from a stale extraction later —
fit_rules.py always reads the latest one.
"""
from __future__ import annotations

import argparse
import sys

from stages.common import (citable_facts, connect, get_job, load_bank, quote_verified,
                            set_job_status)
from ledger import core, llm

ALLOWED_KINDS = {
    "skill_tool", "experience_years", "degree", "clearance", "phd",
    "location", "language", "soft_skill", "attitude", "duty", "other",
}


def verify_requirements(items, description: str, citable_ids: set[str]) -> list[str]:
    """Return a list of violation strings; empty means the output is valid."""
    problems: list[str] = []
    if not isinstance(items, list) or not items:
        return ["output must be a non-empty JSON list of requirement objects"]
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            problems.append(f"item {i}: not a JSON object")
            continue
        text = item.get("text")
        if not text or not isinstance(text, str):
            problems.append(f"item {i}: missing or empty 'text'")
        quote = item.get("quote")
        if not isinstance(quote, str) or not quote:
            problems.append(f"item {i}: missing or empty 'quote'")
        elif not quote_verified(quote, description):
            problems.append(f"item {i}: quote {quote!r} is not a verbatim substring of the description")
        kind = item.get("kind")
        if kind not in ALLOWED_KINDS:
            problems.append(f"item {i}: invalid kind {kind!r}, must be one of {sorted(ALLOWED_KINDS)}")
        required = item.get("required")
        if not isinstance(required, bool):
            problems.append(f"item {i}: 'required' must be true or false")
        years = item.get("years")
        if years is not None and not isinstance(years, (int, float)):
            problems.append(f"item {i}: 'years' must be a number or null")
        fact_ids = item.get("fact_ids")
        if fact_ids is None:
            fact_ids = []
        if not isinstance(fact_ids, list):
            problems.append(f"item {i}: 'fact_ids' must be a list")
        else:
            for fid in fact_ids:
                if fid not in citable_ids:
                    problems.append(f"item {i}: unknown fact_id {fid!r} (not in the facts list)")
    return problems


def run(job_id: str, db_path=None, facts_path: str | None = None,
        actor: str = "code:requirements") -> dict:
    conn = connect(db_path)
    try:
        job = get_job(conn, job_id)
        if job is None:
            raise ValueError(f"job_id {job_id!r} not found in jobs table")

        bank = load_bank(facts_path)
        facts = citable_facts(bank)
        citable_ids = {f["id"] for f in facts}
        description = job.get("description") or ""
        title = job.get("title") or ""

        errors_text = "(none, first attempt)"
        problems: list[str] = []
        items = None
        res = None
        # The title is part of the posting and the model is shown it ("Job title: ...") -- a requirement
        # quoted from it ("Graduate Machine Learning Researcher - London") is verbatim posting text.
        posting_text = f"{title}\n{description}"
        for attempt in range(3):
            # Third attempt: the small model failed verification twice -> ask the next model in the
            # stage's fallback chain once (2026-10-02: 7 of ~135 jobs ended in needs_review on
            # hallucinated fact ids / invalid kinds / paraphrased quotes).
            res = llm.call("requirements", "requirements",
                            {"title": title, "description": description,
                             "facts": facts, "errors": errors_text},
                            job_id=job_id, actor="llm:requirements", conn=conn,
                            start_at=1 if attempt == 2 else 0)
            if not res.ok:
                problems = [f"llm call failed: {res.error}"]
                items = None
            else:
                try:
                    items = res.json()
                except ValueError as exc:
                    items = None
                    problems = [f"could not parse JSON output: {exc}"]
                else:
                    problems = verify_requirements(items, posting_text, citable_ids)
            if not problems:
                break
            errors_text = "Previous attempt was rejected for:\n" + "\n".join(f"- {p}" for p in problems)

        if problems:
            reason = "requirements extraction failed verification: " + "; ".join(problems)
            with core.tx(conn):
                set_job_status(conn, job_id, "needs_review", actor, reason=reason)
            return {"ok": False, "job_id": job_id, "reason": reason,
                    "call_id": res.call_id if res else None}

        extraction = res.call_id
        with core.tx(conn):
            for idx, item in enumerate(items):
                core.record(conn, "requirements", {
                    "job_id": job_id, "extraction": extraction, "idx": idx,
                    "text": item["text"], "quote": item["quote"], "kind": item["kind"],
                    "required": 1 if item["required"] else 0,
                    "years": item.get("years"),
                    "fact_ids": core.canonical(item.get("fact_ids") or []),
                    "created_at": core.now(),
                }, actor=actor, event_type="requirement.recorded", job_id=job_id)

        return {"ok": True, "job_id": job_id, "count": len(items), "extraction": extraction,
                "actual_model": res.actual_model}
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="Extract requirements for one job")
    ap.add_argument("job_id")
    ap.add_argument("--db", default=None)
    ap.add_argument("--facts", default=None)
    args = ap.parse_args()

    try:
        result = run(args.job_id, args.db, args.facts)
    except Exception as exc:  # crash/infra error -> non-zero exit
        print(f"ERROR: {exc}")
        return 1

    if result["ok"]:
        print(f"OK: {result['job_id']} -> {result['count']} requirement(s) "
              f"(extraction {result['extraction']}, model {result['actual_model']})")
    else:
        print(f"NEEDS_REVIEW: {result['job_id']}: {result['reason']}")
    return 0  # stage completed either way; only crashes are non-zero


if __name__ == "__main__":
    sys.exit(main())
