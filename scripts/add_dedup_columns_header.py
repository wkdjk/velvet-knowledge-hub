# Run as: PYTHONPATH=. python scripts/add_dedup_columns_header.py
#
# add_dedup_columns_header.py — C-13 Task 1 one-time sheet migration.
#
# Writes 'duplicate_of_article_id', 'dedup_judged_at', 'manual_override' to
# cells M1:O1 of KVN_Articles (col indices 12-14, 1-based cols 13-15).
# Safe to run repeatedly — checks M1:O1 first; exits 0 if already correct.
# Mirrors scripts/add_english_title_header.py (C-8 P0b) exactly.
#
# L-1: PYTHONPATH=. required.
# L-2: .env at repo root.

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.sheets_auth import _load_config, connect_sheets, resolve_sheet_id  # noqa: E402

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"
TARGET_TAB = "KVN_Articles"
TARGET_RANGE = "M1:O1"
EXPECTED_HEADERS = ["duplicate_of_article_id", "dedup_judged_at", "manual_override"]


def main() -> None:
    config = _load_config(CONFIG_PATH)
    sheet_id = resolve_sheet_id(config)  # type: ignore[arg-type]
    spreadsheet = connect_sheets(sheet_id)
    ws = spreadsheet.worksheet(TARGET_TAB)

    current = ws.get(TARGET_RANGE)
    current_row = current[0] if current else []

    if current_row == EXPECTED_HEADERS:
        print(f"  {TARGET_RANGE} already contains {EXPECTED_HEADERS} — nothing to do.")
        sys.exit(0)

    if current_row and current_row != EXPECTED_HEADERS:
        print(f"  WARNING: {TARGET_RANGE} contains {current_row}, not empty — overwriting with {EXPECTED_HEADERS}.")

    ws.update([EXPECTED_HEADERS], range_name=TARGET_RANGE)
    print(f"  Written {EXPECTED_HEADERS} to {TARGET_TAB}!{TARGET_RANGE}.")
    print("  Sheet is ready for the C-13 Task 1 semantic clustering pass.")


if __name__ == "__main__":
    main()
