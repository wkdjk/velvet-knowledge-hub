# Run as: PYTHONPATH=. python scripts/ingest_qia.py --file /path/to/QIA_2024.xlsx
#
# ingest_qia.py — QIA annual XLSX quarantine data ingestion for Velvet Knowledge Hub.
#
# Commander downloads a QIA annual deer velvet quarantine XLSX file (true openpyxl XLSX,
# distinct from the monthly HTML-XLS files used by VTW predecessor). This script
# parses it and upserts new rows into the VTW_Trade_Monthly tab with series = "korea_quarantine".
#
# Usage:
#   PYTHONPATH=. python scripts/ingest_qia.py --file /path/to/QIA_2024.xlsx
#   PYTHONPATH=. python scripts/ingest_qia.py --file /path/to/QIA_2024.xlsx --dry-run
#
# L-1:  PYTHONPATH=. ensures repo root is importable.
# L-2:  .env must be at repo root (/Users/Qs/C/velvet-knowledge-hub/.env).
# L-3:  GOOGLE_SERVICE_ACCOUNT_JSON must be single-line JSON in .env.
# L-4:  get_all_records() called once; new rows written in one append_rows() call.
# L-10: Dedup key is (date, series, hs_code) — three fields matching setup_sheets.py.
# L-13: Columns detected by header name scan, not by fixed index. QIA annual XLSX
#        shifted column layout between 2019–2023 and 2024+ releases — hardcoding
#        col index silently returned zero records for five years of data.
#
# hs_code type: stored as TEXT dot notation ("0507.90") in VTW_Trade_Monthly.
# This is the VKH schema decision — different from VTW predecessor which used int.
#
# Security: no credentials or secrets in this file. All secrets from .env only.

import argparse
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

import gspread
import openpyxl
import yaml
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

# ---------------------------------------------------------------------------
# L-1: ensure repo root is on sys.path.
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger("ingest_qia")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config.yaml"
TARGET_TAB = "VTW_Trade_Monthly"
SERIES_VALUE = "korea_quarantine"

# Sheets API only — no Drive API required (L-5 workaround).
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# HS code to assign to all QIA quarantine rows (dot notation TEXT — VKH schema).
HS_CODE = "0507.90"
HS_LABEL = "Horns, antlers, hooves, nails, claws and beaks — other"

# Units produced by this source.
UNIT_SHIPMENTS = "shipments"
UNIT_KG = "KG"

# Required column headers that must be present in the QIA annual XLSX.
# L-13: these are scanned by name, never by index.
_REQUIRED_HEADERS = {"국가", "품명"}

# Optional count / weight header patterns — QIA uses different Korean terms
# across file versions for the same concept.
_COUNT_HEADER_PATTERNS = ("건수", "건 수", "수량")
_WEIGHT_HEADER_PATTERNS = ("중량", "kg", "무게")

# Rows whose first-column value starts with these strings are summary rows — skip.
_SKIP_PREFIXES = ("국가계", "총 계", "총계", "합 계", "합계", "계")

# L-4: batch write sleep between gspread calls.
_BATCH_SLEEP = 1.1


# ---------------------------------------------------------------------------
# XLSX parsing helpers
# ---------------------------------------------------------------------------

def _safe_str(value) -> str:
    """Return stripped string, or empty string for None."""
    if value is None:
        return ""
    return str(value).strip()


