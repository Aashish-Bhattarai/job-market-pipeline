"""Geography beyond London/Sussex. Invariants: only AI/ML-core titles may use a nearby city; a UK marker must sit next to
the place (Cambridge MA is not Cambridge); London/Sussex/remote still win; the place list is config, not code;
and the FIT rubric agrees with the filter about which places are acceptable. Temp DB only, like test_ledger.py.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from discover import db as dbmod  # noqa: E402
from discover import filter as flt  # noqa: E402
from ledger import core  # noqa: E402
from stages import fit_rules  # noqa: E402

GEO = {"nearby_places": ["cambridge", "reading", "oxford", "norwich", "surrey"], "uk_unspecified": True}


@pytest.mark.parametrize("location,title,want", [
    # AI/ML-core title + UK marker + listed place -> nearby
    ("Cambridge, UK", "Machine Learning Engineer", "nearby"),
    ("Reading, Berkshire, England", "Data Scientist", "nearby"),
    ("Oxford, United Kingdom", "AI Engineer", "nearby"),
    ("Guildford, Surrey, UK", "Machine Learning Engineer", "nearby"),            # via the county word
    ("Cambridge, UK", "Research Scientist, Machine Learning", "nearby"),         # research title WITH an AI/ML signal
    # the same place names abroad never match
    ("Cambridge, MA, United States", "Machine Learning Engineer", None),
    ("Reading, PA", "Data Scientist", None),
    ("Norwich, Vermont, United States", "Data Scientist", None),
    # only AI/ML-core titles: generic software / data-engineer / FDE / bare research titles stay London-Sussex-remote
    ("Oxford, United Kingdom", "Software Engineer", None),
    ("Cambridge, UK", "Data Engineer", None),
    ("Cambridge, UK", "Forward Deployed Engineer", None),
    ("Cambridge, UK", "Research Scientist", None),
    # a place not on the list is not nearby
    ("Manchester, UK", "Machine Learning Engineer", None),
    # a bare UK location (no city named) counts for AI/ML-core titles when uk_unspecified is on
    ("United Kingdom", "Machine Learning Engineer", "nearby"),
    ("UK", "Data Scientist", "nearby"),
    ("England, United Kingdom", "AI Engineer", "nearby"),
    ("Scotland", "AI Engineer", None),
    # London / Sussex / remote are unchanged and win first
    ("London, UK", "Software Engineer", "london"),
    ("Brighton, UK", "Machine Learning Engineer", "sussex"),
])
def test_region_for_nearby_cities(location, title, want):
    assert flt.compute_region(location, None, title, GEO) == want


def test_remote_still_wins_and_uk_unspecified_can_be_switched_off():
    assert flt.compute_region("Remote, United Kingdom", 1, "Software Engineer", GEO) == "remote"
    off = {**GEO, "uk_unspecified": False}
    assert flt.compute_region("United Kingdom", None, "Machine Learning Engineer", off) is None
    assert flt.compute_region("Cambridge, UK", None, "Machine Learning Engineer", off) == "nearby"   # listed place still fine
    assert flt.compute_region("Cambridge, UK", None, "Machine Learning Engineer") is None            # no geo given -> old behaviour


def test_geography_is_read_from_config_not_code(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("geography:\n  uk_unspecified: false\n  nearby_places: [Bristol, ' St Albans ', '']\n", encoding="utf-8")
    off = {"worldwide_remote": False, "emea_remote": False, "remote_unspecified": False, "anywhere_in_uk": False}
    assert flt.load_geography(cfg) == {"nearby_places": ["bristol", "st albans"], "uk_unspecified": False, **off}
    assert flt.load_geography(tmp_path / "missing.yaml") == {"nearby_places": [], "uk_unspecified": False, **off}
    # the shipped config lists real places (so the feature is on) and Manchester is NOT assumed nearby
    shipped = flt.load_geography()
    assert "cambridge" in shipped["nearby_places"] and "manchester" not in shipped["nearby_places"]


def test_filter_run_keeps_nearby_ai_ml_roles_and_drops_the_rest(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "PIPELINE_ROOT", tmp_path)
    monkeypatch.setattr(flt, "load_geography", lambda: GEO)
    path = tmp_path / "t.db"
    conn = dbmod.connect(path)
    base = {"source": "t", "company": "Acme Robotics", "remote": None, "apply_url": None, "ats": None,
            "posted_at": __import__("datetime").date.today().isoformat(), "description": "d" * 400}
    ids = {}
    for key, (title, loc) in {
        "ml_cam": ("Machine Learning Engineer", "Cambridge, UK"),
        "swe_cam": ("Software Engineer", "Cambridge, UK"),
        "ml_us": ("Machine Learning Engineer", "Cambridge, MA, United States"),
        "ml_man": ("Machine Learning Engineer", "Manchester, UK"),
    }.items():
        ids[key] = dbmod.upsert_job(conn, {**base, "title": title, "location": loc,
                                           "url": f"https://x.example/{key}"}, "t0")[0]
    conn.close()
    flt.run(path)
    conn = dbmod.connect(path)
    got = {k: conn.execute("SELECT status, region FROM jobs WHERE id=?", (v,)).fetchone() for k, v in ids.items()}
    assert (got["ml_cam"]["status"], got["ml_cam"]["region"]) == ("new", "nearby")
    assert got["swe_cam"]["status"] == got["ml_us"]["status"] == got["ml_man"]["status"] == "filtered_out"


def test_fit_location_rule_accepts_sussex_towns_and_nearby_places_but_not_elsewhere(monkeypatch):
    monkeypatch.setattr(fit_rules, "_geography", lambda: GEO)
    for ok in ("Must be based in Cambridge", "Based in Brighton", "Must live in Reading, Berkshire", "UK based", "Remote"):
        assert fit_rules._location_blocks(ok) is False, ok
    for blocked in ("Based in New York", "Office in Boston", "Must relocate to Manchester"):
        assert fit_rules._location_blocks(blocked) is True, blocked


def test_ai_training_gig_listings_stay_excluded_whatever_the_region():
    # a gig/contractor data-labelling fellowship is expert data-labelling gig work (an exclusion rule).
    assert flt.title_reason("Machine Learning Fellow - Human Frontier Collective") == "gig/contractor AI-training listing"
    assert flt.title_reason("Machine Learning Engineer") is None
    assert flt.title_reason("AI Engineer (Freelance)") == "gig/contractor AI-training listing"


def test_numbered_engineer_levels_iii_and_up_are_senior_but_ii_is_mid():
    # "Backend Engineer III" (L40) reached FIT; "Software Engineer II" is mid-level, which is accepted.
    assert flt.title_reason("Backend Engineer III") == "seniority excluded (senior/lead/principal/staff/head/director/manager/vp)"
    assert flt.title_reason("Software Engineer IV") == "seniority excluded (senior/lead/principal/staff/head/director/manager/vp)"
    assert flt.title_reason("Software Engineer II - Full-Stack") is None


def test_2026_ai_role_names_are_core_titles_but_agents_and_sales_are_not():
    # 2026-10-02 audit: 987 UK titles were dropped as off-family; these are the current names for ML-engineering work.
    for t in ("Agent Engineer", "Agent Deployment Engineer", "Frontier Agents Engineer", "Agentic AI Developer",
              "Prompting Engineer", "Model Release Engineer",
              "Junior Quant Researcher - ML Alpha Research", "Software Engineer, AI Product"):
        assert flt.is_ai_ml_core_title(t), t
    # FDE-style titles are in the family (London / UK-remote) but NOT core: never "nearby" or worldwide-remote
    for t in ("Deployed Engineer (UK)", "Forward Deployed Builder", "Forward Deployed Engineer"):
        assert flt.title_reason(t) is None and not flt.is_ai_ml_core_title(t), t
    for t in ("Customer Service Agent", "Insurance Agent", "Real Estate Agent", "Sales Engineer", "Engineer",
              "Quantitative Trader", "Developer Advocate, Marketing"):
        assert not flt.is_ai_ml_core_title(t), t


def test_worldwide_remote_counts_only_for_ai_ml_core_titles_and_only_when_flagged():
    geo = {**GEO, "worldwide_remote": True}
    assert flt.compute_region("Worldwide (Remote)", 1, "Machine Learning Engineer", geo) == "remote"
    assert flt.compute_region("Worldwide (Remote)", 1, "Account Executive", geo) is None
    assert flt.compute_region("Worldwide (Remote)", 1, "Machine Learning Engineer", {**GEO, "worldwide_remote": False}) is None
    assert flt.compute_region("Remote, United States", 1, "Machine Learning Engineer", geo) is None


LADDER = {**GEO, "worldwide_remote": True, "emea_remote": True, "remote_unspecified": True, "anywhere_in_uk": True}


def test_ladder_last_rung_accepts_any_uk_place_for_ai_ml_core_titles_only():
    # "if absolutely not available find other locations as well" (2026-10-02): the whole UK, AI/ML-core titles only
    assert flt.compute_region("Manchester, United Kingdom", None, "Machine Learning Engineer", LADDER) == "nearby"
    assert flt.compute_region("Edinburgh, UK", None, "AI Engineer", LADDER) == "nearby"
    assert flt.compute_region("Manchester, United Kingdom", None, "Software Engineer", LADDER) is None   # not core
    assert flt.compute_region("Manchester, NH", None, "Machine Learning Engineer", LADDER) is None        # no UK marker
    assert flt.compute_region("Manchester, United Kingdom", None, "Machine Learning Engineer", GEO) is None  # rung off


def test_ladder_remote_rungs_are_emea_europe_and_bare_remote_but_never_a_named_other_country():
    for loc in ("Remote - EMEA", "Remote (Europe)", "Remote", "Fully remote", "Remote, Worldwide"):
        assert flt.compute_region(loc, 1, "Machine Learning Engineer", LADDER) == "remote", loc
    for loc in ("Remote, United States", "Remote - Canada", "Remote, India"):
        assert flt.compute_region(loc, 1, "Machine Learning Engineer", LADDER) is None, loc
    assert flt.compute_region("Remote", 1, "Account Executive", LADDER) is None                  # not AI/ML-core
    assert flt.compute_region("Remote - EMEA", 1, "Machine Learning Engineer", GEO) is None       # rungs off


def test_fit_location_rule_does_not_block_a_named_uk_city_once_the_whole_uk_is_accepted(monkeypatch):
    monkeypatch.setattr(fit_rules, "_geography", lambda: LADDER)
    assert fit_rules._location_blocks("Based in Manchester") is False
    assert fit_rules._location_blocks("Based in Austin, Texas") is True
    monkeypatch.setattr(fit_rules, "_geography", lambda: GEO)
    assert fit_rules._location_blocks("Based in Manchester") is True                              # rung off: as before


def test_fit_location_rule_ignores_office_attendance_and_uk_inclusive_regions_but_blocks_named_foreign_places(monkeypatch):
    monkeypatch.setattr(fit_rules, "_geography", lambda: GEO)
    for ok in ("Work from the office 1-2 days per week", "Hybrid working with two days a week from the Hoxton office",
               "In-person work Tuesdays, Wednesdays and Thursdays", "No travel requirement for this role",
               "Role based in EMEA (EMEA-only hiring)", "Location eligibility: US, Canada, or Europe", "Based between GMT-3 and GMT+2 timezones"):
        assert fit_rules._location_blocks(ok) is False, ok
    for blocked in ("Based in France", "Located in Germany", "Lived in the United States at least 3 of the last 5 years",
                    "Must be based in the US", "Open to professionals located in / targeting Poland", "Must relocate to Manchester"):
        assert fit_rules._location_blocks(blocked) is True, blocked


def test_per_city_copies_of_one_role_leave_exactly_one_survivor_and_the_choice_is_stable(tmp_path, monkeypatch):
    # Tether posts each AI role once per country. A London copy used to be dropped as a duplicate of a NEWER New York copy
    # that the location filter then dropped too, so the role vanished; copies with equal dates all survived (double apply).
    import datetime
    monkeypatch.setattr(core, "PIPELINE_ROOT", tmp_path)
    monkeypatch.setattr(flt, "load_geography", lambda: GEO)
    path = tmp_path / "t.db"
    conn = dbmod.connect(path)
    today = datetime.date.today()
    base = {"source": "t", "company": "Acme Robotics", "title": "Machine Learning Engineer", "apply_url": None, "ats": None,
            "description": "d" * 400}
    ids = {}
    for key, (loc, remote, posted) in {
        "london": ("London, United Kingdom", None, today),
        "uk_remote": ("United Kingdom", True, today),
        "new_york": ("New York, United States", None, today + datetime.timedelta(days=1)),
    }.items():
        ids[key] = dbmod.upsert_job(conn, {**base, "location": loc, "remote": remote, "posted_at": posted.isoformat(),
                                           "url": f"https://x.example/{key}"}, "t0")[0]
    conn.close()

    def outcome():
        flt.run(path)
        c = dbmod.connect(path)
        try:
            return {k: tuple(c.execute("SELECT status, reason FROM jobs WHERE id=?", (v,)).fetchone()) for k, v in ids.items()}
        finally:
            c.close()
    first = outcome()
    assert first["new_york"][0] == "filtered_out" and "duplicate" not in first["new_york"][1]
    survivors = [k for k in ("london", "uk_remote") if first[k][0] == "new"]
    assert len(survivors) == 1
    loser = "uk_remote" if survivors == ["london"] else "london"
    assert first[loser][1].startswith(f"duplicate posting of {ids[survivors[0]]}")
    assert outcome() == first            # re-running never flips a duplicate back to new


@pytest.mark.parametrize("has_url", ["first", "second"])
def test_a_twin_that_already_has_an_apply_url_beats_an_aggregator_copy_that_still_needs_resolving(tmp_path, monkeypatch, has_url):
    import datetime
    monkeypatch.setattr(core, "PIPELINE_ROOT", tmp_path)
    monkeypatch.setattr(flt, "load_geography", lambda: GEO)
    path = tmp_path / "t.db"
    conn = dbmod.connect(path)
    base = {"source": "t", "company": "Acme Robotics", "title": "Machine Learning Engineer", "location": "London, United Kingdom",
            "remote": None, "ats": None, "posted_at": datetime.date.today().isoformat(), "description": "d" * 400}
    ids = {}
    for key in ("first", "second"):
        ids[key] = dbmod.upsert_job(conn, {**base, "url": f"https://x.example/{key}",
                                           "apply_url": f"https://boards.example/{key}" if key == has_url else None}, "t0")[0]
    conn.close()
    flt.run(path)
    conn = dbmod.connect(path)
    status = {k: conn.execute("SELECT status FROM jobs WHERE id=?", (v,)).fetchone()["status"] for k, v in ids.items()}
    assert status[has_url] == "new" and status["second" if has_url == "first" else "first"] == "filtered_out"


def test_a_numbered_duplicate_account_is_the_same_employer_and_junk_titles_are_dropped():
    # Workable called OQC's second account "Oxford Quantum Circuits 8": the cap and the duplicate check must see one company.
    assert flt.normalize_company("Oxford Quantum Circuits 8") == flt.normalize_company("Oxford Quantum Circuits")
    assert flt.normalize_company("Humara 2") == "humara" and flt.normalize_company("3M") == "3m"
    assert flt.title_reason("UNPAID VOLUNTEER - Technology Officer - Artificial Intelligence") == "unpaid/volunteer role"
    assert flt.title_reason("Backend Software Engineer - AI Training Projects") == "gig/contractor AI-training listing"
    assert flt.title_reason("Software Engineer - Backend & AI Code Evaluation") == "gig/contractor AI-training listing"
    assert flt.title_reason("Talent Pool - Software Engineer, Backend") == "talent-pool listing, not a role"
    assert flt.title_reason("Machine Learning Engineer") is None


def test_an_employer_board_role_may_be_older_than_an_aggregator_listing(tmp_path, monkeypatch):
    # A board lists only open roles, so a 45-day-old board row is live; the same age on an aggregator may be a dead listing.
    import datetime
    real_loader = flt.load_posting_age
    monkeypatch.setattr(core, "PIPELINE_ROOT", tmp_path)
    monkeypatch.setattr(flt, "load_geography", lambda: GEO)
    monkeypatch.setattr(flt, "load_posting_age", lambda: {"max_days": 30, "max_days_employer_board": 60})
    path = tmp_path / "t.db"
    conn = dbmod.connect(path)
    base = {"company": "Acme Robotics", "location": "London, United Kingdom", "remote": None, "apply_url": None, "ats": None, "description": "d" * 400}
    ids = {}
    for key, (source, title, days) in {
        "board_45": ("greenhouse", "Machine Learning Engineer", 45),
        "aggregator_45": ("himalayas", "Data Scientist", 45),
        "board_70": ("lever", "NLP Engineer", 70),
        "board_20": ("ashby", "Research Engineer", 20),
    }.items():
        posted = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
        ids[key] = dbmod.upsert_job(conn, {**base, "source": source, "title": title, "posted_at": posted, "url": f"https://x.example/{key}"}, "t0")[0]
    conn.close()
    flt.run(path)
    conn = dbmod.connect(path)
    got = {k: tuple(conn.execute("SELECT status, reason FROM jobs WHERE id=?", (v,)).fetchone()) for k, v in ids.items()}
    assert got["board_45"][0] == "new" and got["board_20"][0] == "new"
    assert got["aggregator_45"] == ("filtered_out", "posted more than 30 days ago")
    assert got["board_70"] == ("filtered_out", "posted more than 60 days ago")
    assert real_loader(tmp_path / "missing.yaml") == {"max_days": 30, "max_days_employer_board": 30}
