# sheets_auth.py — Shared Google Sheets auth helpers for VKH scripts.
#
# Extracted from build.py / ingest_*.py / classify_articles.py to eliminate
# copy-paste duplication across 8 scripts.
#
# Security: no credentials in this file. All secrets from environment only.

import json
import os
import sys
from pathlib import Path

import gspread
import yaml
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent

# Sheets-only scope (most scripts — L-5: no Drive API on velvet-trade-watch GCP).
SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Full scopes for scripts that also need Drive read access (build.py, collect_naver.py).
FULL_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def _load_config(path: Path | None = None) -> dict:
    """
    Read config.yaml from the repo root and return the full config dict.

    Exits with code 1 on missing file or YAML parse error.
    path defaults to REPO_ROOT / "config.yaml".
    """
    if path is None:
        path = REPO_ROOT / "config.yaml"
    if not path.exists():
        print(f"ERROR: config.yaml not found at {path}", file=sys.stderr)
        sys.exit(1)
    with path.open("r", encoding="utf-8") as fh:
        try:
            return yaml.safe_load(fh) or {}
        except yaml.YAMLError as exc:
            print(f"ERROR: Failed to parse config.yaml — {exc}", file=sys.stderr)
            sys.exit(1)


def connect_sheets(sheet_id: str, scopes: list[str] | None = None):
    """
    Load credentials from environment and connect to Google Sheets.

    L-2: loads .env from REPO_ROOT.
    L-3: GOOGLE_SERVICE_ACCOUNT_JSON must be single-line JSON.
    L-5: uses open_by_key() — no Drive API required.

    Returns a gspread.Spreadsheet object. Exits on any error.
    scopes defaults to SHEETS_SCOPES.
    """
    if scopes is None:
        scopes = SHEETS_SCOPES

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
            "  Tip: minify to one line with: "
            'python -c "import json,sys; print(json.dumps(json.load(sys.stdin), '
            'separators=(\',\',\':\')))" < key.json',
            file=sys.stderr,
        )
        sys.exit(1)

    creds = Credentials.from_service_account_info(sa_info, scopes=scopes)
    gc = gspread.authorize(creds)

    try:
        return gc.open_by_key(sheet_id)
    except gspread.exceptions.APIError as exc:
        print(
            f"ERROR: Could not open sheet {sheet_id} — {exc}\n"
            "  Check the service account has Editor access to the sheet.",
            file=sys.stderr,
        )
        sys.exit(1)


def resolve_sheet_id(config: dict | None = None) -> str:
    """
    Resolve the Google Sheet ID from environment then config.yaml fallback.

    Priority: VKH_SHEET_ID env var → config.get("sheet_id").
    If config is None, calls _load_config() internally.
    Exits with code 1 if neither source yields an ID.
    """
    load_dotenv(REPO_ROOT / ".env")

    sheet_id = os.environ.get("VKH_SHEET_ID", "").strip()
    if sheet_id:
        return sheet_id

    if config is None:
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
