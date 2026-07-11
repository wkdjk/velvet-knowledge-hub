# Run as: PYTHONPATH=. python scripts/setup_articles_curation_tab.py
#
# setup_articles_curation_tab.py — one-time (idempotent) creation of the
# articles_curation tab (D3, revision 2, Commander decision 2026-07-11).
# See scripts/sync_articles_curation.py for the read/write logic that uses
# this tab. Mirrors scripts/setup_library_curation_tab.py exactly.
#
# Security: no credentials in this file. All secrets from environment only.

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.schema import ARTICLES_CURATION_HEADERS  # noqa: E402
from scripts.setup_sheets import create_tab  # noqa: E402
from scripts.sheets_auth import connect_sheets, resolve_sheet_id  # noqa: E402


def main() -> None:
    sheet_id = resolve_sheet_id()
    print(f"Connecting to sheet: {sheet_id}")
    spreadsheet = connect_sheets(sheet_id)
    print(f"  Connected to: '{spreadsheet.title}'")

    create_tab(spreadsheet, "articles_curation", ARTICLES_CURATION_HEADERS, [])

    print("\narticles_curation tab ready.")


if __name__ == "__main__":
    main()
