# Run as: PYTHONPATH=. python scripts/ingest_kstat.py [--recent N | --file PATH | --historical DIR] [--dry-run]
#
# ingest_kstat.py — KSTAT Korea Customs data ingestion for Velvet Knowledge Hub.
#
# Supports three input modes (mutually exclusive):
#   --recent N        Fetch the last N months from the KSTAT API (requires KSTAT_API_KEY).
#   --file PATH       Parse a single KSTAT CSV file (offline / historical).
#   --historical DIR  Parse all *.csv files in DIR (offline / historical bulk load).
#
# Upserts new rows into the VTW_Trade_Monthly tab of VKH_Data Google Sheet.
#
# Usage:
#   PYTHONPATH=. python scripts/ingest_kstat.py
#   PYTHONPATH=. python scripts/ingest_kstat.py --recent 6
#   PYTHONPATH=. python scripts/ingest_kstat.py --file /path/to/CUSTOMS_2024.csv
#   PYTHONPATH=. python scripts/ingest_kstat.py --historical /path/to/customs_dir/
#   PYTHONPATH=. python scripts/ingest_kstat.py --file /path/to/CUSTOMS_2024.csv --dry-run
#
# CSV format (utf-8-sig BOM, comma-separated):
#   기간,국가,HS코드,품목명,수출 중량,수출 금액,수입 중량,수입 금액,무역수지
#
# Parsing rules:
#   - Skip rows where 국가 is empty or "총계" (summary rows).
#   - Skip rows where 수입 중량 == 0 AND 수입 금액 == 0.
#   - HS코드 mapped to dot notation TEXT ("0507.90") — L-9.
#   - Emits two rows per CSV data row: unit=KG and unit=USD_thousands.
#   - notes = "source=<basename>".
#   - Encoding: utf-8-sig (BOM-aware).
#
# L-1:  PYTHONPATH=. ensures repo root is importable.
# L-2:  .env must be at repo root (/Users/Qs/C/velvet-knowledge-hub/.env).
# L-3:  GOOGLE_SERVICE_ACCOUNT_JSON must be single-line JSON in .env.
# L-4:  get_all_records() called once; new rows written in one append_rows() call.
# L-9:  hs_code stored as TEXT dot notation ("0507.90") — not cast to int.
#       Dedup comparison uses string equality.
# L-10: Dedup key is (date, series, hs_code, country, unit) — five fields.
#       country is required because each country is a separate API row.
#       unit is required because each period/country/hs_code yields two rows (KG + USD_thousands).
# L-13: CSV column positions detected by header name — never by fixed index.
#
# KSTAT_API_KEY: read from .env at repo root (API mode only).
# Commander action: copy KSTAT_API_KEY from /Users/Qs/C/velvet-trade-watch/.env
#
# API endpoint: https://apis.data.go.kr/1220000/Itemtrade/getItemtradeList
# Velvet HS codes: 0507901110 (deer velvet, immature), 0507901190 (deer velvet, other)
#
# Security: no credentials or secrets in this file. All secrets from .env only.

import argparse
import csv
import json
import logging
import os
import sys
import time
from datetime import date
from pathlib import Path

import gspread
import requests
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
logger = logging.getLogger("ingest_kstat")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config.yaml"
TARGET_TAB = "VTW_Trade_Monthly"
SERIES_VALUE = "kstat_api"

# Sheets API only — no Drive API required (L-5 workaround).
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# KSTAT API endpoint and parameters.
_API_ENDPOINT = "https://apis.data.go.kr/1220000/Itemtrade/getItemtradeList"
_NUM_OF_ROWS = 500

# Velvet HS 10-digit codes — only soft velvet (immature and other).
# 0507901200 (녹각, hard dried antler) deliberately excluded.
_VELVET_HS_CODES = ["0507901110", "0507901190"]

# Human-readable labels for each 10-digit code (written to hs_label column).
_HS10_LABEL_MAP: dict[str, str] = {
    "0507901110": "Deer velvet (immature)",
    "0507901190": "Deer velvet (other)",
}

# VKH schema stores hs_code as TEXT dot notation — not the 10-digit API code.
_HS_CODE_DOT = "0507.90"


# ---------------------------------------------------------------------------
# KSTAT API helpers
# ---------------------------------------------------------------------------

def _current_ym() -> str:
    """Return the current year-month as YYYY-MM."""
    today = date.today()
    return f"{today.year:04d}-{today.month:02d}"


