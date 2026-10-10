"""Shared JobTracker.xlsx helpers (owner: BankRecords). Extracted from v1
`record_result.py` so `ingest_attempt.py` and `record_result.py` don't
duplicate the sheet-writing logic.
"""
from __future__ import annotations

import datetime
import subprocess
import sys
from pathlib import Path

import openpyxl
from openpyxl.styles import PatternFill

PIPELINE_ROOT = Path(__file__).resolve().parent.parent
TRACKER_ROOT = PIPELINE_ROOT.parent  # .../Job Application Tracker
DEFAULT_XLSX = TRACKER_ROOT / "JobTracker.xlsx"
BUILD_DASHBOARD = TRACKER_ROOT / "build_dashboard.py"

STATUS_FILL = {
    "Rejected": "FFF8D7DA",
    "Interview": "FFD4EDDA",
    "Pending": "FFFFF3CD",
    "Viewed": "FFFFE8CC",
    "Incomplete": "FFE2E3E5",
}

# Maps a v2 job status (post-transition) to the Tracker's Status column text.
TRACKER_STATUS = {
    "applied": "Pending",
    "queued_manual": "Incomplete",
    "queued_account": "Incomplete",
}


def channel_for(company_ats_or_source: str | None) -> str:
    return f"{company_ats_or_source or 'Direct'} (pipeline)"


def write_tracker_row(xlsx_path: Path, company: str, title: str, channel: str, status: str,
                       note: str, today: datetime.datetime, folder: str, job_id: str) -> int:
    """Append a new Tracker row. `status` must be a key of TRACKER_STATUS's
    values (the Tracker sheet's own Status vocabulary, e.g. 'Pending')."""
    wb = openpyxl.load_workbook(xlsx_path)
    ws = wb["Tracker"]

    if ws.cell(row=2, column=8).value != "Job ID":
        ws.cell(row=2, column=8, value="Job ID")

    last_used_row = 2
    r = 3
    while True:
        c = ws.cell(row=r, column=1).value
        role = ws.cell(row=r, column=2).value
        if c is None and role is None:
            break
        last_used_row = r
        r += 1

    new_row = last_used_row + 1
    notes = f"{note} \u2014 {folder}" if note else folder

    ws.cell(row=new_row, column=1, value=company)
    ws.cell(row=new_row, column=2, value=title)
    ws.cell(row=new_row, column=3, value=channel)
    ws.cell(row=new_row, column=4, value=today)
    ws.cell(row=new_row, column=5, value=status)
    ws.cell(row=new_row, column=6, value=None)
    ws.cell(row=new_row, column=7, value=notes)
    ws.cell(row=new_row, column=8, value=job_id)

    fill = PatternFill(start_color=STATUS_FILL[status], end_color=STATUS_FILL[status], fill_type="solid")
    for col in range(1, 9):
        ws.cell(row=new_row, column=col).fill = fill

    ws.auto_filter.ref = f"A2:H{new_row}"
    wb.save(xlsx_path)
    return new_row


def stamp_existing_row(xlsx_path: Path, existing_row: int, job_id: str) -> None:
    wb = openpyxl.load_workbook(xlsx_path)
    ws = wb["Tracker"]
    if ws.cell(row=2, column=8).value != "Job ID":
        ws.cell(row=2, column=8, value="Job ID")
    ws.cell(row=existing_row, column=8, value=job_id)
    if ws.auto_filter.ref:
        last_row = ws.auto_filter.ref.split(":")[1]
        last_row_num = int("".join(ch for ch in last_row if ch.isdigit()))
        if last_row_num < max(existing_row, 2):
            last_row_num = existing_row
        ws.auto_filter.ref = f"A2:H{last_row_num}"
    wb.save(xlsx_path)


def run_build_dashboard() -> None:
    if BUILD_DASHBOARD.exists():
        subprocess.run([sys.executable, str(BUILD_DASHBOARD)], cwd=str(TRACKER_ROOT), check=True)
