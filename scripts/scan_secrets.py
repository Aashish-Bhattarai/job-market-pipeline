#!/usr/bin/env python3
"""Heuristic secret scanner: greps every tracked text file for credential-
shaped strings (API keys, passwords, bearer tokens, private key headers).

Usage:
    python scripts/scan_secrets.py [path ...]   # defaults to the repo root

Not a guarantee -- a regex sweep catches the obvious cases (a real key pasted
into config.yaml, a password left in a fixture) but cannot prove a file is
clean. Review the diff yourself too, especially around config.yaml, .env,
and anything under profile/.

Exit code 0: nothing flagged. Exit code 1: at least one match found.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Directories never worth scanning even if present locally (gitignored anyway).
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".pytest_cache", "data", "store", "out", "work"}

PATTERNS = [
    ("assigned secret-shaped value",
     re.compile(r"""(?i)\b(api[_-]?key|app[_-]?key|secret|password|passwd|token|access[_-]?key)\b\s*[:=]\s*["']?([A-Za-z0-9_\-/+]{16,})["']?""")),
    ("UUID-style key",
     re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")),
    ("bearer token",
     re.compile(r"(?i)\bbearer\s+[A-Za-z0-9_\-\.]{20,}")),
    ("private key header",
     re.compile(r"-----BEGIN (RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("long hex string (32+)",
     re.compile(r"\b[0-9a-f]{32,}\b")),
]

# Values that commonly match the patterns above but are not secrets.
ALLOWLIST_SUBSTRINGS = [
    'api_key: ""', "api_key=", "YOUR_", "REED_API_KEY=", "<key>", "xxxxxxxx",
    "0" * 16, "sha256(", "sha1(",
]


def iter_text_files(paths: list[Path]):
    for base in paths:
        if base.is_file():
            yield base
            continue
        for p in base.rglob("*"):
            if not p.is_file():
                continue
            if any(part in SKIP_DIRS for part in p.parts):
                continue
            if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".pdf", ".docx", ".db", ".ico", ".woff", ".woff2"}:
                continue
            yield p


def scan(paths: list[Path]) -> list[str]:
    findings = []
    for path in iter_text_files(paths):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if any(a in line for a in ALLOWLIST_SUBSTRINGS):
                continue
            for name, pattern in PATTERNS:
                m = pattern.search(line)
                if m:
                    findings.append(f"{path.relative_to(ROOT)}:{lineno}: {name}: {line.strip()[:120]}")
    return findings


def main() -> int:
    args = sys.argv[1:]
    paths = [Path(a).resolve() for a in args] if args else [ROOT]
    findings = scan(paths)
    if not findings:
        print("scan_secrets: no matches -- clean (heuristic only, not a guarantee)")
        return 0
    print(f"scan_secrets: {len(findings)} possible match(es):")
    for f in findings:
        print(f"  {f}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