def _subtract_months(ym: str, n: int) -> str:
    """Return YYYY-MM string n months before ym (e.g. '2026-03', 2 → '2026-01')."""
    year, month = int(ym[:4]), int(ym[5:7])
    for _ in range(n):
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    return f"{year:04d}-{month:02d}"


def _fetch_kstat_month(api_key: str, year: int, month: int) -> list[dict]:
    """
    Fetch KSTAT import data for a single month across both velvet HS codes.

    Returns a list of raw API record dicts. Empty list on any error.
    Skips rows where both weight and value are zero.
    """
    period_str = f"{year:04d}-{month:02d}"
    month_str = f"{month:02d}"
    results: list[dict] = []

    for hs10 in _VELVET_HS_CODES:
        params = {
            "serviceKey": api_key,
            "year": str(year),
            "month": month_str,
            "hs10": hs10,
            "tradeType": "I",          # I = import
            "numOfRows": str(_NUM_OF_ROWS),
            "pageNo": "1",
            "type": "json",
        }

        try:
            resp = requests.get(_API_ENDPOINT, params=params, timeout=30)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "KSTAT API error for hs=%s %04d-%02d: %s",
                hs10, year, month, exc,
            )
            continue

        # Parse items — the exact path varies; use the VTW fallback chain.
        try:
            items = payload.get(
                "items",
                payload.get("response", {}).get("body", {}).get("items", [])
            )
            if isinstance(items, dict):
                # Some data.go.kr APIs wrap single items in a dict with "item" key.
                items = items.get("item", [])
            if not isinstance(items, list):
                items = [items] if items else []
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "KSTAT response parse error for hs=%s %04d-%02d: %s",
                hs10, year, month, exc,
            )
            continue

        for item in items:
            if not isinstance(item, dict):
                continue

            try:
                country_name = str(item.get("cntyNm", "")).strip()
                imp_weight = int(float(str(item.get("imp_cur_mon_wgt", 0) or 0)))
                imp_value = int(float(str(item.get("imp_cur_mon_usd", 0) or 0)))
            except (ValueError, TypeError) as exc:
                logger.debug("Skipping malformed KSTAT item: %s — %s", item, exc)
                continue

            # Skip rows with no import activity (as per brief).
            if imp_weight == 0 and imp_value == 0:
                continue

            results.append({
                "period": period_str,
                "hs10": hs10,
                "country": country_name,
                "imp_weight_kg": imp_weight,
                "imp_value_usd_thousands": imp_value,
            })

    logger.info("KSTAT %04d-%02d: %d records fetched.", year, month, len(results))
    return results


def fetch_kstat_recent(api_key: str, months_back: int = 3) -> list[dict]:
    """
    Fetch KSTAT import data for the last N calendar months.

    Returns a combined list sorted by period → country → hs10.
    """
    current = _current_ym()
    results: list[dict] = []

    for i in range(months_back):
        ym = _subtract_months(current, i)
        year = int(ym[:4])
        month = int(ym[5:7])
        rows = _fetch_kstat_month(api_key, year, month)
        results.extend(rows)

    results.sort(key=lambda r: (r["period"], r["country"], r["hs10"]))
    return results


def api_records_to_sheet_rows(raw_records: list[dict]) -> list[dict]:
    """
    Convert raw KSTAT API records into VTW_Trade_Monthly schema rows.

    For each raw record, emits TWO rows:
      1. KG row (unit = "KG", value = imp_weight_kg)
      2. USD row (unit = "USD_thousands", value = imp_value_usd_thousands)

    L-9: hs_code stored as TEXT dot notation "0507.90" — not the 10-digit API code.
    """
    output: list[dict] = []

    for rec in raw_records:
        hs10 = rec["hs10"]
        hs_label = _HS10_LABEL_MAP.get(hs10, hs10)
        notes = f"hs10={hs10}"
        period = rec["period"]
        country = rec["country"]

        # KG row.
        output.append({
            "date": period,
            "series": SERIES_VALUE,
            "hs_code": _HS_CODE_DOT,   # TEXT dot notation — L-9 note
            "hs_label": hs_label,
            "value": rec["imp_weight_kg"],
            "unit": "KG",
            "country": country,
            "notes": notes,
        })

        # USD_thousands row.
        output.append({
            "date": period,
            "series": SERIES_VALUE,
            "hs_code": _HS_CODE_DOT,   # TEXT dot notation — L-9 note
            "hs_label": hs_label,
            "value": rec["imp_value_usd_thousands"],
            "unit": "USD_thousands",
            "country": country,
            "notes": notes,
        })

    return output


