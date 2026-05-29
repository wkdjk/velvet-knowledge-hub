# Run as: PYTHONPATH=. python scripts/ingest_nz_export.py --file /path/to/stats_nz_velvet.csv
#
# ingest_nz_export.py — Stats NZ deer velvet CSV ingestion for Velvet Knowledge Hub.
#
# Parses a Stats NZ TEX001F monthly export CSV and upserts new rows into the
# VTW_Trade_Monthly tab of VKH_Data Google Sheet.
#
# Usage:
#   PYTHONPATH=. python scripts/ingest_nz_export.py --file /path/to/NZ_EXPORT_2024.csv
#   PYTHONPATH=. python scripts/ingest_nz_export.py --file /path/to/NZ_EXPORT_2024.csv --dry-run
#
# L-1: sys.path.insert ensures repo root is importable (e.g. config as a fallback).
# L-2: .env must be at repo root (/Users/Qs/C/velvet-knowledge-hub/.env).
# L-3: GOOGLE_SERVICE_ACCOUNT_JSON must be single-line JSON in .env.
# L-4: get_all_records() called once; new rows written in one append_rows() call.
# L-9: hs_code stored as TEXT ("0507.90" dot notation) in VTW_Trade_Monthly —
#       NOT cast to int. Dedup compares strings.
# L-10: Dedup key is (date, series, hs_code). Three fields, matching config.yaml.
# L-13: CSV column layout detected by header scan, not fixed index.
#
# Security: no credentials or secrets in this file. All secrets from .env only.

import argparse
import csv
import json
import logging
import os
import re
import sys
from pathlib import Path

import gspread
import yaml
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

# ---------------------------------------------------------------------------
# L-1: ensure repo root is on sys.path for fallback config.yaml import.
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger("ingest_nz_export")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config.yaml"
TARGET_TAB = "VTW_Trade_Monthly"
SERIES_VALUE = "nz_export"

# Sheets API only — no Drive API required (L-5 workaround).
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# HS code map — order matters: most specific patterns tested first.
# Each entry maps a substring of the product description to a dot-notation
# HS code string (as stored in VTW_Trade_Monthly, schema from setup_sheets.py).
_HS_CODE_MAP: list[tuple[str, str]] = [
    ("other than frozen or dried", "0507.90"),  # velvet other (most specific)
    ("frozen",                     "0507.90"),  # velvet frozen
    ("dried",                      "0507.90"),  # velvet dried
    ("excluding powder",           "0507.90"),  # horns & antlers (no frozen/dried)
    ("powder",                     "0507.90"),  # powder of horns & velvet
]

# HS labels — human-readable descriptions for the hs_label column.
_HS_LABEL_MAP: dict[str, str] = {
    "0507.90": "Horns, antlers, hooves, nails, claws and beaks — other",
    "0510.00": "Ambergris, castoreum, civet and musk; cantharides — other",
}

# Velvet-specific HS codes to include (dot notation, matching _HS_CODE_MAP output).
_VELVET_HS_CODES: frozenset[str] = frozenset(["0507.90", "0510.00"])

# Destination normalisation — raw CSV header → clean country name.
_DESTINATION_MAP: dict[str, str] = {
    "Korea, Republic of":                         "Korea",
    "China, People's Republic of":                "China",
    "Hong Kong (Special Administrative Region)":  "Hong Kong",
    "Taiwan":                                     "Taiwan",
}


# ---------------------------------------------------------------------------
# CSV parsing helpers
# ---------------------------------------------------------------------------

def _resolve_hs_code(description: str) -> str | None:
    """Return dot-notation HS code for a product description, or None if not matched."""
    desc_lower = description.lower()
    for substring, hs_code in _HS_CODE_MAP:
        if substring in desc_lower:
            return hs_code
    return None


def _parse_date(raw: str) -> str:
    """Convert '2021M01' → '2021-01'. Raises ValueError on unexpected format."""
    match = re.fullmatch(r"(\d{4})M(\d{2})", raw.strip())
    if not match:
        raise ValueError(f"Unexpected date format: {raw!r}")
    return f"{match.group(1)}-{match.group(2)}"


