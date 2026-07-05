# Run as: PYTHONPATH=. python scripts/setup_weekly_brief_tab.py
#
# setup_weekly_brief_tab.py — one-time (idempotent) creation of the
# weekly_brief tab (C-14 item 4, 2026-07-05). See scripts/vkh_brief.py for
# the generation/publication-gate logic that reads and writes this tab.
#
# Reuses setup_sheets.py's create_tab() (idempotent: skips a tab that
# already exists) — same pattern as setup_trust_pipeline_tabs.py.
#
# Run scripts/backup_sheet.py first — this writes a new tab to the live
# VKH_Data sheet.
#
# Security: no credentials in this file. All secrets from environment only.

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.schema import WEEKLY_BRIEF_HEADERS  # noqa: E402
from scripts.setup_sheets import create_tab  # noqa: E402
from scripts.sheets_auth import connect_sheets, resolve_sheet_id  # noqa: E402


def main() -> None:
    sheet_id = resolve_sheet_id()
    print(f"Connecting to sheet: {sheet_id}")
    spreadsheet = connect_sheets(sheet_id)
    print(f"  Connected to: '{spreadsheet.title}'")

    create_tab(spreadsheet, "weekly_brief", WEEKLY_BRIEF_HEADERS, [])

    print("\nweekly_brief tab ready.")


if __name__ == "__main__":
    main()
