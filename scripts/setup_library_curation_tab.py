# Run as: PYTHONPATH=. python scripts/setup_library_curation_tab.py
#
# setup_library_curation_tab.py — one-time (idempotent) creation of the
# library_curation tab (D1 full build, 2026-07-10). See
# scripts/ingest_library.py's sync_curation_tab() for the read/write logic
# that uses this tab.
#
# Reuses setup_sheets.py's create_tab() (idempotent: skips a tab that
# already exists) — same pattern as setup_weekly_brief_tab.py /
# setup_trust_pipeline_tabs.py.
#
# Security: no credentials in this file. All secrets from environment only.

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.schema import LIBRARY_CURATION_HEADERS  # noqa: E402
from scripts.setup_sheets import create_tab  # noqa: E402
from scripts.sheets_auth import connect_sheets, resolve_sheet_id  # noqa: E402


def main() -> None:
    sheet_id = resolve_sheet_id()
    print(f"Connecting to sheet: {sheet_id}")
    spreadsheet = connect_sheets(sheet_id)
    print(f"  Connected to: '{spreadsheet.title}'")

    create_tab(spreadsheet, "library_curation", LIBRARY_CURATION_HEADERS, [])

    print("\nlibrary_curation tab ready.")


if __name__ == "__main__":
    main()
