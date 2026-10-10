"""Ledger schema (lives in pipeline/data/jobs.db beside the `jobs` table).

Append-only tables are enforced by triggers: a record, once written, can only
be superseded by a new record, never edited or deleted. `events` is
additionally hash-chained (see core.append_event / verify_audit.py), so even
a tamper that drops the triggers is detectable.
"""
from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 1

DDL = """
CREATE TABLE IF NOT EXISTS ledger_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS profile_versions(
    id            INTEGER PRIMARY KEY,
    sha256        TEXT NOT NULL UNIQUE,
    facts_sha256  TEXT NOT NULL,
    answers_sha256 TEXT NOT NULL,
    facts_yaml    TEXT NOT NULL,
    answers_yaml  TEXT NOT NULL,
    git_commit    TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS blobs(
    sha256     TEXT PRIMARY KEY,
    ext        TEXT NOT NULL,
    size       INTEGER NOT NULL,
    store_path TEXT NOT NULL,          -- relative to pipeline/
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_calls(
    id              INTEGER PRIMARY KEY,
    stage           TEXT NOT NULL,
    job_id          TEXT,
    prompt_name     TEXT NOT NULL,
    prompt_sha256   TEXT NOT NULL,      -- template file content hash (prompt version)
    requested_model TEXT NOT NULL,
    actual_model    TEXT,               -- the model that actually produced the output
    min_rank        INTEGER NOT NULL,
    fallback_errors TEXT NOT NULL,      -- JSON list of models that failed before the answer
    input_sha256    TEXT NOT NULL,      -- blob: full rendered prompt
    raw_sha256      TEXT,               -- blob: raw omp JSON event stream
    output_text     TEXT,
    output_sha256   TEXT,
    tokens_in       INTEGER,
    tokens_out      INTEGER,
    cost_usd        REAL,
    duration_ms     INTEGER,
    ok              INTEGER NOT NULL,
    error           TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS requirements(
    id          INTEGER PRIMARY KEY,
    job_id      TEXT NOT NULL,
    extraction  INTEGER NOT NULL,       -- llm_calls.id of the extraction run
    idx         INTEGER NOT NULL,
    text        TEXT NOT NULL,
    quote       TEXT NOT NULL,          -- verbatim span of the job description
    kind        TEXT NOT NULL,
    required    INTEGER NOT NULL,
    years       REAL,
    fact_ids    TEXT NOT NULL,          -- JSON list
    created_at  TEXT NOT NULL,
    UNIQUE(job_id, extraction, idx)
);

CREATE TABLE IF NOT EXISTS documents(
    id                 INTEGER PRIMARY KEY,
    job_id             TEXT NOT NULL,
    kind               TEXT NOT NULL,   -- cv | cover | selection | cover_json | defensibility | jd
    format             TEXT NOT NULL,   -- pdf | docx | txt | json
    sha256             TEXT NOT NULL REFERENCES blobs(sha256),
    profile_version_id INTEGER REFERENCES profile_versions(id),
    git_commit         TEXT,
    llm_call_ids       TEXT NOT NULL,   -- JSON list
    note               TEXT,
    created_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS applications(
    id                        INTEGER PRIMARY KEY,
    job_id                    TEXT NOT NULL,
    attempt                   INTEGER NOT NULL,
    mode                      TEXT NOT NULL,   -- dry_run | submit | manual | migrated
    status                    TEXT NOT NULL,
    apply_url                 TEXT,
    final_url                 TEXT,
    ats                       TEXT,
    profile_version_id        INTEGER REFERENCES profile_versions(id),
    git_commit                TEXT,
    bank_export_sha256        TEXT,
    cv_document_id            INTEGER REFERENCES documents(id),
    cover_document_id         INTEGER REFERENCES documents(id),
    attempt_json_sha256       TEXT,            -- blob: raw runner record
    screenshot_prefill_sha256 TEXT,
    screenshot_confirm_sha256 TEXT,
    confirmation_text         TEXT,
    confirmation_url          TEXT,
    form_answers_sha256       TEXT NOT NULL,   -- hash of this attempt's form_answers rows
    started_at                TEXT,
    finished_at               TEXT,
    note                      TEXT,
    created_at                TEXT NOT NULL,
    UNIQUE(job_id, attempt)
);

CREATE TABLE IF NOT EXISTS form_answers(
    application_id INTEGER NOT NULL REFERENCES applications(id),
    idx            INTEGER NOT NULL,
    label          TEXT,
    type           TEXT,
    required       INTEGER,
    value          TEXT,                -- JSON-encoded
    readback_value TEXT,                -- JSON-encoded
    source_kind    TEXT NOT NULL,       -- fact | answers_yaml | bank | human | cover | file | ats_autofill | untouched | unanswered | reconstructed
    source_ref     TEXT,
    verified       INTEGER,
    PRIMARY KEY(application_id, idx)
);

CREATE TABLE IF NOT EXISTS answer_bank(
    id            INTEGER PRIMARY KEY,
    question      TEXT NOT NULL,
    question_norm TEXT NOT NULL,
    answer        TEXT NOT NULL,
    scope         TEXT NOT NULL CHECK(scope IN ('global','company','job')),
    company_norm  TEXT,
    job_id        TEXT,
    created_by    TEXT NOT NULL,        -- human | model | migrated
    approved_by   TEXT NOT NULL,        -- human | audit:<llm_call_id> | migrated:<note>
    fact_ids      TEXT NOT NULL,        -- JSON list (empty for personal answers)
    llm_call_ids  TEXT NOT NULL,        -- JSON list
    supersedes_id INTEGER REFERENCES answer_bank(id),
    retired       INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    CHECK((scope='global' AND company_norm IS NULL AND job_id IS NULL)
       OR (scope='company' AND company_norm IS NOT NULL AND job_id IS NULL)
       OR (scope='job' AND job_id IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS answer_aliases(
    question_norm TEXT NOT NULL,
    bank_id       INTEGER NOT NULL REFERENCES answer_bank(id),
    mapped_by     TEXT NOT NULL,        -- human | model:<llm_call_id>
    created_at    TEXT NOT NULL,
    PRIMARY KEY(question_norm, bank_id)
);

CREATE TABLE IF NOT EXISTS question_queue(
    id             INTEGER PRIMARY KEY,
    job_id         TEXT NOT NULL,
    application_id INTEGER REFERENCES applications(id),
    label          TEXT NOT NULL,
    question_norm  TEXT NOT NULL,
    type           TEXT,
    options        TEXT,                -- JSON list
    required       INTEGER,
    category       TEXT,                -- personal | fact_answerable | NULL (unclassified)
    status         TEXT NOT NULL DEFAULT 'open',   -- open | resolved | dismissed
    bank_id        INTEGER REFERENCES answer_bank(id),
    created_at     TEXT NOT NULL,
    resolved_at    TEXT,
    UNIQUE(job_id, question_norm)
);

CREATE TABLE IF NOT EXISTS events(
    seq       INTEGER PRIMARY KEY,
    ts        TEXT NOT NULL,
    actor     TEXT NOT NULL,
    job_id    TEXT,
    type      TEXT NOT NULL,
    payload   TEXT NOT NULL,             -- canonical JSON
    prev_hash TEXT NOT NULL,
    hash      TEXT NOT NULL UNIQUE
);

CREATE INDEX IF NOT EXISTS ix_events_job ON events(job_id);
CREATE INDEX IF NOT EXISTS ix_docs_job ON documents(job_id);
CREATE INDEX IF NOT EXISTS ix_apps_job ON applications(job_id);
CREATE INDEX IF NOT EXISTS ix_llm_job ON llm_calls(job_id);
CREATE INDEX IF NOT EXISTS ix_req_job ON requirements(job_id);
CREATE INDEX IF NOT EXISTS ix_bank_norm ON answer_bank(question_norm);
CREATE INDEX IF NOT EXISTS ix_queue_status ON question_queue(status);
"""

