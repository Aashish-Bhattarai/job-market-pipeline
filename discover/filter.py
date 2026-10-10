"""Filter discovered jobs. Owner: DiscoveryCore.

Reads every `jobs` row that hasn't been through filtering yet (status in
'new'/'filtered_out' — filtering is idempotent and safe to re-run), computes
the `region` column, and either marks `status='filtered_out'` with a
`reason` or leaves/restores `status='new'`.

Rules (see the task ticket and CONTRACTS.md §6):
  - title must be in the AI/ML/DS family, excluding seniority, analyst/BI,
    and intern/student roles.
  - location must be UK: London, Sussex, UK remote -- or, for AI/ML-core titles only, a nearby city
    listed in config.yaml `geography`.
  - posted_at must be within config.yaml `posting_age` days when known (30; 60 for rows read from an employer's own board).
  - company must not be in exclude_companies.txt, linkedin_applied.csv
    (fuzzy-normalized), or the scam list.
  - title must not be a gig/contractor "AI trainer" style listing.
  - max 2 applications (status='applied') per company per 30 days.
"""
from __future__ import annotations

import argparse
import csv
import datetime
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from discover import db as dbmod

HERE = Path(__file__).resolve().parent
TRACKER_ROOT = HERE.parent.parent  # .../Job Application Tracker
EXCLUDE_FILE = TRACKER_ROOT / "exclude_companies.txt"
LINKEDIN_CSV = HERE.parent / "data" / "linkedin_applied.csv"
CONFIG_PATH = HERE.parent / "config.yaml"

# ---------------------------------------------------------------- title ---

_SENIORITY_RE = re.compile(
    r"\b(senior|sr\.?|lead|principal|staff|head|director|manager|vp|"
    r"vice[\s-]president)\b|"
    r"\b(engineer|developer|scientist|programmer|architect|researcher)\s+(iii|iv|v|3|4|5)\b", re.I)

_INTERN_RE = re.compile(r"\b(intern|internship|placement|student)\b", re.I)
_VOLUNTEER_RE = re.compile(r"\b(unpaid|volunteer)\b", re.I)
_TALENT_POOL_RE = re.compile(r"\btalent (pool|community|network)\b", re.I)    # a CV drop for future roles, not a role

# Gig / AI-training-data contractor listings (Scale AI, Outlier, etc.) —
# excluded regardless of company, per pipeline instruction 2026-09-26.
_GIG_RE = re.compile(
    r"(\$\s*\d+(\.\d+)?\s*/\s*hr\b|/\s*hour\b|per\s+hour|upto\s*\$|"
    r"\bai\s+trainer\b|\bai\s+tutor\b|\bcontributor\b|human frontier collective|\bfreelance\b|"
    r"\bai\s+training\s+projects?\b|\bai\s+code\s+evaluation\b)", re.I)

_ANALYST_ONLY_RE = re.compile(r"\banalyst\b|\bbusiness intelligence\b|\bbi\b", re.I)

_AI_ML_SIGNAL_RE = re.compile(
    r"machine learning|\bml\b|\bai\b|artificial intelligence|deep learning|"
    r"\bnlp\b|computer vision|\bllm\b|genai|generative", re.I)

# AI/ML-core title patterns. These are the roles the operator allows OUTSIDE London/Sussex/UK-remote (nearby
# cities, config.yaml `geography`); everything in _ADJACENT_PATTERNS below stays London/Sussex/UK-remote only.
_CORE_PATTERNS = [
    re.compile(r"machine learning", re.I),
    re.compile(r"\bml\s*engineer", re.I),
    re.compile(r"\bai\s*engineer", re.I),
    re.compile(r"artificial intelligence", re.I),
    re.compile(r"applied scientist", re.I),
    re.compile(r"research engineer\s*\(?\s*ml\s*\)?", re.I),
    re.compile(r"data scientist", re.I),
    re.compile(r"\bmlops\b", re.I),
    re.compile(r"\bllm\b", re.I),
    re.compile(r"genai|generative\s*ai|generative\s*model", re.I),
    re.compile(r"\bnlp\b|natural language processing", re.I),
    re.compile(r"computer vision", re.I),
    re.compile(r"deep learning", re.I),
    re.compile(r"\bapplied\s+(ai|ml|research)\s+scientist\b", re.I),
    re.compile(r"\bml\s+operations\b", re.I),
    # 2026-10-02 audit (987 UK titles dropped as off-family): the current names for ML-engineering work --
    # "Agent Engineer", "Agent Deployment Engineer", "Frontier Agents Engineer", "Agentic ... Developer"
    re.compile(r"\bagent(ic|s)?\b[a-z &/,-]{0,24}\b(engineer|developer)\b", re.I),
    re.compile(r"\b(engineer|developer)\b[,(\s-]{1,4}(ai|ml|llm|genai|machine learning|agents?)\b", re.I),
    # "Deployed Engineer", "Forward Deployed Builder" (the FDE family under other names), "Prompt(ing) Engineer"
    re.compile(r"\bprompt(ing)?\s+engineer", re.I),
    re.compile(r"\bmodel\s+(release|deployment|serving|evaluation|inference)\s+engineer", re.I),
    # "Junior Quant Researcher - ML Alpha Research": a quant title counts only with an ML/AI signal in it
    re.compile(r"\bquant\w*\s+(researcher|developer)\b[^,]{0,40}\b(ml|machine learning|ai)\b", re.I),
    # "AI <something> engineer" (e.g. Reply's "Graduate AI Test Automation Engineer")
    re.compile(r"\bai\b(?![a-z &/,-]*\b(sales|solutions?|support|customer|presales|recruit\w*|marketing)\b)"
               r"[a-z &/,-]{0,30}\b(engineer|developer|scientist)\b", re.I),
]