def _parse_number(raw) -> float:
    """Parse a number cell (int, float, or comma-formatted string) to float."""
    if raw is None:
        return 0.0
    if isinstance(raw, (int, float)):
        return float(raw)
    cleaned = str(raw).strip().replace(",", "")
    if not cleaned:
        return 0.0
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def _find_header_row(ws) -> tuple[int, dict[str, int]]:
    """
    Scan rows 0..9 looking for a row containing both '국가' and '품명'.

    Returns (header_row_index, col_map) where col_map maps canonical names
    to 0-based column indices.

    L-13: column positions are detected dynamically because QIA annual XLSX
    shifted its layout between 2019–2023 and 2024+ releases.

    Raises:
        ValueError: If the required headers are not found in the first 10 rows.
    """
    all_rows = list(ws.iter_rows(values_only=True))

    for row_idx, row in enumerate(all_rows[:10]):
        row_strs = [_safe_str(c) for c in row]

        # Check if both required headers are present in this row.
        if "국가" in row_strs and "품명" in row_strs:
            col_map: dict[str, int] = {}

            for col_idx, cell_str in enumerate(row_strs):
                cell_lower = cell_str.lower().replace(" ", "")

                # Country column.
                if cell_str in ("국가", "국 가"):
                    col_map["country"] = col_idx

                # Product name column.
                if cell_str in ("품명", "품 명"):
                    col_map["product"] = col_idx

                # Count / shipments column.
                for pattern in _COUNT_HEADER_PATTERNS:
                    if pattern.replace(" ", "") in cell_lower and "country" in col_map:
                        if "count" not in col_map:
                            col_map["count"] = col_idx
                        break

                # Weight column.
                for pattern in _WEIGHT_HEADER_PATTERNS:
                    if pattern in cell_lower and "country" in col_map:
                        if "weight" not in col_map:
                            col_map["weight"] = col_idx
                        break

            # Require at minimum 국가 and 품명.
            if "country" in col_map and "product" in col_map:
                logger.info(
                    "QIA header row found at index %d: %s", row_idx, col_map
                )
                return row_idx, col_map

    # Header not found — collect actual first-row content for the error message.
    first_row_preview = [_safe_str(c) for c in all_rows[0]] if all_rows else []
    raise ValueError(
        f"Required QIA column headers {_REQUIRED_HEADERS} not found in first 10 rows.\n"
        f"Row 0 contents: {first_row_preview}\n"
        "Check that the XLSX is the QIA annual deer velvet quarantine report."
    )


def _extract_date_from_filename(filename: str) -> str:
    """
    Attempt to derive a YYYY-MM date from the filename.

    QIA annual files are typically named with the year (e.g. QIA_2024.xlsx,
    녹용검사_2024.xlsx). Annual files cover the full calendar year; we return
    the year only with month = '01' as a starting marker. Rows in the parsed
    output will carry the full year-level date.

    Falls back to empty string if no 4-digit year is found.
    """
    match = re.search(r"(20\d{2})", filename)
    if match:
        return match.group(1)  # Return just year string e.g. "2024"
    return ""


def parse_qia_annual_xlsx(filepath: Path) -> list[dict]:
    """
    Parse a QIA annual deer velvet quarantine XLSX file.

    Returns long-format rows ready for the VTW_Trade_Monthly schema:

        {
            "date":     "2024",          # Year string (annual cadence)
            "series":   "korea_quarantine",
            "hs_code":  "0507.90",       # TEXT dot notation — VKH schema
            "hs_label": "Horns, antlers...",
            "value":    14.0,            # count (shipments rows) or weight (KG rows)
            "unit":     "shipments",     # or "KG"
            "country":  "뉴질랜드",
            "notes":    "source: QIA_2024.xlsx | product: 녹용",
        }

    Both shipments and KG rows are produced for each (date, product, country) tuple
    when both count and weight columns are present in the file.

    L-9:  hs_code is TEXT ("0507.90") — matches VKH VTW_Trade_Monthly schema.
    L-13: Column positions detected by header scan, not fixed index.
    """
    logger.info("Parsing QIA annual XLSX: %s", filepath)

    wb = openpyxl.load_workbook(filepath, read_only=True, data_only=True)
    # QIA annual files typically have a single sheet; use the first one.
    ws = wb.worksheets[0]

    header_row_idx, col_map = _find_header_row(ws)

    # Extract year from filename as the date marker.
    date_str = _extract_date_from_filename(filepath.name)
    if not date_str:
        logger.warning(
            "Could not extract year from filename %r — using empty date.", filepath.name
        )

    all_rows = list(ws.iter_rows(values_only=True))
    data_rows = all_rows[header_row_idx + 1:]

    output: list[dict] = []
    current_country = ""

    for row in data_rows:
        # Skip completely empty rows.
        if all(v is None or _safe_str(v) == "" for v in row):
            continue

        country_cell = _safe_str(row[col_map["country"]]) if col_map["country"] < len(row) else ""
        product_cell = _safe_str(row[col_map["product"]]) if col_map["product"] < len(row) else ""

        # Forward-fill country when cell is empty (QIA uses merged cells in some versions).
        if country_cell:
            current_country = country_cell

        # Skip summary rows.
        if any(current_country.startswith(prefix) for prefix in _SKIP_PREFIXES):
            continue
        if any(product_cell.startswith(prefix) for prefix in _SKIP_PREFIXES):
            continue

        if not current_country or not product_cell:
            continue

        notes = f"source: {filepath.name} | product: {product_cell}"

        # Shipments (count) row — if count column is present.
        if "count" in col_map:
            count_val = _parse_number(
                row[col_map["count"]] if col_map["count"] < len(row) else None
            )
            output.append({
                "date":     date_str,
                "series":   SERIES_VALUE,
                "hs_code":  HS_CODE,
                "hs_label": HS_LABEL,
                "value":    count_val,
                "unit":     UNIT_SHIPMENTS,
                "country":  current_country,
                "notes":    notes,
            })

        # Weight (KG) row — if weight column is present.
        if "weight" in col_map:
            weight_val = _parse_number(
                row[col_map["weight"]] if col_map["weight"] < len(row) else None
            )
            output.append({
                "date":     date_str,
                "series":   SERIES_VALUE,
                "hs_code":  HS_CODE,
                "hs_label": HS_LABEL,
                "value":    weight_val,
                "unit":     UNIT_KG,
                "country":  current_country,
                "notes":    notes,
            })

    logger.info(
        "QIA annual parse complete: %d data rows → %d long-format records.",
        len(data_rows),
        len(output),
    )
    return output


