import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ledger import core, llm  # noqa: E402


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "PIPELINE_ROOT", tmp_path)
    c = core.connect(tmp_path / "t.db")
    yield c
    c.close()


def _doc(conn, job="j1", data=b"%PDF cv"):
    with core.tx(conn):
        sha = core.store_bytes(conn, data, "pdf", "test", job)
        return core.record(conn, "documents", {
            "job_id": job, "kind": "cv", "format": "pdf", "sha256": sha,
            "profile_version_id": None, "git_commit": None, "llm_call_ids": "[]",
            "note": None, "created_at": core.now()}, actor="test", event_type="document.created", job_id=job), sha


def _drop_triggers(conn):
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
        conn.execute(f"DROP TRIGGER {name}")


def test_clean_ledger_verifies(conn):
    _doc(conn)
    assert core.verify(conn) == []


def test_append_only_tables_reject_update_and_delete(conn):
    doc_id, _ = _doc(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE documents SET kind='cover' WHERE id=?", (doc_id,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM events")


def test_edited_row_detected_even_with_triggers_dropped(conn):
    doc_id, _ = _doc(conn)
    _drop_triggers(conn)
    conn.execute("UPDATE documents SET kind='cover' WHERE id=?", (doc_id,))
    assert any("edited" in p for p in core.verify(conn))


def test_deleted_row_detected(conn):
    doc_id, _ = _doc(conn)
    _drop_triggers(conn)
    conn.execute("DELETE FROM documents WHERE id=?", (doc_id,))
    assert any("deleted" in p for p in core.verify(conn))


def test_row_inserted_outside_ledger_detected(conn):
    _, sha = _doc(conn)
    conn.execute("INSERT INTO documents(job_id, kind, format, sha256, llm_call_ids, created_at) "
                 "VALUES('j2','cv','pdf',?,'[]','x')", (sha,))
    assert any("no attesting event" in p for p in core.verify(conn))


def test_edited_or_deleted_event_breaks_chain(conn):
    _doc(conn)
    _doc(conn, job="j2", data=b"other")
    _drop_triggers(conn)
    conn.execute("UPDATE events SET actor='someone-else' WHERE seq=1")
    assert any("does not match its hash" in p for p in core.verify(conn))
    conn.execute("DELETE FROM events WHERE seq=2")
    assert any("gap" in p for p in core.verify(conn))


def test_swapped_file_detected(conn):
    _, sha = _doc(conn)
    p = core.blob_path(conn, sha)
    p.chmod(0o666)
    p.write_bytes(b"forged cv")
    assert any("content changed" in x for x in core.verify(conn))


def test_form_answers_tamper_detected(conn):
    rows = [{"application_id": None, "idx": 0, "label": "Salary", "type": "text", "required": 1,
             "value": '"£40,000"', "readback_value": '"£40,000"', "source_kind": "answers_yaml",
             "source_ref": "answers.salary.rule", "verified": 1}]
    with core.tx(conn):
        app_id = core.record(conn, "applications", {
            "job_id": "j1", "attempt": 1, "mode": "submit", "status": "submitted",
            "form_answers_sha256": core.form_answers_hash(rows), "created_at": core.now()},
            actor="test", event_type="application.recorded", job_id="j1")
        for r in rows:
            r["application_id"] = app_id
            conn.execute("INSERT INTO form_answers(application_id, idx, label, type, required, value, "
                         "readback_value, source_kind, source_ref, verified) VALUES(?,?,?,?,?,?,?,?,?,?)",
                         [r[k] for k in ("application_id", "idx", "label", "type", "required", "value",
                                         "readback_value", "source_kind", "source_ref", "verified")])
    assert core.verify(conn) == []
    _drop_triggers(conn)
    conn.execute("UPDATE form_answers SET value='\"£60,000\"'")
    assert any("form answers differ" in p for p in core.verify(conn))


def test_answer_bank_only_retire_flag_may_change(conn):
    with core.tx(conn):
        bid = core.record(conn, "answer_bank", {
            "question": "Nationality?", "question_norm": "nationality", "answer": "French",
            "scope": "global", "created_by": "human", "approved_by": "human", "fact_ids": "[]",
            "llm_call_ids": "[]", "created_at": core.now()}, actor="test", event_type="bank.added")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE answer_bank SET answer='British' WHERE id=?", (bid,))
    conn.execute("UPDATE answer_bank SET retired=1 WHERE id=?", (bid,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE answer_bank SET retired=0 WHERE id=?", (bid,))
    assert core.verify(conn) == []  # retire flag is outside the row hash


def _stream(*msgs):
    return "\n".join(json.dumps({"type": "message_end", "message": m}) for m in msgs)


def _fake_omp(monkeypatch, stdout):
    def run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")
    monkeypatch.setattr(llm.subprocess, "run", run)


def test_llm_records_actual_model_after_silent_fallback(conn, monkeypatch, tmp_path):
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "p.md").write_text("Q: {{x}}", encoding="utf-8")
    monkeypatch.setattr(llm, "PROMPTS_DIR", tmp_path / "prompts")
    _fake_omp(monkeypatch, _stream(
        {"role": "assistant", "provider": "zai", "model": "glm-5.3-flash", "stopReason": "error",
         "errorStatus": 429, "errorMessage": "Usage limit", "content": []},
        {"role": "assistant", "provider": "anthropic", "model": "claude-sonnet-5", "stopReason": "stop",
         "content": [{"type": "text", "text": '{"ok": true}'}], "usage": {"input": 5, "output": 3}}))
    res = llm.call("requirements", "p", {"x": "hi"}, job_id="j1", conn=conn)
    assert res.ok and res.actual_model == "anthropic/claude-sonnet-5"
    row = conn.execute("SELECT * FROM llm_calls WHERE id=?", (res.call_id,)).fetchone()
    assert row["requested_model"] == "zai/glm-5.3-flash"
    assert json.loads(row["fallback_errors"])[0]["status"] == 429
    assert core.verify(conn) == []


def test_llm_rejects_answer_from_model_below_stage_rank(conn, monkeypatch, tmp_path):
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "p.md").write_text("audit", encoding="utf-8")
    monkeypatch.setattr(llm, "PROMPTS_DIR", tmp_path / "prompts")
    _fake_omp(monkeypatch, _stream(
        {"role": "assistant", "provider": "zai", "model": "glm-5.3-flash", "stopReason": "stop",
         "content": [{"type": "text", "text": '{"pass": true}'}]}))
    res = llm.call("auditor", "p", {}, conn=conn)
    assert not res.ok and "requires rank" in res.error
    assert conn.execute("SELECT ok FROM llm_calls WHERE id=?", (res.call_id,)).fetchone()["ok"] == 0


def test_render_requires_every_placeholder():
    with pytest.raises(KeyError):
        llm.render("{{a}} {{b}}", {"a": "x"})
