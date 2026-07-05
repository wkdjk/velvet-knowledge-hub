# Run as: PYTHONPATH=. python scripts/backfill_term_mapping.py [--dry-run]
#
# backfill_term_mapping.py — patch missing country_origin_en/
# country_export_en/product_type_en values in VFI_Import_Records via
# map_countries/map_types, log unmatched ones to needs_review.
#
# C-16 (2026-07-05) — the live-site "—" dashes bug. Same shape as
# backfill_company_mapping.py (C-15): _parse_mfds() previously hardcoded
# these 3 fields to "" for every row, so every row ingested by --mfds mode
# since the MFDS pipeline went live carries a blank _en value for all 3.
# Historical rows (576, via --historical mode) already have these fields
# filled from the retired manual VLOOKUP and are untouched by this script
# (skip condition: only touches rows with a currently-blank _en value).
#
# Safe to re-run any time (idempotent on already-filled rows) — same
# operational role as backfill_company_mapping.py for manually-pasted rows
# that no ingest-script gate can intercept.
#
# Security: no credentials in this file. All secrets from environment only.

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import gspread

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.ingest_common import (  # noqa: E402
    load_company_mapping,
    normalise_company_key,
    resolve_company,
)
from scripts.sheets_auth import connect_sheets, resolve_sheet_id  # noqa: E402

MASTER_TAB = "VFI_Import_Records"
NEEDS_REVIEW_TAB = "needs_review"

# (ko_field, en_field, mapping_tab) — same shape as ingest_vfi_records.py's
# main() gate, reused here against the already-ingested master rows.
_TERM_GATE_FIELDS = [
    ("country_origin_ko", "country_origin_en", "map_countries"),
    ("country_export_ko", "country_export_en", "map_countries"),
    ("product_type_ko", "product_type_en", "map_types"),
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill country/type _en fields in VFI_Import_Records.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    sheet_id = resolve_sheet_id()
    spreadsheet = connect_sheets(sheet_id)

    mappings: dict = {}
    for _, _, tab_name in _TERM_GATE_FIELDS:
        if tab_name in mappings:
            continue
        map_ws = spreadsheet.worksheet(tab_name)
        mappings[tab_name] = load_company_mapping(map_ws)
        print(f"  {tab_name}: {len(mappings[tab_name])} keys loaded")

    master_ws = spreadsheet.worksheet(MASTER_TAB)
    records = master_ws.get_all_records()
    print(f"  {MASTER_TAB}: {len(records)} rows read")

    header = master_ws.row_values(1)
    col_by_field = {
        en_field: header.index(en_field) + 1  # 1-based gspread column
        for _, en_field, _ in _TERM_GATE_FIELDS
    }

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    cell_updates: list = []  # (row_number, col, value)
    needs_review_rows: list = []
    resolved_count = 0
    unmatched_count = 0

    for i, row in enumerate(records):
        row_number = i + 2  # +1 header, +1 zero-index
        for ko_field, en_field, tab_name in _TERM_GATE_FIELDS:
            if str(row.get(en_field, "")).strip():
                continue  # already filled — never overwrite
            ko = str(row.get(ko_field, "")).strip()
            if not ko:
                continue
            canonical_en, matched = resolve_company(ko, mappings[tab_name])
            if matched and canonical_en:
                cell_updates.append((row_number, col_by_field[en_field], canonical_en))
                resolved_count += 1
            else:
                unmatched_count += 1
                needs_review_rows.append([
                    MASTER_TAB, str(row_number), en_field, ko,
                    normalise_company_key(ko), f"no {tab_name} match", now,
                ])

    print(f"  resolved: {resolved_count} | still unmatched: {unmatched_count}")

    if args.dry_run:
        print("[DRY RUN] no writes made.")
        print(f"  cell updates: {len(cell_updates)}")
        print(f"  needs_review rows: {len(needs_review_rows)}")
        return

    if cell_updates:
        # L-4: one batch call, not one update_cell() per row.
        cells = [gspread.Cell(row=r, col=c, value=v) for r, c, v in cell_updates]
        master_ws.update_cells(cells, value_input_option="USER_ENTERED")
        print(f"  {MASTER_TAB}: {len(cell_updates)} cells patched.")

    if needs_review_rows:
        review_ws = spreadsheet.worksheet(NEEDS_REVIEW_TAB)
        review_ws.append_rows(needs_review_rows, value_input_option="USER_ENTERED")
        print(f"  {NEEDS_REVIEW_TAB}: {len(needs_review_rows)} rows flagged.")


if __name__ == "__main__":
    main()
