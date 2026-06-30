# Run as: PYTHONPATH=. python scripts/ingest_vfi_price.py --file /path/to/file.csv
#         PYTHONPATH=. python scripts/ingest_vfi_price.py --file /path/to/file.pdf
#
# ingest_vfi_price.py — MFDS annual deer velvet price rankings ingestion.
#
# Accepts CSV (manual extract) or PDF (Claude API extracts the table).
# Upserts rows to the VKH VFI_Price_Annual tab.
#
# Usage:
#   PYTHONPATH=. python scripts/ingest_vfi_price.py --file /path/to/price_rankings_2024.csv
#   PYTHONPATH=. python scripts/ingest_vfi_price.py --file /path/to/mfds_stats_2024.pdf
#   PYTHONPATH=. python scripts/ingest_vfi_price.py --file /path/to/file.csv --dry-run
#
# Expected CSV format (headers must match VKH schema or accepted Korean variants):
#   year,rank,product_name,price_krw,company_name,origin_country,notes
#   2024,1,녹용 제품명 A,150000,수입업체명,뉴질랜드,
#   2024,2,...
#
# L-1:  PYTHONPATH=. ensures repo root is importable.
# L-2:  .env must be at repo root (/Users/Qs/C/velvet-knowledge-hub/.env).
# L-3:  GOOGLE_SERVICE_ACCOUNT_JSON must be single-line JSON in .env.
# L-4:  get_all_records() called once; batch writes with 200-row batches / 1.1s sleep.
# L-10: Dedup key is (year, rank, product_name) — three fields (from setup_sheets.py).
# L-13: CSV header names validated by name, not by index. Korean variants accepted.
# L-14: PDF → Claude API extracts table; ANTHROPIC_API_KEY from .env or GitHub Secrets.
#
# VFI_Price_Annual schema (7 columns) — from setup_sheets.py:
#   year, rank, product_name, price_krw, company_name, origin_country, notes
#
# Security: no credentials or secrets in this file. All secrets from .env only.

import argparse
import base64
import csv
import json
import logging
import os
import sys
import time
from pathlib import Path

import anthropic

import gspread
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
logger = logging.getLogger("ingest_vfi_price")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent

# ponytail: haiku for table extraction — cheap repeated runs; upgrade to opus if accuracy degrades
_PDF_EXTRACTION_MODEL = "claude-haiku-4-5"

_PDF_EXTRACTION_PROMPT = """\
This is a Korean MFDS (식품의약품안전처) publication containing annual deer velvet \
(녹용) price ranking data.

Extract the deer velvet annual price ranking table and return it as CSV with exactly \
these columns:
year,rank,product_name,price_krw,company_name,origin_country,notes

Rules:
- year: 4-digit year (e.g. 2024). If not shown in the table, infer from the document title.
- rank: integer ranking position (1, 2, 3 …).
- product_name: full product name in Korean.
- price_krw: price in Korean won as a plain integer — no commas, no ₩ symbol (e.g. 150000).
- company_name: importer or company name if shown, else leave blank.
- origin_country: country of origin if shown (e.g. 뉴질랜드), else leave blank.
- notes: any footnotes or remarks, else leave blank.

Return ONLY the CSV rows including the header line. No explanation, no markdown fences.\
"""
CONFIG_PATH = REPO_ROOT / "config.yaml"
TARGET_TAB = "VFI_Price_Annual"

# Sheets API only — no Drive API required (L-5 workaround).
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# VKH VFI_Price_Annual schema — from setup_sheets.py.
VFI_PRICE_HEADERS = [
    "year",
    "rank",
    "product_name",
    "price_krw",
    "company_name",
    "origin_country",
    "notes",
]

# Accepted header aliases per column (English canonical + Korean variants).
# L-13: headers validated by name. CSV may use Korean headers from MFDS report.
_HEADER_ALIASES: dict[str, list[str]] = {
    "year":          ["year", "연도", "연 도", "년도"],
    "rank":          ["rank", "순위", "순 위"],
    "product_name":  ["product_name", "제품명", "품목명", "product name"],
    "price_krw":     ["price_krw", "가격", "단가(원)", "단가", "가격(원)", "price"],
    "company_name":  ["company_name", "업체명", "수입업체", "company name"],
    "origin_country":["origin_country", "원산지", "원산지국", "country"],
    "notes":         ["notes", "비고", "메모", "note"],
}

# L-4: batch write parameters.
_BATCH_SIZE = 200
_BATCH_SLEEP = 1.1


# ---------------------------------------------------------------------------
# PDF extraction (L-14)
# ---------------------------------------------------------------------------

