# Job-Market Intelligence Pipeline

A multi-board UK job-search pipeline: 16 job-board/ATS adapters feed a discovery and geography
filter, an LLM-driven requirement-extraction and fit-matching stage decides which postings are
worth pursuing, a fact-grounded writer/auditor pair drafts CVs and cover letters that can only ever
restate a fact on file, and an append-only, hash-chained ledger records exactly what was sent,
where every word came from, and which model (or human) produced it. A browser-automation engine
for filling and submitting employer application forms is one component among several — not the
headline feature.

## Why it's built this way

Most "auto-apply" tools either guess at form answers or trust whatever a resume parser infers.
This pipeline treats every claim a CV, cover letter, or form answer makes as something that must
be *provable*: traced back to a line in `profile/facts.yaml`, a declared standing answer in
`profile/answers.yaml`, a prior human-approved answer-bank entry, or an audited, model-drafted
answer that a second, independent model checked against the facts before it was ever used. Nothing
is invented, and everything is logged so the chain from "why did I say X to company Y" back to the
exact fact and model call is always reconstructable.

## Architecture

```
discover (16 board/ATS adapters) --> filter (geography/title/dedupe)
  --> fit (small model: requirement extraction, code-verified; pure-code rubric)
  --> tailor (fact ranking, writer + independent auditor)
  --> render (CV/cover -> DOCX + PDF)
  --> questions (answer bank / fact-grounded essay, audited)
  --> apply (browser form-fill + pre-submit snapshot gate)
  --> ledger (append-only, hash-chained: every file, model call, and field traced)
```

See `DESIGN.md` for the full data model and stage-by-stage rationale, and `CONTRACTS.md` for every
file/data shape the stages agree on.

## Components

- `discover/` — job discovery: 16 source adapters (Greenhouse, Lever, Ashby, Workable,
  Recruitee, Teamtailor, SmartRecruiters, Himalayas, Reed, Gradcracker, HiringCafe, jobs.ac.uk,
  HN "Who is hiring", Welcome to the Jungle, and more) plus geography/title filtering.
- `stages/` — LLM pipeline stages: requirement extraction, fit rubric, tailoring, the answer-bank
  question resolver, and the base-cover-letter builder.
- `ledger/` — the append-only, hash-chained SQLite ledger: every model call, document, application
  attempt, and form answer, with an audit-verifiable event chain.
- `apply/` — the Node/puppeteer-core browser-fill engine: pure-logic field-classification rules
  (`field_rules.mjs`, fully unit-tested with no browser needed), DOM extraction, and the runner
  that drives the browser and snapshots every field before submit.
- `render/` — CV and cover-letter renderers (DOCX + PDF), validated against the fact bank so no
  rendered bullet can say something not in `facts.yaml`.
- `prompts/` — the LLM prompt templates for every model stage.
- `run/` — CLI entrypoints (`batch.py` orchestrates everything; `approve.py`, `verify_audit.py`,
  `daily_report.py`, etc. are individually runnable).
- `tests/` (pytest) and `apply/test_*.mjs` (Node) — the unit test suites; all use temp databases
  and synthetic fixtures, never real data.

## Setup

1. **Python 3.13+**: stdlib plus `pyyaml`, `python-docx`, `requests`, `openpyxl` (`pip install
   pyyaml python-docx requests openpyxl`).
2. **Node 20+**: `cd apply && npm install` (puppeteer-core, js-yaml, linkedom).
3. **Your profile**: copy `profile/answers.example.yaml` → `profile/answers.yaml` and
   `profile/facts.example.yaml` → `profile/facts.yaml` (both gitignored) and fill in your own
   data — the examples are a synthetic "Demo Candidate" so the test suites and a first dry run
   work out of the box.
4. **Source API keys** (optional, free registrations): set `REED_API_KEY` etc. as an env var
   (see `.env.example`) or, if you must, paste it into `config.yaml`'s `sources:` block locally —
   never commit a real key. Missing keys just skip that source.
5. **Run the tests**: `pytest tests/` and `cd apply && npm test` (or `node test_rules.mjs && node
   test_extraction.mjs`).
6. **Dry run**: `python run/batch.py` with `config.yaml` `submissions.enabled: false` or
   `auto_submit: false` to see discovery/fit/tailor without ever touching a browser.

## Limitations, honestly

- The browser-fill engine has field-classification rules tuned against dozens of real ATS forms
  (Greenhouse, Lever, Ashby, Workable, Teamtailor, SmartRecruiters, Recruitee...); a genuinely novel
  form layout will still queue unknown fields for a human rather than guess.
- The fact-grounded audit gate is deliberately strict — it will hold back more than it needs to
  rather than let an uncited claim through. Expect to answer a steady trickle of genuinely new
  questions via the answer bank, especially early on.
- Discovery adapters depend on each board's public API/HTML shape; boards change layouts and
  adapters will need maintenance over time.
- This is a personal-scale tool, not a hardened multi-tenant service — see `SECURITY.md` for what
  it does and doesn't protect against.

## License

MIT — see `LICENSE`.
