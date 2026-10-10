"""Ledger core: content-addressed store, attested records, hash-chained events.

Every evidence record is written through `record()`, which inserts the row and,
in the same transaction, appends an event carrying the sha256 of the row's
canonical JSON. `verify()` recomputes every row hash, the event chain, and every
stored file hash — so a record that was edited, deleted, forged without an
event, or whose file was swapped, is reported.
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
from pathlib import Path

from . import schema

PIPELINE_ROOT = Path(__file__).resolve().parent.parent
TRACKER_ROOT = PIPELINE_ROOT.parent
DB_PATH = PIPELINE_ROOT / "data" / "jobs.db"
STORE_ROOT = PIPELINE_ROOT / "store"
FACTS_PATH = PIPELINE_ROOT / "profile" / "facts.yaml"
ANSWERS_PATH = PIPELINE_ROOT / "profile" / "answers.yaml"

GENESIS = "0" * 64

# Tables whose every row is attested by an event carrying the row's hash.
# answer_bank's `retired` flag is excluded from its hash (retirement is its
# own event); form_answers rows are attested through their application's
# form_answers_sha256.
ATTESTED = {
    "profile_versions": "id",
    "blobs": "sha256",
    "llm_calls": "id",
    "requirements": "id",
    "documents": "id",
    "applications": "id",
    "answer_bank": "id",
    "answer_aliases": None,   # composite key; attested by (question_norm, bank_id)
}
HASH_EXCLUDE = {"answer_bank": {"retired"}}


# ---------------------------------------------------------------- helpers ---

def now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_text(s: str) -> str:
    return sha256_bytes(s.encode("utf-8"))


_NORM_RE = re.compile(r"[^a-z0-9]+")


def norm(text: str | None) -> str:
    """Question/company normalisation. MUST stay identical to
    apply/field_rules.mjs `normQuestion` (parity checked in tests)."""
    return _NORM_RE.sub(" ", (text or "").lower()).strip()


def git_commit() -> str | None:
    """HEAD commit of the project repo, suffixed '+dirty' when the working
    tree has uncommitted changes to tracked/unignored files."""
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=TRACKER_ROOT,
                              capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=TRACKER_ROOT,
                               capture_output=True, text=True, check=True).stdout.strip()
        return head + ("+dirty" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return None


# ------------------------------------------------------------- connection ---

def connect(db_path: Path | str = DB_PATH) -> sqlite3.Connection:
    """Autocommit connection (isolation_level=None) so `tx()` controls
    transactions explicitly with BEGIN IMMEDIATE."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), isolation_level=None, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=60000")
    schema.migrate(conn)  # idempotent CREATE ... IF NOT EXISTS
    return conn


@contextlib.contextmanager
def tx(conn: sqlite3.Connection):
    """Exclusive write transaction: record rows + their events commit together
    or not at all, and concurrent writers (batch run + manual CLI) serialise."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


# ------------------------------------------------------------------ store ---

def store_bytes(conn: sqlite3.Connection, data: bytes, ext: str, actor: str,
                job_id: str | None = None) -> str:
    """Content-addressed, write-once file store. Returns the sha256. Must be
    called inside tx()."""
    ext = ext.lstrip(".").lower() or "bin"
    digest = sha256_bytes(data)
    rel = Path("store") / digest[:2] / f"{digest}.{ext}"
    path = PIPELINE_ROOT / rel
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        os.chmod(path, stat.S_IREAD)
    elif sha256_bytes(path.read_bytes()) != digest:
        raise RuntimeError(f"store corruption: {path} does not hash to its name")
    exists = conn.execute("SELECT 1 FROM blobs WHERE sha256=?", (digest,)).fetchone()
    if not exists:
        record(conn, "blobs", {"sha256": digest, "ext": ext, "size": len(data),
                               "store_path": rel.as_posix(), "created_at": now()},
               actor=actor, job_id=job_id, event_type="blob.stored")
    return digest


def store_file(conn: sqlite3.Connection, path: str | Path, actor: str,
               job_id: str | None = None) -> str:
    path = Path(path)
    return store_bytes(conn, path.read_bytes(), path.suffix, actor, job_id)


def blob_path(conn: sqlite3.Connection, digest: str) -> Path:
    row = conn.execute("SELECT store_path FROM blobs WHERE sha256=?", (digest,)).fetchone()
    if row is None:
        raise KeyError(f"no blob {digest}")
    return PIPELINE_ROOT / row["store_path"]


def export_blob(conn: sqlite3.Connection, digest: str, dest: str | Path) -> Path:
    """Copy a stored blob to a working location (e.g. out/<job_id>/ view)."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        os.chmod(dest, stat.S_IWRITE | stat.S_IREAD)
    shutil.copyfile(blob_path(conn, digest), dest)
    return dest