_ADJACENT_PATTERNS = [
    re.compile(r"data engineer", re.I),
    # Adjacent roles the CV itself evidences (Junior Application Developer, React/front-end
    # internship, Python throughout) -- added 2026-10-02 so a 50-application round has a funnel to
    # draw from. FIT still decides: >1 year required or unmapped required skills fail honestly.
    re.compile(r"\b(software|application|python|backend|back[- ]end|full[- ]?stack|front[- ]?end|web)\b"
               r"[a-z &/,-]{0,25}\b(engineer|developer)\b", re.I),
    re.compile(r"\bsoftware development engineer\b|\bsde\b", re.I),
    re.compile(r"\bproduct engineer\b", re.I),
    # AI roles whose titles do not read "AI Engineer" (2026-10-02 audit of 757 UK titles filtered as
    # off-family): FDE and research engineer/scientist (the latter is AI-core only with an AI/ML signal).
    re.compile(r"\bforward[- ]deployed\b[a-z &/,-]{0,20}\b(engineer|builder)", re.I),
    re.compile(r"\bdeployed (engineer|builder)\b", re.I),   # LangChain's "Deployed Engineer (UK)"
    re.compile(r"\bresearch (engineer|scientist)\b", re.I),
]

# Positive family patterns; any one match qualifies the title.
_FAMILY_PATTERNS = _CORE_PATTERNS + _ADJACENT_PATTERNS


def is_ai_ml_core_title(title: str) -> bool:
    """An AI/ML engineering / data-science title (the only family allowed in nearby cities)."""
    t = title or ""
    if any(p.search(t) for p in _CORE_PATTERNS):
        return True
    # a bare "research engineer/scientist" is AI/ML only when the title itself says so
    return bool(re.search(r"\bresearch (engineer|scientist)\b", t, re.I) and _AI_ML_SIGNAL_RE.search(t))


def title_reason(title: str) -> str | None:
    """Return a filter reason if the title should be dropped, else None."""
    t = title or ""
    if _GIG_RE.search(t):
        return "gig/contractor AI-training listing"
    if _VOLUNTEER_RE.search(t):
        return "unpaid/volunteer role"
    if _TALENT_POOL_RE.search(t):
        return "talent-pool listing, not a role"
    if _INTERN_RE.search(t):
        return "intern/student/placement role"
    if _SENIORITY_RE.search(t):
        return "seniority excluded (senior/lead/principal/staff/head/director/manager/vp)"
    if _ANALYST_ONLY_RE.search(t) and not _AI_ML_SIGNAL_RE.search(re.sub(r"\banalyst\b", "", t, flags=re.I)):
        return "analyst/BI-only role"
    if not any(p.search(t) for p in _FAMILY_PATTERNS):
        return "title not in AI/ML/DS family"
    return None


# -------------------------------------------------------------- region ---

SUSSEX_TOWNS = [
    "brighton", "hove", "worthing", "lancing", "crawley",
    "gatwick", "horsham", "chichester", "burgess hill", "haywards heath",
    "lewes", "eastbourne", "bognor",
]
UK_HINTS = ["united kingdom", "uk", "england", "scotland", "wales",
            "northern ireland", "gb", "britain"]


