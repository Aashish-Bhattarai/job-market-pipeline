"""Adzuna UK jobs API -- deliberately NOT used by the pipeline.

Verified 2026-10-01 against developer.adzuna.com/docs/terms_of_service and the
signed-in plan page. The terms permit "personal research" and require every
query to go "via Adzuna": "Any attempt to contact a third party, even where
they provide listings content, will be considered a breach of these terms
and conditions. If an API user contacts a third party content provider their
access to the API will be revoked immediately."

This pipeline does the opposite by design: resolve_apply_url.py probes the
employer's ATS (Greenhouse/Lever/Ashby/Workable) to turn an aggregator
listing into the real posting, and apply/ fills the employer's own form.
The API also returns only ~500-character description snippets, and the full
text is reachable only by contacting the employer's system -- the same
prohibited act -- so FIT could never judge an Adzuna job on its real posting.

The operator's instruction, 2026-10-01: do not work against the policy. So no
Adzuna row may enter jobs.db. (Plan was "Trial Access": 25 hits/min, 250/day,
1000/week, 2500/month; no expiry shown.) Re-enable only with Adzuna's written
consent for this use (api-partnerships@adzuna.com).
"""
from __future__ import annotations


def fetch(config: dict) -> list[dict]:
    raise NotImplementedError(
        "Adzuna's API terms require all queries to go via Adzuna and treat contacting "
        "the employer / third-party content provider as a breach (API access revoked); "
        "this pipeline resolves and applies on the employer's own system, and the API "
        "returns only ~500-char descriptions. Not used -- see this module's docstring."
    )
