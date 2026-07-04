# Run as: PYTHONPATH=. python scripts/backfill_company_mapping.py [--dry-run]
#
# backfill_company_mapping.py — patch missing importer_en values in
# VFI_Import_Records via map_companies, log unmapped ones to needs_review.
#
# Directive §4.4 step 3 — backfill the "—"/blank-importer_en rows (9 found
# in the 2026-07-05 trust-pipeline audit).
#
# Root cause (confirmed against the live sheet, 2026-07-05): all 591 rows in
# VFI_Import_Records have an empty `notes` column, meaning none were written
# by ingest_vfi_records.py's --mfds mode (which always stamps notes with
# MFDS-only fields) — every row including the 9 blank-importer_en ones
# arrived via the manual-paste workflow README_Admin documents ("Update
# import records: paste rows below the last existing row"). That path is a
# human pasting into the Sheets UI directly and CANNOT be intercepted by any
# ingest-script gate. This script is therefore the actual mapping-repair
# mechanism for VFI_Import_Records — re-run it any time after a manual paste
# or a map_companies edit; it only touches rows with a currently-blank
# importer_en, so it is always safe to re-run (idempotent on already-filled
# rows).
#
# Of the 7 distinct unmapped companies found: 3 (Eryong Pharm., Bibong Herb,
# KNC Deertrade) already have a canonical EN name elsewhere in the sheet and
# resolve automatically. 4 (마더스초이스/Mothers Choice, (주)엘지생활건강/LG
# H&H, 아로하가든/Aloha Garden, (주)유앤젯인터내셔날/U&Jet International) have
# never been mapped anywhere — this script writes canonical_name_en for them
# from a one-time manual English-name lookup (public company names, not
# secrets) directly into map_companies, then resolves the master rows from
# there. This keeps map_companies (not this script) as the single mapping
# source of truth going forward.
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
MAP_TAB = "map_companies"
NEEDS_REVIEW_TAB = "needs_review"

# One-time manual additions for companies never previously mapped anywhere
# in the sheet (confirmed public company names; no automatic EN source
# exists for these — VKH ships no translation table by design, G-2).
_NEW_COMPANY_NAMES: dict[str, str] = {
    "마더스초이스": "Mothers Choice",
    "(주)엘지생활건강": "LG H&H",
    "아로하가든": "Aloha Garden",
    "(주)유앤젯인터내셔날": "U&Jet International",
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill importer_en in VFI_Import_Records.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    sheet_id = resolve_sheet_id()
    spreadsheet = connect_sheets(sheet_id)

    map_ws = spreadsheet.worksheet(MAP_TAB)
    mapping = load_company_mapping(map_ws)
    print(f"  {MAP_TAB}: {len(mapping)} company keys loaded")

    # Add the never-before-mapped companies to map_companies first, so the
    # single source of truth grows instead of hardcoding a bypass.
    new_map_rows = []
    for ko_name, en_name in _NEW_COMPANY_NAMES.items():
        key = normalise_company_key(ko_name)
        if key not in mapping:
            new_map_rows.append([ko_name, key, en_name, en_name, "KR", "added by backfill_company_mapping.py 2026-07-05"])
            mapping[key] = {"canonical_name_en": en_name}

    master_ws = spreadsheet.worksheet(MASTER_TAB)
    records = master_ws.get_all_records()
    print(f"  {MASTER_TAB}: {len(records)} rows read")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    cell_updates = []  # list of (row_number, col_letter, value) — importer_en column
    needs_review_rows = []
    header = master_ws.row_values(1)
    importer_en_col = header.index("importer_en") + 1  # 1-based gspread column

    resolved_count = 0
    unmatched_count = 0

    for i, row in enumerate(records):
        if str(row.get("importer_en", "")).strip():
            continue  # already filled — never overwrite
        ko = str(row.get("importer_ko", "")).strip()
        if not ko:
            continue
        canonical_en, matched = resolve_company(ko, mapping)
        row_number = i + 2  # +1 header, +1 zero-index
        if matched and canonical_en:
            cell_updates.append((row_number, importer_en_col, canonical_en))
            resolved_count += 1
        else:
            unmatched_count += 1
            needs_review_rows.append([
                MASTER_TAB, str(row_number), "importer_en", ko,
                normalise_company_key(ko), "no map_companies match", now,
            ])

    print(f"  resolved: {resolved_count} | still unmatched: {unmatched_count}")

    if args.dry_run:
        print("[DRY RUN] no writes made.")
        print(f"  new map_companies rows: {len(new_map_rows)}")
        print(f"  importer_en cell updates: {len(cell_updates)}")
        print(f"  needs_review rows: {len(needs_review_rows)}")
        return

    if new_map_rows:
        map_ws.append_rows(new_map_rows, value_input_option="USER_ENTERED")
        print(f"  {MAP_TAB}: {len(new_map_rows)} new company rows added.")

    if cell_updates:
        # L-4: one batch call, not one update_cell() per row.
        cells = [gspread.Cell(row=r, col=c, value=v) for r, c, v in cell_updates]
        master_ws.update_cells(cells, value_input_option="USER_ENTERED")
        print(f"  {MASTER_TAB}: {len(cell_updates)} importer_en cells patched.")

    if needs_review_rows:
        review_ws = spreadsheet.worksheet(NEEDS_REVIEW_TAB)
        review_ws.append_rows(needs_review_rows, value_input_option="USER_ENTERED")
        print(f"  {NEEDS_REVIEW_TAB}: {len(needs_review_rows)} rows flagged.")


if __name__ == "__main__":
    main()
