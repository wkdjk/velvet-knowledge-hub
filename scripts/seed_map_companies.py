# Run as: PYTHONPATH=. python scripts/seed_map_companies.py [--dry-run]
#
# seed_map_companies.py — one-time seed of the map_companies tab.
#
# Directive §4.4 step 1: "Create map_companies seeded from the old manual
# VLOOKUP file (recover from Drive archive)." The Drive archive was not
# reachable; per the dispatch's own fallback ("판단은 TechQ에게 맡김"), this
# reconstructs the same information from the live VFI_Import_Records tab
# itself — 582 of 591 rows already carry a hand/VLOOKUP-populated importer_en
# next to importer_ko, which IS the retired VLOOKUP file's content, just
# stored row-by-row instead of as a separate lookup table.
#
# Method: group all (importer_ko, importer_en) pairs by normalise_company_key
# (ingest_common), pick the most frequent non-empty importer_en per key as
# canonical_name_en. Keys with zero non-empty EN anywhere (genuinely new
# companies) are skipped here — they surface via needs_review instead
# (scripts/backfill_company_mapping.py).
#
# Idempotent: no-ops if map_companies already has data rows.
#
# Security: no credentials in this file. All secrets from environment only.

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.ingest_common import normalise_company_key  # noqa: E402
from scripts.sheets_auth import connect_sheets, resolve_sheet_id  # noqa: E402

SOURCE_TAB = "VFI_Import_Records"
TARGET_TAB = "map_companies"


def build_seed_rows(records: list[dict]) -> list[list]:
    """
    Group VFI_Import_Records rows by normalised company key, pick the most
    common non-empty importer_en per key. Returns rows in MAP_COMPANIES_HEADERS
    order: source_name_kr, match_key, canonical_name_en, public_display_name,
    country, notes.
    """
    by_key: dict[str, Counter] = {}
    raw_by_key: dict[str, str] = {}

    for row in records:
        ko = str(row.get("importer_ko", "")).strip()
        en = str(row.get("importer_en", "")).strip()
        if not ko:
            continue
        key = normalise_company_key(ko)
        if not key:
            continue
        raw_by_key.setdefault(key, ko)  # first-seen raw form as source_name_kr
        if en:
            by_key.setdefault(key, Counter())[en] += 1

    seed_rows: list[list] = []
    for key, counter in sorted(by_key.items()):
        canonical_en = counter.most_common(1)[0][0]
        seed_rows.append([raw_by_key[key], key, canonical_en, canonical_en, "KR", ""])

    return seed_rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed map_companies from VFI_Import_Records.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    sheet_id = resolve_sheet_id()
    spreadsheet = connect_sheets(sheet_id)

    source_ws = spreadsheet.worksheet(SOURCE_TAB)
    records = source_ws.get_all_records()
    print(f"  {SOURCE_TAB}: {len(records)} rows read")

    seed_rows = build_seed_rows(records)
    print(f"  {len(seed_rows)} distinct mapped company keys found")

    target_ws = spreadsheet.worksheet(TARGET_TAB)
    existing = target_ws.get_all_values()
    if len(existing) > 1:
        print(f"  {TARGET_TAB} already has {len(existing) - 1} data rows — skipping (idempotent).")
        return

    if args.dry_run:
        print("[DRY RUN] Sample rows (first 5):")
        for row in seed_rows[:5]:
            print(f"    {row}")
        return

    target_ws.append_rows(seed_rows, value_input_option="USER_ENTERED")
    print(f"  {TARGET_TAB}: {len(seed_rows)} seed rows written.")


if __name__ == "__main__":
    main()