# Tables whose rows are immutable once written.
IMMUTABLE = ["profile_versions", "blobs", "llm_calls", "requirements", "documents",
             "form_answers", "answer_aliases", "events"]


def _immutable_triggers() -> str:
    out = []
    for t in IMMUTABLE:
        out.append(f"""
CREATE TRIGGER IF NOT EXISTS {t}_no_update BEFORE UPDATE ON {t}
BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END;
CREATE TRIGGER IF NOT EXISTS {t}_no_delete BEFORE DELETE ON {t}
BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END;""")
    # answer_bank: only the `retired` flag may change (0 -> 1), nothing else;
    # a changed answer is a new row that supersedes the old one.
    out.append("""
CREATE TRIGGER IF NOT EXISTS answer_bank_limited_update BEFORE UPDATE ON answer_bank
WHEN NEW.id IS NOT OLD.id OR NEW.question IS NOT OLD.question OR NEW.question_norm IS NOT OLD.question_norm
  OR NEW.answer IS NOT OLD.answer OR NEW.scope IS NOT OLD.scope OR NEW.company_norm IS NOT OLD.company_norm
  OR NEW.job_id IS NOT OLD.job_id OR NEW.created_by IS NOT OLD.created_by OR NEW.approved_by IS NOT OLD.approved_by
  OR NEW.fact_ids IS NOT OLD.fact_ids OR NEW.llm_call_ids IS NOT OLD.llm_call_ids
  OR NEW.supersedes_id IS NOT OLD.supersedes_id OR NEW.created_at IS NOT OLD.created_at
  OR (OLD.retired = 1 AND NEW.retired = 0)
BEGIN SELECT RAISE(ABORT, 'answer_bank rows are immutable; add a superseding row instead'); END;
CREATE TRIGGER IF NOT EXISTS answer_bank_no_delete BEFORE DELETE ON answer_bank
BEGIN SELECT RAISE(ABORT, 'answer_bank is append-only'); END;""")
    # applications: lifecycle fields may not be rewritten once the record is
    # final; the identity + evidence columns never change.
    out.append("""
CREATE TRIGGER IF NOT EXISTS applications_immutable BEFORE UPDATE ON applications
BEGIN SELECT RAISE(ABORT, 'applications is append-only; record a new attempt'); END;
CREATE TRIGGER IF NOT EXISTS applications_no_delete BEFORE DELETE ON applications
BEGIN SELECT RAISE(ABORT, 'applications is append-only'); END;""")
    return "\n".join(out)


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)
    conn.executescript(_immutable_triggers())
    conn.execute("INSERT OR IGNORE INTO ledger_meta(key, value) VALUES('schema_version', ?)",
                 (str(SCHEMA_VERSION),))
    conn.commit()
