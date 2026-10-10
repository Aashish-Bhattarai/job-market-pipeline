"""Parallel FIT/tailor stages (2026-10-02): 50 applications in one round is hours of work if every
model call is sequential. Invariants: every job is still processed exactly once with N workers,
and the one shared resource that is not safe to parallelise -- MS Word PDF conversion -- stays
mutually exclusive across processes."""
from __future__ import annotations

import importlib
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import stages.common  # noqa: E402,F401  (puts pipeline/render on sys.path)

batch = importlib.import_module("run.batch")
render_common = importlib.import_module("common")  # pipeline/render/common.py


def test_word_conversion_lock_excludes_concurrent_holders(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    inside = peak = 0
    guard = threading.Lock()

    def worker():
        nonlocal inside, peak
        with render_common._converter_lock():
            with guard:
                inside += 1
                peak = max(peak, inside)
            time.sleep(0.05)
            with guard:
                inside -= 1

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak == 1
    assert not list(tmp_path.glob("*.lock"))  # released, nothing left behind


def test_parallel_stages_process_every_job_exactly_once(monkeypatch):
    seen = []
    lock = threading.Lock()
    ids = [f"j{i}" for i in range(9)]

    def fake_run_step(args, label, job_id=None, timeout=3600):
        with lock:
            seen.append((label, job_id))
        return 0

    monkeypatch.setattr(batch, "run_step", fake_run_step)
    monkeypatch.setattr(batch, "jobs_with_status",
                        lambda *a, **k: [{"id": i, "title": "AI Engineer", "company": f"Co{i}"} for i in ids])
    monkeypatch.setattr(batch, "_title_ok", lambda t: True)
    monkeypatch.setattr(batch.core, "connect", lambda *a: (_ for _ in ()).throw(RuntimeError("no db in this test")))
    monkeypatch.setattr(batch, "companies_at_cap", lambda: {"coj3"})  # 2 applications in 30 days already
    monkeypatch.setattr(batch, "_slots_taken", lambda: {})            # (reads the DB; covered by the slot-limit test)

    batch.stage_tailor(20, workers=4)
    # a capped company is not tailored (the apply step would hold it); every other job exactly once
    assert sorted(j for l, j in seen if l == "tailor") == sorted(i for i in ids if i != "j3")


# ---- submission caps (2026-10-02) -------------------------------------------------------------
# auto_submit tailored jobs used to bypass submissions.enabled and daily_cap entirely, and the cap
# was only read once before the run, so it could never stop a run midway.

import datetime as dt  # noqa: E402
import sqlite3  # noqa: E402


def _caps_db(path):
    c = sqlite3.connect(str(path))
    c.row_factory = sqlite3.Row
    c.executescript("CREATE TABLE jobs(id TEXT PRIMARY KEY, status TEXT, company TEXT, apply_url TEXT);"
                    "CREATE TABLE applications(id INTEGER PRIMARY KEY, job_id TEXT, status TEXT, started_at TEXT, note TEXT);")
    return c


def _utc(delta_hours=0.0):
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=delta_hours)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def test_submit_blocked_by_switch_start_date_and_daily_cap(tmp_path):
    c = _caps_db(tmp_path / "t.db")
    on = {"submissions": {"enabled": True, "daily_cap": 2}}
    assert batch._submit_blocked(on, c) is None
    c.executemany("INSERT INTO applications (status, started_at) VALUES (?, ?)",
                  [("submitted", _utc()), ("queued_manual", _utc()), ("submitted", _utc(-30))])  # only 1 counts today
    assert batch._submit_blocked(on, c) is None
    c.execute("INSERT INTO applications (status, started_at) VALUES ('submitted', ?)", (_utc(),))
    assert "daily submission cap (2/day)" in batch._submit_blocked(on, c)
    assert batch._submit_blocked({"submissions": {"enabled": True, "daily_cap": None}}, c) is None
    assert "enabled is false" in batch._submit_blocked({"submissions": {"enabled": False}}, c)
    tomorrow = (dt.date.today() + dt.timedelta(days=1)).isoformat()
    assert "enabled_from" in batch._submit_blocked({"submissions": {"enabled": True, "enabled_from": tomorrow}}, c)


