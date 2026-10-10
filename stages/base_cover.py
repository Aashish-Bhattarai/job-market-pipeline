"""Draft and audit the BASE cover letter core (one-time, regenerable).

Usage:
    python -m stages.base_cover [--force]

The operator asked for a base cover letter drafted by a cheap model that
per-role letters then tweak slightly. The audited artefact is
`pipeline/profile/base_cover_letter.json`: 2–3 fact-grounded core paragraphs
(company-agnostic, verbatim-reusable) + the standard close. Per-role letters
(write stage) write only the opening paragraph and pick which core paragraphs
to include; the per-role Opus audit still checks every final letter.

The chosen base CV document (`profile/base_cv.pdf`) is recorded in the ledger
alongside this artefact. Re-run with --force after a major facts.yaml revision.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from stages.common import PIPELINE_ROOT, citable_facts, connect, load_bank
from ledger import core, llm

OUT_PATH = PIPELINE_ROOT / "profile" / "base_cover_letter.json"
BASE_CV_SOURCE = PIPELINE_ROOT / "profile" / "base_cv.pdf"
MAX_ATTEMPTS = 3

# Facts chosen for the base letter: education + the strongest, broadly
# relevant pieces of work. Replace these ids with ones from your own
# facts.yaml -- any UK ML/AI/DS employer should find at least two of the
# three core paragraphs relevant.
BASE_FACT_IDS = [
    "wessex-msc", "northfield-bsc",
    "exampletech.doc-intel", "sidecorp.gbm", "roadmap-tool.summary",
]


def _validate_shape(doc) -> list[str]:
    problems = []
    if not isinstance(doc, dict):
        return ["output must be a JSON object"]
    paras = doc.get("core_paragraphs")
    if not isinstance(paras, list) or not (2 <= len(paras) <= 3):
        return ["core_paragraphs must be a list of 2-3 paragraphs"]
    allowed = set(BASE_FACT_IDS)
    for i, p in enumerate(paras):
        if not isinstance(p, dict) or not isinstance(p.get("text"), str) or not p["text"].strip():
            problems.append(f"core_paragraphs[{i}]: missing text")
            continue
        fids = p.get("fact_ids")
        if not isinstance(fids, list) or not fids:
            problems.append(f"core_paragraphs[{i}]: no fact_ids")
        elif any(f not in allowed for f in fids):
            problems.append(f"core_paragraphs[{i}]: fact_ids outside the base set")
    close = doc.get("close")
    if not (isinstance(close, dict) and close.get("kind") == "close"
            and close.get("fact_ids") == [] and "one month" in (close.get("text") or "")):
        problems.append("close must be the standard one-month close with kind=close")
    return problems


def run(force: bool = False, actor: str = "code:base_cover") -> dict:
    if OUT_PATH.exists() and not force:
        return {"ok": True, "skipped": True, "path": str(OUT_PATH)}

    conn = connect()
    try:
        bank = load_bank()
        facts = [f for f in citable_facts(bank) if f["id"] in BASE_FACT_IDS]
        rules = (PIPELINE_ROOT / "profile" / "cover_template.md").read_text(encoding="utf-8")

        llm_call_ids: list[int] = []
        errors_text = "(none, first attempt)"
        doc = None
        problems: list[str] = []
        audit_problems: list[str] = []
        passed = False
        writer_model = None
        audit_res = None

        for round_num in range(1, MAX_ATTEMPTS + 1):
            res = llm.call("base_writer", "base_cover",
                           {"facts": facts, "rules": rules, "errors": errors_text},
                           job_id=None, actor=actor, conn=conn)
            llm_call_ids.append(res.call_id)
            if not res.ok:
                problems = [f"base_writer llm call failed: {res.error}"]
            else:
                try:
                    doc = res.json()
                except ValueError as exc:
                    problems = [f"could not parse JSON output: {exc}"]
                else:
                    problems = _validate_shape(doc)
            if problems:
                errors_text = f"Round {round_num} rejected:\n" + "\n".join(f"- {p}" for p in problems)
                continue

            writer_model = res.actual_model
            audit_res = _audit(conn, facts, doc, actor)
            llm_call_ids.append(audit_res.call_id)
            passed, audit_problems = _check_audit(audit_res, writer_model)
            if passed:
                break
            errors_text = (f"Round {round_num} was rejected by the auditor:\n" +
                           "\n".join(f"- {p}" for p in audit_problems))

        if problems or not passed:
            reason = f"base cover letter failed after {MAX_ATTEMPTS} round(s): " + \
                "; ".join(problems or audit_problems)
            with core.tx(conn):
                core.append_event(conn, actor, "base_cover.failed", {"reason": reason})
            return {"ok": False, "reason": reason}

        _write_and_record(conn, doc, writer_model, audit_res, llm_call_ids, actor)
        return {"ok": True, "path": str(OUT_PATH), "writer_model": writer_model,
                "audit_call_id": audit_res.call_id, "rounds": len(llm_call_ids)}
    finally:
        conn.close()


def _audit(conn, facts, doc, actor):
    content = [{"where": f"core.{i + 1}", "text": p["text"], "fact_ids": p["fact_ids"]}
               for i, p in enumerate(doc["core_paragraphs"])]
    content.append({"where": "close", "text": doc["close"]["text"], "fact_ids": [], "kind": "close"})
    return llm.call("auditor", "audit", {
        "facts": facts,
        "description": ("BASE letter core: must hold unchanged for any UK ML/AI/data "
                        "science role. Company/role names must not appear in core "
                        "paragraphs. The close paragraph is the standard notice-period "
                        "close."),
        "content": content}, job_id=None, actor=actor, conn=conn)


def _write_and_record(conn, doc, writer_model, audit_res, llm_call_ids, actor):
    artefact = {
        "schema": 1,
        "note": ("Audited base cover letter core. Per-role letters write a fresh opening "
                 "paragraph and pick from core_paragraphs verbatim; the close is fixed."),
        "writer_model": writer_model,
        "audit_call_id": audit_res.call_id,
        "llm_call_ids": sorted(set(llm_call_ids)),
        "created_at": core.now(),
        "core_paragraphs": doc["core_paragraphs"],
        "close": doc["close"],
    }
    OUT_PATH.write_text(json.dumps(artefact, indent=2, ensure_ascii=False), encoding="utf-8")

    with core.tx(conn):
        pv = core.profile_version(conn, actor=actor)
        commit = core.git_commit()

        def _store(path: Path, kind: str, fmt: str, note: str):
            sha = core.store_file(conn, path, actor, None)
            core.record(conn, "documents", {
                "job_id": "base", "kind": kind, "format": fmt, "sha256": sha,
                "profile_version_id": pv, "git_commit": commit,
                "llm_call_ids": core.canonical(sorted(set(llm_call_ids))), "note": note,
                "created_at": core.now()},
                actor=actor, event_type="document.recorded", job_id=None)

        _store(OUT_PATH, "base_cover", "json",
               f"base cover core, writer={writer_model}, audit call {audit_res.call_id}")
        if BASE_CV_SOURCE.exists():
            _store(BASE_CV_SOURCE, "base_cv", "pdf",
                   "the operator's chosen base CV; the document facts.yaml was built from")
        core.append_event(conn, actor, "base_cover.recorded",
                          {"path": str(OUT_PATH), "writer_model": writer_model,
                           "audit_call_id": audit_res.call_id,
                           "paragraphs": len(doc["core_paragraphs"])})


def reaudit(actor: str = "code:base_cover") -> dict:
    """Audit the artefact as it stands on disk -- after a core paragraph was reworded by hand --
    with the same strong-model gate as a fresh draft. Nothing is rewritten unless it passes."""
    doc = json.loads(OUT_PATH.read_text(encoding="utf-8"))
    problems = _validate_shape(doc)
    if problems:
        return {"ok": False, "reason": "; ".join(problems)}
    conn = connect()
    try:
        facts = [f for f in citable_facts(load_bank()) if f["id"] in BASE_FACT_IDS]
        audit_res = _audit(conn, facts, doc, actor)
        passed, audit_problems = _check_audit(audit_res, None)
        if not passed:
            return {"ok": False, "reason": "; ".join(audit_problems)}
        _write_and_record(conn, doc, f"hand-edited, audited by {audit_res.actual_model}", audit_res,
                          list(doc.get("llm_call_ids") or []) + [audit_res.call_id], actor)
        return {"ok": True, "path": str(OUT_PATH), "audit_call_id": audit_res.call_id}
    finally:
        conn.close()


def _check_audit(audit_res, writer_model: str | None) -> tuple[bool, list[str]]:
    if not audit_res.ok:
        return False, [f"auditor llm call failed: {audit_res.error}"]
    if audit_res.actual_model and writer_model and audit_res.actual_model == writer_model:
        return False, ["auditor must be a different model from the writer"]
    try:
        doc = audit_res.json()
    except ValueError as exc:
        return False, [f"could not parse auditor JSON output: {exc}"]
    if not isinstance(doc, dict):
        return False, ["auditor output must be a JSON object"]
    passed = doc.get("pass") is True
    problems = doc.get("problems") or []
    if problems and passed:
        return False, ["auditor returned pass=true with problems listed (contradiction)"]
    return passed and not problems, [f"{p.get('where')}: {p.get('why')}" for p in problems]


def main() -> int:
    ap = argparse.ArgumentParser(description="Draft + audit the base cover letter core")
    ap.add_argument("--force", action="store_true", help="regenerate even if the artefact exists")
    ap.add_argument("--reaudit", action="store_true",
                    help="audit the artefact as it stands on disk (after a hand edit) and re-record it")
    args = ap.parse_args()
    try:
        if args.reaudit:
            result = reaudit()
            print(f"OK: re-audited {result['path']} (audit call {result['audit_call_id']})" if result.get("ok")
                  else f"FAILED: {result.get('reason')}")
            return 0 if result.get("ok") else 1
        result = run(force=args.force)
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 1
    if not result.get("ok"):
        print(f"FAILED: {result.get('reason')}")
        return 1
    if result.get("skipped"):
        print(f"base cover letter already exists: {result['path']} (use --force to regenerate)")
    else:
        print(f"OK: {result['path']} (writer={result['writer_model']}, "
              f"audit call {result['audit_call_id']}, {result['rounds']} llm rounds)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