# ---------------------------------------------------------------------------
# CSV file parsing helpers (--file / --historical modes)
# ---------------------------------------------------------------------------

# Column name constants for the KSTAT CSV format (Korean headers).
_CSV_COL_PERIOD = "기간"
_CSV_COL_COUNTRY = "국가"
_CSV_COL_HS = "HS코드"
_CSV_COL_LABEL = "품목명"
_CSV_COL_IMP_WEIGHT = "수입 중량"
_CSV_COL_IMP_VALUE = "수입 금액"

# Summary row markers — these rows carry totals, not per-country data.
_CSV_SKIP_COUNTRY_VALUES = {"", "총계"}


def _hs_code_to_dot(raw_hs: str) -> str:
    """
    Map a raw KSTAT HS code string to dot notation TEXT.

    The KSTAT CSV uses 10-digit codes (e.g. "0507901110").
    VKH schema stores the 6-digit WCO heading in dot notation ("0507.90").

    Mapping rules (L-9: store as TEXT dot notation):
      - Starts with "050790" → "0507.90"
      - Starts with "051000" → "0510.00"
      - Default: first 4 chars + "." + chars 5-6 (e.g. "0507901110" → "0507.90").
    """
    raw = raw_hs.strip().replace(".", "").replace(" ", "")
    if raw.startswith("050790"):
        return "0507.90"
    if raw.startswith("051000"):
        return "0510.00"
    # Generic fallback: first 4 digits + "." + next 2 digits.
    if len(raw) >= 6:
        return f"{raw[:4]}.{raw[4:6]}"
    return raw


def parse_kstat_csv(filepath: Path) -> list[dict]:
    """
    Parse a single KSTAT CSV file into VTW_Trade_Monthly schema rows.

    Each data row emits TWO output rows:
      1. unit = "KG",            value = 수입 중량
      2. unit = "USD_thousands", value = 수입 금액

    Skip rules:
      - 국가 is empty or "총계" (summary rows).
      - Both 수입 중량 == 0 AND 수입 금액 == 0.

    Encoding: utf-8-sig (BOM-aware — KSTAT CSVs start with BOM U+FEFF).
    L-13: column positions detected by header name, never by fixed index.
    L-9:  hs_code stored as TEXT dot notation.
    """
    logger.info("Parsing KSTAT CSV: %s", filepath)
    source_note = f"source={filepath.name}"
    output: list[dict] = []

    with filepath.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)

        # Validate required columns are present (L-13: dynamic header check).
        if reader.fieldnames is None:
            raise ValueError(f"CSV has no header row: {filepath}")

        # Strip whitespace from field names to normalise.
        fieldnames = [f.strip() if f else f for f in reader.fieldnames]
        required = {_CSV_COL_PERIOD, _CSV_COL_COUNTRY, _CSV_COL_HS, _CSV_COL_LABEL,
                    _CSV_COL_IMP_WEIGHT, _CSV_COL_IMP_VALUE}
        missing = required - set(fieldnames)
        if missing:
            raise ValueError(
                f"CSV missing required columns {missing} in {filepath.name}.\n"
                f"  Found columns: {fieldnames}"
            )

        for row in reader:
            # Strip keys to match normalised fieldnames.
            row = {k.strip() if k else k: v for k, v in row.items()}

            country = row.get(_CSV_COL_COUNTRY, "").strip().strip('"')
            # Skip summary rows.
            if country in _CSV_SKIP_COUNTRY_VALUES:
                continue

            period = row.get(_CSV_COL_PERIOD, "").strip().strip('"')
            raw_hs = row.get(_CSV_COL_HS, "").strip().strip('"')
            hs_label = row.get(_CSV_COL_LABEL, "").strip().strip('"')

            # Parse weight and value — strip commas from formatted numbers.
            try:
                imp_weight = int(float(row.get(_CSV_COL_IMP_WEIGHT, "0").strip().replace(",", "") or "0"))
                imp_value = int(float(row.get(_CSV_COL_IMP_VALUE, "0").strip().replace(",", "") or "0"))
            except (ValueError, TypeError):
                logger.debug("Skipping malformed CSV row: %s", row)
                continue

            # Skip rows with no import activity.
            if imp_weight == 0 and imp_value == 0:
                continue

            hs_dot = _hs_code_to_dot(raw_hs)

            # KG row.
            output.append({
                "date":     period,
                "series":   SERIES_VALUE,
                "hs_code":  hs_dot,
                "hs_label": hs_label,
                "value":    imp_weight,
                "unit":     "KG",
                "country":  country,
                "notes":    source_note,
            })

            # USD_thousands row.
            output.append({
                "date":     period,
                "series":   SERIES_VALUE,
                "hs_code":  hs_dot,
                "hs_label": hs_label,
                "value":    imp_value,
                "unit":     "USD_thousands",
                "country":  country,
                "notes":    source_note,
            })

    logger.info("KSTAT CSV parse complete: %d schema rows from %s", len(output), filepath.name)
    return output


