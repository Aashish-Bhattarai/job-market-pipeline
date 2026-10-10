"""Probe candidate (company, ats, slug) rows against the live ATS board APIs
and print which ones actually exist, so companies.yaml only ever gains
confirmed boards. A candidate is "live" if the board API returns at least one
posting; UK-ness is checked by filter.py's region rules later (most of these
are London employers), but obviously non-UK boards (0 UK/London/remote hits
and 0 total UK words) are flagged.

Usage: python -m discover.probe_companies
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from discover import sources as srcs

CANDIDATES = [
    # (name, ats, slug) — slug guesses; only live ones get added.
    ("OpenAI", "greenhouse", "openai"),
    ("Hugging Face", "greenhouse", "huggingface"),
    ("Synthesia", "greenhouse", "synthesia"),
    ("Wayve", "greenhouse", "wayve"),
    ("Improbable", "greenhouse", "improbable"),
    ("Cohere", "greenhouse", "cohere"),
    ("Seldon", "greenhouse", "seldon"),
    ("Tractable", "greenhouse", "tractable"),
    ("Paddle", "greenhouse", "paddle"),
    ("Beamery", "greenhouse", "beamery"),
    ("Quantexa", "greenhouse", "quantexa"),
    ("Featurespace", "greenhouse", "featurespace"),
    ("Speechmatics", "greenhouse", "speechmatics"),
    ("PhysicsX", "ashby", "physicsx"),
    ("Stability AI", "ashby", "stability"),
    ("Robin AI", "ashby", "robin-ai"),
    ("PolyAI", "ashby", "polyai"),
    ("Synthesia", "ashby", "synthesia"),
    ("Tractable", "ashby", "tractable"),
    ("Wand", "ashby", "wand"),
    ("Darktrace", "workable", "darktrace"),
    ("Cleo", "workable", "cleo"),
    ("Octopus Energy", "workable", "octopusenergy"),
    ("Wise", "smartrecruiters", "Wise"),
    ("Revolut", "smartrecruiters", "Revolut"),
    ("Monzo", "smartrecruiters", "Monzo"),
]


def probe(name: str, ats: str, slug: str) -> tuple[bool, int, str]:
    mod = srcs.get(ats)
    if mod is None:
        return False, 0, f"no module for ats {ats}"
    try:
        rows = mod.fetch({"name": name, "slug": slug})
        uk_words = sum(1 for r in rows
                       if any(w in (r.get("location") or "").lower()
                              for w in ("london", "uk", "united kingdom", "england")))
        return bool(rows), len(rows), f"{uk_words} UK-hinting rows"
    except Exception as exc:  # noqa: BLE001 — probing, errors are the signal
        return False, 0, f"error: {str(exc)[:80]}"


def main() -> int:
    live = []
    for name, ats, slug in CANDIDATES:
        ok, n, detail = probe(name, ats, slug)
        mark = "LIVE " if ok else "dead "
        print(f"{mark} {ats:16s} {slug:16s} {name:16s} {n:4d} postings  ({detail})")
        if ok:
            live.append({"name": name, "ats": ats, "slug": slug})
        time.sleep(1)
    print("\nLIVE (add to companies.yaml as region: london):")
    print(json.dumps(live, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