def _extract_table_from_pdf(pdf_path: Path) -> str:
    """
    Send the PDF to Claude API and return the extracted price table as a CSV string.

    L-14: uses ANTHROPIC_API_KEY from environment (never hardcoded).
    """
    load_dotenv(REPO_ROOT / ".env")
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise ValueError(
            "ANTHROPIC_API_KEY not set. "
            "Local dev: add to .env at repo root. "
            "GitHub Actions: add to repository Secrets."
        )

    with pdf_path.open("rb") as fh:
        pdf_b64 = base64.standard_b64encode(fh.read()).decode("utf-8")

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=_PDF_EXTRACTION_MODEL,
        max_tokens=4096,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": pdf_b64,
                    },
                },
                {"type": "text", "text": _PDF_EXTRACTION_PROMPT},
            ],
        }],
    )
    return response.content[0].text


# ---------------------------------------------------------------------------
# CSV parsing helpers
# ---------------------------------------------------------------------------

def _normalise_header(raw: str) -> str | None:
    """
    Map a raw CSV header string to a canonical VKH field name.

    Returns the canonical field name if a match is found, else None.
    L-13: accepts both English canonical names and Korean variants.
    """
    normalised = raw.strip().lower().replace(" ", "_")
    for canonical, aliases in _HEADER_ALIASES.items():
        for alias in aliases:
            if normalised == alias.replace(" ", "_").lower():
                return canonical
    return None


def _build_header_map(raw_headers: list[str]) -> dict[str, int]:
    """
    Build a map from canonical field name → CSV column index.

    L-13: raises ValueError if any required VKH field is not matched.
    """
    canonical_to_idx: dict[str, int] = {}

    for col_idx, raw_header in enumerate(raw_headers):
        canonical = _normalise_header(raw_header)
        if canonical and canonical not in canonical_to_idx:
            canonical_to_idx[canonical] = col_idx

    required = {"year", "rank", "product_name", "price_krw"}
    missing = required - set(canonical_to_idx.keys())
    if missing:
        raise ValueError(
            f"CSV is missing required columns: {missing}\n"
            f"Actual headers: {raw_headers}\n"
            f"Accepted aliases — year: {_HEADER_ALIASES['year']}, "
            f"rank: {_HEADER_ALIASES['rank']}, "
            f"product_name: {_HEADER_ALIASES['product_name']}, "
            f"price_krw: {_HEADER_ALIASES['price_krw']}"
        )

    return canonical_to_idx


def _parse_int(raw) -> tuple[int | None, str]:
    """
    Parse an integer from a raw cell value.

    Returns (int_value, error_message). On success, error_message is "".
    """
    if raw is None:
        return None, "value is None"
    s = str(raw).strip().replace(",", "")
    if not s:
        return None, "value is empty"
    try:
        return int(s), ""
    except ValueError:
        return None, f"cannot parse {raw!r} as integer"


def _parse_price(raw) -> tuple[float | None, str]:
    """
    Parse a price (numeric) from a raw cell value. Strips commas.

    Returns (float_value, error_message). On success, error_message is "".
    """
    if raw is None:
        return None, "value is None"
    s = str(raw).strip().replace(",", "")
    if not s:
        return None, "value is empty"
    try:
        return float(s), ""
    except ValueError:
        return None, f"cannot parse {raw!r} as number"


