"""Shared helpers for pipeline/render/*.py.

Loads profile/facts.yaml (schema per CONTRACTS.md #1) and selection/cover
JSON (schema per CONTRACTS.md #3), and builds lookup indexes used by the
renderers and the validator. Nothing here invents content: every string
that ends up on a CV or cover letter must come straight out of facts.yaml.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

import yaml

DEFAULT_FACTS_PATH = os.path.join(
    os.path.dirname(__file__), "..", "profile", "facts.yaml"
)
EXAMPLE_FACTS_PATH = os.path.join(
    os.path.dirname(__file__), "..", "profile", "facts.example.yaml"
)


def filename_stem(name: str) -> str:
    """Turn a contact name into a safe filename stem ('Demo Candidate' -> 'Demo_Candidate'),
    falling back to 'Candidate' when facts.yaml has no name yet."""
    return re.sub(r"[^A-Za-z0-9]+", "_", (name or "").strip()).strip("_") or "Candidate"


def load_yaml(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


class FactsBank:
    """Indexed view over facts.yaml."""

    def __init__(self, data: dict):
        self.data = data
        self.contact: dict = data.get("contact", {}) or {}
        self.education: list[dict] = data.get("education", []) or []
        self.certifications: list[dict] = data.get("certifications", []) or []
        self.skills: dict[str, list[str]] = data.get("skills", {}) or {}
        self.experience: list[dict] = data.get("experience", []) or []
        self.projects: list[dict] = data.get("projects", []) or []

        self.education_by_id = {e["id"]: e for e in self.education}
        self.certification_by_id = {c["id"]: c for c in self.certifications}
        self.experience_by_id = {e["id"]: e for e in self.experience}
        self.project_by_id = {p["id"]: p for p in self.projects}

        # fact_id -> {"text", "tags", "source", "block_id", "block_type", "block": <entry>}
        self.fact_by_id: dict[str, dict] = {}
        for block_type, blocks in (("experience", self.experience), ("project", self.projects)):
            for block in blocks:
                for fact in block.get("facts", []) or []:
                    fid = fact["id"]
                    self.fact_by_id[fid] = {
                        "text": fact.get("text", ""),
                        "tags": fact.get("tags", []) or [],
                        "source": fact.get("source"),
                        "block_id": block.get("id"),
                        "block_type": block_type,
                        "block": block,
                    }
        # Summary sentences and cover-letter paragraphs may also cite whole
        # education/certification entries directly (e.g. "MSc Data Science,
        # Distinction") -- those aren't nested under experience/project
        # "facts" lists, so they aren't in fact_by_id, but they are still
        # facts.yaml facts. citable_ids() below is the broader set used for
        # summary/cover validation; fact_by_id/known_fact_ids() stays the
        # narrower set used to verify CV bullets are experience/project fact
        # text verbatim.

    def fact_text(self, fact_id: str) -> str | None:
        entry = self.fact_by_id.get(fact_id)
        return entry["text"] if entry else None

    def known_fact_ids(self) -> set[str]:
        return set(self.fact_by_id.keys())

    def citable_ids(self) -> set[str]:
        """fact ids plus education/certification ids -- everything a summary
        sentence or cover-letter paragraph may legitimately cite."""
        return (
            self.known_fact_ids()
            | set(self.education_by_id.keys())
            | set(self.certification_by_id.keys())
        )


def load_facts_bank(path: str) -> FactsBank:
    return FactsBank(load_yaml(path))


def resolve_facts_path(cli_value: str | None) -> str:
    """An explicit --facts path wins; otherwise your own profile/facts.yaml (once you've copied
    it from the example and filled in your data); otherwise the synthetic example facts, so a
    fresh clone can run the tests and a first dry run before personalizing."""
    if cli_value:
        return cli_value
    if os.path.exists(DEFAULT_FACTS_PATH):
        return DEFAULT_FACTS_PATH
    return EXAMPLE_FACTS_PATH


NBSP = "\u00a0"


def contact_line(contact: dict, include_name: bool = False) -> str:
    parts = [
        contact.get("name", "") if include_name else "",
        contact.get("email", ""),
        contact.get("phone", ""),
        contact.get("linkedin", ""),
        contact.get("github", ""),
    ]
    return "  |  ".join(p for p in parts if p)


_ACRONYMS = {"ml", "ai", "sql", "bi", "ui", "ux", "api", "llm", "ocr", "nlp"}


def skill_group_label(group: str) -> str:
    if "_" not in group and any(c.isupper() for c in group):
        # Already human-formatted (e.g. "Machine Learning", "LLM and AI") --
        # use verbatim rather than re-titlecasing it.
        return group
    words = group.replace("_", " ").split()
    return " ".join(w.upper() if w.lower() in _ACRONYMS else w.capitalize() for w in words)



def build_cv_content(selection: dict, bank: FactsBank) -> dict:
    """Turn a selection.json + FactsBank into a plain content model that both
    the DOCX builder and the HTML (Chrome-print fallback) builder consume, so
    every renderer shows exactly the same, fact-grounded text."""

    def bullets_for(entries: list[dict], index: dict) -> list[dict]:
        out = []
        for entry in entries:
            block = index.get(entry["id"])
            if block is None:
                continue
            texts = [bank.fact_text(fid) for fid in entry.get("fact_ids", []) or []]
            texts = [t for t in texts if t]
            out.append(
                {
                    "org": block.get("org"),
                    "role": block.get("role"),
                    "location": block.get("location"),
                    "dates": block.get("dates"),
                    "bullets": texts,
                }
            )
        return out

    education = [bank.education_by_id[eid] for eid in selection.get("education_ids", []) or [] if eid in bank.education_by_id]
    certifications = [bank.certification_by_id[cid] for cid in selection.get("certification_ids", []) or [] if cid in bank.certification_by_id]

    return {
        "name": bank.contact.get("name", ""),
        "contact_line": contact_line(bank.contact),
        "headline": selection.get("headline", ""),
        "summary": (selection.get("summary") or {}).get("text", ""),
        "skills": selection.get("skills", {}) or {},
        "experience": bullets_for(selection.get("experience", []) or [], bank.experience_by_id),
        "projects": bullets_for(selection.get("projects", []) or [], bank.project_by_id),
        "education": education,
        "certifications": certifications,
        "right_to_work": "Eligible to work in the UK.",
    }


def build_cover_content(cover: dict, bank: FactsBank) -> dict:
    import datetime

    return {
        "name": bank.contact.get("name", ""),
        "contact_line": contact_line(bank.contact),
        "date": datetime.date.today().strftime("%d %B %Y"),
        "company": cover.get("company", ""),
        "role": cover.get("role", ""),
        "paragraphs": [p.get("text", "") for p in cover.get("paragraphs", []) or []],
    }


def _converter_lock(timeout: float = 900.0):
    """Cross-process mutex around MS Word automation (docx2pdf): parallel tailoring workers must
    never drive Word concurrently. A lock older than 3 minutes belongs to a crashed holder."""
    import contextlib
    import os
    import tempfile
    import time
    from pathlib import Path

    @contextlib.contextmanager
    def lock():
        path = Path(tempfile.gettempdir()) / "omp_pipeline_word_convert.lock"
        deadline = time.monotonic() + timeout
        while True:
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                break
            except FileExistsError:
                try:
                    if time.time() - path.stat().st_mtime > 180:
                        path.unlink(missing_ok=True)
                        continue
                except OSError:
                    pass
                if time.monotonic() > deadline:
                    raise TimeoutError("another process held the Word conversion lock for too long")
                time.sleep(0.4)
        try:
            yield
        finally:
            path.unlink(missing_ok=True)

    return lock()


def convert_docx_to_pdf(docx_path: str, pdf_path: str) -> str:
    with _converter_lock():
        return _convert_docx_to_pdf_unlocked(docx_path, pdf_path)


def _convert_docx_to_pdf_unlocked(docx_path: str, pdf_path: str) -> str:
    """Try, in order: MS Word (docx2pdf), LibreOffice (soffice), and report
    which one produced the PDF. Returns the method name used, or raises
    RuntimeError if every converter failed."""
    import os as _os
    import subprocess

    errors = []

    # 1. MS Word via docx2pdf
    try:
        from docx2pdf import convert as _docx2pdf_convert

        _docx2pdf_convert(docx_path, pdf_path)
        if _os.path.exists(pdf_path):
            return "msword-docx2pdf"
    except Exception as exc:  # noqa: BLE001
        errors.append(f"docx2pdf/MS Word failed: {exc}")

    # 2. LibreOffice headless
    soffice_candidates = [
        "soffice",
        "C:/Program Files/LibreOffice/program/soffice.exe",
        "C:/Program Files (x86)/LibreOffice/program/soffice.exe",
    ]
    out_dir = _os.path.dirname(pdf_path) or "."
    for soffice in soffice_candidates:
        try:
            subprocess.run(
                [soffice, "--headless", "--convert-to", "pdf", "--outdir", out_dir, docx_path],
                check=True,
                capture_output=True,
                timeout=120,
            )
            produced = _os.path.join(out_dir, _os.path.splitext(_os.path.basename(docx_path))[0] + ".pdf")
            if _os.path.exists(produced):
                if produced != pdf_path:
                    _os.replace(produced, pdf_path)
                return "libreoffice-soffice"
        except Exception as exc:  # noqa: BLE001
            errors.append(f"soffice ({soffice}) failed: {exc}")

    raise RuntimeError(
        "No DOCX->PDF converter succeeded (tried MS Word/docx2pdf, LibreOffice); "
        "fall back to HTML+Chrome instead. Errors: " + " | ".join(errors)
    )


CHROME_CANDIDATES = [
    "C:/Program Files/Google/Chrome/Application/chrome.exe",
    "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
]


def convert_html_to_pdf_with_chrome(html_path: str, pdf_path: str) -> str:
    import os as _os
    import subprocess

    chrome = next((c for c in CHROME_CANDIDATES if _os.path.exists(c)), None)
    if chrome is None:
        raise RuntimeError("Chrome not found for HTML->PDF fallback")
    subprocess.run(
        [
            chrome,
            "--headless=new",
            "--disable-gpu",
            f"--print-to-pdf={pdf_path}",
            "--no-pdf-header-footer",
            _os.path.abspath(html_path),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    if not _os.path.exists(pdf_path):
        raise RuntimeError("Chrome headless did not produce a PDF")
    return "chrome-headless-html"


def count_pdf_pages(pdf_path: str) -> int:
    from pypdf import PdfReader

    return len(PdfReader(pdf_path).pages)