def _run_stage_apply(tmp_path, monkeypatch, jobs, cap, failing=(), asks=(), walled=(), prior_wall=()):
    """stage_apply over `jobs` [(id, company)] with a faked browser step; ids in `failing` end
    queued_manual, ids in `asks` end queued_manual AND surface an open required question, the rest
    are submitted. Returns (attempted ids, applied ids, number of stage_questions runs)."""
    dbfile = tmp_path / "t.db"
    seed = _caps_db(dbfile)
    seed.executemany("INSERT INTO jobs (id, status, company) VALUES (?, 'tailored', ?)", jobs)
    for jid in prior_wall:   # a job re-queued by hand after its last attempt hit a wall
        seed.execute("INSERT INTO applications (job_id, status, started_at, note) VALUES (?, 'submit_failed', ?, ?)",
                     (jid, _utc(-3), "A captcha challenge was raised after Submit and the application was not sent: the site wants a human check (bot wall), not retried"))
    seed.commit()
    seed.close()

    def connect(*_a):
        c = sqlite3.connect(str(dbfile))
        c.row_factory = sqlite3.Row
        return c

    attempted, surfaced, questions_runs = [], set(), []

    def fake_apply_one(job_id, mode, answers):
        attempted.append(job_id)
        c = connect()
        if job_id in asks:
            surfaced.add(job_id)
        if job_id in walled:
            c.execute("INSERT INTO applications (job_id, status, started_at, note) VALUES (?, 'submit_failed', ?, ?)",
                      (job_id, _utc(), "We couldn't submit your applicationYour application submission was flagged as possible spam."))
        if job_id in failing or job_id in asks or job_id in walled:
            c.execute("UPDATE jobs SET status='queued_manual' WHERE id=?", (job_id,))
        else:
            c.execute("UPDATE jobs SET status='applied' WHERE id=?", (job_id,))
            c.execute("INSERT INTO applications (status, started_at) VALUES ('submitted', ?)", (_utc(),))
        c.commit()
        c.close()
        return "ok"

    monkeypatch.setattr(batch.core, "connect", connect)
    monkeypatch.setattr(batch.llm, "load_config", lambda: {"auto_submit": True,
                        "submissions": {"enabled": True, "daily_cap": cap}})
    monkeypatch.setattr(batch, "companies_at_cap", lambda: set())
    monkeypatch.setattr(batch, "_title_ok", lambda t: True)
    monkeypatch.setattr(batch, "_open_required_questions", lambda conn, jid: 1 if jid in surfaced else 0)
    monkeypatch.setattr(batch, "stage_questions", lambda: questions_runs.append(1))
    monkeypatch.setattr(batch, "jobs_with_status", lambda status, *a, **k: (
        [{"id": j, "title": "AI Engineer", "fit_score": 100} for j, _ in jobs] if status == "tailored" else []))
    monkeypatch.setattr(batch, "apply_one", fake_apply_one)
    monkeypatch.setattr(batch, "log", lambda *a, **k: None)
    batch.stage_apply(60)
    return (attempted, [r["id"] for r in connect().execute("SELECT id FROM jobs WHERE status='applied' ORDER BY id")],
            len(questions_runs))


def test_stage_apply_stops_at_the_daily_cap_midway_and_a_failed_form_does_not_use_a_company_slot(tmp_path, monkeypatch):
    jobs = [("a1", "Acme"), ("a2", "Acme"), ("a3", "Acme"), ("b1", "Bravo"), ("c1", "Cobalt"), ("d1", "Delta")]
    attempted, applied, _ = _run_stage_apply(tmp_path, monkeypatch, jobs, cap=4, failing={"a1"})
    assert len(applied) == 4 and "d1" not in applied  # cap of 4 held mid-run although 6 jobs were queued
    assert "a2" in applied and "a3" in applied        # Acme's failed a1 did not use up a slot
    assert attempted == ["a1", "a2", "a3", "b1", "c1"]  # d1 and a1's end-of-round retry were held by the cap


def test_a_failing_form_is_retried_exactly_once_at_the_end_after_questions_are_answered(tmp_path, monkeypatch):
    attempted, applied, questions_runs = _run_stage_apply(
        tmp_path, monkeypatch, [("z1", "Zeta"), ("y1", "Yankee")], cap=None, failing={"z1"})
    assert attempted == ["z1", "y1", "z1"] and applied == ["y1"]
    assert questions_runs == 1   # the surfaced questions are answered once, before the retry


def test_no_retry_when_the_form_still_needs_a_human_answer(tmp_path, monkeypatch):
    attempted, applied, questions_runs = _run_stage_apply(
        tmp_path, monkeypatch, [("w1", "Whiskey"), ("v1", "Victor")], cap=None, asks={"w1"})
    assert attempted == ["w1", "v1"] and applied == ["v1"]   # w1 still has an open required question: no retry
    assert questions_runs == 1


