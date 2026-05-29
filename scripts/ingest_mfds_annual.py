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
import json
import os
import sys
from pathlib import Path

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

REPO_ROOT = Path(__file__).parent.parent
TARGET_TAB = "VTW_Trade_Monthly"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

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

    load_dotenv(REPO_ROOT / ".env")
    sheet_id = os.environ.get("VKH_SHEET_ID", "").strip()
    sa_json_raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()

    if not sheet_id or not sa_json_raw:
        print("ERROR: VKH_SHEET_ID or GOOGLE_SERVICE_ACCOUNT_JSON not set.", file=sys.stderr)
        sys.exit(1)

    sa_info = json.loads(sa_json_raw)
    creds = Credentials.from_service_account_info(sa_info, scopes=SCOPES)
    gc = gspread.authorize(creds)
    sheet = gc.open_by_key(sheet_id)
    ws = sheet.worksheet(TARGET_TAB)

    headers = ws.row_values(1)
    existing = ws.get_all_records()
    existing_keys = {
        (str(r.get("date", "")), str(r.get("series", "")), str(r.get("hs_code", "")), str(r.get("country", "")))
        for r in existing
    }

    print(f"  existing rows in tab: {len(existing)}")

    new_rows = build_rows()
    to_write = []
    skipped = 0
    for row in new_rows:
        key = (row["date"], row["series"], row["hs_code"], row["country"])
        if key in existing_keys:
            skipped += 1
        else:
            to_write.append([row.get(h, "") for h in headers])

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
