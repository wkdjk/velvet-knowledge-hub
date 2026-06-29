"""
setup_sheets.py - one-time VKH_Data Google Sheet schema setup script.

Usage:
    PYTHONPATH=. python scripts/setup_sheets.py --sheet-id <GOOGLE_SHEET_ID>

The sheet must already exist and be shared with the service account as Editor.
Commander creates the blank sheet, shares it with the service account email,
then passes the sheet ID to this script.

Why: the service account (velvet-trade-watch GCP project) does not have
permission to create new Google Drive files. This is a Workspace configuration
constraint documented in project-patterns.md (KVN pattern: Drive API absent).

Idempotent: if all Phase 1 tabs already exist, prints a confirmation and exits
without creating duplicates or overwriting data.

Security: credentials loaded only from environment - never hardcoded.
L-1: PYTHONPATH=. required for local runs.
L-2: .env must be at repo root (/Users/Qs/C/velvet-knowledge-hub/.env).
L-3: GOOGLE_SERVICE_ACCOUNT_JSON must be single-line JSON in .env.
L-5 workaround: service account cannot create Drive files. Commander creates
    the sheet manually, shares with service account, passes --sheet-id.
"""

import argparse
import json
import os
import sys
import time

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SHEET_NAME = "VKH_Data"
COMMANDER_EMAIL = "seouldesk.help@gmail.com"
SERVICE_ACCOUNT_EMAIL = "velvet-trade-watch@velvet-trade-watch.iam.gserviceaccount.com"

# Sheets API only - no Drive API required.
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
]

# Batch write sleep (L-4: never loop API; batch once with a brief pause).
BATCH_SLEEP = 1.1  # seconds between gspread bulk writes

# ---------------------------------------------------------------------------
# Tab schemas
# Each entry: (tab_name, headers_list, seed_rows_list_of_lists)
# Header row is row 1 - sacred, code never edits it after creation.
# ---------------------------------------------------------------------------

VTW_TRADE_MONTHLY_HEADERS = [
    "date",
    "series",
    "hs_code",
    "hs_label",
    "value",
    "unit",
    "country",
    "notes",
    "hs_code_10digit",
    "product_type",
]
# date: YYYY-MM format (monthly)
# series: nz_export | korea_quarantine | kstat_api
# hs_code: stored as TEXT string - e.g. "0507.90" - preserves dot notation (L-9 note: not int here)
# hs_label: human-readable HS code description
# value: numeric
# unit: NZD | KG | USD_thousands
# country: origin or destination country
# notes: free text
# hs_code_10digit: full 10-digit HS code as TEXT (GAP-5 fix, C-3e 2026-06-02)
#   NZ: "0507901110" (frozen), "0507901190" (dried/other)
#   KSTAT: "0507901110" (immature/frozen), "0507901190" (other/dried)
#   Existing rows without this field will have empty string (backward compatible).
# product_type: "frozen" | "dried" | "other" (GAP-5 fix, C-3e 2026-06-02)
#   frozen → apply 0.33 dried-equivalent conversion in build.py
#   dried  → no conversion (1:1)
#   other  → no conversion (treated same as dried; flagged for review)

# VFI historical xlsx: 16 columns (positions 0-15) mapped to snake_case.
# Source: VFI_PhaseA_progress_2026-05-28.md - confirmed against actual file.
# Plus consolidated importer and notes fields for pipeline compatibility.
# Dedup key: (date, importer, product_name) - L-10.
VFI_IMPORT_RECORDS_HEADERS = [
    "date",              # ISO date string YYYY-MM-DD (derived from year+month+day)
    "year",              # int - xlsx col 0
    "month",             # int - xlsx col 1
    "day",               # int - xlsx col 2
    "importer_en",       # xlsx col 3 - Importer (English)
    "product_type_en",   # xlsx col 4 - Translation of type
    "country_origin_en", # xlsx col 5 - Country of origin
    "country_export_en", # xlsx col 6 - Country of export
    "importer_ko",       # xlsx col 7 - Importer (Korean) - dedup key field
    "product_name",      # xlsx col 8 - Product name (Korean) - dedup key field
    "product_en",        # xlsx col 9 - Product name (English)
    "product_type_ko",   # xlsx col 10 - Product type (Korean)
    "exporter_en",       # xlsx col 11 - Exporter (English)
    "expiry_date",       # xlsx col 13 - Expire date
    "country_origin_ko", # xlsx col 14 - Country of origin (Korean)
    "country_export_ko", # xlsx col 15 - Country of export (Korean)
    "importer",          # consolidated importer for dedup key (importer_ko preferred)
    "notes",             # free text / MFDS metadata
]

VFI_PRICE_ANNUAL_HEADERS = [
    "year",
    "rank",
    "product_name",
    "price_krw",
    "company_name",
    "origin_country",
    "notes",
]
# Dedup key: (year, rank, product_name)

