"""Tests for the discovery sources (2026-09-28): no network, canned data.

Covers the invariants that matter: a source with a missing API key skips with
a printed reason instead of crashing the batch; Reed rows carry the whole
posting as plain text (never the search snippet) and never hammer the API; the
HN UK filter keeps UK roles and rejects US-only posts and unstructured comments.
"""
import importlib
import json
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from discover.sources import _settings  # noqa: E402

reed = importlib.import_module("discover.sources.reed")
hnhiring = importlib.import_module("discover.sources.hnhiring")
himalayas = importlib.import_module("discover.sources.himalayas")


@pytest.fixture
def keyless_config(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("sources:\n  reed: {api_key: ''}\n", encoding="utf-8")
    monkeypatch.setattr(_settings, "CONFIG_PATH", cfg)
    return cfg


def test_reed_skips_cleanly_without_a_key(keyless_config, capsys):
    assert reed.fetch({}) == []
    assert "reed" in capsys.readouterr().out


def test_real_config_path_is_the_pipeline_config():
    # Regression (2026-10-01): CONFIG_PATH resolved to pipeline/discover/config.yaml
    # (nonexistent), so keys in the real pipeline/config.yaml were silently ignored.
    assert _settings.CONFIG_PATH.is_file()
    assert "sources" in yaml.safe_load(_settings.CONFIG_PATH.read_text(encoding="utf-8"))


class _Resp:
    def __init__(self, payload, status=200):
        self._payload, self.status_code, self.ok = payload, status, status < 400

    def json(self):
        return self._payload

    def raise_for_status(self):
        assert self.ok


@pytest.fixture
def reed_api(tmp_path, monkeypatch):
    """Fake Reed API: install(search_results, details) -> list of requested URLs.
    `details` maps jobId -> payload dict, or an int HTTP status for a failing call."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("sources:\n  reed: {api_key: 'k'}\n", encoding="utf-8")
    monkeypatch.setattr(_settings, "CONFIG_PATH", cfg)
    monkeypatch.setattr(reed, "SEARCHES", ["ai engineer"])
    monkeypatch.setattr(reed, "GRADUATE_SEARCHES", [])
    monkeypatch.setattr(reed, "LOCATIONS", [None])
    monkeypatch.setattr(reed, "MAX_PAGES_PER_SEARCH", 1)
    monkeypatch.setattr(reed.time, "sleep", lambda _s: None)

    def install(results, details):
        calls = []

        def fake_get(url, params=None, auth=None, timeout=None):
            calls.append(url)
            if url == reed.API:
                return _Resp({"results": results})
            d = details[int(url.rsplit("/", 1)[1])]
            return _Resp({}, d) if isinstance(d, int) else _Resp(d)

        monkeypatch.setattr(reed.requests, "get", fake_get)
        return calls

    return install


def _reed_job(job_id, title="AI Engineer"):
    return {"jobId": job_id, "jobTitle": title, "employerName": "Acme", "locationName": "London",
            "jobUrl": f"https://www.reed.co.uk/jobs/{job_id}", "date": "28/09/2026",
            "jobDescription": "search snippet only..."}


def test_reed_ingests_the_whole_posting_as_text_never_the_snippet(reed_api):
    calls = reed_api(
        [_reed_job(1), _reed_job(2), _reed_job(3, "Warehouse Operative")],
        {1: {"jobDescription": "<p>Full &amp; complete posting</p>"}, 2: 500})
    rows = reed.fetch({})
    assert [r["url"] for r in rows] == ["https://www.reed.co.uk/jobs/1"]  # job 2: details failed -> skipped
    assert rows[0]["description"] == "Full & complete posting"            # HTML stripped
    assert not any(u.endswith("/3") for u in calls)                        # off-family title: no details call


def test_reed_details_rate_limit_stops_the_source(reed_api):
    calls = reed_api([_reed_job(1), _reed_job(2)], {1: 429, 2: {"jobDescription": "x"}})
    assert reed.fetch({}) == []
    assert not any(u.endswith("/2") for u in calls)


def test_reed_keeps_the_employers_own_apply_link_and_its_ats(reed_api):
    # Reed's details API returns `externalUrl` (the employer's application page) for jobs not applied
    # for inside Reed; without it a Reed job can only be applied for behind Reed's login.
    reed_api([_reed_job(1), _reed_job(2), _reed_job(3)],
             {1: {"jobDescription": "x", "externalUrl": "https://boards.greenhouse.io/acme/jobs/9"},
              2: {"jobDescription": "x", "externalUrl": "https://acme.wd3.myworkdayjobs.com/en/job/1"},
              3: {"jobDescription": "x", "externalUrl": None}})
    rows = {r["url"].rsplit("/", 1)[1]: r for r in reed.fetch({})}
    assert (rows["1"]["apply_url"], rows["1"]["ats"]) == ("https://boards.greenhouse.io/acme/jobs/9", "greenhouse")
    assert rows["2"]["ats"] == "workday"           # tailor's account gate then queues it, never automated
    assert (rows["3"]["apply_url"], rows["3"]["ats"]) == (None, None)  # Reed-only apply: left for the resolver


def test_hn_uk_filter():
    keep = hnhiring._parse_comment({"id": 1, "text": "Acme AI | ML Engineer | London (hybrid) | "
                                    "We build vision models. Python, PyTorch."})
    assert keep and keep["company"] == "Acme AI" and keep["title"] == "ML Engineer"
    assert keep["url"].endswith("item?id=1")

    us_only = hnhiring._parse_comment({"id": 2, "text": "USCorp | Backend | San Francisco. "
                                       "Must be in the US. Python."})
    assert us_only is None

    non_uk = hnhiring._parse_comment({"id": 3, "text": "EuroCorp | ML | Berlin. PyTorch."})
    assert non_uk is None

    unstructured = hnhiring._parse_comment({"id": 4, "text": "just a question about the thread"})
    assert unstructured is None

    remote_uk = hnhiring._parse_comment({"id": 5, "text": "RemoteCo | DS | Remote (UK/Europe). "
                                         "Pandas, SQL."})
    assert remote_uk is not None


def test_hn_html_stripping():
    row = hnhiring._parse_comment({"id": 6, "text": "<p>HyperCo &#124; MLE | London (hybrid)</p> "
                                   "<a href=\"x\">We train large vision models, apply via site.</a>"})
    assert row and "<p>" not in row["description"]


def test_title_family_admits_cv_evidenced_developer_roles_but_not_unrelated_engineers():
    from discover.filter import title_reason
    for ok in ("Graduate Software Engineer", "Junior Python Developer", "Full Stack Engineer",
               "Front End Developer", "Application Developer", "Machine Learning Engineer",
               "Backend Product Engineer (Mid-level)", "Software Development Engineer - 2026",
               "AI Forward Deployed Engineer - London", "Forward Deployed Engineer", "Research Scientist, Wayve Labs",
               "Research Engineer - Inference", "Applied AI Scientist - LLMs & Voice", "ML Operations Engineer",
               "Graduate AI Test Automation Engineer"):
        assert title_reason(ok) is None, ok
    # seniority, interns and unrelated engineering disciplines stay out
    assert title_reason("Senior Software Engineer")
    assert title_reason("Software Engineering Intern")
    for no in ("Platform Engineer", "Solutions Engineer", "Security Engineer", "Sales Engineer", "AI Sales Engineer",
               "AI Solutions Engineer - Pre-sales", "Technical Recruiter", "Regional Research Economist"):
        assert title_reason(no) == "title not in AI/ML/DS family", no


def test_discovery_retries_a_network_blip_once_but_never_a_404(tmp_path, monkeypatch):
    # 2026-10-02: a DNS blip failed 120+ boards (NameResolutionError) mid-round and the round carried
    # on with a silently shrunken funnel. A failed board gets one retry after a pause; a 404 is a
    # permanent "no such board" and is not hammered.
    from discover import discover as disc
    from ledger import core
    monkeypatch.setattr(core, "PIPELINE_ROOT", tmp_path)
    companies = tmp_path / "c.yaml"
    companies.write_text(
        "- {name: Ok, ats: greenhouse, slug: ok}\n- {name: Flaky, ats: greenhouse, slug: flaky}\n"
        "- {name: Gone, ats: greenhouse, slug: gone}\n- {name: Slow, ats: greenhouse, slug: slow}\n", encoding="utf-8")
    calls = {}

    class Fake:
        @staticmethod
        def fetch(row):
            n = calls[row["slug"]] = calls.get(row["slug"], 0) + 1
            if row["slug"] == "gone":
                raise RuntimeError("404 Client Error: Not Found")
            if row["slug"] == "slow":
                raise RuntimeError("429 Client Error: Too Many Requests")
            if row["slug"] == "flaky" and n == 1:
                raise RuntimeError("NameResolutionError: Max retries exceeded")
            return [{"source": "greenhouse", "company": row["name"], "title": "ML Engineer", "location": "London",
                     "remote": None, "url": f"https://x.example/{row['slug']}", "apply_url": None, "ats": "greenhouse",
                     "posted_at": "2026-10-01", "description": "d" * 400}]

    monkeypatch.setattr(disc, "REGISTRY", {"greenhouse": Fake})
    monkeypatch.setattr(disc, "_ATS_MODULES", {"greenhouse"})
    monkeypatch.setattr(disc, "RETRY_PAUSE_S", 0)
    res = disc.run(companies, ["greenhouse"], tmp_path / "t.db")["greenhouse"]
    assert res["fetched"] == 2                       # Ok + Flaky (second try)
    assert calls == {"ok": 1, "flaky": 2, "gone": 1, "slow": 1}  # 404 and 429 boards were asked exactly once
    assert len(res["errors"]) == 2 and any("Gone" in e for e in res["errors"]) and any("Slow" in e for e in res["errors"])


def test_himalayas_walks_pages_until_a_short_page_and_stops_cleanly_on_429(monkeypatch):
    # The documented API returns 20 jobs per page and rate-limits with 429; the old fetch read page 1 only.
    def job(i):
        return {"guid": f"g{i}", "title": "Software Engineer", "companyName": "Acme", "locationRestrictions": [],
                "categories": [], "parentCategories": [], "description": "d", "pubDate": 1.7e12,
                "applicationLink": f"https://himalayas.app/j/{i}"}

    calls = []

    class Resp:
        def __init__(self, status, jobs): self.status_code, self._j = status, jobs
        def raise_for_status(self): assert self.status_code < 400
        def json(self): return {"jobs": self._j}

    def fake_get(self, url, params=None, timeout=None):
        calls.append((params["q"], params["page"]))
        if params["q"] == "limited":
            return Resp(429, [])
        n = {1: 20, 2: 20, 3: 5}.get(params["page"], 0)
        return Resp(200, [job(params["page"] * 100 + k) for k in range(n)])

    monkeypatch.setattr(himalayas.requests.Session, "get", fake_get)
    monkeypatch.setattr(himalayas.time, "sleep", lambda _s: None)
    rows = himalayas.fetch({"queries": ["swe"]})
    assert len(rows) == 45 and calls == [("swe", 1), ("swe", 2), ("swe", 3)]   # stops at the short page
    calls.clear()
    assert himalayas.fetch({"queries": ["limited", "swe"]}) == [] and calls == [("limited", 1)]  # 429: stop, no more calls


def test_polite_get_honours_retry_after_across_calls_and_lets_go_when_it_lapses(tmp_path, monkeypatch):
    # Workable's Cloudflare answered "1015 / Retry-After: 15.8h" to a pipeline that probed it every round.
    import time
    import types
    from discover import util
    monkeypatch.setattr(util, "COOLDOWN_FILE", tmp_path / "cool.json")
    monkeypatch.setattr(util, "MIN_INTERVAL", 0)
    calls = []

    class Resp:
        def __init__(self, status, retry_after=None):
            self.status_code, self.headers = status, ({"Retry-After": retry_after} if retry_after else {})

    def fake_get(url, **kw):
        calls.append(url)
        return Resp(429, "3600") if len(calls) == 1 else Resp(200)

    monkeypatch.setattr(util.requests, "get", fake_get)
    assert util.polite_get("https://h.example/a").status_code == 429
    with pytest.raises(util.HostCoolingDown):
        util.polite_get("https://h.example/b")
    assert calls == ["https://h.example/a"]                        # the second request was never sent
    assert util.polite_get("https://other.example/x").status_code == 200   # other hosts unaffected
    later = time.time() + 3601
    monkeypatch.setattr(util, "time", types.SimpleNamespace(time=lambda: later, monotonic=time.monotonic, sleep=time.sleep))
    assert util.polite_get("https://h.example/c").status_code == 200      # Retry-After has passed
    # a header must never become a multi-day blackout, nor a hair-trigger of seconds
    huge = Resp(429, "999999999")
    util.note_rate_limit("big.example", huge)
    assert util.cooldown_remaining("big.example") <= util.MAX_COOLDOWN_S + 5


def test_workable_is_fetched_at_most_once_per_six_seconds_while_other_hosts_keep_one_second(tmp_path, monkeypatch):
    # Workable's Cloudflare rate-limited us for 15.8h after a steady 1 req/s (2026-10-02)
    from discover import util
    monkeypatch.setattr(util, "COOLDOWN_FILE", tmp_path / "cool.json")
    monkeypatch.setattr(util, "_last_request_at", {})
    slept = []
    clock = [1000.0]
    monkeypatch.setattr(util.time, "sleep", lambda s: (slept.append(round(s, 1)), clock.__setitem__(0, clock[0] + s)))
    monkeypatch.setattr(util.time, "monotonic", lambda: clock[0])

    class Resp:
        status_code, headers = 200, {}
    monkeypatch.setattr(util.requests, "get", lambda url, **kw: Resp())
    util.polite_get("https://apply.workable.com/api/v1/widget/accounts/a")
    util.polite_get("https://apply.workable.com/api/v1/widget/accounts/b")
    util.polite_get("https://boards-api.greenhouse.io/v1/boards/x/jobs")
    util.polite_get("https://boards-api.greenhouse.io/v1/boards/y/jobs")
    assert slept == [6.0, 1.0]        # workable waited the full 6s; greenhouse only its usual 1s


def test_recruitee_remote_offers_keep_their_country_not_the_ui_label(monkeypatch):
    from discover.sources import recruitee

    class Resp:
        def raise_for_status(self): pass
        def json(self):
            return {"offers": [
                {"title": "AI Research Engineer", "location": "Remote job", "city": "London", "country": "United Kingdom", "remote": True,
                 "slug": "ai-uk", "published_at": "2026-10-01 10:00:00 UTC", "description": "<p>d</p>"},
                {"title": "Data Engineer", "location": "London, Greater London, United Kingdom", "city": "", "country": "", "remote": False,
                 "slug": "de", "published_at": "2026-10-01 10:00:00 UTC", "description": "<p>d</p>"}]}
    monkeypatch.setattr(recruitee, "polite_get", lambda url: Resp())
    jobs = recruitee.fetch({"slug": "acme", "name": "Acme"})
    assert (jobs[0]["location"], jobs[0]["remote"]) == ("London, United Kingdom", True)
    assert jobs[1]["location"] == "London, Greater London, United Kingdom"     # no city/country: the API string is the fallback


def test_workablesearch_pages_dedupes_skips_unpublished_and_stops_on_a_cooldown(monkeypatch):
    from discover import util
    from discover.sources import workablesearch as ws
    job = lambda i, **kw: {"id": i, "title": f"ML Engineer {i}", "state": "published", "created": "2026-10-01T09:00:00Z", "workplace": "hybrid",
                           "url": f"https://jobs.workable.com/view/{i}", "company": {"title": "Acme"}, "location": {"city": "London", "countryName": "United Kingdom"},
                           "description": "<p>Build models</p>", "requirementsSection": "<p>Python</p>", **kw}
    pages = {("q1", None): {"jobs": [job("a"), job("b", state="closed")], "nextPageToken": "t2"},
             ("q1", "t2"): {"jobs": [job("c", workplace="remote", location={"city": "", "countryName": "United Kingdom"})]},
             ("q2", None): {"jobs": [job("a")], "nextPageToken": "t9"}}          # "a" again: already seen
    calls = []

    class Resp:
        def __init__(self, data): self.data = data
        def raise_for_status(self): pass
        def json(self): return self.data

    def fake_get(url, **kw):
        q, tok = kw["params"]["query"], kw["params"].get("pageToken")
        calls.append((q, tok))
        if (q, tok) == ("q2", "t9"):
            raise util.HostCoolingDown("jobs.workable.com rate-limited us")
        assert kw["params"]["day_range"] == 30 and kw["params"]["location"] == "United Kingdom"
        return Resp(pages[(q, tok)])
    monkeypatch.setattr(ws, "polite_get", fake_get)
    monkeypatch.setattr(ws, "QUERIES", ["q1", "q2", "q3"])
    rows = ws.fetch({})
    assert [r["url"].rsplit("/", 1)[1] for r in rows] == ["a", "c"]               # closed skipped, duplicate dropped
    assert rows[0]["location"] == "London, United Kingdom" and rows[0]["remote"] is False and rows[0]["posted_at"] == "2026-10-01"
    assert rows[1]["location"] == "United Kingdom" and rows[1]["remote"] is True
    assert rows[0]["apply_url"] is None and "Python" in rows[0]["description"]
    assert ("q3", None) not in calls                                               # a 429 stops the whole source
