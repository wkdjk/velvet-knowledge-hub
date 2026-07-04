# Run as: PYTHONPATH=. python scripts/setup_trust_pipeline_tabs.py
#
# setup_trust_pipeline_tabs.py — one-time (idempotent) creation of the four
# trust-pipeline tabs for VFI_Import_Records (directive §4, C-15 2026-07-05):
#   raw_vfi        — append-only KR-language raw layer
#   map_companies  — human-edited KR->EN company mapping (replaces old VLOOKUP)
#   needs_review   — exceptions gate output, shared by all future ingest scripts
#   review_view    — generated human-readable snapshot (§4.3), scripts read
#                    master (VFI_Import_Records) directly; humans read this tab
#
# Reuses setup_sheets.py's create_tab() (idempotent: skips a tab that already
# exists) rather than re-implementing tab creation — ponytail rung 2.
#
# Security: no credentials in this file. All secrets from environment only.

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.schema import (  # noqa: E402
    MAP_COMPANIES_HEADERS,
    NEEDS_REVIEW_HEADERS,
    RAW_VFI_HEADERS,
)
from scripts.setup_sheets import create_tab  # noqa: E402
from scripts.sheets_auth import connect_sheets, resolve_sheet_id  # noqa: E402

REVIEW_VIEW_HEADERS = [
    "date", "importer_ko", "importer_en", "product_name", "product_en",
    "country_origin_ko", "country_origin_en", "flags",
]

TABS = [
    ("raw_vfi", RAW_VFI_HEADERS, []),
    ("map_companies", MAP_COMPANIES_HEADERS, []),
    ("needs_review", NEEDS_REVIEW_HEADERS, []),
    ("review_view", REVIEW_VIEW_HEADERS, []),
]


def main() -> None:
    sheet_id = resolve_sheet_id()
    print(f"Connecting to sheet: {sheet_id}")
    spreadsheet = connect_sheets(sheet_id)
    print(f"  Connected to: '{spreadsheet.title}'")

    for tab_name, headers, seed_rows in TABS:
        create_tab(spreadsheet, tab_name, headers, seed_rows)

    print("\nTrust-pipeline tabs ready: raw_vfi, map_companies, needs_review, review_view.")


if __name__ == "__main__":
    main()
