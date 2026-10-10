"""Stage `tailor`: account-required gate -> select (code) -> writer (mid
model) -> validate -> auditor (strong model, different from writer) -> render
+ store, for one `fit_pass` job. Never submits anything; only writes files
into pipeline/out/<job_id>/ and the ledger.

Usage: python -m stages.tailor <job_id> [--db path] [--facts path]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from stages.common import PIPELINE_ROOT, RENDER_DIR, citable_facts, connect, get_job, load_bank, set_job_status
from stages.select import build_selection
from ledger import core, llm

if str(RENDER_DIR) not in sys.path:
    sys.path.insert(0, str(RENDER_DIR))
import validate as validate_mod  # noqa: E402
import render_cv  # noqa: E402
import render_cover  # noqa: E402

OUT_ROOT = PIPELINE_ROOT / "out"
MAX_PAGES = 2
# The auditor is a maximally strict adversarial check (no analogies, no
# added weight, no uncited claims) -- real testing (2026-09-27, real
# Sonnet writer + Opus auditor) showed even a good writer rarely produces
# a fully clean draft within a single rewrite. Each round costs one writer
# + one auditor call (~$0.1-0.2); widening the budget buys a meaningfully
# higher automatic pass rate without loosening the bar itself -- a job that
# still fails after this many rounds goes to needs_review either way.
MAX_TAILOR_ATTEMPTS = 4

# Moved from v1 pipeline/run/prepare_tailor.py: apply pages that need an
# account to be created are never automated (non-negotiable: never create
# accounts/passwords).
ACCOUNT_REQUIRED_ATS = {"workday", "icims", "taleo", "successfactors"}
ACCOUNT_REQUIRED_URL_RE = re.compile(
    r"myworkdayjobs\.com|\.icims\.com|taleo\.net|successfactors\.(?:com|eu)|"
    r"oraclecloud\.com/hcmUI|workday\.com", re.I)


def needs_account(ats: str | None, apply_url: str | None, url: str | None) -> str | None:
    ats_l = (ats or "").strip().lower()
    if ats_l in ACCOUNT_REQUIRED_ATS:
        return f"ATS '{ats_l}' requires creating an account to apply"
    for candidate in (apply_url, url):
        if candidate and ACCOUNT_REQUIRED_URL_RE.search(candidate):
            return f"apply page requires an account ({candidate})"
    return None


def company_account_wall(conn, company: str, job_id: str) -> str | None:
    """An earlier attempt at THIS company's application site ended at a login/registration wall (Reply:
    every Gradcracker role lands on a SuccessFactors sign-in -- four tailored applications, four walls,
    on 2026-10-02). Hold the company's other roles before any tailoring spend."""
    key = re.sub(r"[^a-z0-9]+", "", (company or "").lower())
    if not key:
        return None
    for r in conn.execute("SELECT j.company, j.title FROM applications a JOIN jobs j ON j.id = a.job_id "
                          "WHERE a.status = 'queued_account' AND j.id != ?", (job_id,)):
        if re.sub(r"[^a-z0-9]+", "", (r["company"] or "").lower()) == key:
            return f"{company}'s application site already ended at a login/registration wall ({r['title']}); never automated"
    return None


# Apply funnels this pipeline cannot drive (each checked against the live pages on 2026-10-02).
# Tailoring one costs ~$0.50 for an application that cannot be submitted automatically, so they
# are held for a human before any model call.
#  - SmartRecruiters: OneClick is a multi-step wizard built from shadow DOM (1,800+ shadow hosts) and
#    DataDome blocks automated browsers.
#  - click.appcast.io / efinancialcareers: Reed's external links for agency postings lead to a DataDome
#    challenge, Aquent's email-first flow, or eFinancialCareers' own login-gated "Apply now".
UNSUPPORTED_FORMS = (
    (re.compile(r"smartrecruiters\.com", re.I),
     "SmartRecruiters' multi-step shadow-DOM form is not automatable; apply manually"),
    (re.compile(r"click\.appcast\.io|efinancialcareers\.", re.I),
     "agency apply funnel (Appcast / eFinancialCareers) is bot- or login-gated; apply manually"),
)


def unsupported_form(ats: str | None, apply_url: str | None, url: str | None) -> str | None:
    if (ats or "").strip().lower() == "smartrecruiters":
        return UNSUPPORTED_FORMS[0][1]
    for candidate in (apply_url, url):
        for pattern, reason in UNSUPPORTED_FORMS:
            if candidate and pattern.search(candidate):
                return reason
    return None


