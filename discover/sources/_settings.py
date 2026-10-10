"""Shared settings loader for site discovery sources.

API keys live in pipeline/config.yaml (versioned in git) under `sources:`,
e.g.:

    sources:
      reed: {api_key: "..."}

Prefer an env var over editing config.yaml: an env var named
`<SOURCE>_API_KEY` (e.g. `REED_API_KEY`) overrides the config value, so a
real key never has to be written into this tracked file (see SECURITY.md).

Sources that need credentials skip with a clear printed reason when their
section is missing or empty — a missing key never crashes the batch.
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config.yaml"


def source_settings(name: str) -> dict:
    try:
        cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except OSError:
        cfg = {}
    settings = dict((cfg.get("sources") or {}).get(name) or {})
    env_key = os.environ.get(f"{name.upper()}_API_KEY")
    if env_key:
        settings["api_key"] = env_key
    return settings


def keys_missing(source: str, required: list[str]) -> list[str]:
    have = source_settings(source)
    return [k for k in required if not have.get(k)]
