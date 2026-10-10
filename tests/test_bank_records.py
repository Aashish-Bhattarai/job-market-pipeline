import json
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "run"))
from ledger import bank, core, orders  # noqa: E402
from discover import db as dbmod  # noqa: E402
import ingest_attempt  # noqa: E402
import answer_question  # noqa: E402


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "PIPELINE_ROOT", tmp_path)
    (tmp_path / "profile").mkdir(parents=True, exist_ok=True)
    facts_path = tmp_path / "profile" / "facts.yaml"
    answers_path = tmp_path / "profile" / "answers.yaml"
    facts_path.write_text("education:\n  - id: edu-msc\n  - id: edu-bsc\n", encoding="utf-8")
    answers_path.write_text(
        "career_ambitions: Collaborative team.\n"
        "academic_background: MSc Data Science.\n"
        "motivation: Because of the calibration work.\n"
        "ai_usage: I route questions to deterministic functions.\n",
        encoding="utf-8")
    monkeypatch.setattr(core, "FACTS_PATH", facts_path)
    monkeypatch.setattr(core, "ANSWERS_PATH", answers_path)

    c = core.connect(tmp_path / "data" / "jobs.db")
    c.executescript(dbmod.SCHEMA)
    yield c
    c.close()


def _add_job(conn, job_id, company="Acme", title="Eng", status="tailored", **extra):
    conn.execute(
        "INSERT INTO jobs(id, source, company, title, location, region, url, apply_url, ats, "
        "description, status, discovered_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (job_id, extra.get("source", "ashby"), company, title, extra.get("location", "London"),
         extra.get("region", "london"), extra.get("url", "u"), extra.get("apply_url", "a"),
         extra.get("ats", "ashby"), extra.get("description", "desc"), status, core.now()))
    conn.commit()


def _add_documents(conn, job_id):
    with core.tx(conn):
        sha_cv = core.store_bytes(conn, b"%PDF cv", ".pdf", "code:test", job_id)
        cv_id = core.record(conn, "documents", {
            "job_id": job_id, "kind": "cv", "format": "pdf", "sha256": sha_cv,
            "profile_version_id": None, "git_commit": None, "llm_call_ids": "[]",
            "note": None, "created_at": core.now()}, actor="code:test",
            event_type="document.created", job_id=job_id)
    with core.tx(conn):
        sha_cp = core.store_bytes(conn, b"%PDF cover", ".pdf", "code:test", job_id)
        cover_id = core.record(conn, "documents", {
            "job_id": job_id, "kind": "cover", "format": "pdf", "sha256": sha_cp,
            "profile_version_id": None, "git_commit": None, "llm_call_ids": "[]",
            "note": None, "created_at": core.now()}, actor="code:test",
            event_type="document.created", job_id=job_id)
    with core.tx(conn):
        sha_ct = core.store_bytes(conn, b"cover text", ".txt", "code:test", job_id)
        core.record(conn, "documents", {
            "job_id": job_id, "kind": "cover", "format": "txt", "sha256": sha_ct,
            "profile_version_id": None, "git_commit": None, "llm_call_ids": "[]",
            "note": None, "created_at": core.now()}, actor="code:test",
            event_type="document.created", job_id=job_id)
    return cv_id, cover_id


# --------------------------------------------------------------- bank.py ---

def test_scope_precedence_job_beats_company_beats_global(conn):
    _add_job(conn, "j1" * 20, company="Acme")
    with core.tx(conn):
        bank.add_answer(conn, "Notice period?", "1 month (global)", "global",
                         created_by="human", approved_by="human", actor="human")
        bank.add_answer(conn, "Notice period?", "2 months (company)", "company", company="Acme",
                         created_by="human", approved_by="human", actor="human")
        bank.add_answer(conn, "Notice period?", "3 months (job)", "job", job_id="j1" * 20,
                         created_by="human", approved_by="human", actor="human")

    assert bank.lookup(conn, "Notice period?", "j1" * 20, "Acme")["answer"] == "3 months (job)"
    assert bank.lookup(conn, "Notice period?", "other-job", "Acme")["answer"] == "2 months (company)"
    assert bank.lookup(conn, "Notice period?", "other-job", "OtherCo")["answer"] == "1 month (global)"