def parse_kstat_historical(directory: Path) -> list[dict]:
    """
    Parse all *.csv files in directory and return combined schema rows.

    Files are sorted by name (ascending) to process chronologically.
    L-13: relies on parse_kstat_csv for per-file column detection.
    """
    csv_files = sorted(directory.glob("*.csv"))
    if not csv_files:
        raise ValueError(f"No *.csv files found in directory: {directory}")

    all_rows: list[dict] = []
    for csv_path in csv_files:
        try:
            rows = parse_kstat_csv(csv_path)
            all_rows.extend(rows)
            print(f"  parsed {csv_path.name}: {len(rows)} schema rows")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Skipping %s — %s", csv_path.name, exc)

    return all_rows


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
            "  Minify: python -c \"import json,sys; "
            "print(json.dumps(json.load(sys.stdin), separators=(',',':')))\" < key.json",
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
# Dedup helpers
# ---------------------------------------------------------------------------

# L-15: Sheets coerces "0507.90" → float 507.9 at write time; str(507.9) = "507.9" ≠ "0507.90".
# Normalise before dedup comparison.
_FLOAT_TO_DOT: dict = {507.9: "0507.90", 510.0: "0510.00"}


def _normalise_hs_code(raw) -> str:
    if isinstance(raw, (int, float)):
        return _FLOAT_TO_DOT.get(float(raw), str(raw))
    return str(raw)