KVN_ARTICLES_HEADERS = [
    "article_id",
    "title",
    "url",
    "content_hash",
    "published_date",
    "source",
    "category",
    "english_summary",
    "ai_processed_at",
    "include_on_site",
    "crawled_at",
    "english_title",  # C-8 P0b: English headline ≤12 words, written by classifier
]
# content_hash: SHA-256 of URL+title for cross-publication dedup
# ai_processed_at: empty until AI classification runs
# include_on_site: TRUE/FALSE

KEYWORDS_HEADERS = ["term", "type", "language"]
KEYWORDS_SEED_ROWS = [
    ["녹용", "allow", "ko"],
    ["사슴뿔", "allow", "ko"],
    ["사슴 녹용", "allow", "ko"],
    ["deer velvet", "allow", "en"],
    ["velvet antler", "allow", "en"],
]
# type: allow | block
# language: ko | en

README_ADMIN_HEADERS = ["section", "instruction", "example"]
README_ADMIN_SEED_ROWS = [
    [
        "Add trade data",
        "Download Stats NZ monthly CSV. Open VTW_Trade_Monthly tab. Paste rows below the last data row. Do NOT edit row 1 (headers are sacred).",
        "Paste 12 new rows for 2024-01 through 2024-12. Set series = nz_export.",
    ],
    [
        "Update keywords",
        "Open the _keywords tab. Add a new row with: term (the keyword), type (allow or block), language (ko or en). Save. Keywords take effect on the next scheduled build.",
        "term=록용, type=allow, language=ko",
    ],
    [
        "Check if build failed",
        "Open the GitHub repository Actions tab at github.com/wkdjk/velvet-knowledge-hub/actions. A red X means the last build failed. You will also receive an alert email if ALERT_EMAIL is configured.",
        "Look for a red circle icon on the most recent workflow run.",
    ],
    [
        "Row 1 is sacred",
        "Never edit, move, or delete row 1 in any data tab. Row 1 contains the column headers that all scripts depend on. Editing row 1 will break the pipeline.",
        "If a header looks wrong, contact TechQ - do not edit it yourself.",
    ],
    [
        "Add a new source",
        "Create a new Sheets tab with the correct headers (row 1). Then add one YAML block to config.yaml in the repo. No Python edits required. See config.yaml for the block format.",
        "id: my_new_source, tab: My_New_Tab, kind: records, section: import_intelligence, enabled: true",
    ],
    [
        "Update import records",
        "Download the MFDS 수입식품 정보마루 CSV for the latest quarter. Open VFI_Import_Records tab. Paste rows below the last existing row. The build script deduplicates on (date, importer, product_name).",
        "Paste Q3 2024 rows. Duplicates from earlier quarters are automatically skipped on the next build.",
    ],
    [
        "Source_Status freshness",
        "The Source_Status tab tracks when each source was last updated. After pasting new data into any source tab, update the last_updated cell in the corresponding Source_Status row to today's date (YYYY-MM-DD).",
        "Find the nz_export row in Source_Status. Update last_updated to 2024-09-30.",
    ],
    [
        "Annual price data",
        "MFDS publishes annual deer velvet price rankings once per year. Download the annual report PDF, extract the ranking table, and paste into VFI_Price_Annual. Dedup key is (year, rank, product_name).",
        "Paste 2024 annual rankings. Year column = 2024. Rank = 1, 2, 3 in order.",
    ],
]

SOURCE_STATUS_HEADERS = [
    "source_id",
    "owner_email",
    "update_frequency",
    "last_updated",
    "freshness_days",
    "public_source_note",
]
SOURCE_STATUS_SEED_ROWS = [
    ["nz_export", COMMANDER_EMAIL, "monthly", "", "45", "Stats NZ public data"],
    ["korea_quarantine", COMMANDER_EMAIL, "monthly", "", "60", "QIA public data"],
    ["kstat_api", "automated", "monthly", "", "45", "KSTAT API public data"],
    [
        "vfi_import_records",
        COMMANDER_EMAIL,
        "quarterly",
        "",
        "120",
        "MFDS 수입식품 정보마루",
    ],
    [
        "vfi_price_annual",
        COMMANDER_EMAIL,
        "annually",
        "",
        "365",
        "MFDS annual price report",
    ],
    ["kvn_articles", "automated", "weekly", "", "7", "Naver News API"],
]