# ---------------------------------------------------------------------------
# Google Sheets helpers — reused from ingest_nz_export.py pattern
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    """Read config.yaml from repo root. Returns empty dict on failure."""
    if not CONFIG_PATH.exists():
        return {}
    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except yaml.YAMLError:
        return {}


def connect_sheets(sheet_id: str):
    """
    Connect to Google Sheets using service account credentials.

    L-3: GOOGLE_SERVICE_ACCOUNT_JSON must be single-line JSON.
    Returns gspread.Spreadsheet object. Calls sys.exit(1) on failure.
    """
    load_dotenv(REPO_ROOT / ".env")

    sa_json_raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not sa_json_raw:
        print(
            "ERROR: GOOGLE_SERVICE_ACCOUNT_JSON environment variable is not set.\n"
            "  Local dev: add it to .env at the repo root (single-line JSON — L-3).\n"
            "  GitHub Actions: add it to repository Secrets.",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        sa_info = json.loads(sa_json_raw)
    except json.JSONDecodeError as exc:
        print(
            f"ERROR: GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON — {exc}\n"
            "  Tip: minify with: "
            'python -c "import json,sys; print(json.dumps(json.load(sys.stdin), '
            "separators=(',',':')))" < key.json",
            file=sys.stderr,
        )
        sys.exit(1)

    creds = Credentials.from_service_account_info(sa_info, scopes=SCOPES)
    gc = gspread.authorize(creds)

    try:
        spreadsheet = gc.open_by_key(sheet_id)
    except gspread.exceptions.APIError as exc:
        print(
            f"ERROR: Could not open sheet {sheet_id} — {exc}\n"
            "  Check the service account has Editor access to the sheet.",
            file=sys.stderr,
        )
        sys.exit(1)

    return spreadsheet


def resolve_sheet_id() -> str:
    """
    Resolve the Google Sheet ID from environment then config.yaml fallback.

    Priority: VKH_SHEET_ID env var → config.yaml sheet_id.
    Calls sys.exit(1) if neither is set.
    """
    load_dotenv(REPO_ROOT / ".env")

    sheet_id = os.environ.get("VKH_SHEET_ID", "").strip()
    if sheet_id:
        return sheet_id

    config = _load_config()
    sheet_id = config.get("sheet_id", "").strip()
    if sheet_id:
        print("  (VKH_SHEET_ID not set — using sheet_id from config.yaml)")
        return sheet_id

    print(
        "ERROR: Sheet ID not found.\n"
        "  Set VKH_SHEET_ID in .env, or ensure sheet_id is set in config.yaml.",
        file=sys.stderr,
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# Dedup and write helpers
# ---------------------------------------------------------------------------

def build_dedup_key(row: dict) -> tuple:
    """
    Return the dedup key tuple for a VTW_Trade_Monthly row.

    L-10: key is (date, series, hs_code). Unit is appended to distinguish
    shipments rows from KG rows that share the same (date, series, hs_code).
    """
    return (
        str(row.get("date", "")),
        str(row.get("series", "")),
        str(row.get("hs_code", "")),
        str(row.get("unit", "")),
        str(row.get("country", "")),
    )


def load_existing_keys(worksheet) -> tuple[set, int]:
    """
    Read all rows from the worksheet once and return a set of dedup keys.

    L-4: get_all_records() is called exactly once — never inside a loop.
    Filters to series = korea_quarantine to keep the key set small.
    """
    existing_rows = worksheet.get_all_records()
    quarantine_rows = [r for r in existing_rows if r.get("series") == SERIES_VALUE]
    return {build_dedup_key(r) for r in quarantine_rows}, len(existing_rows)


def rows_to_append(
    new_rows: list[dict],
    existing_keys: set,
    headers: list[str],
) -> tuple[list[list], int]:
    """
    Filter new_rows to those not already in existing_keys.

    Returns (list_of_lists_for_gspread, skipped_count).
    """
    to_write: list[list] = []
    skipped = 0

    for row in new_rows:
        key = build_dedup_key(row)
        if key in existing_keys:
            skipped += 1
            continue
        to_write.append([row.get(h, "") for h in headers])

    return to_write, skipped


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Ingest a QIA annual deer velvet quarantine XLSX into the "
            "VTW_Trade_Monthly tab of VKH_Data Google Sheet."
        )
    )
    parser.add_argument(
        "--file",
        required=True,
        help="Path to the QIA annual quarantine XLSX file.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and deduplicate without writing to Google Sheets.",
    )
    args = parser.parse_args()

    xlsx_path = Path(args.file).resolve()

    if not xlsx_path.exists():
        print(f"ERROR: XLSX file not found: {xlsx_path}", file=sys.stderr)
        sys.exit(1)

    print("ingest_qia.py — VKH QIA annual quarantine ingestion")
    print(f"  file: {xlsx_path.name}")
    print(f"  dry-run: {args.dry_run}")

    # --- Parse XLSX -----------------------------------------------------------
    errors = 0
    try:
        parsed_rows = parse_qia_annual_xlsx(xlsx_path)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: Unexpected parse failure — {exc}", file=sys.stderr)
        sys.exit(1)

    rows_parsed = len(parsed_rows)

    if rows_parsed == 0:
        print("  WARNING: no rows parsed from XLSX. Check file format and headers.")
        print(f"rows_parsed: {rows_parsed} | rows_written: 0 | rows_skipped: 0 | errors: 0")
        sys.exit(0)

    if args.dry_run:
        print()
        print("[DRY RUN] Parsing complete — no Sheets write.")
        print(f"  rows_parsed: {rows_parsed}")
        print("  Sample rows (first 3):")
        for row in parsed_rows[:3]:
            print(f"    {row}")
        print(f"\nrows_parsed: {rows_parsed} | rows_written: 0 | rows_skipped: 0 | errors: 0")
        sys.exit(0)

    # --- Connect to Sheets ---------------------------------------------------
    sheet_id = resolve_sheet_id()
    print(f"  sheet_id: {sheet_id}")

    spreadsheet = connect_sheets(sheet_id)
    print(f"  sheet title: {spreadsheet.title}")

    try:
        ws = spreadsheet.worksheet(TARGET_TAB)
    except gspread.exceptions.WorksheetNotFound:
        print(
            f"ERROR: tab '{TARGET_TAB}' not found in sheet {sheet_id}.\n"
            "  Run scripts/setup_sheets.py first to create all Phase 1 tabs.",
            file=sys.stderr,
        )
        sys.exit(1)

    # --- Dedup (L-4: one API call to read all existing rows) -----------------
    existing_keys, existing_count = load_existing_keys(ws)
    print(f"  existing rows in tab: {existing_count}")

    headers = ws.row_values(1)
    if not headers:
        print(
            f"ERROR: tab '{TARGET_TAB}' has no header row. "
            "Re-run setup_sheets.py to restore it.",
            file=sys.stderr,
        )
        sys.exit(1)

    new_rows_lists, rows_skipped = rows_to_append(parsed_rows, existing_keys, headers)
    rows_new = len(new_rows_lists)

    if rows_new == 0:
        print("  Nothing to write — all rows already present.")
        print(f"\nrows_parsed: {rows_parsed} | rows_written: 0 | rows_skipped: {rows_skipped} | errors: {errors}")
        sys.exit(0)

    # --- Write (L-4: batch append — never loop) ------------------------------
    # Batch in groups of 200 rows with a 1.1s sleep to respect rate limits.
    rows_written = 0
    batch_size = 200

    for batch_start in range(0, len(new_rows_lists), batch_size):
        batch = new_rows_lists[batch_start:batch_start + batch_size]
        try:
            ws.append_rows(batch, value_input_option="USER_ENTERED")
            rows_written += len(batch)
            if batch_start + batch_size < len(new_rows_lists):
                time.sleep(_BATCH_SLEEP)
        except Exception as exc:  # noqa: BLE001
            logger.error("Sheets write error on batch starting %d: %s", batch_start, exc)
            errors += 1
            break

    print()
    print(f"rows_parsed: {rows_parsed} | rows_written: {rows_written} | rows_skipped: {rows_skipped} | errors: {errors}")

    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