def build_dedup_key(row: dict) -> tuple:
    """
    Return the dedup key tuple for a VTW_Trade_Monthly row.

    L-10: key is (date, series, hs_code, country, unit) — five fields.
    Five fields are required because:
      - country: each country has its own row in the API response
      - unit: each period/country/hs_code produces two rows (KG + USD_thousands)
    L-9: hs_code is TEXT dot notation. L-15: normalise before comparison.
    """
    return (
        str(row.get("date", "")),
        str(row.get("series", "")),
        _normalise_hs_code(row.get("hs_code", "")),
        str(row.get("country", "")),
        str(row.get("unit", "")),
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
        to_write.append([row.get(h, "") for h in headers])

    return to_write, skipped


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Ingest KSTAT Korea Customs data into the VTW_Trade_Monthly tab "
            "of VKH_Data Google Sheet. Three input modes: --recent (API), "
            "--file (single CSV), --historical (directory of CSVs)."
        )
    )

    # Input mode arguments — mutually exclusive.
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--recent",
        type=int,
        default=None,
        metavar="N",
        help="Fetch the last N calendar months from the KSTAT API (requires KSTAT_API_KEY).",
    )
    mode_group.add_argument(
        "--file",
        metavar="PATH",
        help="Parse a single KSTAT CSV file (offline / historical mode).",
    )
    mode_group.add_argument(
        "--historical",
        metavar="DIR",
        help="Parse all *.csv files in DIR (offline / bulk historical load).",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse only — do not write to Google Sheets.",
    )
    args = parser.parse_args()

    # Default to --recent 3 if no mode supplied.
    if args.file is None and args.historical is None and args.recent is None:
        args.recent = 3

    # L-2: load .env from repo root.
    load_dotenv(REPO_ROOT / ".env")

    # ----- Mode: --file -------------------------------------------------------
    if args.file is not None:
        csv_path = Path(args.file).resolve()
        print("ingest_kstat.py — VKH KSTAT CSV file mode")
        print(f"  file: {csv_path.name}")
        print(f"  dry-run: {args.dry_run}")

        if not csv_path.exists():
            print(f"ERROR: CSV file not found: {csv_path}", file=sys.stderr)
            sys.exit(1)

        try:
            sheet_rows = parse_kstat_csv(csv_path)
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR: CSV parse failed — {exc}", file=sys.stderr)
            sys.exit(1)

        rows_parsed = len(sheet_rows)
        print(f"  schema rows parsed: {rows_parsed}")

        if args.dry_run:
            print()
            print("[DRY RUN] Parse complete — no Sheets write.")
            print("  Sample rows (first 3):")
            for row in sheet_rows[:3]:
                print(f"    {row}")
            print(f"rows_parsed: {rows_parsed} | new_rows: 0 | skipped_duplicates: 0")
            sys.exit(0)

        _write_rows_to_sheets(sheet_rows, rows_parsed)
        return

    # ----- Mode: --historical ------------------------------------------------
    if args.historical is not None:
        hist_dir = Path(args.historical).resolve()
        print("ingest_kstat.py — VKH KSTAT historical CSV mode")
        print(f"  directory: {hist_dir}")
        print(f"  dry-run: {args.dry_run}")

        if not hist_dir.is_dir():
            print(f"ERROR: directory not found: {hist_dir}", file=sys.stderr)
            sys.exit(1)

        try:
            sheet_rows = parse_kstat_historical(hist_dir)
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR: historical parse failed — {exc}", file=sys.stderr)
            sys.exit(1)

        rows_parsed = len(sheet_rows)
        print(f"  total schema rows parsed: {rows_parsed}")

        if args.dry_run:
            print()
            print("[DRY RUN] Parse complete — no Sheets write.")
            print("  Sample rows (first 3):")
            for row in sheet_rows[:3]:
                print(f"    {row}")
            print(f"rows_parsed: {rows_parsed} | new_rows: 0 | skipped_duplicates: 0")
            sys.exit(0)

        _write_rows_to_sheets(sheet_rows, rows_parsed)
        return

    # ----- Mode: --recent (API) -----------------------------------------------
    months_back = args.recent
    print("ingest_kstat.py — VKH KSTAT API ingestion")
    print(f"  months_back: {months_back}")
    print(f"  dry-run: {args.dry_run}")

    # Graceful skip if KSTAT_API_KEY is not set.
    api_key = os.environ.get("KSTAT_API_KEY", "").strip()
    if not api_key:
        print(
            "INFO: KSTAT_API_KEY is not set — skipping KSTAT API fetch.\n"
            "  To enable: add KSTAT_API_KEY to .env at the repo root.\n"
            "  Commander action: copy KSTAT_API_KEY from "
            "/Users/Qs/C/velvet-trade-watch/.env"
        )
        print("rows_fetched: 0 | new_rows: 0 | skipped_duplicates: 0")
        sys.exit(0)

    print(f"  fetching last {months_back} month(s) from KSTAT API...")
    raw_records = fetch_kstat_recent(api_key, months_back=months_back)
    print(f"  raw API records: {len(raw_records)}")

    sheet_rows = api_records_to_sheet_rows(raw_records)
    rows_fetched = len(sheet_rows)
    print(f"  schema rows generated: {rows_fetched}")

    if args.dry_run:
        print()
        print("[DRY RUN] Fetch complete — no Sheets write.")
        print("  Sample rows (first 3):")
        for row in sheet_rows[:3]:
            print(f"    {row}")
        print(f"rows_fetched: {rows_fetched} | new_rows: 0 | skipped_duplicates: 0")
        sys.exit(0)

    if rows_fetched == 0:
        print("  No API records returned — nothing to write.")
        print("rows_fetched: 0 | new_rows: 0 | skipped_duplicates: 0")
        sys.exit(0)

    _write_rows_to_sheets(sheet_rows, rows_fetched)


def _write_rows_to_sheets(sheet_rows: list[dict], rows_count: int) -> None:
    """
    Connect to Google Sheets, dedup, and write new rows.

    Shared by all three input modes. L-4: one read call, one write call.
    """
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

    # L-4: one read call to load existing rows.
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

    new_rows_lists, rows_skipped = rows_to_append(sheet_rows, existing_keys, headers)
    rows_new = len(new_rows_lists)

    if rows_new == 0:
        print("  Nothing to write — all rows already present.")
        print(
            f"rows_count: {rows_count} | new_rows: 0 | "
            f"skipped_duplicates: {rows_skipped}"
        )
        sys.exit(0)

    # L-4: one bulk append_rows call — never in a loop.
    ws.append_rows(new_rows_lists, value_input_option="USER_ENTERED")

    print()
    print(f"  DONE: {rows_new} rows written to '{TARGET_TAB}'.")
    print(
        f"rows_count: {rows_count} | new_rows: {rows_new} | "
        f"skipped_duplicates: {rows_skipped}"
    )


if __name__ == "__main__":
    main()
