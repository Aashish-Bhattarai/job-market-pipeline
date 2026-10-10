# Security

## What this repo expects you to provide yourself
- **Source API keys** (e.g. Reed): set via an env var (`REED_API_KEY`, see `.env.example`) or,
  if you must, directly in `config.yaml`'s `sources:` block on your own machine. Either way,
  **no real key belongs in a commit** — the tracked `config.yaml` ships with every key empty.
- **Your facts and form answers**: copy `profile/facts.example.yaml` → `profile/facts.yaml` and
  `profile/answers.example.yaml` → `profile/answers.yaml`. Both filenames are gitignored
  specifically so your personal data can never be committed by accident, even if you edit them
  in place.
- **Your own base CV**: `profile/base_cv.pdf` (gitignored) if you use `stages/base_cover.py`.

## What never belongs in this repo
- API keys, passwords, or any other credential value, in code, config, comments, fixtures, or
  commit messages.
- Real CVs, cover letters, screenshots, the SQLite job database, or any other personal application
  data. `data/`, `store/`, `out/`, and `work/` are gitignored outright.
- Anything that would let someone reconstruct a specific person's job-search activity: real
  addresses, phone numbers, nationality, date of birth, employer names from a real application
  history, or similar. Tests and fixtures use synthetic data only (`profile/*.example.yaml`,
  `render/` fixtures) — keep it that way in any contribution.

## Before you push
Run a manual sweep for credential-shaped strings and personal data before pushing anything beyond
this starting point:
```sh
python scripts/scan_secrets.py
```
It flags API-key-shaped assignments (`api_key:`/`app_key:`/`password:` followed by a long
hex/base64/UUID-like value), bearer tokens, and private-key headers across every tracked text file.
It is a heuristic scan, not a guarantee — review the diff yourself too, especially around
`config.yaml`, `.env`, and anything under `profile/`.

## Reporting a problem
This is a personal/example project rather than a maintained security-sensitive service; if you
spot a credential or personal-data leak in the published history, open an issue describing what
you found (without reposting the sensitive value itself).