def _writer_shape_errors(doc) -> list[str]:
    problems = []
    if not isinstance(doc, dict):
        return ["writer output must be a JSON object"]
    summary = doc.get("summary")
    if not isinstance(summary, dict) or not isinstance(summary.get("text"), str) or not summary.get("text", "").strip():
        problems.append("summary.text missing or empty")
    if not isinstance(summary, dict) or not isinstance(summary.get("fact_ids"), list):
        problems.append("summary.fact_ids missing or not a list")
    paragraphs = doc.get("paragraphs")
    if not isinstance(paragraphs, list) or not paragraphs:
        problems.append("paragraphs missing or empty")
    return problems


def _call_writer(conn, job_id, title, company, description, facts, cover_rules, errors_text,
                 base_letter=None):
    return llm.call("writer", "writer", {
        "title": title, "company": company, "description": description,
        "facts": facts, "cover_rules": cover_rules, "errors": errors_text,
        "base_letter": base_letter or "(none available: write every paragraph fresh)",
    }, job_id=job_id, actor="llm:writer", conn=conn)


def _assemble(job_id, company, title, base_selection, writer_doc):
    selection = {**base_selection, "summary": writer_doc["summary"]}
    cover = {"job_id": job_id, "company": company, "role": title,
              "paragraphs": writer_doc["paragraphs"]}
    return selection, cover


def _write_and_validate(bank, selection, cover):
    problems = list(validate_mod.validate_selection(selection, bank))
    problems += [f"cover: {r}" for r in validate_mod.validate_cover(cover, bank)]
    return problems


def _drop_fact(selection: dict, fact_id: str) -> dict:
    out = dict(selection)
    for key in ("experience", "projects"):
        blocks = []
        for block in out.get(key, []):
            fids = [f for f in block.get("fact_ids", []) if f != fact_id]
            if fids:
                blocks.append({**block, "fact_ids": fids})
        out[key] = blocks
    return out