def _parse_number(raw: str) -> float:
    """Parse a number string to float. Returns 0.0 for empty or whitespace."""
    cleaned = raw.strip().replace(",", "")
    if not cleaned:
        return 0.0
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def _build_column_map(row2: list[str], row3: list[str], row4: list[str]) -> list[dict]:
    """
    Parse header rows 2–4 of the TEX001F CSV into a list of column descriptors.

    TEX001F structure:
      Row 1: blank / file title
      Row 2: destination country names (sparse — forward-filled)
      Row 3: product descriptions (sparse — forward-filled)
      Row 4: metrics ("Quantity" / "Free on board NZ dollars")

    Each descriptor dict contains:
      col_index   — 0-based index into a data row
      destination — normalised country name
      hs_code     — dot-notation string (e.g. "0507.90") or None
      metric      — "quantity" | "fob"

    L-13: columns detected from header content, not fixed index.
    """
    column_map: list[dict] = []
    current_dest_raw = ""
    current_desc = ""

    for col_idx in range(1, len(row2)):
        dest_cell = row2[col_idx].strip() if col_idx < len(row2) else ""
        desc_cell = row3[col_idx].strip() if col_idx < len(row3) else ""
        metric_cell = row4[col_idx].strip().lower() if col_idx < len(row4) else ""

        # Forward-fill destination label.
        if dest_cell:
            current_dest_raw = dest_cell
        destination = _DESTINATION_MAP.get(current_dest_raw, current_dest_raw)

        # Forward-fill product description (ignore bare-space cells).
        if desc_cell and desc_cell != " ":
            current_desc = desc_cell

        hs_code = _resolve_hs_code(current_desc)

        if "free on board" in metric_cell:
            metric = "fob"
        elif "quantity" in metric_cell:
            metric = "quantity"
        else:
            metric = metric_cell  # unknown — logged but kept for audit

        column_map.append({
            "col_index": col_idx,
            "destination": destination,
            "hs_code": hs_code,
            "metric": metric,
        })

    return column_map


def parse_nz_export_csv(filepath: Path, source_file_name: str) -> list[dict]:
    """
    Parse a Stats NZ TEX001F monthly export CSV.

    Filters to rows whose hs_code is in _VELVET_HS_CODES.
    Returns long-format rows ready for the VTW_Trade_Monthly schema:

        {
            "date":        "2024-01",   # YYYY-MM
            "series":      "nz_export",
            "hs_code":     "0507.90",   # dot notation TEXT (schema requirement)
            "hs_label":    "Horns, antlers...",
            "value":       622.0,       # numeric — quantity OR fob depending on unit
            "unit":        "KG",        # KG for quantity rows, NZD for fob rows
            "country":     "Korea",
            "notes":       "source: NZ_EXPORT_2024.csv | provisional: False",
        }

    Both KG and NZD rows are produced for each (date, hs_code, destination) tuple.
    L-9: hs_code is TEXT ("0507.90") — matches VTW_Trade_Monthly schema (not int).
    """
    logger.info("Parsing NZ Export CSV: %s", filepath)

    with open(filepath, encoding="utf-8") as fh:
        all_rows = list(csv.reader(fh))

    if len(all_rows) < 5:
        logger.error("CSV has fewer than 5 rows — unexpected format: %s", filepath)
        return []

    # TEX001F: row 0 = title/blank, rows 1–3 = headers, data starts at row 4.
    row2 = all_rows[1]
    row3 = all_rows[2]
    row4 = all_rows[3]

    column_map = _build_column_map(row2, row3, row4)

    # ------------------------------------------------------------------
    # Pass 1 — collect all unique dates for provisional-date detection.
    # The last 3 unique dates in a Stats NZ file are flagged provisional.
    # ------------------------------------------------------------------
    date_order: list[str] = []
    data_rows: list[list[str]] = []

    for raw_row in all_rows[4:]:
        if not raw_row or not raw_row[0].strip():
            continue
        date_raw = raw_row[0].strip()
        if not re.fullmatch(r"\d{4}M\d{2}", date_raw):
            break  # footer reached
        try:
            parsed_date = _parse_date(date_raw)
        except ValueError:
            logger.warning("Skipping unrecognised date cell: %r", date_raw)
            continue
        if parsed_date not in date_order:
            date_order.append(parsed_date)
        data_rows.append(raw_row)

    provisional_dates: set[str] = (
        set(date_order[-3:]) if len(date_order) >= 3 else set(date_order)
    )

    # ------------------------------------------------------------------
    # Pass 2 — pair Quantity / FOB columns; emit one row per measurement.
    # ------------------------------------------------------------------
    # Build (destination, hs_code, qty_col, fob_col) tuples from column_map.
    product_slots: list[tuple[str, str | None, int, int]] = []
    i = 0
    while i < len(column_map):
        qty_desc = column_map[i]
        if (
            qty_desc["metric"] == "quantity"
            and i + 1 < len(column_map)
        ):
            fob_desc = column_map[i + 1]
            if (
                fob_desc["metric"] == "fob"
                and fob_desc["destination"] == qty_desc["destination"]
            ):
                product_slots.append((
                    qty_desc["destination"],
                    qty_desc["hs_code"],
                    qty_desc["col_index"],
                    fob_desc["col_index"],
                ))
                i += 2
                continue
        logger.warning(
            "Unexpected column order at index %d: %s", i, column_map[i]
        )
        i += 1

    output: list[dict] = []

    for raw_row in data_rows:
        try:
            parsed_date = _parse_date(raw_row[0].strip())
        except ValueError:
            continue
        is_provisional = parsed_date in provisional_dates

        for destination, hs_code, qty_col, fob_col in product_slots:
            # Only include velvet-related HS codes.
            if hs_code not in _VELVET_HS_CODES:
                continue

            qty = (
                _parse_number(raw_row[qty_col])
                if qty_col < len(raw_row)
                else 0.0
            )
            fob = (
                _parse_number(raw_row[fob_col])
                if fob_col < len(raw_row)
                else 0.0
            )

            notes_base = f"source: {source_file_name} | provisional: {is_provisional}"
            hs_label = _HS_LABEL_MAP.get(hs_code, hs_code)

            # Quantity row (unit = KG).
            output.append({
                "date":    parsed_date,
                "series":  SERIES_VALUE,
                "hs_code": hs_code,
                "hs_label": hs_label,
                "value":   qty,
                "unit":    "KG",
                "country": destination,
                "notes":   notes_base,
            })

            # FOB value row (unit = NZD).
            output.append({
                "date":    parsed_date,
                "series":  SERIES_VALUE,
                "hs_code": hs_code,
                "hs_label": hs_label,
                "value":   fob,
                "unit":    "NZD",
                "country": destination,
                "notes":   notes_base,
            })

    logger.info(
        "NZ Export parse complete: %d data rows → %d long-format records.",
        len(data_rows),
        len(output),
    )
    return output