def test_questions_stage_is_not_run_by_the_apply_stage_when_nothing_failed(tmp_path, monkeypatch):
    attempted, applied, questions_runs = _run_stage_apply(tmp_path, monkeypatch, [("u1", "Uniform")], cap=None)
    assert applied == ["u1"] and questions_runs == 0


def test_a_form_stopped_by_a_spam_wall_is_not_retried(tmp_path, monkeypatch):
    # Ashby answered "flagged as possible spam" to a submit and again to the retry minutes later.
    attempted, applied, _ = _run_stage_apply(tmp_path, monkeypatch, [("s1", "Spamco"), ("o1", "Okco")], cap=None, walled={"s1"})
    assert attempted == ["s1", "o1"] and applied == ["o1"]        # one attempt, no retry
    (tmp_path / "b").mkdir()
    attempted2, _, _ = _run_stage_apply(tmp_path / "b", monkeypatch, [("f1", "Failco")], cap=None, failing={"f1"})
    assert attempted2 == ["f1", "f1"]                              # an ordinary failure still gets its one retry


def test_tailoring_stops_at_each_companys_remaining_cap_slots():
    # Bjak posted a dozen near-identical roles (2026-10-02): twelve tailoring runs for at most two submissions
    jobs = [{"id": f"b{i}", "company": "Bjak"} for i in range(12)] + [{"id": f"f{i}", "company": "Faculty AI"} for i in range(3)]
    kept = batch.limit_per_company(jobs, {"faculty ai": 1})
    assert [j["id"] for j in kept] == ["b0", "b1", "f0"]      # 2 Bjak slots, 1 Faculty slot left, order preserved
    assert batch.limit_per_company(jobs, {"bjak": 2, "faculty ai": 2}) == []


def test_a_submit_with_no_error_and_no_confirmation_is_never_retried_but_a_validation_failure_may_be(tmp_path):
    # Stripe's confirmation renders in an embedded frame: both roles were submitted while the runner said
    # "submit_failed", and Deliveroo got three real applications from retries of the same kind of ambiguity.
    conn = _caps_db(tmp_path / "u.db")
    conn.execute("INSERT INTO jobs (id, status, company) VALUES('j1','tailored','Stripe'), ('j2','tailored','Acme'), ('j3','tailored','Acme2')")
    conn.executemany("INSERT INTO applications(job_id, status, started_at, note) VALUES(?,?,?,?)", [
        ("j1", "submit_failed", "2026-10-02T10:00:00Z", "No success confirmation detected after clicking submit"),
        ("j2", "submit_failed", "2026-10-02T10:00:00Z", "page after submit: This field is required"),
        ("j3", "submitted", "2026-10-02T10:00:00Z", ""),
    ])
    assert batch._unconfirmed_submit(conn, "j1") is True
    assert batch._unconfirmed_submit(conn, "j2") is False      # real validation text: a retry is legitimate
    assert batch._unconfirmed_submit(conn, "j3") is False


def test_a_company_side_refusal_is_a_wall_never_retried_but_a_fill_error_is_not():
    # ElevenLabs' Ashby form refused both the first click and the automatic retry (90-day-per-domain rule).
    refusal = ("We couldn't submit your applicationI'm sorry we are limiting applications to give everyone a chance. "
               "As you have applied for a position in this domain within the last 90 days, you cannot submit an application")
    assert batch._HUMAN_WALL_RE.search(refusal)
    assert batch._HUMAN_WALL_RE.search("You have already applied for this position")
    assert not batch._HUMAN_WALL_RE.search('"Email" read back "" after setting "a@b.c"')


def test_attempt_cap_counts_only_submits_that_may_have_been_sent(tmp_path):
    # Jane Street: four clicks refused by the form's own validation sent nothing and must not use up the cap.
    conn = _caps_db(tmp_path / "cap.db")
    conn.executemany("INSERT INTO applications(job_id, status, started_at, note) VALUES(?,?,?,?)", [
        ("jc", "submit_failed", "2026-10-03T00:00:00Z", "This field is required."),
        ("jc", "submit_failed", "2026-10-03T00:01:00Z", "This field is required."),
        ("jc", "submit_failed", "2026-10-03T00:02:00Z", batch._AMBIGUOUS_NOTE),
        ("jc", "queued_manual", "2026-10-03T00:03:00Z", None),
        ("jc", "submitted", "2026-10-03T00:04:00Z", ""),
    ])
    assert batch._risky_submit_count(conn, "jc") == 2        # the ambiguous click and the confirmed one
    assert batch._risky_submit_count(conn, "other") == 0


