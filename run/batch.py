"""One command for the whole pipeline. Owner: integrator.

Usage (from the `Job Application Tracker` folder):
    python pipeline/run/batch.py                  # full batch
    python pipeline/run/batch.py --skip-discover  # reuse today's discovery
    python pipeline/run/batch.py --no-apply       # stop before the browser step

Resumable: every stage selects its work from jobs.status (and the ledger), so a
crashed or interrupted batch simply picks up where each job stands on the next
run. Stages run as subprocesses so one job's failure never stops the batch;
each failure is logged as an event and surfaces in the digest.

Order:
  0. lock, DB backup, git auto-snapshot, ledger verify, `batch.started` event
  1. discover + filter + resolve aggregator apply URLs         (code)
  2. requirements (small model) + fit rules (code) for `new` jobs with posting text (limit fit_per_batch)
  3. tailor top `fit_pass` by score (code select, mid writer, strong auditor)
  4. questions: bank lookup / small-model mapping / fact-grounded essays
  5. apply, one Brave page at a time (runner.mjs), ingest each attempt
       auto_submit=false: tailored -> dry run -> `ready`; `approved` -> submit
       auto_submit=true:  tailored -> submit (all gates still apply)
  6. questions again (new ones from step 5), views, digest, verify, `batch.finished`
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PIPELINE = Path(__file__).resolve().parent.parent
ROOT = PIPELINE.parent
sys.path.insert(0, str(PIPELINE))
from ledger import core, llm  # noqa: E402
from discover import filter as filtermod  # noqa: E402

PY = sys.executable
LOCK = PIPELINE / "data" / "batch.lock"
BACKUPS = PIPELINE / "data" / "backups"
KEEP_BACKUPS = 14
RUNNER = PIPELINE / "apply" / "runner.mjs"
ACTOR = "code:batch"

# FIT only judges a job whose posting text is really there. Aggregator rows
# arrive as stubs (Gradcracker ~65 chars, jobs.ac.uk none) until
# resolve_apply_url.py swaps in the employer's posting; real postings run 1k+.
# ponytail: crude length bar; add a per-source "description_complete" flag if it ever misjudges.
MIN_JD_CHARS = 300


def log(msg: str) -> None:
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


# ------------------------------------------------------------ safety rails ---

def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True,
                         timeout=30)
    return str(pid) in out.stdout


def acquire_lock() -> None:
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            pid = json.loads(LOCK.read_text())["pid"]
        except (ValueError, KeyError, OSError):
            pid = -1
        if _pid_alive(pid):
            sys.exit(f"another batch is running (pid {pid}); refusing to start")
        LOCK.unlink()
        fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    with os.fdopen(fd, "w") as fh:
        json.dump({"pid": os.getpid(), "started_at": core.now()}, fh)


def backup_db() -> Path:
    BACKUPS.mkdir(parents=True, exist_ok=True)
    dest = BACKUPS / f"jobs-{dt.datetime.now():%Y%m%d-%H%M%S}.db"
    src = sqlite3.connect(str(core.DB_PATH))
    dst = sqlite3.connect(str(dest))
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    for old in sorted(BACKUPS.glob("jobs-*.db"))[:-KEEP_BACKUPS]:
        old.unlink()
    return dest


def git_snapshot() -> str | None:
    """Commit any uncommitted code/profile edits so every record of this batch
    points at an exact, recoverable commit."""
    status = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True,
                            timeout=120)
    if status.returncode != 0:
        return None
    if status.stdout.strip():
        subprocess.run(["git", "add", "-A"], cwd=ROOT, check=True, timeout=120)
        subprocess.run(["git", "commit", "-qm", f"auto-snapshot before batch {core.now()}"], cwd=ROOT,
                       check=True, timeout=120)
    return core.git_commit()


# ----------------------------------------------------------------- helpers ---

def run_step(args: list[str], label: str, job_id: str | None = None, timeout: int = 3600) -> int:
    """Run a stage subprocess from pipeline/; a non-zero exit is recorded,
    never raised."""
    log(f"{label}{' ' + job_id if job_id else ''}")
    try:
        proc = subprocess.run(args, cwd=PIPELINE, timeout=timeout, capture_output=True,
                              text=True, encoding="utf-8", errors="replace")
        rc, out = proc.returncode, (proc.stdout + proc.stderr)
    except subprocess.TimeoutExpired:
        rc, out = -1, f"timeout after {timeout}s"
    tail = out.strip().splitlines()[-6:]
    for line in tail:
        log(f"    {line}")
    if rc != 0:
        conn = core.connect()
        try:
            with core.tx(conn):
                core.append_event(conn, ACTOR, "stage.failed",
                                  {"stage": label, "exit_code": rc, "output_tail": tail}, job_id=job_id)
        finally:
            conn.close()
    return rc


# The operator's location ladder (config.yaml geography): London/Sussex first, then remote, then the rest of the UK.
LADDER_ORDER = ("CASE region WHEN 'london' THEN 0 WHEN 'sussex' THEN 0 WHEN 'remote' THEN 1 ELSE 2 END, "
                "fit_score DESC")


def jobs_with_status(status: str, order: str, limit: int, min_desc: int = 0) -> list[sqlite3.Row]:
    conn = core.connect()
    try:
        return conn.execute(
            "SELECT id, company, title FROM jobs WHERE status=? AND LENGTH(COALESCE(description,''))>=? "
            f"ORDER BY {order} LIMIT ?", (status, min_desc, limit)).fetchall()
    finally:
        conn.close()


def approved_answers(conn, job_id: str):
    row = conn.execute("SELECT payload FROM events WHERE job_id=? AND type='submit.approved' "
                       "ORDER BY seq DESC LIMIT 1", (job_id,)).fetchone()
    return json.loads(row["payload"]).get("answers") if row else None


def companies_at_cap() -> set[str]:
    conn = sqlite3.connect(str(core.DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        return filtermod.companies_at_cap(conn, dt.date.today())
    finally:
        conn.close()


# ------------------------------------------------------------------ stages ---

def stage_discover() -> None:
    run_step([PY, "discover/discover.py", "--companies", "discover/companies.yaml", "--sources", "all"], "discover")
    run_step([PY, "discover/filter.py"], "filter")
    run_step([PY, "run/resolve_apply_url.py", "--all-missing"], "resolve_apply_url")


def _title_ok(title: str | None) -> bool:
    """When config.yaml's role_title_filter is set, only matching titles are
    processed end to end (scoped runs, e.g. AI Engineer only)."""
    pattern = (llm.load_config().get("role_title_filter") or "").strip()
    if not pattern:
        return True
    return re.search(pattern, title or "", re.I) is not None


def stage_fit(limit: int, workers: int = 1) -> None:
    pool = jobs_with_status("new", "posted_at DESC", limit * 3, MIN_JD_CHARS)
    candidates = [j for j in pool if _title_ok(j["title"])]
    skipped = len(pool) - len(candidates)
    if skipped:
        log(f"role_title_filter: {skipped} new job(s) skipped (title outside scope)")
    conn = core.connect()
    try:
        stubs = conn.execute("SELECT COUNT(*) FROM jobs WHERE status='new' AND LENGTH(COALESCE(description,''))<?",
                             (MIN_JD_CHARS,)).fetchone()[0]
    finally:
        conn.close()
    if stubs:
        log(f"{stubs} new job(s) not FIT-checked: posting text under {MIN_JD_CHARS} chars "
            "(waiting for resolve_apply_url to find the employer posting)")
    def _fit_one(job) -> None:
        if run_step([PY, "-m", "stages.requirements", job["id"]], "requirements", job["id"]) == 0:
            run_step([PY, "-m", "stages.fit_rules", job["id"]], "fit_rules", job["id"])

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        list(ex.map(_fit_one, candidates[:limit]))


def limit_per_company(jobs, taken: dict[str, int], cap: int = 2) -> list:
    """Keep, per company, only as many of `jobs` (already in priority order) as the company still has cap slots
    for: `taken` = its applications in the last 30 days + jobs already tailored and waiting. A company with a dozen
    near-identical postings (Bjak: 12) would otherwise burn twelve tailoring runs for at most two submissions."""
    out, used = [], dict(taken)
    for j in jobs:
        key = filtermod.normalize_company(j["company"])
        if used.get(key, 0) >= cap:
            continue
        used[key] = used.get(key, 0) + 1
        out.append(j)
    return out


_JOB_ID_RES = (
    re.compile(r"/j/([A-Za-z0-9]{8,12})(?:[/?#]|$)"),                                              # Workable shortcode
    re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", re.I),           # Lever / Ashby uuid
    re.compile(r"(?:gh_jid=|/jobs/)(\d{5,})"),                                                      # Greenhouse job id
)


def job_identity(url: str | None) -> str | None:
    """What makes two apply URLs one posting: the ATS's own job id (Workable shortcode, Lever/Ashby uuid, Greenhouse id),
    whatever slug, suffix or tracking parameters surround it; else the URL without query or a trailing /apply."""
    for rx in _JOB_ID_RES:
        m = rx.search(url or "")
        if m:
            return m.group(1).lower()
    u = (url or "").split("?")[0].split("#")[0].rstrip("/").lower()
    return (u[:-len("/apply")] if u.endswith("/apply") else u) or None


def duplicate_of_applied(conn, job_id: str) -> str | None:
    """Id of an already-applied job whose application page is the same posting as `job_id`'s, else None. Aggregator rows
    arrive under another source, company spelling or title, so name-based duplicate detection can miss them; the page
    they would submit to cannot differ."""
    row = conn.execute("SELECT apply_url FROM jobs WHERE id=?", (job_id,)).fetchone()
    ident = job_identity(row["apply_url"]) if row else None
    if not ident:
        return None
    for r in conn.execute("SELECT id, apply_url FROM jobs WHERE status='applied' AND id<>?", (job_id,)):
        if job_identity(r["apply_url"]) == ident:
            return r["id"]
    return None


def _slots_taken() -> dict[str, int]:
    """Per normalised company: applications in the last 30 days + jobs tailored/approved and waiting to be applied."""
    conn = core.connect()
    try:
        cutoff = (dt.date.today() - dt.timedelta(days=30)).isoformat()
        taken: dict[str, int] = {}
        for r in conn.execute("SELECT company, status, applied_at FROM jobs WHERE status IN ('applied','tailored','approved')"):
            if r["status"] == "applied" and (r["applied_at"] or "")[:10] < cutoff:
                continue
            key = filtermod.normalize_company(r["company"])
            taken[key] = taken.get(key, 0) + 1
        return taken
    finally:
        conn.close()


def stage_tailor(limit: int, workers: int = 1) -> None:
    """Tailor the top `limit` fit_pass jobs, `workers` at a time. Each job is its own subprocess
    (one job's failure never stops the batch); the ledger serialises concurrent writers
    (BEGIN IMMEDIATE, 60s busy timeout) and the Word PDF conversion takes a cross-process lock."""
    at_cap = companies_at_cap()
    jobs = [j for j in jobs_with_status("fit_pass", LADDER_ORDER, limit)
            if filtermod.normalize_company(j["company"]) not in at_cap]  # the apply step would hold them anyway
    jobs = limit_per_company(jobs, _slots_taken())
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        list(pool.map(lambda j: run_step([PY, "-m", "stages.tailor", j["id"]], "tailor", j["id"]), jobs))


def stage_questions() -> None:
    run_step([PY, "-m", "stages.questions"], "questions")


def apply_one(job_id: str, mode: str, answers) -> str:
    """Build the order, drive Brave, ingest. Returns 'ok', 'browser_down' or 'failed'."""
    sys.path.insert(0, str(PIPELINE))
    from ledger import orders  # BankRecords slice
    conn = core.connect()
    try:
        order_path = orders.build_order(conn, job_id, mode, approved_answers=answers)
    finally:
        conn.close()
    rc = run_step(["node", str(RUNNER), str(order_path)], f"apply[{mode}]", job_id, timeout=900)
    attempt_json = Path(order_path).parent / "attempt.json"
    if attempt_json.exists():
        run_step([PY, "run/ingest_attempt.py", str(Path(order_path).parent)], "ingest_attempt", job_id)
        return "ok"
    return "browser_down" if rc in (3, 4) else "failed"


def _open_required_questions(conn, job_id: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM question_queue WHERE job_id=? AND required=1 AND status='open'",
        (job_id,)).fetchone()[0]


def _submit_block_reason(cfg: dict) -> str | None:
    """Why submit mode may not run right now, or None if it may. Approved
    jobs simply stay approved — the next eligible batch submits them."""
    sub = cfg.get("submissions") or {}
    if not sub.get("enabled", False):
        return "submissions.enabled is false in config.yaml"
    enabled_from = str(sub.get("enabled_from") or "").strip()
    if enabled_from:
        today = dt.date.today().isoformat()
        if today < enabled_from:
            return f"submissions.enabled_from is {enabled_from} (today is {today})"
    return None


def _submitted_today_count(conn) -> int:
    """Real submissions since LOCAL midnight. applications.started_at is UTC ('...Z'), so the local
    day is converted to a UTC bound -- comparing UTC date strings to the local date miscounts for
    the first hour of every BST day."""
    midnight = dt.datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    since = midnight.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return conn.execute(
        "SELECT COUNT(*) FROM applications WHERE status='submitted' AND started_at >= ?",
        (since,)).fetchone()[0]


def _submit_blocked(cfg: dict, conn) -> str | None:
    """Why a REAL submission may not happen right now (master switch, start date, daily cap), or
    None. Checked immediately before every submit-mode attempt -- for approved jobs AND for
    auto_submit tailored jobs, which used to bypass it entirely -- so the cap holds mid-run."""
    block = _submit_block_reason(cfg)
    if block:
        return block
    cap = (cfg.get("submissions") or {}).get("daily_cap")
    if cap is not None and _submitted_today_count(conn) >= int(cap):
        return f"daily submission cap ({cap}/day) reached"
    return None


def _unconfirmed_submit(conn, job_id: str) -> bool:
    """The last attempt's ONLY error was "No success confirmation detected after clicking submit": no validation
    message, no captcha, no closed posting. The application may well have been sent."""
    row = conn.execute("SELECT status, note FROM applications WHERE job_id=? ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
    return bool(row and row["status"] == "submit_failed"
                and (row["note"] or "").strip() == "No success confirmation detected after clicking submit")


_AMBIGUOUS_NOTE = "No success confirmation detected after clicking submit"


def _risky_submit_count(conn, job_id: str) -> int:
    """Submit clicks that may have SENT the application: a confirmed submit, or one whose only outcome was the generic "no
    confirmation" note. A click the site refused with its own validation text ("This field is required.") sent nothing, so
    it is not counted: Jane Street's form used all 4 attempts on such refusals while a fix was still being built."""
    return conn.execute(
        "SELECT COUNT(*) FROM applications WHERE job_id=? AND (status='submitted' OR "
        "(status='submit_failed' AND TRIM(COALESCE(note,''))=?))", (job_id, _AMBIGUOUS_NOTE)).fetchone()[0]


# What a site's own text says when it has stopped an attempt for a human to look at (never worked around), or when the
# company itself refuses the application: ElevenLabs' Ashby form answered "you have applied for a position in this
# domain within the last 90 days, you cannot submit an application" to a retry as well as to the first click.
_HUMAN_WALL_RE = re.compile(
    r"spam|captcha|verify (that )?you are|are you a (human|robot)|too many (requests|attempts)|rate.?limit"
    r"|temporarily (blocked|restricted)|unusual activity|suspicious|access (denied|is temporarily)"
    r"|limiting applications|you cannot (submit|apply)|already (applied|submitted)|applied (for|to) [^.]{0,60}within the last", re.I)


def stage_apply(limit: int) -> None:
    cfg = llm.load_config()
    at_cap = companies_at_cap()
    work: list[tuple[str, str, object]] = []
    conn = core.connect()
    try:
        approved = [j for j in jobs_with_status("approved", "id", limit) if _title_ok(j["title"])]
        tailored = [j for j in jobs_with_status("tailored", LADDER_ORDER, limit)
                    if _title_ok(j["title"])]
        out_of_scope = (len(jobs_with_status("approved", "id", limit)) - len(approved)) + \
                       (len(jobs_with_status("tailored", LADDER_ORDER, limit)) - len(tailored))
        if out_of_scope:
            log(f"role_title_filter: {out_of_scope} tailored/approved job(s) skipped "
                "(title outside scope)")
        for job in approved:
            work.append((job["id"], "submit", approved_answers(conn, job["id"])))
        # A 'tailored' job (including v1-era rows carried over by the
        # migration) can already have an open required question from an
        # earlier attempt -- e.g. Aalyria's nationality question. Dry-running
        # it again can only ever land back on queued_manual, so skip it here
        # (recording why) instead of wasting an attempt and a Tracker row.
        for job in tailored:
            pending = _open_required_questions(conn, job["id"])
            if pending:
                with core.tx(conn):
                    conn.execute("UPDATE jobs SET status='queued_manual', "
                                 "reason=? WHERE id=?",
                                 (f"{pending} open required question(s) pending", job["id"]))
                    core.append_event(conn, ACTOR, "job.status", {
                        "from": "tailored", "to": "queued_manual",
                        "reason": f"{pending} open required question(s) pending"}, job_id=job["id"])
                log(f"skip apply {job['id']}: {pending} open required question(s) pending")
                continue
            work.append((job["id"], "submit" if cfg.get("auto_submit") else "dry_run", None))
        companies = {r["id"]: r["company"] for r in conn.execute(
            f"SELECT id, company FROM jobs WHERE id IN ({','.join('?' * len(work))})",
            [w[0] for w in work])} if work else {}
    finally:
        conn.close()

    submitted_this_run: dict[str, int] = {}

    def _stopped_by_human_check(conn, job_id: str) -> bool:
        row = conn.execute("SELECT note FROM applications WHERE job_id=? ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
        return bool(row and row["note"] and _HUMAN_WALL_RE.search(row["note"]))

    def attempt(job_id: str, mode: str, answers) -> str:
        """One apply attempt: the job's status afterwards, or 'held' / 'browser_down'. A submit is
        re-checked against the master switch, the daily cap and the 2-per-company cap immediately
        before it runs, so the caps hold mid-run (and on the retry pass), and only a job that
        actually reached 'applied' counts toward a company's cap -- a failed form must not use up
        the slot."""
        key = filtermod.normalize_company(companies.get(job_id, ""))
        if mode == "submit":
            conn = core.connect()
            try:
                if _stopped_by_human_check(conn, job_id):
                    # A wall (captcha, spam flag, "limiting applications") is a person's to clear. Re-queueing the job by hand
                    # must not click Submit at it again: causaLens and Spotify were each clicked several times at an hCaptcha.
                    log(f"skip submit {job_id}: its last attempt hit a human-verification wall; leave it for a person")
                    return "held"
                dup = duplicate_of_applied(conn, job_id)
                if dup:
                    from stages.common import set_job_status
                    with core.tx(conn):
                        set_job_status(conn, job_id, "skipped", "code:batch",
                                       reason=f"same application page as already-applied job {dup}")
                held = (f"same application page as already-applied job {dup}; skipped" if dup
                        else f"company cap (2 per 30 days) reached for {companies.get(job_id)}"
                        if key in at_cap or submitted_this_run.get(key, 0) >= 2
                        else _submit_blocked(cfg, conn))
                max_attempts = int((cfg.get("submissions") or {}).get("max_attempts_per_job", 4))
                # Only attempts that CLICKED submit count: a form held before the click (a missing answer, queued_manual)
                # has no side effect, and ElevenLabs Full-Stack used all 4 on holds that a bank fix then cleared.
                n_attempts = _risky_submit_count(conn, job_id)
                if held is None and n_attempts >= max_attempts:
                    # Deliveroo reached 5 submit attempts on 2026-10-02, every one "no success
                    # confirmation": a form the pipeline cannot confirm must not be re-clicked forever.
                    held = (f"{n_attempts} previous submit attempts that may have sent the application, unconfirmed "
                            f"(submissions.max_attempts_per_job={max_attempts})")
            finally:
                conn.close()
            if held:
                log(f"skip submit {job_id}: {held}")
                return "held"
        if apply_one(job_id, mode, answers) == "browser_down":
            log("Brave is not reachable on port 9223 (or the browser lock is held); stopping the apply step")
            return "browser_down"
        conn = core.connect()
        try:
            row = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            status = row["status"] if row else ""
            wall = status not in ("applied", "ready") and _stopped_by_human_check(conn, job_id)
            unconfirmed = (mode == "submit" and status not in ("applied", "ready")
                           and _unconfirmed_submit(conn, job_id))
        finally:
            conn.close()
        if mode == "submit" and status == "applied":
            submitted_this_run[key] = submitted_this_run.get(key, 0) + 1
        if unconfirmed:
            # The click produced neither a confirmation nor an error: it may well have gone through (Stripe's
            # confirmation renders in an embedded frame; both Stripe roles were submitted while the runner said
            # "submit_failed"). A retry could duplicate a real application (Deliveroo got three), so never.
            log(f"{job_id}: submit unconfirmed (no error either) -- NOT retrying; verify in the inbox")
            return "held"
        if wall:
            # A spam / captcha / rate-limit wall is the site asking for a human. Retrying minutes later
            # only compounds it (Ashby: "flagged as possible spam" twice in a row) and is never a way
            # round it: leave it queued_manual for the operator.
            log(f"{job_id}: stopped by a human-verification wall -- not retrying")
            return "held"
        return status

    retry_pass: list[tuple[str, str, object]] = []
    for job_id, mode, answers in work[:limit]:
        status = attempt(job_id, mode, answers)
        if status == "browser_down":
            return
        # something broken -> move on to the next
        # application, retry it ONCE at the end, and if it still fails leave
        # it queued_manual for the morning (never a repeated loop).
        if status not in ("applied", "ready", "held", ""):
            retry_pass.append((job_id, mode, answers))
    if retry_pass:
        # The attempts above surfaced their form questions; answer what can be answered (bank match
        # or audited fact-grounded essay) BEFORE the one retry, otherwise a form that failed on an
        # unanswered question just fails identically.
        stage_questions()
    for job_id, mode, answers in retry_pass:
        conn = core.connect()
        try:
            pending = _open_required_questions(conn, job_id)
        finally:
            conn.close()
        if pending:
            log(f"no retry for {job_id}: {pending} open required question(s) need the operator")
            continue
        log(f"first pass did not complete {job_id} -- retrying once, then leaving it for the morning")
        if attempt(job_id, mode, answers) == "browser_down":
            return


def stage_report() -> None:
    run_step([PY, "run/views.py"], "views")
    run_step([PY, "run/daily_report.py"], "digest")
    run_step([PY, "run/verify_audit.py"], "verify_audit")


# -------------------------------------------------------------------- main ---

def main() -> int:
    ap = argparse.ArgumentParser(description="Run the job pipeline end to end")
    ap.add_argument("--skip-discover", action="store_true")
    ap.add_argument("--no-apply", action="store_true", help="stop before the browser step")
    ap.add_argument("--fit-limit", type=int, default=None)
    ap.add_argument("--apply-limit", type=int, default=None)
    args = ap.parse_args()
    cfg = llm.load_config()
    fit_limit = args.fit_limit or cfg.get("fit_per_batch", 30)
    apply_limit = args.apply_limit or cfg.get("apply_per_batch", 10)
    fit_workers = int(cfg.get("fit_workers", 1))
    tailor_workers = int(cfg.get("tailor_workers", 1))

    acquire_lock()
    try:
        backup = backup_db()
        commit = git_snapshot()
        conn = core.connect()
        try:
            problems = core.verify(conn)
            with core.tx(conn):
                core.append_event(conn, ACTOR, "batch.started", {
                    "git_commit": commit, "backup": backup.name,
                    "config_sha256": core.sha256_text((PIPELINE / "config.yaml").read_text(encoding="utf-8")),
                    "ledger_problems_at_start": problems[:50], "args": vars(args)})
        finally:
            conn.close()
        if problems:
            log(f"LEDGER VERIFY FOUND {len(problems)} PROBLEM(S) — continuing, see digest")

        if not args.skip_discover:
            stage_discover()
        stage_fit(fit_limit, fit_workers)
        stage_tailor(apply_limit, tailor_workers)
        stage_questions()
        if not args.no_apply:
            stage_apply(apply_limit)
            stage_questions()
        stage_report()

        conn = core.connect()
        try:
            counts = {r["status"]: r["n"] for r in conn.execute(
                "SELECT status, COUNT(*) n FROM jobs GROUP BY status")}
            with core.tx(conn):
                core.append_event(conn, ACTOR, "batch.finished", {"job_status_counts": counts})
        finally:
            conn.close()
        log(f"done: {counts}")
        return 0
    finally:
        LOCK.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
