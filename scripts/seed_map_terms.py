# Run as: PYTHONPATH=. python scripts/seed_map_terms.py [--dry-run]
#
# seed_map_terms.py — one-time seed of the map_countries and map_types tabs.
#
# Same method as seed_map_companies.py (C-15), applied to country_origin_en/
# country_export_en/product_type_en (C-16, directive §4 follow-up, 2026-07-05):
# these fields are a small bounded set (a few dozen countries, ~4-5 product
# types) already correctly filled for the 576 historical rows via the
# retired manual VLOOKUP — so the mapping tables are seeded once from those
# rows' KO->EN pairs rather than requiring the Commander to hand-type them.
#
# map_countries is seeded from BOTH country_origin_ko/en and
# country_export_ko/en pairs (same set of country names, one tab).
# map_types is seeded from product_type_ko/en pairs.
#
# Idempotent: no-ops per tab if that tab already has data rows.
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

# (ko_field, en_field) pairs contributing to each mapping tab.
COUNTRY_FIELD_PAIRS = [
    ("country_origin_ko", "country_origin_en"),
    ("country_export_ko", "country_export_en"),
]
TYPE_FIELD_PAIRS = [
    ("product_type_ko", "product_type_en"),
]


def build_seed_rows(records: list[dict], field_pairs: list[tuple[str, str]]) -> list[list]:
    """
    Group KO->EN pairs (across all given field_pairs) by normalised key, pick
    the most common non-empty EN value per key. Returns rows in
    (source_name_kr, match_key, canonical_name_en, notes) order — same
    majority-vote logic as seed_map_companies.build_seed_rows, generalised
    over multiple field pairs so map_countries can merge origin + export
    columns into one mapping.

    Keys with zero non-empty EN anywhere are skipped — they surface via
    needs_review/backfill instead of a fabricated guess.
    """
    by_key: dict[str, Counter] = {}
    raw_by_key: dict[str, str] = {}

    for row in records:
        for ko_field, en_field in field_pairs:
            ko = str(row.get(ko_field, "")).strip()
            if not ko:
                continue
            key = normalise_company_key(ko)
            if not key:
                continue
            raw_by_key.setdefault(key, ko)
            en = str(row.get(en_field, "")).strip()
            if en:
                by_key.setdefault(key, Counter())[en] += 1

    seed_rows: list[list] = []
    for key, counter in sorted(by_key.items()):
        canonical_en = counter.most_common(1)[0][0]
        seed_rows.append([raw_by_key[key], key, canonical_en, ""])

    return seed_rows


def _seed_tab(spreadsheet, tab_name: str, records: list[dict],
              field_pairs: list[tuple[str, str]], dry_run: bool) -> None:
    seed_rows = build_seed_rows(records, field_pairs)
    print(f"  {tab_name}: {len(seed_rows)} distinct mapped keys found")

    target_ws = spreadsheet.worksheet(tab_name)
    existing = target_ws.get_all_values()
    if len(existing) > 1:
        print(f"  {tab_name} already has {len(existing) - 1} data rows — skipping (idempotent).")
        return

    if dry_run:
        print(f"  [DRY RUN] {tab_name} sample rows (first 5):")
        for row in seed_rows[:5]:
            print(f"    {row}")
        return

    target_ws.append_rows(seed_rows, value_input_option="USER_ENTERED")
    print(f"  {tab_name}: {len(seed_rows)} seed rows written.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed map_countries/map_types from VFI_Import_Records.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    sheet_id = resolve_sheet_id()
    spreadsheet = connect_sheets(sheet_id)

    source_ws = spreadsheet.worksheet(SOURCE_TAB)
    records = source_ws.get_all_records()
    print(f"  {SOURCE_TAB}: {len(records)} rows read")

    _seed_tab(spreadsheet, "map_countries", records, COUNTRY_FIELD_PAIRS, args.dry_run)
    _seed_tab(spreadsheet, "map_types", records, TYPE_FIELD_PAIRS, args.dry_run)


if __name__ == "__main__":
    main()