def test_alias_match(conn):
    with core.tx(conn):
        bid = bank.add_answer(conn, "What is your notice period?", "1 month", "global",
                               created_by="human", approved_by="human", actor="human")
        bank.add_alias(conn, "notice period please", bid, "human", "human")
    assert bank.lookup(conn, "Notice period, please!", None, None)["id"] == bid
    assert bank.lookup(conn, "Some unrelated question", None, None) is None


def test_supersede_and_retire_hide_from_lookup_and_export(conn):
    with core.tx(conn):
        old_id = bank.add_answer(conn, "Salary expectations?", "£40k", "global",
                                  created_by="human", approved_by="human", actor="human")
    assert bank.lookup(conn, "Salary expectations?", None, None)["id"] == old_id

    with core.tx(conn):
        new_id = bank.add_answer(conn, "Salary expectations?", "£45k", "global",
                                  created_by="human", approved_by="human",
                                  supersedes_id=old_id, actor="human")
    found = bank.lookup(conn, "Salary expectations?", None, None)
    assert found["id"] == new_id and found["answer"] == "£45k"

    path, sha = bank.export(conn)
    entries = json.loads(path.read_text())["entries"]
    assert [e["id"] for e in entries] == [new_id]

    with core.tx(conn):
        other_id = bank.add_answer(conn, "Right to work?", "Yes", "global",
                                    created_by="human", approved_by="human", actor="human")
        bank.retire(conn, other_id, "human", "no longer relevant")
    assert bank.lookup(conn, "Right to work?", None, None) is None
    _, sha2 = bank.export(conn)
    ids_after = {e["id"] for e in json.loads(path.read_text())["entries"]}
    assert other_id not in ids_after
    assert core.verify(conn) == []


def test_retiring_a_superseding_answer_never_resurrects_the_old_answer(conn):
    """Regression (independent review, 2026-09-27): retiring the replacement
    used to drop the replacement from the superseded-exclusion set, silently
    reactivating the old answer it had replaced. Once superseded, always
    superseded — the auditable change path is a new superseding row."""
    with core.tx(conn):
        old_id = bank.add_answer(conn, "Salary expectations?", "£40k", "global",
                                  created_by="human", approved_by="human", actor="human")
        new_id = bank.add_answer(conn, "Salary expectations?", "£45k", "global",
                                  created_by="human", approved_by="human",
                                  supersedes_id=old_id, actor="human")
    assert bank.lookup(conn, "Salary expectations?", None, None)["id"] == new_id

    with core.tx(conn):
        bank.retire(conn, new_id, "human", "the replacement was wrong")
    assert bank.lookup(conn, "Salary expectations?", None, None) is None
    path, _ = bank.export(conn)
    assert json.loads(path.read_text())["entries"] == []
    assert core.verify(conn) == []


# ----------------------------------------------------------- orders.py -----

def test_build_order_missing_documents_fails_clearly(conn):
    _add_job(conn, "j2" * 20)
    with pytest.raises(ValueError, match="no cv/pdf document"):
        orders.build_order(conn, "j2" * 20, "dry_run")


def test_build_order_writes_expected_shape(conn):
    job_id = "j3" * 20
    _add_job(conn, job_id)
    cv_id, cover_id = _add_documents(conn, job_id)

    order_path = orders.build_order(conn, job_id, "dry_run")
    doc = json.loads(order_path.read_text())
    assert doc["schema"] == 1 and doc["job_id"] == job_id and doc["mode"] == "dry_run"
    assert Path(doc["files"]["cv_pdf"]).exists()
    assert Path(doc["files"]["cover_pdf"]).exists()
    assert Path(doc["files"]["cover_txt"]).exists()
    assert doc["document_ids"] == {"cv": cv_id, "cover": cover_id}
    assert Path(doc["bank_path"]).exists()
    assert core.verify(conn) == []


