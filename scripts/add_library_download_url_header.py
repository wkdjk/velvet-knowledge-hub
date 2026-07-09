# Run as: PYTHONPATH=. python scripts/add_library_download_url_header.py
#
# add_library_download_url_header.py — one-time migration adding the
# 'download_url' column header to the already-live library_curation tab
# (D1 download-link redesign, 2026-07-10).
#
# Writes 'download_url' to cell H1 of library_curation (col index 8, after
# the existing 7 headers: drive_file_id..summary). Safe to run repeatedly —
# checks H1 first; exits 0 if already correct. Does not touch A1:G1 or any
# existing data rows. Mirrors scripts/add_dedup_columns_header.py exactly.
#
# L-1: PYTHONPATH=. required.
# L-2: .env at repo root.

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.sheets_auth import _load_config, connect_sheets, resolve_sheet_id  # noqa: E402

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"
TARGET_TAB = "library_curation"
TARGET_CELL = "H1"
EXPECTED_HEADER = "download_url"


def main() -> None:
    config = _load_config(CONFIG_PATH)
    sheet_id = resolve_sheet_id(config)  # type: ignore[arg-type]
    spreadsheet = connect_sheets(sheet_id)
    ws = spreadsheet.worksheet(TARGET_TAB)

    current = ws.get(TARGET_CELL)
    current_value = current[0][0] if current and current[0] else ""

    if current_value == EXPECTED_HEADER:
        print(f"  {TARGET_TAB}!{TARGET_CELL} already contains '{EXPECTED_HEADER}' — nothing to do.")
        sys.exit(0)

    if current_value:
        print(f"  WARNING: {TARGET_TAB}!{TARGET_CELL} contains '{current_value}', not empty — overwriting with '{EXPECTED_HEADER}'.")

    ws.update([[EXPECTED_HEADER]], range_name=TARGET_CELL)
    print(f"  Written '{EXPECTED_HEADER}' to {TARGET_TAB}!{TARGET_CELL}.")
    print("  Existing rows A:G are untouched — new column starts blank for every row.")


if __name__ == "__main__":
    main()
