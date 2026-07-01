# Run as: PYTHONPATH=. python scripts/ingest_mfds_annual.py [--dry-run]
#
# ingest_mfds_annual.py — MFDS annual deer velvet import value ingestion.
#
# Source: 2025 MFDS Food & Drug Statistical Yearbook (식품의약품통계연보)
#         표 6-3-8. 고가 한약재(녹용, 침향, 우황) 수입액 현황 - 연도별, 국가별
#         Unit: thousand USD
#
# Two series written to VTW_Trade_Monthly:
#   mfds_annual         — total annual deer velvet import value (all origins) 2004-2024
#   mfds_annual_country — 2024 import value broken down by origin country
#
# Usage:
#   PYTHONPATH=. python scripts/ingest_mfds_annual.py
#   PYTHONPATH=. python scripts/ingest_mfds_annual.py --dry-run

import argparse
import sys
from pathlib import Path

# L-1: ensure repo root is on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.sheets_auth import connect_sheets, resolve_sheet_id  # noqa: E402
from scripts.ingest_common import build_dedup_key, rows_to_append  # noqa: E402

REPO_ROOT = Path(__file__).parent.parent
TARGET_TAB = "VTW_Trade_Monthly"


def load_existing_keys(worksheet) -> tuple[set, int]:
    """
    Read all rows from the worksheet once and return a set of dedup keys.

    L-4: get_all_records() is called exactly once — never inside a loop.
    Not in ingest_common.py — every ingest script defines its own thin wrapper
    around build_dedup_key() since it needs a live worksheet handle.
    """
    existing_rows = worksheet.get_all_records()
    return {build_dedup_key(r) for r in existing_rows}, len(existing_rows)

# ---------------------------------------------------------------------------
# Data — transcribed from 표 6-3-8, 2025 MFDS Food & Drug Statistical Yearbook
# ---------------------------------------------------------------------------

ANNUAL_TOTALS = [
    ("2004-01", 18751), ("2005-01", 17531), ("2006-01", 17806),
    ("2007-01", 21476), ("2008-01", 14962), ("2009-01", 14146),
    ("2010-01", 16786), ("2011-01", 17516), ("2012-01", 18044),
    ("2013-01", 19835), ("2014-01", 26377), ("2015-01", 31094),
    ("2016-01", 29001), ("2017-01", 28944), ("2018-01", 30078),
    ("2019-01", 32244), ("2020-01", 31184), ("2021-01", 34242),
    ("2022-01", 39364), ("2023-01", 24178), ("2024-01", 28026),
]

COUNTRY_2024 = [
    ("New Zealand", 18515),
    ("Russia",       9442),
    ("Kazakhstan",     69),
]

SOURCE_NOTE = "2025 MFDS Statistical Yearbook 표 6-3-8"


def build_rows() -> list[dict]:
    rows = []
    for date, value in ANNUAL_TOTALS:
        rows.append({
            "date":     date,
            "series":   "mfds_annual",
            "hs_code":  "0507.90",
            "hs_label": "Cervi Parvum Cornu (녹용)",
            "value":    value,
            "unit":     "USD_thousands",
            "country":  "all origins",
            "notes":    SOURCE_NOTE,
        })
    for country, value in COUNTRY_2024:
        rows.append({
            "date":     "2024-01",
            "series":   "mfds_annual_country",
            "hs_code":  "0507.90",
            "hs_label": "Cervi Parvum Cornu (녹용)",
            "value":    value,
            "unit":     "USD_thousands",
            "country":  country,
            "notes":    SOURCE_NOTE + " — 2024 country breakdown",
        })
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    print("ingest_mfds_annual.py — MFDS annual deer velvet import value")
    print(f"  dry-run: {args.dry_run}")

    sheet_id = resolve_sheet_id()
    sheet = connect_sheets(sheet_id)
    ws = sheet.worksheet(TARGET_TAB)

    headers = ws.row_values(1)
    existing_keys, existing_count = load_existing_keys(ws)
    print(f"  existing rows in tab: {existing_count}")

    new_rows = build_rows()
    to_write, skipped = rows_to_append(new_rows, existing_keys, headers)

    print(f"  rows to write: {len(to_write)} | skipped (already exist): {skipped}")

    if args.dry_run:
        print("[DRY RUN] — no Sheets write.")
        if to_write:
            print("  Sample (first 3):")
            for r in to_write[:3]:
                print(f"    {r}")
        print(f"rows_parsed: {len(new_rows)} | rows_written: 0 | rows_skipped: {skipped}")
        return

    if to_write:
        ws.append_rows(to_write, value_input_option="USER_ENTERED")

    print(f"rows_parsed: {len(new_rows)} | rows_written: {len(to_write)} | rows_skipped: {skipped}")


if __name__ == "__main__":
    main()