def test_a_job_with_a_confirmed_submission_never_gets_another_submit_order(conn):
    # Deliveroo Full-Stack was submitted three times in five minutes by diagnostic re-runs (2026-10-02).
    job_id = "j4" * 20
    _add_job(conn, job_id)
    _add_documents(conn, job_id)
    orders.build_order(conn, job_id, "submit")                 # first submit order is fine
    with core.tx(conn):
        conn.execute("UPDATE jobs SET status='applied' WHERE id=?", (job_id,))
    with pytest.raises(ValueError, match="already has a confirmed submission"):
        orders.build_order(conn, job_id, "submit")
    orders.build_order(conn, job_id, "dry_run")                # a dry run is still allowed


# ------------------------------------------------------- ingest_attempt.py -

def _write_attempt(attempt_dir, job_id, status, snapshot, unanswered, fields=None):
    attempt = {
        "schema": 1, "job_id": job_id, "mode": "dry_run",
        "started_at": "2026-09-27T10:00:00", "finished_at": "2026-09-27T10:05:00",
        "apply_url": "a", "final_url": "a", "status": status, "gate_reasons": [],
        "fields": fields or [], "unanswered": unanswered, "snapshot": snapshot,
        "autofill_issues": [], "uploads": [], "screenshots": {"prefill": None, "confirm": None},
        "confirmation_text": "Thanks!" if status == "submitted" else None,
        "confirmation_url": None, "errors": [], "bank_sha256": "x",
    }
    (attempt_dir / "attempt.json").write_text(json.dumps(attempt), encoding="utf-8")


@pytest.mark.parametrize("attempt_status,expected_job_status", [
    ("submitted", "applied"),
    ("filled_not_submitted", "ready"),
    ("queued_manual", "queued_manual"),
    ("blocked_gate", "queued_manual"),
    ("submit_failed", "queued_manual"),
    ("queued_account", "queued_account"),
])
def test_ingest_attempt_maps_status_and_hash_verifies(conn, attempt_status, expected_job_status):
    job_id = "j" + attempt_status[:3].ljust(39, "0")
    _add_job(conn, job_id)
    _add_documents(conn, job_id)
    order_path = orders.build_order(conn, job_id, "dry_run")
    attempt_dir = order_path.parent

    snapshot = [
        {"idx": 0, "label": "Right to work", "type": "text", "required": True, "value": "Yes",
         "options": None, "filled_by": "pipeline", "source": "answers.right_to_work"},
        {"idx": 1, "label": "Why this role?", "type": "textarea", "required": True, "value": None,
         "options": None, "filled_by": "untouched", "source": None},
    ]
    unanswered = [{"label": "Why this role?", "type": "textarea", "required": True, "options": None}]
    fields = [{"label": "Right to work", "type": "text", "required": True, "value": "Yes",
               "source": "answers.right_to_work"}]
    _write_attempt(attempt_dir, job_id, attempt_status, snapshot, unanswered, fields)

    result = ingest_attempt.ingest(conn, attempt_dir)
    assert result["new_status"] == expected_job_status
    job_row = conn.execute("SELECT status, applied_at FROM jobs WHERE id=?", (job_id,)).fetchone()
    assert job_row["status"] == expected_job_status
    if expected_job_status == "applied":
        assert job_row["applied_at"] is not None

    app = conn.execute("SELECT * FROM applications WHERE id=?", (result["application_id"],)).fetchone()
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM form_answers WHERE application_id=? ORDER BY idx", (app["id"],))]
    assert core.form_answers_hash(rows) == app["form_answers_sha256"]
    assert core.verify(conn) == []

    # required unanswered field must have been queued
    q = conn.execute("SELECT * FROM question_queue WHERE job_id=?", (job_id,)).fetchone()
    assert q is not None and q["label"] == "Why this role?" and q["status"] == "open"