def load_geography(path: Path | None = None) -> dict:
    """config.yaml `geography`: places (beyond London/Sussex) where AI/ML-core roles are acceptable."""
    try:
        cfg = yaml.safe_load((path or CONFIG_PATH).read_text(encoding="utf-8")) or {}
    except OSError:
        cfg = {}
    g = cfg.get("geography") or {}
    return {"nearby_places": [str(p).strip().lower() for p in (g.get("nearby_places") or []) if str(p).strip()],
            "uk_unspecified": bool(g.get("uk_unspecified", False)),
            "worldwide_remote": bool(g.get("worldwide_remote", False)),
            "emea_remote": bool(g.get("emea_remote", False)),
            "remote_unspecified": bool(g.get("remote_unspecified", False)),
            "anywhere_in_uk": bool(g.get("anywhere_in_uk", False))}


# A UK marker is required next to a nearby place so "Cambridge, MA" / "Reading, PA" / "Norwich, VT" never match.
_UK_MARKER_RE = re.compile(r"\b(uk|u\.k\.|united kingdom|england|great britain|britain|gb)\b")
# What is left of a location once UK words and filler are removed: nothing => the posting names no city.
_UK_FILLER_RE = re.compile(r"\b(united kingdom|u\.k\.|uk|england|great britain|britain|gb|hybrid|on-?site|office|based|"
                           r"flexible|anywhere|in|the|of|and|or|with|work|working|from|home)\b")


# Words that carry no place in a remote location ("Remote", "Fully remote - flexible"): what is left is the place.
_REMOTE_FILLER_RE = re.compile(r"\b(remote|remotely|fully|100%|distributed|wfh|work from home|home|anywhere|flexible|hybrid)\b")

# Major UK cities/towns (config geography.anywhere_in_uk): a requirement that merely names one is not "abroad".
UK_CITIES = [
    "manchester", "birmingham", "bristol", "leeds", "sheffield", "liverpool", "newcastle", "nottingham", "glasgow",
    "edinburgh", "cardiff", "belfast", "leicester", "southampton", "portsmouth", "plymouth", "exeter", "bath", "york",
    "norwich", "cambridge", "oxford", "reading", "milton keynes", "coventry", "derby", "aberdeen", "dundee",
    "swansea", "bournemouth", "ipswich", "sunderland", "canterbury", "guildford", "slough", "luton", "stockport",
]


def is_nearby(location: str, geo: dict) -> bool:
    loc = (location or "").lower()
    if not _UK_MARKER_RE.search(loc):
        return False
    # "if absolutely not available find for other locations as well" -- the last rung of the
    # ladder (London/Sussex -> remote -> anywhere in the UK); AI/ML-core titles only, the caller checks that.
    if geo.get("anywhere_in_uk"):
        return True
    if any(re.search(rf"\b{re.escape(p)}\b", loc) for p in geo.get("nearby_places") or []):
        return True
    if geo.get("uk_unspecified"):
        return not re.sub(r"[^a-z]", "", _UK_FILLER_RE.sub(" ", loc))
    return False


def compute_region(location: str, remote: int | bool | None, title: str | None = None,
                   geo: dict | None = None) -> str | None:
    loc = (location or "").lower()
    if "london" in loc:
        return "london"
    if any(town in loc for town in SUSSEX_TOWNS):
        return "sussex"
    is_remote = bool(remote) or "remote" in loc
    if is_remote and any(re.search(rf"\b{re.escape(h)}\b", loc) for h in UK_HINTS):
        return "remote"
    # AI/ML engineering roles are fine in nearby cities too (config.yaml `geography`).
    if title is not None and geo and is_ai_ml_core_title(title) and is_nearby(location, geo):
        return "nearby"
    # Remote rungs of the ladder, for AI/ML-core titles only (config.yaml geography):
    #   "Worldwide (Remote)" includes the UK; "Remote - EMEA/Europe" includes the UK; a bare "Remote" names no country
    #   (FIT's location rule still blocks a posting whose requirements say US/EU-only).
    if is_remote and title is not None and geo and is_ai_ml_core_title(title):
        if geo.get("worldwide_remote") and re.search(r"\b(worldwide|anywhere|global(ly)?|any location)\b", loc):
            return "remote"
        if geo.get("emea_remote") and re.search(r"\b(emea|europe|european)\b", loc):
            return "remote"
        if geo.get("remote_unspecified") and not re.sub(r"[^a-z]", "", _REMOTE_FILLER_RE.sub(" ", loc)):
            return "remote"
    return None


def region_reason(region: str | None) -> str | None:
    if region is None:
        return "not a UK location (London/Sussex/UK-remote, or a nearby city for AI/ML roles)"
    return None


# -------------------------------------------------------------- dates -----

