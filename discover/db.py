"""SQLite storage for discovered jobs. Owner: DiscoveryCore.

Schema per pipeline/CONTRACTS.md §6. Table `jobs`, keyed by id = sha1 of the
canonical URL (lowercase scheme/host, path with trailing slash stripped,
query/fragment dropped except query keys that are needed to disambiguate the
posting, e.g. Workable/SmartRecruiters numeric ids passed as query params).
"""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "jobs.db"

# Query keys worth keeping when they are the only thing that disambiguates a
# posting (rare for our ATS sources, which normally have a unique path).
_KEEP_QUERY_KEYS = {"gh_jid", "lever-origin", "jobid", "job_id", "id"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            TEXT PRIMARY KEY,
    source        TEXT,
    company       TEXT,
    title         TEXT,
    location      TEXT,
    region        TEXT,
    remote        INTEGER,
    url           TEXT,
    apply_url     TEXT,
    ats           TEXT,
    posted_at     TEXT,
    description   TEXT,
    discovered_at TEXT,
    status        TEXT DEFAULT 'new',
    reason        TEXT,
    fit_score     REAL,
    fit_json      TEXT,
    cv_path       TEXT,
    cover_path    TEXT,
    applied_at    TEXT,
    notes         TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status  ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company);
CREATE INDEX IF NOT EXISTS idx_jobs_region  ON jobs(region);
"""

# Columns that belong to the normalized-job dict (see CONTRACTS §5) and are
# safe to overwrite on every re-fetch. Pipeline-owned columns (status,
# reason, fit_*, cv_path, cover_path, applied_at, notes) are left untouched
# on upsert of an existing row.
_NORMALIZED_FIELDS = [
    "source", "company", "title", "location", "remote", "url",
    "apply_url", "ats", "posted_at", "description",
]

# Aggregator rows (Gradcracker, Himalayas, Reed, HN, ...) never carry the
# employer link or posting text: resolve_apply_url.py fills these in later, so
# re-fetching such a row must not wipe them.
_RESOLVED_FIELDS = ("apply_url", "ats", "description")


def canonical_url(url: str) -> str:
    """Lowercase scheme/host, strip trailing slash, drop query/fragment
    except disambiguating ids, drop fragment entirely."""
    parts = urlsplit(url.strip())
    scheme = (parts.scheme or "https").lower()
    netloc = parts.netloc.lower()
    path = parts.path.rstrip("/") or "/"
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k in _KEEP_QUERY_KEYS]
    query = urlencode(sorted(kept))
    return urlunsplit((scheme, netloc, path, query, ""))


def job_id(url: str) -> str:
    return hashlib.sha1(canonical_url(url).encode("utf-8")).hexdigest()


def connect(db_path: Path | str = DB_PATH) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def upsert_job(conn: sqlite3.Connection, job: dict, discovered_at: str) -> tuple[str, bool]:
    """Insert or update a normalized job dict. Returns (id, is_new)."""
    jid = job_id(job["url"])
    row = conn.execute("SELECT id, apply_url FROM jobs WHERE id = ?", (jid,)).fetchone()
    if row is None:
        cols = ["id", "discovered_at", "status"] + _NORMALIZED_FIELDS
        vals = [jid, discovered_at, "new"] + [
            (1 if job.get("remote") else 0) if f == "remote" else job.get(f)
            for f in _NORMALIZED_FIELDS
        ]
        placeholders = ",".join("?" for _ in cols)
        conn.execute(
            f"INSERT INTO jobs ({','.join(cols)}) VALUES ({placeholders})",
            vals,
        )
        conn.commit()
        return jid, True
    fields = _NORMALIZED_FIELDS
    if row["apply_url"] and not job.get("apply_url"):
        fields = [f for f in fields if f not in _RESOLVED_FIELDS]
    set_clause = ",".join(f"{f}=?" for f in fields)
    vals = [
        (1 if job.get("remote") else 0) if f == "remote" else job.get(f)
        for f in fields
    ]
    conn.execute(f"UPDATE jobs SET {set_clause} WHERE id=?", vals + [jid])
    conn.commit()
    return jid, False


def upsert_many(conn: sqlite3.Connection, jobs: list[dict], discovered_at: str) -> dict:
    """Upsert a batch; returns {'inserted': n, 'updated': n}."""
    inserted = updated = 0
    for job in jobs:
        if not job.get("url"):
            continue
        _, is_new = upsert_job(conn, job, discovered_at)
        inserted += is_new
        updated += not is_new
    return {"inserted": inserted, "updated": updated}
