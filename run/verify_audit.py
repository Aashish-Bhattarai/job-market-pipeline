"""Verify the ledger's integrity. Owner: BankRecords.

Usage:
    python pipeline/run/verify_audit.py [--db path] [--store-root path]

--store-root overrides where blobs are looked up (core.PIPELINE_ROOT) --
use it to verify a scratch copy of the ledger without touching the real
pipeline/store/.

Prints 'LEDGER OK (<n> events, <n> blobs)' and exits 0 when there are no
problems, or prints every problem found and exits 1.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ledger import core


def main() -> int:
    ap = argparse.ArgumentParser(description="Verify the ledger's hash chain and content")
    ap.add_argument("--db", default=None)
    ap.add_argument("--store-root", default=None)
    args = ap.parse_args()
    if args.store_root:
        core.PIPELINE_ROOT = Path(args.store_root)
    conn = core.connect(args.db) if args.db else core.connect()

    problems = core.verify(conn)
    if problems:
        print(f"LEDGER PROBLEMS FOUND ({len(problems)}):")
        for p in problems:
            print(f"  - {p}")
        return 1

    n_events = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    n_blobs = conn.execute("SELECT COUNT(*) AS n FROM blobs").fetchone()["n"]
    print(f"LEDGER OK ({n_events} events, {n_blobs} blobs)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