def run(job_id: str, db_path=None, facts_path: str | None = None,
        actor: str = "code:tailor") -> dict:
    conn = connect(db_path)
    try:
        job = get_job(conn, job_id)
        if job is None:
            raise ValueError(f"job_id {job_id!r} not found in jobs table")

        title, company = job.get("title") or "", job.get("company") or ""
        description = job.get("description") or ""
        apply_url, url, ats = job.get("apply_url"), job.get("url"), job.get("ats")

        account_reason = needs_account(ats, apply_url, url) or company_account_wall(conn, company, job_id)
        if account_reason:
            with core.tx(conn):
                set_job_status(conn, job_id, "queued_account", actor, reason=account_reason)
            return {"ok": True, "job_id": job_id, "status": "queued_account", "reason": account_reason}

        unsupported = unsupported_form(job.get("ats"), apply_url, job.get("url"))
        if unsupported:
            with core.tx(conn):
                set_job_status(conn, job_id, "queued_manual", actor, reason=unsupported)
            return {"ok": True, "job_id": job_id, "status": "queued_manual", "reason": unsupported}

        # No resolved apply URL means there is nothing the runner can legally
        # submit against (aggregator pages, HN comments, JS-gated boards).
        # Tailoring/rendering it would waste a browser attempt later: queue it
        # until a URL is resolved (resolve_apply_url.py) or the operator applies by
        # hand from the posting page.
        if not apply_url:
            with core.tx(conn):
                set_job_status(conn, job_id, "queued_manual", actor,
                               reason="apply_url unresolved — cannot submit without a target")
            return {"ok": True, "job_id": job_id, "status": "queued_manual",
                    "reason": "apply_url unresolved"}

        bank = load_bank(facts_path)
        facts = citable_facts(bank)
        citable_ids = {f["id"] for f in facts}

        latest = conn.execute(
            "SELECT MAX(extraction) AS ext FROM requirements WHERE job_id=?", (job_id,)).fetchone()
        extraction = latest["ext"] if latest else None
        req_rows = []
        if extraction is not None:
            req_rows = conn.execute(
                "SELECT text, kind, required, years, fact_ids FROM requirements "
                "WHERE job_id=? AND extraction=? ORDER BY idx", (job_id, extraction)).fetchall()
        requirements = [{"text": r["text"], "kind": r["kind"], "required": bool(r["required"]),
                          "years": r["years"], "fact_ids": json.loads(r["fact_ids"])} for r in req_rows]

        base_selection, drop_order = build_selection(bank, job_id, title, description, requirements)
        cover_rules = (PIPELINE_ROOT / "profile" / "cover_template.md").read_text(encoding="utf-8")

        base_letter_path = PIPELINE_ROOT / "profile" / "base_cover_letter.json"
        if base_letter_path.exists():
            base_letter = json.loads(base_letter_path.read_text(encoding="utf-8"))
        else:
            # No audited base letter (tests, or pre-2026-09-28 profile): writer
            # works exactly as before, drafting every paragraph fresh.
            base_letter = None

        llm_call_ids: list[int] = []
        errors_text = "(none, first attempt)"
        selection = cover = None
        writer_model = None
        problems: list[str] = []
        audit_problems: list[str] = []
        audit_res = None
        passed = False

        for round_num in range(1, MAX_TAILOR_ATTEMPTS + 1):
            writer_res = _call_writer(conn, job_id, title, company, description, facts,
                                       cover_rules, errors_text, base_letter=base_letter)
            llm_call_ids.append(writer_res.call_id)
            if not writer_res.ok:
                problems = [f"writer llm call failed: {writer_res.error}"]
            else:
                try:
                    writer_doc = writer_res.json()
                except ValueError as exc:
                    problems = [f"could not parse writer JSON output: {exc}"]
                else:
                    problems = _writer_shape_errors(writer_doc)
                    if not problems:
                        selection, cover = _assemble(job_id, company, title, base_selection, writer_doc)
                        problems = _write_and_validate(bank, selection, cover)
            if problems:
                errors_text = (f"Round {round_num} was rejected by validation:\n" +
                               "\n".join(f"- {p}" for p in problems))
                continue

            writer_model = writer_res.actual_model
            content = [{"where": "summary", "text": selection["summary"]["text"],
                        "fact_ids": selection["summary"]["fact_ids"]}]
            for i, p in enumerate(cover["paragraphs"]):
                content.append({"where": f"cover.p{i + 1}", "text": p["text"],
                                 "fact_ids": p.get("fact_ids", []), "kind": p.get("kind")})
            audit_res = llm.call("auditor", "audit", {"facts": facts, "description": description,
                                                        "content": content},
                                  job_id=job_id, actor="llm:auditor", conn=conn)
            llm_call_ids.append(audit_res.call_id)
            passed, audit_problems = _check_audit(audit_res, writer_model)
            if passed:
                break
            errors_text = (f"Round {round_num} was rejected by the defensibility auditor:\n" +
                           "\n".join(f"- {p}" for p in audit_problems))

        if problems or not passed:
            reason = (f"tailoring failed after {MAX_TAILOR_ATTEMPTS} round(s): " +
                      "; ".join(problems or audit_problems))
            with core.tx(conn):
                set_job_status(conn, job_id, "needs_review", actor, reason=reason)
            return {"ok": False, "job_id": job_id, "reason": reason}

        # ---- render, trimming to <= MAX_PAGES; never drop a fact cited by
        # the summary (its bullet may double as evidence for the prose).
        job_dir = OUT_ROOT / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        protected = set(selection["summary"]["fact_ids"])
        trimmable = [fid for fid in drop_order if fid not in protected]

        current = selection
        cv_result = None
        while True:
            (job_dir / "selection.json").write_text(json.dumps(current, indent=2, ensure_ascii=False),
                                                     encoding="utf-8")
            cv_result = render_cv.render(str(job_dir / "selection.json"), str(job_dir), facts_path)
            if cv_result["page_count"] <= MAX_PAGES or not trimmable:
                break
            current = _drop_fact(current, trimmable.pop(0))
        selection = current

        if cv_result["page_count"] > MAX_PAGES:
            reason = f"rendered CV is {cv_result['page_count']} page(s) even after trimming every non-summary fact"
            with core.tx(conn):
                set_job_status(conn, job_id, "needs_review", actor, reason=reason)
            return {"ok": False, "job_id": job_id, "reason": reason}

        (job_dir / "cover.json").write_text(json.dumps(cover, indent=2, ensure_ascii=False), encoding="utf-8")
        cover_result = render_cover.render(str(job_dir / "cover.json"), str(job_dir), facts_path)

        defensibility = {"job_id": job_id, "pass": True, "problems": []}
        (job_dir / "defensibility.json").write_text(json.dumps(defensibility, indent=2, ensure_ascii=False),
                                                     encoding="utf-8")

        fit_json = job.get("fit_json")
        job_snapshot = {
            "job_id": job_id, "company": company, "title": title, "location": job.get("location"),
            "region": job.get("region"), "url": url, "apply_url": apply_url, "ats": ats,
            "posted_at": job.get("posted_at"), "description": description,
            "fit_decision": json.loads(fit_json) if fit_json else None,
            "applied_at": None, "submitted": False, "answers": {},
        }
        (job_dir / "job.json").write_text(json.dumps(job_snapshot, indent=2, ensure_ascii=False), encoding="utf-8")

        with core.tx(conn):
            profile_version_id = core.profile_version(conn, actor=actor)
            git_commit = core.git_commit()
            call_ids_json = core.canonical(sorted(set(llm_call_ids)))

            def _store(path: Path, kind: str, fmt: str, llm_ids=None, note=None):
                sha = core.store_file(conn, path, actor, job_id)
                core.record(conn, "documents", {
                    "job_id": job_id, "kind": kind, "format": fmt, "sha256": sha,
                    "profile_version_id": profile_version_id, "git_commit": git_commit,
                    "llm_call_ids": core.canonical(sorted(set(llm_ids or []))), "note": note,
                    "created_at": core.now(),
                }, actor=actor, event_type="document.recorded", job_id=job_id)
                return sha

            _store(job_dir / "selection.json", "selection", "json", llm_call_ids)
            _store(job_dir / "cover.json", "cover_json", "json", llm_call_ids)
            _store(job_dir / "defensibility.json", "defensibility", "json", [audit_res.call_id])
            _store(job_dir / "job.json", "jd", "json", [])
            _store(Path(cv_result["docx"]), "cv", "docx", llm_call_ids)
            _store(Path(cv_result["pdf"]), "cv", "pdf", llm_call_ids)
            _store(Path(cover_result["docx"]), "cover", "docx", llm_call_ids)
            _store(Path(cover_result["pdf"]), "cover", "pdf", llm_call_ids)
            _store(Path(cover_result["txt"]), "cover", "txt", llm_call_ids)

            set_job_status(conn, job_id, "tailored", actor, reason=None, extra_cols={
                "cv_path": cv_result["pdf"], "cover_path": cover_result["pdf"]})

        return {"ok": True, "job_id": job_id, "status": "tailored", "cv_path": cv_result["pdf"],
                "cover_path": cover_result["pdf"], "pages": cv_result["page_count"]}
    finally:
        conn.close()