# --------------------------------------------------------- records/events ---

def row_hash(table: str, row: dict) -> str:
    excl = HASH_EXCLUDE.get(table, set())
    return sha256_text(canonical({k: v for k, v in row.items() if k not in excl}))


def _event_hash(prev: str, ts: str, actor: str, job_id: str | None, etype: str, payload: str) -> str:
    return sha256_text("\n".join([prev, ts, actor, job_id or "", etype, payload]))


def append_event(conn: sqlite3.Connection, actor: str, etype: str, payload: dict,
                 job_id: str | None = None) -> int:
    """Append to the hash chain. Must be called inside tx()."""
    if not conn.in_transaction:
        raise RuntimeError("append_event must run inside ledger.tx()")
    last = conn.execute("SELECT hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
    prev = last["hash"] if last else GENESIS
    ts = now()
    body = canonical(payload)
    h = _event_hash(prev, ts, actor, job_id, etype, body)
    cur = conn.execute(
        "INSERT INTO events(ts, actor, job_id, type, payload, prev_hash, hash) VALUES(?,?,?,?,?,?,?)",
        (ts, actor, job_id, etype, body, prev, h))
    return cur.lastrowid


def record(conn: sqlite3.Connection, table: str, row: dict, actor: str,
           event_type: str, job_id: str | None = None, extra: dict | None = None):
    """Insert an attested row and its event atomically (inside tx()).
    Returns the new rowid (or the primary key for keyed tables)."""
    if table not in ATTESTED:
        raise ValueError(f"{table} is not an attested table")
    cols = list(row.keys())
    cur = conn.execute(
        f"INSERT INTO {table}({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
        [row[c] for c in cols])
    pk = ATTESTED[table]
    if pk == "id" and "id" not in row:
        row = {"id": cur.lastrowid, **row}
    stored = _fetch_row(conn, table, row)
    payload = {"table": table, "key": _row_key(table, stored), "row_sha256": row_hash(table, stored)}
    if extra:
        payload["extra"] = extra
    append_event(conn, actor, event_type, payload, job_id=job_id)
    return stored.get("id", stored.get("sha256"))


def _row_key(table: str, row: dict):
    pk = ATTESTED[table]
    if pk is None:  # answer_aliases
        return [row["question_norm"], row["bank_id"]]
    return row[pk]


def _fetch_row(conn: sqlite3.Connection, table: str, row: dict) -> dict:
    pk = ATTESTED[table]
    if pk is None:
        r = conn.execute(f"SELECT * FROM {table} WHERE question_norm=? AND bank_id=?",
                         (row["question_norm"], row["bank_id"])).fetchone()
    else:
        r = conn.execute(f"SELECT * FROM {table} WHERE {pk}=?", (row[pk],)).fetchone()
    return dict(r)


def form_answers_hash(rows: list[dict]) -> str:
    cols = ["idx", "label", "type", "required", "value", "readback_value",
            "source_kind", "source_ref", "verified"]
    return sha256_text(canonical([{c: r.get(c) for c in cols} for r in sorted(rows, key=lambda r: r["idx"])]))


# --------------------------------------------------------- profile version ---

def profile_version(conn: sqlite3.Connection, actor: str = "ledger") -> int:
    """Snapshot facts.yaml + answers.yaml (full text) if changed since the last
    snapshot; return the version id. Must be called inside tx()."""
    facts = FACTS_PATH.read_text(encoding="utf-8")
    answers = ANSWERS_PATH.read_text(encoding="utf-8")
    fsha, asha = sha256_text(facts), sha256_text(answers)
    combined = sha256_text(fsha + asha)
    row = conn.execute("SELECT id FROM profile_versions WHERE sha256=?", (combined,)).fetchone()
    if row:
        return row["id"]
    return record(conn, "profile_versions", {
        "sha256": combined, "facts_sha256": fsha, "answers_sha256": asha,
        "facts_yaml": facts, "answers_yaml": answers, "git_commit": git_commit(),
        "created_at": now()}, actor=actor, event_type="profile.version")


# ------------------------------------------------------------------ verify ---

def verify(conn: sqlite3.Connection, check_files: bool = True) -> list[str]:
    problems: list[str] = []

    # 1. Event chain: contiguous seq, correct links, correct hashes.
    prev, expected_seq = GENESIS, 1
    attest: dict[tuple, str] = {}
    for e in conn.execute("SELECT * FROM events ORDER BY seq"):
        if e["seq"] != expected_seq:
            problems.append(f"event chain gap: expected seq {expected_seq}, found {e['seq']}")
            expected_seq = e["seq"]
        if e["prev_hash"] != prev:
            problems.append(f"event {e['seq']}: prev_hash does not link to event {e['seq'] - 1}")
        h = _event_hash(e["prev_hash"], e["ts"], e["actor"], e["job_id"], e["type"], e["payload"])
        if h != e["hash"]:
            problems.append(f"event {e['seq']}: content does not match its hash (edited)")
        try:
            p = json.loads(e["payload"])
        except ValueError:
            problems.append(f"event {e['seq']}: payload is not JSON")
            p = {}
        if isinstance(p, dict) and "table" in p and "row_sha256" in p:
            attest[(p["table"], canonical(p["key"]))] = p["row_sha256"]
        prev, expected_seq = e["hash"], expected_seq + 1

    # 2. Every attested row matches its event; no event points at a missing row.
    seen = set()
    for table in ATTESTED:
        for r in conn.execute(f"SELECT * FROM {table}"):
            row = dict(r)
            key = (table, canonical(_row_key(table, row)))
            seen.add(key)
            want = attest.get(key)
            if want is None:
                problems.append(f"{table} {key[1]}: no attesting event (inserted outside the ledger)")
            elif want != row_hash(table, row):
                problems.append(f"{table} {key[1]}: row differs from what was recorded (edited)")
    for key in attest:
        if key not in seen:
            problems.append(f"{key[0]} {key[1]}: recorded by an event but missing (deleted)")

    # 3. Form answers still hash to what their application recorded.
    for app in conn.execute("SELECT id, form_answers_sha256 FROM applications"):
        rows = [dict(r) for r in conn.execute("SELECT * FROM form_answers WHERE application_id=?", (app["id"],))]
        if form_answers_hash(rows) != app["form_answers_sha256"]:
            problems.append(f"application {app['id']}: form answers differ from what was recorded")

    # 4. Stored files still hash to their names.
    if check_files:
        for b in conn.execute("SELECT sha256, store_path FROM blobs"):
            p = PIPELINE_ROOT / b["store_path"]
            if not p.exists():
                problems.append(f"blob {b['sha256']}: file missing ({b['store_path']})")
            elif sha256_bytes(p.read_bytes()) != b["sha256"]:
                problems.append(f"blob {b['sha256']}: file content changed ({b['store_path']})")

    # 5. llm outputs still hash to what was recorded.
    for c in conn.execute("SELECT id, output_text, output_sha256 FROM llm_calls WHERE output_text IS NOT NULL"):
        if sha256_text(c["output_text"]) != c["output_sha256"]:
            problems.append(f"llm_call {c['id']}: output_text does not match output_sha256")
    return problems