# --------------------------------------------------------- answer_question -

def test_answer_resolves_matching_open_questions_and_requeues_job_only_when_all_resolved(conn):
    job_id = "jq" * 20
    other_job = "jo" * 20
    _add_job(conn, job_id, status="queued_manual")
    _add_job(conn, other_job, company="OtherCo", status="queued_manual")
    with core.tx(conn):
        q1 = bank.enqueue(conn, job_id, None, "Why are you looking for a new role?", "textarea", None, True,
                           "code:test")
        q2 = bank.enqueue(conn, job_id, None, "Career ambitions?", "textarea", None, True, "code:test")
        # a second job with the exact same first question, still open elsewhere
        q3 = bank.enqueue(conn, other_job, None, "Why are you looking for a new role?", "textarea", None, True,
                           "code:test")

    # Answer q1 globally -> resolves q1 and q3 (same question_norm), but not q2.
    rc = answer_question.cmd_answer(conn, q1, "Because I want full-time work.", "global")
    assert rc == 0

    q1_row = conn.execute("SELECT status FROM question_queue WHERE id=?", (q1,)).fetchone()
    q2_row = conn.execute("SELECT status FROM question_queue WHERE id=?", (q2,)).fetchone()
    q3_row = conn.execute("SELECT status FROM question_queue WHERE id=?", (q3,)).fetchone()
    assert q1_row["status"] == "resolved"
    assert q3_row["status"] == "resolved"
    assert q2_row["status"] == "open"

    # job_id still queued_manual: q2 (required) is still open.
    assert conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()["status"] == "queued_manual"
    # other_job had no other required open question -> moved back to tailored.
    assert conn.execute("SELECT status FROM jobs WHERE id=?", (other_job,)).fetchone()["status"] == "tailored"

    # Resolve the last required question for job_id -> it should requeue too.
    answer_question.cmd_answer(conn, q2, "A collaborative team.", "global")
    assert conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()["status"] == "tailored"
    assert core.verify(conn) == []


# ----------------------------------------------------------------- norm ----

def test_norm_parity_cases():
    cases = json.loads((Path(__file__).parent / "norm_cases.json").read_text(encoding="utf-8"))
    for text, expected in cases:
        assert core.norm(text) == expected


# ---- model-made aliases are gated (2026-10-02): the small mapper once aliased "Start date year" to the DATE OF
# BIRTH entry. A model alias counts only when the question shares real wording with the entry's own question.

@pytest.mark.parametrize("alias,question,ok", [
    ("start date year", "what is your date of birth", False),
    ("a levels or equivalent", "what is your academic background", False),
    ("have you used elevenlabs even in a personal or side project what did you build or explore",
     "describe how you ve used ai in a piece of work or something you re building or planning", False),
    ("date of birth", "what is your date of birth", True),
    ("describe your python experience", "describe your experience with python", True),
    ("what is your current notice period", "what is your notice period", True),
])
def test_alias_plausibility(alias, question, ok):
    assert bank.alias_plausible(core.norm(alias), core.norm(question)) is ok


def test_bogus_model_alias_is_ignored_by_lookup_and_export_but_human_aliases_are_not_gated(conn):
    with core.tx(conn):
        dob = bank.add_answer(conn, "What is your date of birth?", "1990-01-01", "global",
                              created_by="human", approved_by="human", actor="human")
        bank.add_alias(conn, "start date year", dob, "model:1185", "code:questions")        # the real incident
        bank.add_alias(conn, "date of birth", dob, "model:1186", "code:questions")          # a genuine paraphrase
        bank.add_alias(conn, "birthday please", dob, "human", "human")                      # human: never gated
    assert bank.lookup(conn, "Start date year", None, None) is None
    assert bank.lookup(conn, "Date of birth", None, None)["id"] == dob
    assert bank.lookup(conn, "Birthday, please!", None, None)["id"] == dob
    path, _ = bank.export(conn)
    aliases = json.loads(path.read_text(encoding="utf-8"))["entries"][0]["aliases"]
    assert "start date year" not in aliases and "date of birth" in aliases and "birthday please" in aliases


