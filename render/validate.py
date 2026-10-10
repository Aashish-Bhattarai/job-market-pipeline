"""Validate a selection.json or cover.json against profile/facts.yaml.

Usage:
    python pipeline/render/validate.py <selection.json|cover.json> [--facts <path>]

Exit code 0 and no output on success. Exit code 1 and one reason per line
on failure. Auto-detects selection vs cover by shape ("paragraphs" key
means cover letter).
"""
from __future__ import annotations

import argparse
import re
import sys
from common import FactsBank, load_facts_bank, load_json, resolve_facts_path


def validate_selection(doc: dict, bank: FactsBank) -> list[str]:
    reasons: list[str] = []
    known = bank.known_fact_ids()

    citable = bank.citable_ids()
    summary = doc.get("summary") or {}
    for fid in summary.get("fact_ids", []) or []:
        if fid not in citable:
            reasons.append(f"unknown fact_id '{fid}' referenced in summary.fact_ids")

    for block_key, index in (
        ("experience", bank.experience_by_id),
        ("projects", bank.project_by_id),
    ):
        for entry in doc.get(block_key, []) or []:
            block_id = entry.get("id")
            block = index.get(block_id)
            if block is None:
                reasons.append(f"unknown {block_key[:-1] if block_key=='experience' else 'project'} id '{block_id}'")
                continue
            block_fact_ids = {f["id"] for f in block.get("facts", []) or []}
            for fid in entry.get("fact_ids", []) or []:
                if fid not in known:
                    reasons.append(f"unknown fact_id '{fid}' referenced in {block_key}[{block_id}].fact_ids")
                elif fid not in block_fact_ids:
                    reasons.append(
                        f"fact_id '{fid}' does not belong to {block_key[:-1]} '{block_id}' "
                        f"(cross-referenced fact from a different block)"
                    )
                else:
                    # Bullet text must be the fact text verbatim -- guaranteed by
                    # construction (renderer looks it up by id), but confirm the
                    # fact actually carries non-empty text.
                    if not bank.fact_text(fid):
                        reasons.append(f"fact_id '{fid}' has empty text in facts.yaml")

    for eid in doc.get("education_ids", []) or []:
        if eid not in bank.education_by_id:
            reasons.append(f"unknown education id '{eid}'")

    for cid in doc.get("certification_ids", []) or []:
        if cid not in bank.certification_by_id:
            reasons.append(f"unknown certification id '{cid}'")

    all_bank_skills = {s for group in bank.skills.values() for s in group}
    for group, items in (doc.get("skills") or {}).items():
        if group not in bank.skills:
            reasons.append(f"skill group '{group}' not present in facts.yaml skills")
            continue
        for item in items:
            if item not in bank.skills.get(group, []):
                reasons.append(f"skill '{item}' in group '{group}' is not in facts.yaml skills.{group}")
            if item not in all_bank_skills:
                reasons.append(f"skill '{item}' is not evidenced anywhere in facts.yaml")

    return reasons


CLOSE_BANNED_WORDS = [
    "experience",
    "built",
    "developed",
    "delivered",
    "led",
    "production",
    "skilled",
    "expert",
]
_NOTICE_PERIOD_RE = re.compile(r"\b\d+\s*-?\s*month(s)?\b", re.IGNORECASE)


def validate_cover(doc: dict, bank: FactsBank) -> list[str]:
    reasons: list[str] = []
    citable = bank.citable_ids()
    paragraphs = doc.get("paragraphs", []) or []
    if not paragraphs:
        reasons.append("cover letter has no paragraphs")

    close_indices = [i for i, p in enumerate(paragraphs) if p.get("kind") == "close"]
    if len(close_indices) > 1:
        reasons.append("cover letter has more than one paragraph marked kind 'close'")
    if close_indices and close_indices[-1] != len(paragraphs) - 1:
        reasons.append("paragraph marked kind 'close' must be the final paragraph")

    for i, para in enumerate(paragraphs):
        text = para.get("text", "") or ""
        if para.get("kind") == "close":
            remainder = _NOTICE_PERIOD_RE.sub("", text)
            if any(ch.isdigit() for ch in remainder):
                reasons.append(f"paragraph {i} (close) contains digits outside the notice period")
            lowered = text.lower()
            for word in CLOSE_BANNED_WORDS:
                if word in lowered:
                    reasons.append(f"paragraph {i} (close) uses banned word '{word}'")
            continue
        fact_ids = para.get("fact_ids", []) or []
        if not fact_ids:
            reasons.append(f"paragraph {i} cites no fact_ids")
            continue
        for fid in fact_ids:
            if fid not in citable:
                reasons.append(f"unknown fact_id '{fid}' referenced in paragraph {i}")
    return reasons


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("doc_path")
    parser.add_argument("--facts", default=None)
    args = parser.parse_args()

    bank = load_facts_bank(resolve_facts_path(args.facts))
    doc = load_json(args.doc_path)

    if "paragraphs" in doc:
        reasons = validate_cover(doc, bank)
    else:
        reasons = validate_selection(doc, bank)

    if reasons:
        for r in reasons:
            print(f"FAIL: {r}", file=sys.stderr)
        print(f"{len(reasons)} problem(s) found in {args.doc_path}", file=sys.stderr)
        return 1

    print(f"OK: {args.doc_path} is consistent with {resolve_facts_path(args.facts)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
