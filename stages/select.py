"""Stage `select`: pure code, no model call. Builds the non-summary part of
`selection.json` (CONTRACTS.md §3 shape minus `summary` -- stages/tailor.py's
writer stage produces the summary and merges it in) by ranking facts.yaml
entries against the extracted requirements, plus a `drop_order` list (weakest
selected fact first) that stages/tailor.py uses to trim the CV to <= 2 pages.

Headline: one of the four fixed options, chosen from the posting's title.
Experience blocks stay in facts.yaml order; each block's facts are ranked
(cited-by-required > cited-by-nice-to-have > tag/JD overlap > neither) and
capped. Projects: same ranking, top few. Skills: facts.yaml groups/items
only, reordered (JD-mentioned items first within each group) -- never added
to or removed from. Education/certifications: every id, facts.yaml order.
"""
from __future__ import annotations

import re

HEADLINES = ["Machine Learning and AI Engineer", "Machine Learning Engineer",
             "AI Engineer", "Data Scientist"]

DEFAULT_EXPERIENCE_CAP = 5
DEFAULT_PROJECT_CAP = 3

_AI_RE = re.compile(r"\bai\b|artificial intelligence", re.I)
_ML_RE = re.compile(r"machine learning|\bml\b", re.I)
_DS_RE = re.compile(r"data scientist|data science", re.I)
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9+.#-]{1,}")


def select_headline(title: str) -> str:
    t = title or ""
    has_ml = bool(_ML_RE.search(t))
    has_ai = bool(_AI_RE.search(t))
    has_ds = bool(_DS_RE.search(t))
    if has_ml and has_ai:
        return "Machine Learning and AI Engineer"
    if has_ml:
        return "Machine Learning Engineer"
    if has_ai:
        return "AI Engineer"
    if has_ds:
        return "Data Scientist"
    return "Machine Learning and AI Engineer"


def _requirement_fact_sets(requirements: list[dict]) -> tuple[set[str], set[str]]:
    required_ids: set[str] = set()
    nice_ids: set[str] = set()
    for req in requirements or []:
        ids = req.get("fact_ids") or []
        if req.get("required"):
            required_ids.update(ids)
        else:
            nice_ids.update(ids)
    return required_ids, nice_ids - required_ids


def _jd_tokens(title: str, description: str) -> set[str]:
    return set(_WORD_RE.findall(f"{title} {description}".lower()))


def _fact_tier(fact: dict, required_ids: set[str], nice_ids: set[str], jd_tokens: set[str]) -> int:
    fid = fact["id"]
    if fid in required_ids:
        return 0
    if fid in nice_ids:
        return 1
    tags = [t.lower() for t in (fact.get("tags") or [])]
    if any(tag in jd_tokens for tag in tags):
        return 2
    return 3


def _rank_block(block: dict, required_ids: set[str], nice_ids: set[str], jd_tokens: set[str],
                 cap: int) -> tuple[list[str], list[tuple[int, str]]]:
    """Return (selected fact_ids in facts.yaml order, [(tier, fact_id), ...]
    for every selected fact, worst tier first -- used to build the global
    drop_order)."""
    facts = block.get("facts") or []
    scored = [(_fact_tier(f, required_ids, nice_ids, jd_tokens), idx, f["id"]) for idx, f in enumerate(facts)]
    scored.sort(key=lambda x: (x[0], x[1]))
    chosen = scored[:cap]
    ordered_ids = [fid for _, _, fid in sorted(chosen, key=lambda x: x[1])]
    worst_first = sorted(chosen, key=lambda x: (-x[0], -x[1]))
    return ordered_ids, [(tier, fid) for tier, _, fid in worst_first]


def _reorder_skills(bank, jd_text_lower: str) -> dict:
    skills: dict[str, list[str]] = {}
    for group, items in bank.skills.items():
        mentioned = [it for it in items if it.lower() in jd_text_lower]
        rest = [it for it in items if it not in mentioned]
        skills[group] = mentioned + rest
    return skills


def build_selection(bank, job_id: str, title: str, description: str, requirements: list[dict],
                     experience_cap: int = DEFAULT_EXPERIENCE_CAP,
                     project_cap: int = DEFAULT_PROJECT_CAP) -> tuple[dict, list[str]]:
    """Returns (selection_dict_without_summary, drop_order).

    drop_order lists every selected experience/project fact_id, weakest
    (worst tier, then later block, then later bullet) first -- the order
    stages/tailor.py removes bullets in when the rendered CV runs over the
    page limit. It never includes a fact that isn't in the selection."""
    required_ids, nice_ids = _requirement_fact_sets(requirements)
    jd_tokens = _jd_tokens(title, description)
    jd_text_lower = f"{title} {description}".lower()

    experience = []
    drop_entries: list[tuple[int, int, int, str]] = []  # (tier, block_rank, -bullet_rank, fact_id)
    for block_rank, block in enumerate(bank.experience):
        ids, worst_first = _rank_block(block, required_ids, nice_ids, jd_tokens, experience_cap)
        if not ids:
            continue
        experience.append({"id": block["id"], "fact_ids": ids})
        for bullet_rank, (tier, fid) in enumerate(worst_first):
            drop_entries.append((tier, block_rank, bullet_rank, fid))

    projects = []
    proj_offset = len(bank.experience)
    for block_rank, block in enumerate(bank.projects):
        ids, worst_first = _rank_block(block, required_ids, nice_ids, jd_tokens, project_cap)
        if not ids:
            continue
        projects.append({"id": block["id"], "fact_ids": ids})
        for bullet_rank, (tier, fid) in enumerate(worst_first):
            drop_entries.append((tier, proj_offset + block_rank, bullet_rank, fid))

    # Global drop order: worst tier first; within a tier, later/weaker blocks
    # before earlier ones, and later bullets within a block before earlier
    # ones (facts.yaml order already favours the strongest bullet per block
    # first).
    drop_entries.sort(key=lambda e: (-e[0], -e[1], -e[2]))
    drop_order = [fid for *_, fid in drop_entries]

    selection = {
        "job_id": job_id,
        "headline": select_headline(title),
        "skills": _reorder_skills(bank, jd_text_lower),
        "experience": experience,
        "projects": projects,
        "education_ids": [e["id"] for e in bank.education],
        "certification_ids": [c["id"] for c in bank.certifications],
    }
    return selection, drop_order
