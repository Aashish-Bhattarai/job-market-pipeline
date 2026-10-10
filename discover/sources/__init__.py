"""Registry of discovery source modules, each exposing fetch(config) -> list[dict].

ATS modules (greenhouse, lever, ashby, workable, smartrecruiters) are owned by
DiscoveryCore and always present. Site modules (himalayas, jobsacuk,
gradcracker, hiringcafe, wttj) are written concurrently by SiteSources; they
are imported if present and skipped without error if missing.
"""
from __future__ import annotations

import importlib
import logging

log = logging.getLogger(__name__)

# name -> module import path, all under pipeline.discover.sources
_ATS_MODULES = {
    "greenhouse": "greenhouse",
    "lever": "lever",
    "ashby": "ashby",
    "workable": "workable",
    "smartrecruiters": "smartrecruiters",
    "recruitee": "recruitee",
    "teamtailor": "teamtailor",
}

_SITE_MODULES = {
    "himalayas": "himalayas",
    "jobsacuk": "jobsacuk",
    "gradcracker": "gradcracker",
    "hiringcafe": "hiringcafe",
    "wttj": "wttj",
    "adzuna": "adzuna",        # inert: its API terms forbid the resolve+apply flow
    "reed": "reed",            # Reed.co.uk API — needs a key in config.yaml
    "hnhiring": "hnhiring",    # HN Who-is-hiring via Algolia — keyless
    "workablesearch": "workablesearch",   # jobs.workable.com: every Workable-hosted employer, keyless public API
}


def _load_registry() -> dict:
    registry = {}
    for name, modname in {**_ATS_MODULES, **_SITE_MODULES}.items():
        try:
            mod = importlib.import_module(f".{modname}", __name__)
        except ModuleNotFoundError:
            log.info("source module %s not present yet, skipping", modname)
            continue
        if not hasattr(mod, "fetch"):
            log.warning("source module %s has no fetch(), skipping", modname)
            continue
        registry[name] = mod
    return registry


REGISTRY = _load_registry()


def available() -> list[str]:
    """Names of currently importable sources (ATS + any site modules found)."""
    return sorted(REGISTRY.keys())


def get(name: str):
    """Return the module for `name`, or None if not available."""
    return REGISTRY.get(name)
