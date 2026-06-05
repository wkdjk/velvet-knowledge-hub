# Run as: PYTHONPATH=. python scripts/curate_articles_c6d.py [--dry-run]
#
# curate_articles_c6d.py — One-off curation for C-6d walkthrough.
#
# Sets include_on_site=FALSE for all KVN_Articles rows where:
#   - published_date >= 2026-03-08 (90-day window cutoff)
#   - include_on_site is truthy
#   - article_id is NOT in the Commander's 4-article keep list
#     (or is a duplicate of a kept article_id)
#
# Rows outside the 90-day window are NOT touched.
#
# L-1: PYTHONPATH=. ensures repo root is importable.
# L-2: .env must be at repo root (/Users/Qs/C/velvet-knowledge-hub/.env).
# L-3: GOOGLE_SERVICE_ACCOUNT_JSON must be single-line JSON in .env.
# L-4: get_all_records() called once; batch_update called once.
#
# Security: no credentials or secrets in this file. All secrets from .env only.

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

# ---------------------------------------------------------------------------
# Path setup — L-1
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REPO_ROOT = Path(__file__).resolve().parent.parent

# Sheets API only — no Drive API required (L-5 workaround).
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

SHEET_ID = "1idbPiaK_Scd8znktn2cPutWP5Lg4azo1XBfXNyt5K2U"
TAB_NAME = "KVN_Articles"
CUTOFF_DATE = date(2026, 3, 8)

# The 4 article IDs the Commander has selected to keep as include_on_site=TRUE.
KEEP_IDS: frozenset[str] = frozenset([
    "c3d99c360a5ee132",
    "57d4e3d54bef75b0",
    "6d3c6f454e334380",
    "68376486080b81d3",
])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_truthy(value) -> bool:
    """Return True for any value that represents a truthy include_on_site."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().upper() in ("TRUE", "YES", "1")
    if isinstance(value, (int, float)):
        return bool(value)
    return False


def _parse_date(raw) -> date | None:
    """Parse a date string YYYY-MM-DD. Returns None if unparseable."""
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw).strip()[:10])
    except ValueError:
        return None


def connect_sheets() -> gspread.Spreadsheet:
    """Authenticate via service account and return the spreadsheet."""
    load_dotenv(REPO_ROOT / ".env")

    sa_json_raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not sa_json_raw:
        print(
            "ERROR: GOOGLE_SERVICE_ACCOUNT_JSON environment variable is not set.\n"
            "  Local dev: add it to .env at repo root (single-line JSON — L-3).\n"
            "  GitHub Actions: add it to repository Secrets.",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        sa_info = json.loads(sa_json_raw)
    except json.JSONDecodeError as exc:
        print(
            f"ERROR: GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON — {exc}",
            file=sys.stderr,
        )
        sys.exit(1)

    creds = Credentials.from_service_account_info(sa_info, scopes=SCOPES)
    gc = gspread.authorize(creds)

    try:
        return gc.open_by_key(SHEET_ID)
    except gspread.exceptions.APIError as exc:
        print(f"ERROR: Could not open sheet {SHEET_ID} — {exc}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------

def main(dry_run: bool = False) -> dict:
    """
    Scan KVN_Articles and build a batch update that sets include_on_site=FALSE
    for all 90-day-window rows not in KEEP_IDS.

    Returns a stats dict: {set_false, kept, outside_window, total_rows}.
    """
    spreadsheet = connect_sheets()
    ws = spreadsheet.worksheet(TAB_NAME)

    # L-4: read entire worksheet once.
    print(f"Reading {TAB_NAME}…")
    all_values = ws.get_all_values()  # list of lists (including header)

    if not all_values:
        print("ERROR: sheet is empty.", file=sys.stderr)
        sys.exit(1)

    headers = all_values[0]
    print(f"  Headers: {headers}")

    # Locate required columns by name (L-13: never hardcode column indices).
    def _col(name: str) -> int:
        try:
            return headers.index(name)
        except ValueError:
            print(
                f"ERROR: column '{name}' not found in headers: {headers}",
                file=sys.stderr,
            )
            sys.exit(1)

    col_article_id = _col("article_id")
    col_published_date = _col("published_date")
    col_include_on_site = _col("include_on_site")

    # Column letter for A1 notation (0-based index → letter).
    col_letter = chr(ord("A") + col_include_on_site)

    print(
        f"  article_id col:      {col_article_id} ({chr(ord('A') + col_article_id)})\n"
        f"  published_date col:  {col_published_date} ({chr(ord('A') + col_published_date)})\n"
        f"  include_on_site col: {col_include_on_site} ({col_letter})"
    )

    # --- Scan rows, build batch updates ---
    # Row 1 = headers; data rows start at index 1 (sheet row 2).
    data_rows = all_values[1:]

    stats = {"set_false": 0, "kept": 0, "outside_window": 0, "total_rows": len(data_rows)}
    seen_keep_ids: set[str] = set()  # track first occurrence of each keep ID

    # Collect cell updates: list of {"range": "J42", "values": [["FALSE"]]}
    updates: list[dict] = []

    for i, row in enumerate(data_rows):
        sheet_row_number = i + 2  # 1-indexed; row 1 = header

        # Pad short rows with empty strings to avoid IndexError.
        while len(row) <= max(col_article_id, col_published_date, col_include_on_site):
            row.append("")

        article_id = row[col_article_id].strip()
        published_date = _parse_date(row[col_published_date])
        include_on_site = row[col_include_on_site]

        # Only process rows in the 90-day window.
        if published_date is None or published_date < CUTOFF_DATE:
            stats["outside_window"] += 1
            continue

        # Only act on rows that are currently truthy.
        if not _is_truthy(include_on_site):
            # Already FALSE — nothing to do.
            continue

        # Row is in window AND currently TRUE.
        if article_id in KEEP_IDS and article_id not in seen_keep_ids:
            # First occurrence of a keep ID — leave TRUE.
            seen_keep_ids.add(article_id)
            stats["kept"] += 1
            print(f"  KEEP  row {sheet_row_number:5d}  {article_id}  {published_date}")
        else:
            # Set to FALSE.
            cell_range = f"{col_letter}{sheet_row_number}"
            updates.append({
                "range": cell_range,
                "values": [["FALSE"]],
            })
            stats["set_false"] += 1

    print(
        f"\nSummary:\n"
        f"  Total data rows:   {stats['total_rows']}\n"
        f"  Outside window:    {stats['outside_window']}\n"
        f"  Kept as TRUE:      {stats['kept']}\n"
        f"  To set FALSE:      {stats['set_false']}\n"
    )

    missing_keeps = KEEP_IDS - seen_keep_ids
    if missing_keeps:
        print(f"WARNING: These keep IDs were not found in the sheet: {missing_keeps}")

    if not updates:
        print("No updates needed.")
        return stats

    if dry_run:
        print(f"DRY RUN — would write {len(updates)} cell updates. No changes made.")
        return stats

    # L-4: single batch update call — not row-by-row.
    print(f"Writing {len(updates)} cell updates in one batch call…")
    ws.batch_update(updates, value_input_option="RAW")
    print("Batch update complete.")

    return stats


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="C-6d article curation — set 90-day-window articles to FALSE except 4 kept IDs."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would change without writing to Sheets.",
    )
    args = parser.parse_args()

    stats = main(dry_run=args.dry_run)
    print(f"\nDone. set_false={stats['set_false']} | kept={stats['kept']}")