EMPLOYER_BOARD_SOURCES = {"greenhouse", "lever", "ashby", "workable", "smartrecruiters", "recruitee", "teamtailor"}


def load_posting_age(path: Path | None = None) -> dict:
    """config.yaml `posting_age`: days a posting may be, for aggregator rows and for rows read from an employer's own board."""
    try:
        cfg = yaml.safe_load((path or CONFIG_PATH).read_text(encoding="utf-8")) or {}
    except OSError:
        cfg = {}
    a = cfg.get("posting_age") or {}
    days = int(a.get("max_days", 30))
    return {"max_days": days, "max_days_employer_board": int(a.get("max_days_employer_board", days))}


def posted_reason(posted_at: str | None, today: datetime.date, max_days: int = 30) -> str | None:
    if not posted_at:
        return None
    try:
        d = datetime.datetime.strptime(posted_at[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
    if (today - d).days > max_days:
        return f"posted more than {max_days} days ago"
    return None


# ------------------------------------------------------------ company -----

_SUFFIX_RE = re.compile(
    r"\b(ltd|limited|plc|group|inc|llc|corp|corporation|co)\b\.?", re.I)
_PUNCT_RE = re.compile(r"[^\w\s]")


def normalize_company(name: str) -> str:
    n = (name or "").lower()
    n = n.replace(".com", "")
    n = _PUNCT_RE.sub(" ", n)
    n = _SUFFIX_RE.sub(" ", n)
    n = re.sub(r"\s+", " ", n).strip()
    # Workable names a second account "Oxford Quantum Circuits 8": one employer must be one key, or its cap and its
    # duplicate check never see each other (the aggregator's "Oxford Quantum Circuits" would have been applied to again).
    return re.sub(r"\s\d{1,2}$", "", n)


# CONTRACTS.md §6 scam list, plus the 2026-09-26 additions from SiteSources'
# himalayas findings (AI-training-data brokers, not genuine employers).
SCAM_COMPANIES = {
    "information tech consultants itc", "itc", "crossing hurdles", "micro1",
    "capiteltitles", "hire feed", "dataannotation", "mercor", "outlier",
    "remotasks",
    # sells a paid ~130-hour course with a "job guarantee"; its postings list course steps, not hiring requirements
    "newto training",
}


def load_excluded_companies() -> set[str]:
    names = set()
    if EXCLUDE_FILE.exists():
        for line in EXCLUDE_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("(") :
                continue
            names.add(normalize_company(line))
    return names


def load_linkedin_applied_companies() -> set[str]:
    names = set()
    if LINKEDIN_CSV.exists():
        with open(LINKEDIN_CSV, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                company = row.get("company")
                if company:
                    names.add(normalize_company(company))
    return names


# ------------------------------------------------------------ cap check ---

def companies_at_cap(conn, today: datetime.date, max_per_company: int = 2,
                      window_days: int = 30) -> set[str]:
    cutoff = (today - datetime.timedelta(days=window_days)).isoformat()
    rows = conn.execute(
        "SELECT company, applied_at FROM jobs WHERE status='applied' AND applied_at IS NOT NULL"
    ).fetchall()
    counts: dict[str, int] = {}
    for r in rows:
        applied_at = (r["applied_at"] or "")[:10]
        if applied_at >= cutoff:
            key = normalize_company(r["company"])
            counts[key] = counts.get(key, 0) + 1
    return {c for c, n in counts.items() if n >= max_per_company}


# --------------------------------------------------------------- main -----

def run(db_path: Path) -> dict:
    conn = dbmod.connect(db_path)
    today = datetime.date.today()

    excluded = load_excluded_companies() | SCAM_COMPANIES
    linkedin_applied = load_linkedin_applied_companies()
    at_cap = companies_at_cap(conn, today)
    geo = load_geography()
    age = load_posting_age()

    rows = conn.execute(
        "SELECT id, company, title, location, remote, posted_at, status, apply_url, source FROM jobs "
        "WHERE status IN ('new', 'filtered_out')"
    ).fetchall()

    # Cross-source duplicate detection: the same company+title arrives via different boards (an Ashby posting and its
    # Himalayas mirror) or as per-city copies of one role (Tether posts each AI role once per country), with different
    # URLs, so URL dedupe never sees them. Without this, one role can be tailored (and applied to) twice, burning the
    # 2-per-30-days cap. Only rows that pass every OTHER filter compete: judging a London copy a duplicate of a newer
    # New York copy that the location filter then dropped lost the role entirely (Tether, 2026-10-03). Exactly one
    # survivor per (company, title): one that already has an apply URL (an aggregator twin still needs resolving), then
    # the newest posting, then id so every run makes the same choice (a row dropped as a duplicate used to be revived
    # and dropped again on alternate runs).
    def _norm_title(t: str | None) -> str:
        return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()

    def _key(r) -> tuple[str, str]:
        return normalize_company(r["company"]), _norm_title(r["title"])

    regions = {r["id"]: compute_region(r["location"], r["remote"], r["title"], geo) for r in rows}

    def _own_reason(row) -> str | None:
        key = normalize_company(row["company"])
        if key in excluded:
            return "excluded company (exclude list / scam list)"
        if key in linkedin_applied:
            return "already applied via LinkedIn"
        if key in at_cap:
            return "company application cap reached (2 per 30 days)"
        max_days = age["max_days_employer_board"] if row["source"] in EMPLOYER_BOARD_SOURCES else age["max_days"]
        return (title_reason(row["title"]) or region_reason(regions[row["id"]])
                or posted_reason(row["posted_at"], today, max_days))

    own = {r["id"]: _own_reason(r) for r in rows}
    in_pipeline: dict[tuple[str, str], str] = {}     # a twin already past new/filtered_out (whole table, not just `rows`)
    for r in conn.execute("SELECT id, company, title FROM jobs WHERE status NOT IN ('new', 'filtered_out')"):
        in_pipeline.setdefault(_key(r), r["id"])
    best: dict[tuple[str, str], tuple[int, str, str]] = {}
    for r in rows:
        if own[r["id"]] is None:
            best[_key(r)] = max(best.get(_key(r), (0, "", "")), (int(bool(r["apply_url"])), r["posted_at"] or "", r["id"]))
    duplicates: dict[str, str] = {}
    for r in rows:
        if own[r["id"]] is not None:
            continue
        key = _key(r)
        if key in in_pipeline:
            duplicates[r["id"]] = (f"duplicate posting of {in_pipeline[key]} "
                                   f"({r['company']} — {r['title']}, already in pipeline)")
        elif best[key][2] != r["id"]:
            duplicates[r["id"]] = f"duplicate posting of {best[key][2]} (same role from another source, preferred)"

    reason_counts: dict[str, int] = {}
    decisions: list[tuple[str, str, str | None]] = []
    transitions: list[tuple[str, str, str, str | None]] = []
    kept = dropped = 0
    for row in rows:
        region = regions[row["id"]]
        reason = own[row["id"]] or duplicates.get(row["id"])

        new_status = "filtered_out" if reason else "new"
        decisions.append((row["id"], new_status, reason))
        if new_status != row["status"]:
            transitions.append((row["id"], row["status"], new_status, reason))
        if reason:
            conn.execute(
                "UPDATE jobs SET region=?, status='filtered_out', reason=? WHERE id=?",
                (region, reason, row["id"]),
            )
            dropped += 1
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        else:
            conn.execute(
                "UPDATE jobs SET region=?, status='new', reason=NULL WHERE id=?",
                (region, row["id"]),
            )
            kept += 1

    conn.commit()
    conn.close()
    _record_run(db_path, decisions, transitions, reason_counts)
    return {"total": len(rows), "kept": kept, "dropped": dropped,
            "reason_counts": reason_counts}


def _record_run(db_path, decisions, transitions, reason_counts) -> None:
    """One summary event per run (hash of every decision, so any past run's
    full outcome is verifiable) + one event per job entering/leaving the
    candidate pool. Per-row events for all ~17k re-evaluated rows would
    drown the log without adding accountability: only `new` jobs go on."""
    from ledger import core
    lconn = core.connect(db_path)
    try:
        with core.tx(lconn):
            core.append_event(lconn, "code:filter", "filter.run", {
                "evaluated": len(decisions), "reason_counts": reason_counts,
                "decisions_sha256": core.sha256_text(core.canonical(sorted(decisions))),
                "transitions": len(transitions)})
            for jid, old, new, reason in transitions:
                core.append_event(lconn, "code:filter", "job.status",
                                  {"from": old, "to": new, "reason": reason}, job_id=jid)
    finally:
        lconn.close()


def main():
    ap = argparse.ArgumentParser(description="Filter jobs.db rows")
    ap.add_argument("--db", default=str(dbmod.DB_PATH))
    args = ap.parse_args()
    result = run(Path(args.db))
    print(f"total={result['total']} kept={result['kept']} dropped={result['dropped']}")
    for reason, n in sorted(result["reason_counts"].items(), key=lambda kv: -kv[1]):
        print(f"  {n:4d}  {reason}")


if __name__ == "__main__":
    main()