def parse_price_csv(filepath: Path) -> tuple[list[dict], int]:
    """
    Parse the Commander's annual price ranking file (CSV or PDF).

    Returns (list_of_valid_rows, error_count).

    For PDF files: Claude API extracts the table as CSV first (L-14).
    For CSV files: direct parsing.

    Validation per row:
    - year: 4-digit integer (2000–2099)
    - rank: positive integer
    - product_name: non-empty string
    - price_krw: numeric (commas stripped)

    Invalid rows are logged and counted but do not stop the run.
    L-13: header names matched by alias, not by column index.
    """
    valid_rows: list[dict] = []
    error_count = 0

    if filepath.suffix.lower() == ".pdf":
        print(f"  PDF detected — extracting table via Claude API ({_PDF_EXTRACTION_MODEL})...")
        content = _extract_table_from_pdf(filepath)
        logger.info("PDF extraction returned %d characters.", len(content))
    else:
        # Try UTF-8 first; fall back to cp949 for files exported from Korean Windows.
        for encoding in ("utf-8-sig", "utf-8", "cp949"):
            try:
                with open(filepath, encoding=encoding, newline="") as fh:
                    content = fh.read()
                break
            except UnicodeDecodeError:
                continue
        else:
            raise ValueError(f"Could not decode {filepath.name} as UTF-8 or CP949.")

    reader = csv.DictReader(content.splitlines())

    # Build header map from raw CSV headers.
    if reader.fieldnames is None:
        raise ValueError(f"CSV {filepath.name} has no header row.")

    # Use raw headers for alias mapping.
    header_map = _build_header_map(list(reader.fieldnames))

    for line_no, raw_row in enumerate(reader, start=2):  # row 1 is header
        # Extract values using the canonical field names.
        get = lambda field: raw_row.get(reader.fieldnames[header_map[field]], "").strip() if field in header_map else ""

        raw_year      = get("year")
        raw_rank      = get("rank")
        raw_name      = get("product_name")
        raw_price     = get("price_krw")
        raw_company   = get("company_name")
        raw_country   = get("origin_country")
        raw_notes     = get("notes")

        # --- Validate ---
        row_errors: list[str] = []

        year_val, year_err = _parse_int(raw_year)
        if year_err or year_val is None or not (2000 <= year_val <= 2099):
            row_errors.append(f"year invalid: {raw_year!r}")
            year_val = None

        rank_val, rank_err = _parse_int(raw_rank)
        if rank_err or rank_val is None or rank_val < 1:
            row_errors.append(f"rank invalid: {raw_rank!r}")
            rank_val = None

        product_name = raw_name.strip()
        if not product_name:
            row_errors.append("product_name is empty")

        price_val, price_err = _parse_price(raw_price)
        if price_err or price_val is None:
            row_errors.append(f"price_krw invalid: {raw_price!r}")
            price_val = None

        if row_errors:
            logger.warning("Row %d skipped — %s", line_no, "; ".join(row_errors))
            error_count += 1
            continue

        valid_rows.append({
            "year":          str(year_val),
            "rank":          str(rank_val),
            "product_name":  product_name,
            "price_krw":     str(int(price_val)) if price_val == int(price_val) else str(price_val),
            "company_name":  raw_company,
            "origin_country": raw_country,
            "notes":         raw_notes,
        })

    logger.info(
        "Price CSV parse complete: %d valid rows, %d errors from %s.",
        len(valid_rows),
        error_count,
        filepath.name,
    )
    return valid_rows, error_count


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
            "separators=(',',':')))\" < key.json",
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
    Return the dedup key tuple for a VFI_Price_Annual row.

    L-10: key is (year, rank, product_name) — three fields (from setup_sheets.py).
    """
    return (
        str(row.get("year", "")),
        str(row.get("rank", "")),
        str(row.get("product_name", "")),
    )


def load_existing_keys(worksheet) -> tuple[set, int]:
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
            "Ingest MFDS annual deer velvet price rankings into the "
            "VFI_Price_Annual tab of VKH_Data Google Sheet."
        )
    )
    parser.add_argument(
        "--file",
        required=True,
        help="Path to the annual price ranking file (CSV or PDF).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and deduplicate without writing to Google Sheets.",
    )
    args = parser.parse_args()

    csv_path = Path(args.file).resolve()

    if not csv_path.exists():
        print(f"ERROR: CSV file not found: {csv_path}", file=sys.stderr)
        sys.exit(1)

    print("ingest_vfi_price.py — VKH MFDS annual price rankings ingestion")
    print(f"  file: {csv_path.name}")
    print(f"  dry-run: {args.dry_run}")

    # --- Parse CSV -----------------------------------------------------------
    try:
        parsed_rows, parse_errors = parse_price_csv(csv_path)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: Unexpected parse failure — {exc}", file=sys.stderr)
        sys.exit(1)

    rows_parsed = len(parsed_rows)

    if rows_parsed == 0:
        print("  WARNING: no valid rows parsed from CSV. Check file format.")
        print(f"\nrows_parsed: {rows_parsed} | rows_written: 0 | rows_skipped: 0 | errors: {parse_errors}")
        sys.exit(0 if parse_errors == 0 else 1)

    if args.dry_run:
        print()
        print("[DRY RUN] Parsing complete — no Sheets write.")
        print(f"  rows_parsed: {rows_parsed}")
        print(f"  parse_errors: {parse_errors}")
        print("  Sample rows (first 3):")
        for row in parsed_rows[:3]:
            print(f"    {row}")
        print(f"\nrows_parsed: {rows_parsed} | rows_written: 0 | rows_skipped: 0 | errors: {parse_errors}")
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

    # --- Dedup (L-4: one API call) -------------------------------------------
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
        print(f"\nrows_parsed: {rows_parsed} | rows_written: 0 | rows_skipped: {rows_skipped} | errors: {parse_errors}")
        sys.exit(0)

    # --- Write (L-4: 200-row batches with 1.1s sleep) ------------------------
    rows_written = 0
    write_errors = 0

    for batch_start in range(0, len(new_rows_lists), _BATCH_SIZE):
        batch = new_rows_lists[batch_start:batch_start + _BATCH_SIZE]
        try:
            ws.append_rows(batch, value_input_option="USER_ENTERED")
            rows_written += len(batch)
            if batch_start + _BATCH_SIZE < len(new_rows_lists):
                time.sleep(_BATCH_SLEEP)
        except Exception as exc:  # noqa: BLE001
            logger.error("Sheets write error on batch starting %d: %s", batch_start, exc)
            write_errors += 1
            break

    total_errors = parse_errors + write_errors

    print()
    print(f"rows_parsed: {rows_parsed} | rows_written: {rows_written} | rows_skipped: {rows_skipped} | errors: {total_errors}")

    if total_errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
