"""Shared helpers for pipeline/stages/*.py.

Bootstraps sys.path so every stage module imports cleanly whether invoked as
`python -m stages.<name>` from `pipeline/` (per RUNBOOK/batch.py) or imported
by a test file from anywhere else.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

STAGES_DIR = Path(__file__).resolve().parent
PIPELINE_ROOT = STAGES_DIR.parent
if str(PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(PIPELINE_ROOT))

from ledger import core  # noqa: E402
from discover import db as jobsdb  # noqa: E402

RENDER_DIR = PIPELINE_ROOT / "render"
if str(RENDER_DIR) not in sys.path:
    sys.path.insert(0, str(RENDER_DIR))
from common import FactsBank, load_facts_bank, resolve_facts_path  # noqa: E402

__all__ = [
    "PIPELINE_ROOT", "RENDER_DIR", "FactsBank", "load_facts_bank", "resolve_facts_path",
    "connect", "get_job", "set_job_status", "citable_facts", "norm_ws",
    "quote_verified", "load_bank",
]


def connect(db_path=None):
    """Single sqlite3 connection with both the ledger schema and the `jobs`
    table migrated; autocommit (core.tx() controls transactions explicitly)."""
    conn = core.connect(db_path or core.DB_PATH)
    conn.executescript(jobsdb.SCHEMA)
    return conn


def load_bank(facts_path: str | None = None) -> FactsBank:
    return load_facts_bank(resolve_facts_path(facts_path))


def get_job(conn, job_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return dict(row) if row else None


def set_job_status(conn, job_id: str, status: str, actor: str, *, reason: str | None = None,
                    extra_cols: dict | None = None) -> None:
    """Update jobs.status (+ optional extra columns) and log a `job.status`
    event in the same call. MUST be invoked inside `core.tx(conn)`."""
    row = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
    old = row["status"] if row else None
    cols = {"status": status, "reason": reason}
    if extra_cols:
        cols.update(extra_cols)
    set_clause = ", ".join(f"{k}=?" for k in cols)
    conn.execute(f"UPDATE jobs SET {set_clause} WHERE id=?", [*cols.values(), job_id])
    core.append_event(conn, actor, "job.status", {"from": old, "to": status, "reason": reason},
                       job_id=job_id)


def citable_facts(bank: FactsBank) -> list[dict]:
    """Every id the requirements extractor may cite in fact_ids: experience/
    project facts plus education and certification entries (CONTRACTS.md
    citable_ids()), each rendered as {id, text, tags}."""
    out = []
    for fid, entry in bank.fact_by_id.items():
        out.append({"id": fid, "text": entry["text"], "tags": list(entry.get("tags") or [])})
    for e in bank.education:
        out.append({"id": e["id"], "text": e.get("degree", ""), "tags": ["education", "degree"]})
    for c in bank.certifications:
        out.append({"id": c["id"], "text": c.get("name", ""), "tags": ["certification"]})
    return out


_WS_RE = re.compile(r"\s+")
# JD text routinely carries typographic apostrophes/quotes ("You're") while
# the model echoes ASCII ones, and vice versa — normalise both sides before
# the verbatim check so punctuation style alone can't fail an honest quote.
_TYPOGRAPHIC_RE = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-"})


def norm_ws(s: str | None) -> str:
    """Whitespace/case/quote-style normalisation for verbatim-quote checks."""
    return _WS_RE.sub(" ", (s or "").translate(_TYPOGRAPHIC_RE)).strip().lower()


def quote_verified(quote: str | None, haystack: str | None) -> bool:
    return bool(quote) and norm_ws(quote) in norm_ws(haystack)