# ---------------------------------------------------------------------------
# Google Sheets helpers
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
    # L-2: load .env from repo root.
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
    # L-2: load .env from repo root (may already be loaded; load_dotenv is idempotent).
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

    L-10: key is (date, series, hs_code) — three fields, matches config.yaml.
    An additional 'unit' dimension is included to avoid KG and NZD rows
    conflating each other (both share the same date/series/hs_code).
    """
    return (
        str(row.get("date", "")),
        str(row.get("series", "")),
        str(row.get("hs_code", "")),
        str(row.get("unit", "")),
    )


def load_existing_keys(worksheet) -> set:
    """
    Read all rows from the worksheet once and return a set of dedup keys.

    L-4: get_all_records() is called exactly once — never inside a loop.
    """
    existing_rows = worksheet.get_all_records()
    return {build_dedup_key(r) for r in existing_rows}, len(existing_rows)


def rows_to_append(
    new_rows: list[dict],
    existing_keys: set,
    headers: list[str],
) -> tuple[list[list], int]:
    """
    Filter new_rows to those not in existing_keys.

    Returns (list_of_lists_for_gspread, skipped_count).
    Each row is converted to a list matching the headers order.
    """
    to_write: list[list] = []
    skipped = 0

    for row in new_rows:
        key = build_dedup_key(row)
        if key in existing_keys:
            skipped += 1
            continue
        # Convert dict to ordered list matching headers.
        to_write.append([row.get(h, "") for h in headers])

    return to_write, skipped


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Ingest a Stats NZ deer velvet export CSV into the "
            "VTW_Trade_Monthly tab of VKH_Data Google Sheet."
        )
    )
    parser.add_argument(
        "--file",
        required=True,
        help="Path to the Stats NZ monthly export CSV file.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and deduplicate without writing to Google Sheets.",
    )
    args = parser.parse_args()

    csv_path = Path(args.file).resolve()

    # Validate input file.
    if not csv_path.exists():
        print(f"ERROR: CSV file not found: {csv_path}", file=sys.stderr)
        sys.exit(1)

    print(f"ingest_nz_export.py — VKH Stats NZ ingestion")
    print(f"  file: {csv_path.name}")
    print(f"  dry-run: {args.dry_run}")

    # --- Parse CSV -----------------------------------------------------------
    parsed_rows = parse_nz_export_csv(csv_path, csv_path.name)
    rows_parsed = len(parsed_rows)
    print(f"  rows parsed: {rows_parsed}")

    if rows_parsed == 0:
        print("  WARNING: no velvet rows found in CSV. Check file format.")
        sys.exit(0)

    if args.dry_run:
        print()
        print("[DRY RUN] Parsing complete — no Sheets write.")
        print(f"  rows_parsed : {rows_parsed}")
        print(f"  Sample rows (first 3):")
        for row in parsed_rows[:3]:
            print(f"    {row}")
        sys.exit(0)

    # --- Connect to Sheets ---------------------------------------------------
    sheet_id = resolve_sheet_id()
    print(f"  sheet_id: {sheet_id}")

    spreadsheet = connect_sheets(sheet_id)
    print(f"  sheet title: {spreadsheet.title}")

    # Locate target tab.
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

    # Retrieve tab headers from row 1 to build ordered insert lists.
    # get_all_records() already consumed the header; re-read header only once.
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

    print(f"  rows_new    : {rows_new}")
    print(f"  rows_skipped: {rows_skipped}")

    if rows_new == 0:
        print("  Nothing to write — all rows already present.")
        sys.exit(0)

    # --- Write (L-4: one bulk append_rows call — never in a loop) ------------
    ws.append_rows(new_rows_lists, value_input_option="USER_ENTERED")

    print()
    print(f"  DONE: {rows_new} rows written to '{TARGET_TAB}'.")
    print(f"  rows_parsed : {rows_parsed}")
    print(f"  rows_new    : {rows_new}")
    print(f"  rows_skipped: {rows_skipped}")


if __name__ == "__main__":
    main()
