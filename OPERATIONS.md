# Operations

Short operator notes. See `DESIGN.md` for architecture and `CONTRACTS.md` for file/data shapes.

## One-time setup
1. `pip install -r requirements.txt` equivalent (stdlib + `pyyaml`, `python-docx`, `requests`,
   `openpyxl` — install as needed) and `node >= 20`.
2. `cd apply && npm install` (puppeteer-core, js-yaml, linkedom).
3. Copy `profile/answers.example.yaml` → `profile/answers.yaml` and `profile/facts.example.yaml`
   → `profile/facts.yaml` (both gitignored); fill in your own data. Place your base CV at
   `profile/base_cv.pdf` if you use `stages/base_cover.py`.
4. Add any source API keys you have (Reed, etc.) via an env var (`REED_API_KEY=...`) or, if you
   must, directly in `config.yaml` under `sources:` — never commit a real key (see `SECURITY.md`).
5. `git init` your own working copy if you forked this repo for personal use; this public repo's
   history should stay generic.

## Daily run
- `python run/batch.py` runs discover → filter → fit → tailor → render → apply in one pass,
  resuming from `jobs.status` if interrupted. Safe to schedule unattended (`run/nightly.cmd` on
  Windows Task Scheduler) while `config.yaml` `auto_submit: false` — it only submits jobs you
  approved with `run/approve.py`.
- `python run/approve.py --all-ready` approves every `ready` job for the next batch's submit pass.
- `python run/daily_report.py` / `run/show.py <job_id>` for a human-readable summary.
- `python run/verify_audit.py` recomputes the ledger's hash chain; run it after anything looks off.

## Stages, in order
1. **discover** (`discover/discover.py`) — pulls from every enabled source in `config.yaml`.
2. **filter** (`discover/filter.py`) — title family, geography, posting age, exclusion lists,
   per-company cap.
3. **fit** (`stages/requirements.py` + `stages/fit_rules.py`) — a small model extracts requirements
   (code-verified verbatim quotes), then a pure-code rubric decides pass/fail.
4. **tailor** (`stages/select.py` + `stages/tailor.py`) — ranks facts against requirements, drafts
   a summary/cover letter, and a stronger model audits every sentence against `facts.yaml` before
   anything renders.
5. **render** (`render/render_cv.py`, `render/render_cover.py`) — CV/cover letter to DOCX + PDF.
6. **questions** (`stages/questions.py`) — resolves open form questions against the answer bank,
   or drafts+audits a fact-grounded essay answer; anything else queues for you.
7. **apply** (`apply/runner.mjs` via `run/batch.py`) — fills the form, snapshots every field
   (including anything the ATS autofilled) against facts/answers, and submits only if every
   required field is sourced and the gate passes.

## Submit gate
A job is only ever submitted when: every required field has a traceable source (a fact, an
answers.yaml value, a bank entry, or an explicit human answer), the cover-letter/CV audit passed,
and (in submit mode) the fields about to be sent exactly match what was approved. Anything else
is held as `needs_review` or `queued_manual` with the specific reason recorded.

## Troubleshooting
- A stuck browser lock (`data/browser.lock`): confirm no other `runner.mjs` process is alive, then
  delete the lock file.
- A question that keeps queuing: check `run/answer_question.py --help`; answer it once at the
  right scope (`global` reuses everywhere, `company` only for that employer, `job` only for that
  posting) and every matching open question resolves on the next batch.
- Node test failures after editing `apply/field_rules.mjs` or `apply/dom.mjs`: run
  `node apply/test_rules.mjs && node apply/test_extraction.mjs` directly for fast feedback.