# ---------------------------------------------------------------------------
# Tab registry - ordered for creation.
# Tuple: (tab_name, headers, seed_rows)
# ---------------------------------------------------------------------------
TABS = [
    ("VTW_Trade_Monthly", VTW_TRADE_MONTHLY_HEADERS, []),
    ("VFI_Import_Records", VFI_IMPORT_RECORDS_HEADERS, []),
    ("VFI_Price_Annual", VFI_PRICE_ANNUAL_HEADERS, []),
    ("KVN_Articles", KVN_ARTICLES_HEADERS, []),
    ("_keywords", KEYWORDS_HEADERS, KEYWORDS_SEED_ROWS),
    ("README_Admin", README_ADMIN_HEADERS, README_ADMIN_SEED_ROWS),
    ("Source_Status", SOURCE_STATUS_HEADERS, SOURCE_STATUS_SEED_ROWS),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_credentials() -> Credentials:
    """Load service account credentials from environment (L-3: single-line JSON)."""
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        print("ERROR: GOOGLE_SERVICE_ACCOUNT_JSON environment variable is not set.")
        print("  Check that .env exists at the repo root and contains the variable.")
        sys.exit(1)
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"ERROR: Could not parse GOOGLE_SERVICE_ACCOUNT_JSON as JSON: {exc}")
        print("  Ensure the value is single-line JSON (L-3). See development_lessons.md.")
        sys.exit(1)
    return Credentials.from_service_account_info(info, scopes=SCOPES)


def sheet_already_set_up(spreadsheet: gspread.Spreadsheet) -> bool:
    """Return True if all expected Phase 1 tabs already exist - idempotency check."""
    existing_titles = {ws.title for ws in spreadsheet.worksheets()}
    expected_titles = {tab_name for tab_name, _, _ in TABS}
    missing = expected_titles - existing_titles
    if not missing:
        print("All Phase 1 tabs already exist. Sheet is already set up.")
        return True
    print(f"Missing tabs: {sorted(missing)}. Proceeding with setup.")
    return False


def create_tab(
    spreadsheet: gspread.Spreadsheet,
    tab_name: str,
    headers: list,
    seed_rows: list,
) -> None:
    """Add tab, write headers in row 1, optionally write seed rows. L-4: batch write."""
    existing_titles = {ws.title for ws in spreadsheet.worksheets()}

    if tab_name in existing_titles:
        print(f"  Tab '{tab_name}' already exists - skipping.")
        return

    print(f"  Creating tab: {tab_name}")
    ws = spreadsheet.add_worksheet(title=tab_name, rows=500, cols=len(headers) + 2)
    time.sleep(0.5)  # brief pause after worksheet creation

    # Write headers - row 1 is sacred.
    ws.update("A1", [headers])
    time.sleep(BATCH_SLEEP)

    if seed_rows:
        # L-4: bulk write in one call, not a loop.
        ws.append_rows(seed_rows, value_input_option="USER_ENTERED")
        time.sleep(BATCH_SLEEP)
        print(f"    Headers + {len(seed_rows)} seed rows written.")
    else:
        print(f"    Headers written. No seed rows.")


def remove_default_sheet(spreadsheet: gspread.Spreadsheet) -> None:
    """Remove the default 'Sheet1' tab if present and other tabs exist."""
    try:
        default = spreadsheet.worksheet("Sheet1")
        if len(spreadsheet.worksheets()) > 1:
            spreadsheet.del_worksheet(default)
            print("  Removed default 'Sheet1' tab.")
    except gspread.exceptions.WorksheetNotFound:
        pass  # Already removed or never existed.


def update_config_yaml(sheet_id: str) -> None:
    """Replace sheet_id: \"\" with the actual sheet_id in config.yaml."""
    config_path = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "config.yaml")
    )
    if not os.path.exists(config_path):
        print(f"WARNING: config.yaml not found at {config_path}. Cannot update sheet_id.")
        return

    with open(config_path, "r") as f:
        content = f.read()

    old = 'sheet_id: ""'
    new = f'sheet_id: "{sheet_id}"'
    if old in content:
        content = content.replace(old, new, 1)
        with open(config_path, "w") as f:
            f.write(content)
        print(f"  config.yaml updated: sheet_id = {sheet_id}")
    elif sheet_id in content:
        print(f"  config.yaml already contains sheet_id = {sheet_id}. No change needed.")
    else:
        print(f"  WARNING: Could not locate 'sheet_id: \"\"' in config.yaml.")
        print(f"  Set manually: sheet_id: \"{sheet_id}\"")


def update_env_sheet_id(sheet_id: str) -> None:
    """Update VKH_SHEET_ID in the .env file at repo root."""
    repo_root = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
    env_path = os.path.join(repo_root, ".env")
    if not os.path.exists(env_path):
        print(f"  No .env at {env_path}. Set VKH_SHEET_ID={sheet_id} manually.")
        return

    with open(env_path, "r") as f:
        lines = f.readlines()

    updated = False
    new_lines = []
    for line in lines:
        if line.startswith("VKH_SHEET_ID="):
            new_lines.append(f"VKH_SHEET_ID={sheet_id}\n")
            updated = True
        else:
            new_lines.append(line)

    if not updated:
        new_lines.append(f"VKH_SHEET_ID={sheet_id}\n")

    with open(env_path, "w") as f:
        f.writelines(new_lines)
    print(f"  .env updated: VKH_SHEET_ID={sheet_id}")