def test_one_posting_under_two_rows_is_never_submitted_twice(tmp_path):
    # OQC's Data Engineer came back from an aggregator as a second row (another company spelling, another source) for the
    # page already applied to; the shortcode is the same however the URL is dressed.
    c = _caps_db(tmp_path / "t.db")
    c.executemany("INSERT INTO jobs (id, status, company, apply_url) VALUES (?,?,?,?)", [
        ("applied1", "applied", "Oxford Quantum Circuits 8", "https://apply.workable.com/j/5821573B68/apply"),
        ("twin", "tailored", "Oxford Quantum Circuits", "https://apply.workable.com/oxford-quantum-circuits-8/j/5821573b68/?utm_source=x"),
        ("other", "tailored", "Oxford Quantum Circuits", "https://apply.workable.com/oxford-quantum-circuits-8/j/0A1B2C3D4E/"),
        ("lever_a", "applied", "Acme", "https://jobs.lever.co/acme/6A2A32AB-2ED8-4CE3-94D5-0BE9BE9B01B7/apply"),
        ("lever_b", "tailored", "Acme", "https://jobs.lever.co/acme/6a2a32ab-2ed8-4ce3-94d5-0be9be9b01b7"),
        ("none", "tailored", "Nobody", None)])
    assert batch.duplicate_of_applied(c, "twin") == "applied1"
    assert batch.duplicate_of_applied(c, "lever_b") == "lever_a"
    assert batch.duplicate_of_applied(c, "other") is None          # a different role at the same employer
    assert batch.duplicate_of_applied(c, "none") is None
    assert batch.duplicate_of_applied(c, "applied1") is None       # never its own duplicate


def test_stage_apply_skips_a_row_that_is_already_applied_under_another_id(tmp_path, monkeypatch):
    import stages.common as sc
    monkeypatch.setattr(sc, "set_job_status", lambda conn, jid, status, actor, reason=None:
                        conn.execute("UPDATE jobs SET status=? WHERE id=?", (status, jid)))
    dbfile = tmp_path / "t.db"
    seed = _caps_db(dbfile)
    seed.executemany("INSERT INTO jobs (id, status, company, apply_url) VALUES (?,?,?,?)", [
        ("done", "applied", "Acme 8", "https://apply.workable.com/j/5821573B68/apply"),
        ("twin", "tailored", "Acme", "https://apply.workable.com/acme/j/5821573B68/"),
        ("fresh", "tailored", "Beta", "https://apply.workable.com/beta/j/AAAAAAAAAA/")])
    seed.commit()
    seed.close()

    def connect(*_a):
        c = sqlite3.connect(str(dbfile)); c.row_factory = sqlite3.Row
        return c
    attempted = []
    monkeypatch.setattr(batch.core, "connect", connect)
    import contextlib

    @contextlib.contextmanager
    def tx(conn):
        yield conn
        conn.commit()
    monkeypatch.setattr(batch.core, "tx", tx)
    monkeypatch.setattr(batch.llm, "load_config", lambda: {"auto_submit": True, "submissions": {"enabled": True, "daily_cap": 50}})
    monkeypatch.setattr(batch, "companies_at_cap", lambda: set())
    monkeypatch.setattr(batch, "_title_ok", lambda t: True)
    monkeypatch.setattr(batch, "stage_questions", lambda: None)
    monkeypatch.setattr(batch, "_open_required_questions", lambda conn, jid: 0)
    monkeypatch.setattr(batch, "jobs_with_status", lambda status, *a, **k: (
        [{"id": j, "title": "AI Engineer", "fit_score": 100} for j in ("twin", "fresh")] if status == "tailored" else []))
    def fake_apply_one(job_id, mode, answers):
        attempted.append(job_id)
        c = connect(); c.execute("UPDATE jobs SET status='applied' WHERE id=?", (job_id,)); c.commit(); c.close()
        return "ok"
    monkeypatch.setattr(batch, "apply_one", fake_apply_one)
    monkeypatch.setattr(batch, "log", lambda *a, **k: None)
    batch.stage_apply(60)
    c = connect()
    assert attempted == ["fresh"]
    assert c.execute("SELECT status FROM jobs WHERE id='twin'").fetchone()[0] == "skipped"


def test_a_job_whose_last_attempt_hit_a_wall_is_not_clicked_again_when_it_is_requeued(tmp_path, monkeypatch):
    attempted, applied, _ = _run_stage_apply(tmp_path, monkeypatch, [("jwall", "CauseCo"), ("jfresh", "Other")], cap=50, prior_wall=("jwall",))
    assert attempted == ["jfresh"] and applied == ["jfresh"]
