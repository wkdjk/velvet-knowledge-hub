# Run as: PYTHONPATH=. python scripts/add_english_title_header.py
#
# add_english_title_header.py — C-8 P0b one-time sheet migration.
#
# Writes 'english_title' to cell L1 of KVN_Articles (col index 11, 1-based col 12).
# Safe to run repeatedly — checks L1 first; exits 0 if already correct.
#
# L-1: PYTHONPATH=. required.
# L-2: .env at repo root.

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.sheets_auth import _load_config, connect_sheets, resolve_sheet_id  # noqa: E402

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"
TARGET_TAB = "KVN_Articles"
TARGET_CELL = "L1"
EXPECTED_HEADER = "english_title"


def main() -> None:
    config = _load_config(CONFIG_PATH)
    sheet_id = resolve_sheet_id(config)  # type: ignore[arg-type]
    spreadsheet = connect_sheets(sheet_id)
    ws = spreadsheet.worksheet(TARGET_TAB)

    current = ws.acell(TARGET_CELL).value
    if current == EXPECTED_HEADER:
        print(f"  L1 already contains '{EXPECTED_HEADER}' — nothing to do.")
        sys.exit(0)

    if current and current != EXPECTED_HEADER:
        print(f"  WARNING: L1 contains '{current}', not empty — overwriting with '{EXPECTED_HEADER}'.")

    ws.update(TARGET_CELL, [[EXPECTED_HEADER]])
    print(f"  Written '{EXPECTED_HEADER}' to {TARGET_TAB}!{TARGET_CELL}.")
    print("  Sheet is ready for C-8 re-classify run.")


if __name__ == "__main__":
    main()