def _check_audit(audit_res, writer_model: str | None) -> tuple[bool, list[str]]:
    problems = []
    if not audit_res.ok:
        return False, [f"auditor llm call failed: {audit_res.error}"]
    try:
        doc = audit_res.json()
    except ValueError as exc:
        return False, [f"could not parse auditor JSON output: {exc}"]
    if not isinstance(doc, dict):
        return False, ["auditor output must be a JSON object"]
    passed = doc.get("pass") is True
    doc_problems = doc.get("problems") or []
    if doc_problems:
        problems.extend(f"{p.get('where')}: {p.get('why')}" for p in doc_problems)
    if passed and doc_problems:
        problems.append("auditor returned pass=true with a non-empty problems list (contradiction)")
        passed = False
    if not passed and not doc_problems:
        problems.append("auditor returned pass=false with no problems listed")
    if audit_res.actual_model == writer_model:
        problems.append(f"auditor model {audit_res.actual_model!r} is the same as the writer model "
                         "(auditor must be a different model)")
        passed = False
    return passed and not problems, problems


def main() -> int:
    ap = argparse.ArgumentParser(description="Tailor a CV + cover letter for one fit_pass job")
    ap.add_argument("job_id")
    ap.add_argument("--db", default=None)
    ap.add_argument("--facts", default=None)
    args = ap.parse_args()

    try:
        result = run(args.job_id, args.db, args.facts)
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 1

    if result["ok"]:
        print(f"OK: {result['job_id']} -> {result['status']}")
    else:
        print(f"NEEDS_REVIEW: {result['job_id']}: {result['reason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