def print_manual_create_instructions() -> None:
    """Print step-by-step instructions for Commander to create the sheet manually."""
    print()
    print("=" * 65)
    print("ACTION REQUIRED: create the VKH_Data sheet manually")
    print("=" * 65)
    print()
    print("The service account cannot create Google Drive files directly.")
    print("(velvet-trade-watch GCP project - known constraint, see L-5)")
    print()
    print("Steps:")
    print("  1. Open Google Sheets: https://sheets.google.com")
    print("     Log in as seouldesk.help@gmail.com")
    print()
    print("  2. Create a new blank spreadsheet.")
    print("     Name it exactly: VKH_Data")
    print()
    print("  3. Share it with the service account as Editor:")
    print(f"     Email: {SERVICE_ACCOUNT_EMAIL}")
    print()
    print("  4. Copy the sheet ID from the URL:")
    print("     https://docs.google.com/spreadsheets/d/COPY_THIS_PART/edit")
    print()
    print("  5. Re-run this script with the sheet ID:")
    print("     PYTHONPATH=. python scripts/setup_sheets.py --sheet-id <ID>")
    print()
    print("=" * 65)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Set up VKH_Data Google Sheet schema with all Phase 1 tabs."
    )
    parser.add_argument(
        "--sheet-id",
        help="Google Sheet ID of the pre-created VKH_Data sheet.",
    )
    args = parser.parse_args()

    # L-2: load .env from repo root.
    repo_root = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
    env_path = os.path.join(repo_root, ".env")
    if os.path.exists(env_path):
        load_dotenv(env_path)
        print(f"Loaded .env from: {env_path}")
    else:
        print(f"No .env at {env_path} - relying on environment variables already set.")

    # Load credentials (L-3: single-line JSON only).
    creds = load_credentials()
    gc = gspread.authorize(creds)

    # Resolve sheet ID: CLI arg > config.yaml > stop and print instructions.
    sheet_id = args.sheet_id

    if not sheet_id:
        # Check config.yaml for a previously recorded sheet_id.
        config_path = os.path.normpath(os.path.join(repo_root, "config.yaml"))
        if os.path.exists(config_path):
            with open(config_path, "r") as f:
                for line in f:
                    stripped = line.strip()
                    if stripped.startswith("sheet_id:"):
                        val = stripped.split(":", 1)[1].strip().strip('"').strip("'")
                        if val:
                            sheet_id = val
                            print(f"Using sheet_id from config.yaml: {sheet_id}")
                            break

    if not sheet_id:
        print_manual_create_instructions()
        sys.exit(0)

    # Open the sheet by key.
    print(f"Opening sheet: {sheet_id}")
    try:
        spreadsheet = gc.open_by_key(sheet_id)
        print(f"  Connected to: '{spreadsheet.title}'")
    except gspread.exceptions.SpreadsheetNotFound:
        print(f"ERROR: Sheet {sheet_id} not found or service account not shared as Editor.")
        print(f"  Share the sheet with: {SERVICE_ACCOUNT_EMAIL}")
        sys.exit(1)

    # Idempotency check.
    if sheet_already_set_up(spreadsheet):
        print(f"\nSheet ID: {sheet_id}")
        print("No changes made.")
        return

    # Create all Phase 1 tabs (L-4: batch writes inside create_tab).
    print("\nCreating Phase 1 tabs ...")
    for tab_name, headers, seed_rows in TABS:
        create_tab(spreadsheet, tab_name, headers, seed_rows)

    # Remove the default empty Sheet1.
    remove_default_sheet(spreadsheet)

    # Update config.yaml and .env with the real sheet_id.
    print("\nRecording sheet_id ...")
    update_config_yaml(sheet_id)
    update_env_sheet_id(sheet_id)

    # Row count summary for seeded tabs.
    print("\nSeeded tab row counts:")
    for tab_name, _, seed_rows in TABS:
        if seed_rows:
            print(f"  {tab_name}: {len(seed_rows)} seed rows")

    # Summary.
    print(f"\n{'='*60}")
    print(f"VKH_Data sheet schema setup complete.")
    print(f"Sheet ID : {sheet_id}")
    print(f"Sheet URL: https://docs.google.com/spreadsheets/d/{sheet_id}")
    print(f"{'='*60}")
    print("\nNext step: verify all tabs visible in Google Sheets UI.")


if __name__ == "__main__":
    main()
