# Run as: PYTHONPATH=. python scripts/generate_review_view.py
#
# generate_review_view.py — populate the review_view tab (directive §4.3).
#
# "Do not make the master tab prettier — make a separate generated
# review_view tab: most recent 50 records, KR and EN names side by side,
# missing fields highlighted. Humans read this; scripts read master."
#
# Overwrites review_view's data rows on every run (it is a generated view,
# not a source of truth — the header row and target tab are created by
# scripts/setup_trust_pipeline_tabs.py, not here).
#
# Security: no credentials in this file. All secrets from environment only.

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.sheets_auth import connect_sheets, resolve_sheet_id  # noqa: E402

SOURCE_TAB = "VFI_Import_Records"
TARGET_TAB = "review_view"
_RECENT_N = 50


def build_review_rows(records: list[dict]) -> list[list]:
    """
    Return the most recent _RECENT_N rows (by date desc), KR/EN side by
    side, with a `flags` column naming any missing field.
    """
    sortable = sorted(records, key=lambda r: str(r.get("date", "")), reverse=True)
    recent = sortable[:_RECENT_N]

    rows: list[list] = []
    for r in recent:
        flags = []
        if not str(r.get("importer_en", "")).strip():
            flags.append("importer_en missing")
        if not str(r.get("product_en", "")).strip():
            flags.append("product_en missing")
        if not str(r.get("country_origin_en", "")).strip():
            flags.append("country_origin_en missing")
        rows.append([
            r.get("date", ""),
            r.get("importer_ko", ""),
            r.get("importer_en", ""),
            r.get("product_name", ""),
            r.get("product_en", ""),
            r.get("country_origin_ko", ""),
            r.get("country_origin_en", ""),
            "; ".join(flags) if flags else "",
        ])
    return rows


def main() -> None:
    sheet_id = resolve_sheet_id()
    spreadsheet = connect_sheets(sheet_id)

    source_ws = spreadsheet.worksheet(SOURCE_TAB)
    records = source_ws.get_all_records()
    print(f"  {SOURCE_TAB}: {len(records)} rows read")

    review_rows = build_review_rows(records)

    target_ws = spreadsheet.worksheet(TARGET_TAB)
    header = target_ws.row_values(1)

    # Clear existing data rows (keep header), then write the fresh snapshot —
    # this tab is fully regenerated every run, never hand-edited.
    existing_row_count = len(target_ws.get_all_values())
    if existing_row_count > 1:
        target_ws.batch_clear([f"A2:Z{existing_row_count}"])

    if review_rows:
        target_ws.append_rows(review_rows, value_input_option="USER_ENTERED")

    flagged = sum(1 for r in review_rows if r[-1])
    print(f"  {TARGET_TAB}: {len(review_rows)} rows written ({flagged} with missing-field flags)")


if __name__ == "__main__":
    main()
