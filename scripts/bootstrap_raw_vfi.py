# Run as: PYTHONPATH=. python scripts/bootstrap_raw_vfi.py [--dry-run]
#
# bootstrap_raw_vfi.py — one-time seed of the raw_vfi tab from the existing
# VFI_Import_Records master tab.
#
# Original pre-ingest source files for past batches (historical xlsx +
# individual MFDS portal downloads) were not retained — Downloads/ is
# transit-only per fleet rule, and manually-pasted rows (README_Admin's
# documented workflow) never had a source file at all. This script bootstraps
# raw_vfi with the KR-facing fields already present in master, so the raw
# layer exists going forward; scripts/ingest_vfi_records.py appends new rows
# to raw_vfi from here on (both --historical and --mfds modes).
#
# Idempotent: no-ops if raw_vfi already has data rows.
#
# Security: no credentials in this file. All secrets from environment only.

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.schema import RAW_VFI_HEADERS  # noqa: E402
from scripts.sheets_auth import connect_sheets, resolve_sheet_id  # noqa: E402

SOURCE_TAB = "VFI_Import_Records"
TARGET_TAB = "raw_vfi"

_BATCH_SIZE = 200


def build_raw_rows(records: list[dict], bootstrapped_at: str) -> list[list]:
    """Project master rows onto RAW_VFI_HEADERS order."""
    rows = []
    for row in records:
        rows.append([
            row.get("date", ""),
            row.get("importer_ko", ""),
            row.get("product_name", ""),
            row.get("product_type_ko", ""),
            row.get("country_origin_ko", ""),
            row.get("country_export_ko", ""),
            row.get("expiry_date", ""),
            row.get("notes", ""),
            bootstrapped_at,
        ])
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Bootstrap raw_vfi from VFI_Import_Records.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    sheet_id = resolve_sheet_id()
    spreadsheet = connect_sheets(sheet_id)

    source_ws = spreadsheet.worksheet(SOURCE_TAB)
    records = source_ws.get_all_records()
    print(f"  {SOURCE_TAB}: {len(records)} rows read")

    target_ws = spreadsheet.worksheet(TARGET_TAB)
    existing = target_ws.get_all_values()
    if len(existing) > 1:
        print(f"  {TARGET_TAB} already has {len(existing) - 1} data rows — skipping (idempotent).")
        return

    bootstrapped_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") + " (bootstrap)"
    raw_rows = build_raw_rows(records, bootstrapped_at)

    if args.dry_run:
        print(f"[DRY RUN] {len(raw_rows)} rows would be written. Sample (first 2):")
        for row in raw_rows[:2]:
            print(f"    {row}")
        return

    for start in range(0, len(raw_rows), _BATCH_SIZE):
        batch = raw_rows[start:start + _BATCH_SIZE]
        target_ws.append_rows(batch, value_input_option="USER_ENTERED")
        if start + _BATCH_SIZE < len(raw_rows):
            time.sleep(1.1)  # L-4: batch sleep between Sheets writes

    print(f"  {TARGET_TAB}: {len(raw_rows)} rows bootstrapped from master.")


if __name__ == "__main__":
    main()
