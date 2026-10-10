"""FIT reads jobs.description, but aggregator rows carry only a stub
(Gradcracker ~65 chars, jobs.ac.uk none), and Greenhouse rows carried literal
tag soup. Invariants (2026-10-01): the stub is replaced by the employer's
posting once resolved, survives re-discovery, a job already judged on the stub
is judged again, FIT never sees a stub, and posting text is plain text.

Temp DB only (monkeypatched core.PIPELINE_ROOT), like tests/test_ledger.py.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from discover import db as dbmod  # noqa: E402
from ledger import core  # noqa: E402
from discover.util import html_to_text  # noqa: E402

resolver = importlib.import_module("run.resolve_apply_url")
batch = importlib.import_module("run.batch")

POSTING = "Full employer posting text. " * 40
GREENHOUSE_URL = "https://boards.greenhouse.io/acme/jobs/1"


def _job(**kw):
    base = {"source": "gradcracker", "company": "Acme", "title": "AI Engineer", "location": "London",
            "remote": None, "url": "https://aggregator.example/job/1", "apply_url": None, "ats": None,
            "posted_at": "2026-10-01", "description": "Graduate AI Engineer | Deadline: 1 Nov"}
    return {**base, **kw}


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "PIPELINE_ROOT", tmp_path)
    path = tmp_path / "t.db"
    conn = dbmod.connect(path)
    yield conn, path
    conn.close()


def _resolve(db, tmp_path, monkeypatch, status, posting_text):
    conn, path = db
    jid, _ = dbmod.upsert_job(conn, _job(), "t0")
    conn.execute("UPDATE jobs SET status=? WHERE id=?", (status, jid))
    conn.commit()
    monkeypatch.setattr(resolver, "OUT_ROOT", tmp_path / "out")
    monkeypatch.setattr(resolver, "resolve", lambda *a, **k: (
        {"ats": "greenhouse", "slug": "acme", "score": 1.0, "resolved_from": "greenhouse:acme",
         "posting": {"title": "AI Engineer", "location": "London", "apply_url": GREENHOUSE_URL,
                     "description": posting_text}}, []))
    lconn = core.connect(path)
    try:
        resolver.process_job(conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone(), [], conn, lconn)
    finally:
        lconn.close()
    return conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()


def test_resolver_swaps_the_stub_for_the_employer_posting(db, tmp_path, monkeypatch):
    row = _resolve(db, tmp_path, monkeypatch, "new", POSTING)
    assert row["description"] == POSTING.strip()
    assert row["apply_url"] == GREENHOUSE_URL
    assert row["status"] == "new"  # never FIT-checked, nothing to redo


def test_resolver_sends_an_already_judged_job_back_to_fit(db, tmp_path, monkeypatch):
    # fit_pass was decided on the stub; it has to be decided again on the real text
    assert _resolve(db, tmp_path, monkeypatch, "fit_pass", POSTING)["status"] == "new"


def test_resolver_never_replaces_text_with_a_shorter_posting(db, tmp_path, monkeypatch):
    row = _resolve(db, tmp_path, monkeypatch, "fit_pass", "too short")
    assert row["description"] == _job()["description"]
    assert row["status"] == "fit_pass"


def test_rediscovery_keeps_resolved_url_and_posting_text(db):
    conn, _ = db
    jid, _ = dbmod.upsert_job(conn, _job(), "t0")
    conn.execute("UPDATE jobs SET apply_url=?, ats=?, description=? WHERE id=?",
                 (GREENHOUSE_URL, "greenhouse", POSTING, jid))
    dbmod.upsert_job(conn, _job(title="AI Engineer (Graduate)"), "t1")  # aggregator re-fetch: no apply_url
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    assert (row["apply_url"], row["ats"], row["description"]) == (GREENHOUSE_URL, "greenhouse", POSTING)
    assert row["title"] == "AI Engineer (Graduate)"  # everything else still refreshes

    dbmod.upsert_job(conn, _job(apply_url=GREENHOUSE_URL + "2", ats="greenhouse", description="edited"), "t2")
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    assert (row["apply_url"], row["description"]) == (GREENHOUSE_URL + "2", "edited")  # a source that carries them wins


def test_fit_only_selects_jobs_with_a_real_posting(db, monkeypatch):
    conn, path = db
    lengths = (0, 65, batch.MIN_JD_CHARS - 1, batch.MIN_JD_CHARS, 5000)
    ids = [dbmod.upsert_job(conn, _job(url=f"https://x.example/{n}", description="x" * n), "t0")[0]
           for n in lengths]
    real_connect = core.connect
    monkeypatch.setattr(core, "connect", lambda *a: real_connect(path))
    picked = [j["id"] for j in batch.jobs_with_status("new", "id", 10, batch.MIN_JD_CHARS)]
    assert sorted(picked) == sorted(ids[3:])


def test_greenhouse_escaped_markup_becomes_plain_text():
    # Greenhouse's `content` is HTML entity-escaped; it used to be stored as literal tag soup
    # (all ~11k Greenhouse rows, ~145 tags each) and FIT read that as the posting.
    escaped = "&lt;h2&gt;About us&amp;nbsp;&lt;/h2&gt;&lt;p&gt;We build &amp;amp; ship.&lt;/p&gt;"
    assert html_to_text(escaped) == "About us\nWe build & ship."


def test_board_fetch_is_cached_per_process_including_failures(monkeypatch):
    # Five Harnham postings used to re-probe the same ATS boards five times each; the repeated
    # probing is what rate-limited Workable (HTTP 429) for discovery as well.
    monkeypatch.setattr(resolver, "_FETCH_CACHE", {})
    calls = []

    def fetch(slug):
        calls.append(slug)
        if slug == "boom":
            raise RuntimeError("429")
        return [{"title": "x"}]

    monkeypatch.setattr(resolver, "FETCHERS", {"workable": fetch})
    assert resolver._cached_fetch("workable", "acme") == resolver._cached_fetch("workable", "acme")
    for _ in range(2):
        with pytest.raises(RuntimeError):
            resolver._cached_fetch("workable", "boom")
    assert calls == ["acme", "boom"]  # one fetch per board, the failed one too


def test_resolver_does_not_reprobe_jobs_it_tried_recently(db):
    conn, path = db
    fresh = dbmod.upsert_job(conn, _job(url="https://x.example/fresh"), "t0")[0]
    tried = dbmod.upsert_job(conn, _job(url="https://x.example/tried"), "t0")[0]
    lconn = core.connect(path)
    try:
        with core.tx(lconn):
            core.append_event(lconn, "code:resolve_apply_url", "job.status",
                              {"from": "new", "to": "new", "reason": "apply_url unresolved: x"}, job_id=tried)
    finally:
        lconn.close()
    throttled = dbmod.upsert_job(conn, _job(url="https://x.example/throttled"), "t0")[0]
    lconn = core.connect(path)
    try:
        with core.tx(lconn):
            core.append_event(lconn, "code:resolve_apply_url", "job.status",
                              {"from": "new", "to": "new",
                               "reason": "apply_url unresolved: workable:acme -> error (429 Client Error: Too Many Requests)"},
                              job_id=throttled)
    finally:
        lconn.close()
    picked = {r["id"] for r in resolver.select_all_missing(conn, 3)}
    # tried a moment ago with a definitive answer -> left alone; probe that only hit a 429 answered nothing -> retried
    assert picked == {fresh, throttled}
    assert {r["id"] for r in resolver.select_all_missing(conn, -1)} == {fresh, tried, throttled}  # window elapsed


def test_resolve_falls_back_to_the_aggregators_own_page(monkeypatch):
    monkeypatch.setattr(resolver, "FETCHERS", {})  # no board probing in this test
    page = ('<div class="tw-flex tw-gap-12 md:tw-container"><div class="job-description mb20">'
            '<div class="body-content"><p>We build &amp; ship models.</p></div></div>'
            '<a href="https://www.gradcracker.com/out/691/84528/tok">Apply online now</a>')

    class _Resp:
        status_code, text = 200, page

    fetched = []
    monkeypatch.setattr(resolver, "polite_get", lambda url, **kw: (fetched.append(url), _Resp())[1])
    result, _ = resolver.resolve("Reply", "Graduate AI Engineer", None, [],
                                 "https://www.gradcracker.com/hub/691/reply/graduate-job/84528/x")
    assert fetched == ["https://www.gradcracker.com/hub/691/reply/graduate-job/84528/x"]  # /out itself is never fetched
    assert result["resolved_from"] == "aggregator-page:www.gradcracker.com"
    assert result["ats"] == "other"
    assert result["posting"]["apply_url"].endswith("/out/691/84528/tok")
    assert result["posting"]["description"] == "We build & ship models."

    _Resp.text = page.replace("https://www.gradcracker.com/out/691/84528/tok", "#nowhere")
    result2, attempts = resolver.resolve("Reply", "Graduate AI Engineer", None, [],
                                         "https://www.gradcracker.com/hub/691/reply/graduate-job/84528/x")
    assert result2 is None and any("no apply link" in a for a in attempts)


def test_region_wide_posting_matches_a_job_that_lists_the_uk_but_country_specific_postings_do_not():
    # Amplemarket: Himalayas lists the countries (UK among them); the employer's Greenhouse says "Remote, EMEA/LATAM".
    uk_listed = "Austria, Belgium, United Kingdom, Germany"
    assert resolver.location_ok("Remote, EMEA/LATAM", uk_listed) is True
    assert resolver.location_ok("Remote, EMEA/LATAM", "Austria, Belgium") is False
    assert resolver.location_ok("Berlin, Germany", uk_listed) is False