def test_mapper_refuses_to_alias_an_unrelated_question_to_a_personal_data_entry(conn, monkeypatch):
    from stages import questions
    _add_job(conn, "j1", company="Acme")
    with core.tx(conn):
        dob = bank.add_answer(conn, "What is your date of birth?", "1990-01-01", "global",
                              created_by="human", approved_by="human", actor="human")
        qid = bank.enqueue(conn, "j1", None, "Start date year", "number", None, True, "code:test")

    class Fake:
        ok, call_id, actual_model, error, text = True, 7, "m", None, json.dumps({"bank_id": dob})
        def json(self):
            return {"bank_id": dob}

    monkeypatch.setattr(questions.llm, "call", lambda *a, **k: Fake())
    row = dict(conn.execute("SELECT * FROM question_queue WHERE id=?", (qid,)).fetchone())
    assert questions._try_map_question(conn, row, "Acme", "code:questions") is False
    assert conn.execute("SELECT COUNT(*) FROM answer_aliases").fetchone()[0] == 0
    assert conn.execute("SELECT status FROM question_queue WHERE id=?", (qid,)).fetchone()["status"] == "open"


def test_choice_field_model_aliases_are_trusted_because_the_options_check_guards_them(conn):
    # "Visa status?" <-> "Which visa do you hold?" shares one word: fine for a select (the answer must equal an
    # option, checked by the mapper and by bankLookup), not for free text.
    with core.tx(conn):
        bid = bank.add_answer(conn, "Which visa do you hold?", "Skilled Worker", "global",
                              created_by="human", approved_by="human", actor="human")
        bank.add_alias(conn, "visa status", bid, "model:5:choice", "code:questions")
        bank.add_alias(conn, "your visa situation please", bid, "model:6", "code:questions")
    assert bank.lookup(conn, "Visa status?", None, None)["id"] == bid
    assert bank.lookup(conn, "Your visa situation please", None, None) is None


def test_resolved_question_reopens_when_a_later_attempt_still_cannot_answer_it_but_dismissed_stays_closed(conn):
    # ElevenLabs Full-Stack (2026-10-02): its rows were "resolved" to an entry that was later superseded, so nothing
    # served the label, the form kept coming back unanswered, and the questions stage never looked at the row again.
    jid = "jr" * 20
    _add_job(conn, jid, status="queued_manual")
    with core.tx(conn):
        stale = bank.enqueue(conn, jid, None, "What is the most impactful thing you have built?", "textarea", None, True, "t")
        kept = bank.enqueue(conn, jid, None, "Pronouns", "text", None, True, "t")
        old_entry = bank.add_answer(conn, "Describe an AI project", "x", "global", created_by="human", approved_by="human", actor="t")
        bank.resolve_question(conn, stale, old_entry, "t")
        bank.dismiss_question(conn, kept, "t", "human decision")
    with core.tx(conn):
        assert bank.enqueue(conn, jid, None, "What is the most impactful thing you have built?", "textarea", None, True, "t") == stale
        assert bank.enqueue(conn, jid, None, "Pronouns", "text", None, True, "t") == kept
    row = conn.execute("SELECT status, bank_id, resolved_at FROM question_queue WHERE id=?", (stale,)).fetchone()
    assert (row["status"], row["bank_id"], row["resolved_at"]) == ("open", None, None)
    assert conn.execute("SELECT status FROM question_queue WHERE id=?", (kept,)).fetchone()["status"] == "dismissed"
    assert core.verify(conn) == []
