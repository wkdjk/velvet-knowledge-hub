"""
backup_sheet.py — Export every tab of the VKH_Data spreadsheet to CSV.

Disaster-recovery gap fix (see Domain_Knowledge/VKH_code_review_addendum_2026-07-03.md
section C3): VKH's entire dataset lives in one Google Sheet with no backup. This
script reads every worksheet with get_all_values() (raw cells, no formula/format
concerns) and writes one CSV per tab to backup/<tab_name>.csv.

Usage (from repo root):
    PYTHONPATH=. python scripts/backup_sheet.py

Run weekly by .github/workflows/backup_sheet.yml, and can be run manually.
"""

import csv
import sys
from pathlib import Path

from scripts.sheets_auth import connect_sheets, resolve_sheet_id

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKUP_DIR = REPO_ROOT / "backup"


def backup_all_tabs() -> dict[str, int]:
    """
    Connect to VKH_Data, export every worksheet to backup/<tab_name>.csv.

    Returns a dict of {tab_name: row_count} for reporting.
    """
    sheet_id = resolve_sheet_id()
    spreadsheet = connect_sheets(sheet_id)

    BACKUP_DIR.mkdir(exist_ok=True)

    row_counts: dict[str, int] = {}
    for worksheet in spreadsheet.worksheets():
        values = worksheet.get_all_values()
        # ponytail: filesystem-safe tab name — VKH tab names are already
        # simple identifiers (no slashes), so this is a no-op guard, not
        # a full sanitiser. Upgrade if a tab name ever contains "/".
        safe_name = worksheet.title.replace("/", "_")
        out_path = BACKUP_DIR / f"{safe_name}.csv"

        with out_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerows(values)

        row_counts[worksheet.title] = len(values)
        print(f"  {worksheet.title}: {len(values)} rows -> {out_path.relative_to(REPO_ROOT)}")

    return row_counts


def main() -> None:
    print("Backing up VKH_Data sheet tabs to CSV...")
    row_counts = backup_all_tabs()
    total = sum(row_counts.values())
    print(f"Done. {len(row_counts)} tabs, {total} total rows.")
    if total == 0:
        print("WARNING: backup captured zero rows across all tabs.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
