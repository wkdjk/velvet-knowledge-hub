# Run as: PYTHONPATH=. python scripts/classify_articles.py [--dry-run] [--limit N]
#
# classify_articles.py — AI classification of KVN_Articles rows for Velvet Knowledge Hub.
#
# Reads KVN_Articles rows with an empty ai_processed_at column, calls Haiku 4.5
# to classify each article, then updates category, english_summary, ai_processed_at,
# and include_on_site in-place via batch_update.
#
# Usage:
#   PYTHONPATH=. python scripts/classify_articles.py
#   PYTHONPATH=. python scripts/classify_articles.py --dry-run
#   PYTHONPATH=. python scripts/classify_articles.py --limit 10
#
# L-1: PYTHONPATH=. ensures repo root is importable.
# L-2: .env must be at repo root (/Users/Qs/C/velvet-knowledge-hub/.env).
# L-3: GOOGLE_SERVICE_ACCOUNT_JSON must be single-line JSON in .env.
# L-4: get_all_records() called once; batch_update called once per batch of rows.
# L-11: ANTHROPIC_API_KEY validated — must start with "sk-ant-api03-".
#
# AI model: claude-haiku-4-5-20251001
# Rate limiting: 0.5s sleep between API calls.
#
# In-place update: uses gspread worksheet.batch_update() with A1 cell ranges.
# Header is row 1 → first data row is row 2.
#
# Security: no credentials or secrets in this file. All secrets from .env only.

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
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
logger = logging.getLogger("classify_articles")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config.yaml"
TARGET_TAB = "KVN_Articles"

# Sheets API only — no Drive API required (L-5 workaround).
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Haiku model ID as specified in the brief.
_HAIKU_MODEL = "claude-haiku-4-5-20251001"

# KVN_Articles column indices (0-based, from actual live sheet schema).
# C-5g fix: original constants assumed collect_naver.py schema (12 columns)
# but the live sheet has 11 columns in a different order.
# Verified 2026-06-05 by reading ws.row_values(1) directly from the sheet.
#
# Actual header row (0-based):
#   article_id(0) | title(1) | url(2) | content_hash(3) | published_date(4) |
#   source(5) | category(6) | english_summary(7) | ai_processed_at(8) |
#   include_on_site(9) | crawled_at(10)
#
# Columns used to read input for classification:
_COL_TITLE_KO = 1       # 'title' column (B)
_COL_DESCRIPTION = None  # no separate description column; use title only
# Columns written by the classifier (0-based, A=0):
_COL_CATEGORY = 6        # 'category' (G)
_COL_ENGLISH_SUMMARY = 7  # 'english_summary' (H)
_COL_AI_PROCESSED_AT = 8  # 'ai_processed_at' (I)
_COL_INCLUDE_ON_SITE = 9  # 'include_on_site' (J)

# Valid category values — classifier must return one of these.
_VALID_CATEGORIES = frozenset([
    "규제정책", "무역시장", "건강제품", "수입유통", "업계소식", "기타"
])

# Rate limit pause between individual Claude API calls (brief specifies 0.5s).
_API_SLEEP_SECONDS = 0.5

# System prompt for Haiku classification.
_SYSTEM_PROMPT = (
    "You are a Korean deer velvet industry news classifier. "
    "Given a Korean news article title and description, return JSON with three fields:\n"
    "- category: one of [규제정책, 무역시장, 건강제품, 수입유통, 업계소식, 기타]\n"
    "- english_summary: 1-2 sentence English summary of the article\n"
    "- include_on_site: true if this article is relevant to the NZ deer velvet "
    "trade/import/market in Korea; false if it is off-topic\n"
    "Return ONLY valid JSON. No explanation."
)


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


def _col_letter(col_index: int) -> str:
    """Convert a 0-based column index to a Sheets column letter (A, B, ..., Z, AA, ...)."""
    result = ""
    n = col_index + 1  # 1-based
    while n > 0:
        n, remainder = divmod(n - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _cell_ref(row_number: int, col_index: int) -> str:
    """Return an A1 cell reference, e.g. row_number=2, col_index=8 → 'I2'."""
    return f"{_col_letter(col_index)}{row_number}"


# ---------------------------------------------------------------------------
# Anthropic API helpers
# ---------------------------------------------------------------------------

def validate_api_key(api_key: str) -> None:
    """
    L-11: Validate ANTHROPIC_API_KEY prefix.

    A valid key starts with 'sk-ant-api03-'. Exits with code 1 if invalid.
    """
    if not api_key.startswith("sk-ant-api03-"):
        print(
            "ERROR: ANTHROPIC_API_KEY does not start with 'sk-ant-api03-'.\n"
            "  This indicates a corrupted paste or wrong key.\n"
            "  Check: echo $ANTHROPIC_API_KEY | cut -c1-14  (must print 'sk-ant-api03-')\n"
            "  Reissue at: https://console.anthropic.com/settings/keys",
            file=sys.stderr,
        )
        sys.exit(1)


def classify_article(
    client: anthropic.Anthropic,
    title_ko: str,
    description: str,
) -> dict:
    """
    Call Haiku 4.5 to classify a single article.

    Returns a dict with keys: category, english_summary, include_on_site.
    On any failure (API error, JSON parse error), returns safe defaults and
    logs a warning. The caller must still write ai_processed_at so the row
    is not reprocessed.
    """
    user_msg = f"Title: {title_ko}\nDescription: {description}"

    try:
        message = client.messages.create(
            model=_HAIKU_MODEL,
            max_tokens=256,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw_text = message.content[0].text.strip()

        # Strip markdown code fences if present (```json ... ``` or ``` ... ```).
        if raw_text.startswith("```"):
            raw_text = raw_text.strip("`").strip()
            if raw_text.lower().startswith("json"):
                raw_text = raw_text[4:].strip()

        result = json.loads(raw_text)

    except json.JSONDecodeError as exc:
        logger.warning(
            "JSON parse error for title='%s': %s", title_ko[:60], exc
        )
        return {"category": "기타", "english_summary": "", "include_on_site": False, "_error": True}
    except anthropic.APIError as exc:
        logger.warning(
            "Anthropic API error for title='%s': %s", title_ko[:60], exc
        )
        return {"category": "기타", "english_summary": "", "include_on_site": False, "_error": True}
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Unexpected error for title='%s': %s", title_ko[:60], exc
        )
        return {"category": "기타", "english_summary": "", "include_on_site": False, "_error": True}

    # Normalise category — ensure it is one of the valid values.
    category = str(result.get("category", "기타")).strip()
    if category not in _VALID_CATEGORIES:
        logger.warning(
            "Invalid category '%s' returned for title='%s' — defaulting to '기타'",
            category, title_ko[:60],
        )
        category = "기타"

    english_summary = str(result.get("english_summary", "")).strip()

    # include_on_site — accept bool or string.
    raw_include = result.get("include_on_site", False)
    if isinstance(raw_include, bool):
        include_on_site = raw_include
    else:
        include_on_site = str(raw_include).strip().lower() in ("true", "yes", "1")

    return {
        "category": category,
        "english_summary": english_summary,
        "include_on_site": include_on_site,
        "_error": False,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Classify unprocessed KVN_Articles rows using Haiku 4.5 and "
            "write results back to Google Sheets in-place."
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Classify but do not write back to Sheets; print first 5 results.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Only process the first N unprocessed rows (for testing).",
    )
    args = parser.parse_args()

    print("classify_articles.py — VKH article AI classification")
    print(f"  model: {_HAIKU_MODEL}")
    print(f"  dry-run: {args.dry_run}")
    if args.limit:
        print(f"  limit: {args.limit}")

    # L-2: load .env from repo root.
    load_dotenv(REPO_ROOT / ".env")

    # L-11: validate ANTHROPIC_API_KEY prefix before any API call.
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not anthropic_key:
        print(
            "ERROR: ANTHROPIC_API_KEY is not set.\n"
            "  Local dev: add to .env at the repo root.\n"
            "  GitHub Actions: add to repository Secrets.",
            file=sys.stderr,
        )
        sys.exit(1)
    validate_api_key(anthropic_key)

    # --- Connect to Sheets ----------------------------------------------------
    sheet_id = resolve_sheet_id()
    print(f"  sheet_id: {sheet_id}")

    spreadsheet = connect_sheets(sheet_id)
    print(f"  sheet title: {spreadsheet.title}")

    # Locate KVN_Articles tab (L-12: graceful skip if missing).
    try:
        ws = spreadsheet.worksheet(TARGET_TAB)
    except gspread.exceptions.WorksheetNotFound:
        print(
            f"ERROR: tab '{TARGET_TAB}' not found in sheet {sheet_id}.\n"
            "  Run scripts/setup_sheets.py first, then collect_naver.py to populate it.",
            file=sys.stderr,
        )
        sys.exit(1)

    # --- Read all rows once (L-4) ---------------------------------------------
    # get_all_records() returns list of dicts keyed by header row.
    all_rows = ws.get_all_records()
    total_rows = len(all_rows)
    print(f"  total KVN_Articles rows: {total_rows}")

    if total_rows == 0:
        print("  Tab is empty — nothing to classify.")
        print("articles_processed: 0 | written_to_sheets: 0 | errors: 0")
        sys.exit(0)

    # --- Filter to unprocessed rows (ai_processed_at is empty) ----------------
    # Row number in Sheets: header = row 1, first data row = row 2.
    # Index in all_rows list: 0-based. Sheet row = list_index + 2.
    #
    # C-5g fix: live sheet column is 'ai_processed_at' (col I). Due to the
    # prior column mismatch, this column currently holds category values
    # (e.g. "기타") for all rows. After the C-5g re-classification run,
    # it will hold ISO timestamps. A row is treated as unprocessed when
    # ai_processed_at is empty OR is a category value (not a timestamp).
    # Detection: a valid timestamp starts with a 4-digit year, e.g. "2026-".
    def _is_unprocessed(row: dict) -> bool:
        val = str(row.get("ai_processed_at", "")).strip()
        if not val:
            return True
        # If it looks like a timestamp (YYYY-...) it was written by this script.
        import re as _re
        return not bool(_re.match(r"^\d{4}-", val))

    unprocessed: list[tuple[int, dict]] = []
    for idx, row in enumerate(all_rows):
        if _is_unprocessed(row):
            sheet_row_number = idx + 2  # header is row 1
            unprocessed.append((sheet_row_number, row))

    print(f"  unprocessed rows (empty ai_processed_at): {len(unprocessed)}")

    if not unprocessed:
        print("  All rows already classified — nothing to do.")
        print("articles_processed: 0 | written_to_sheets: 0 | errors: 0")
        sys.exit(0)

    # Apply --limit if set.
    if args.limit is not None:
        unprocessed = unprocessed[: args.limit]
        print(f"  applying --limit: processing {len(unprocessed)} rows")

    # --- Classify and collect results -----------------------------------------
    ai_client = anthropic.Anthropic(api_key=anthropic_key)
    processed_at_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    results: list[tuple[int, dict]] = []  # (sheet_row_number, classification_result)
    errors = 0

    for i, (sheet_row_num, row) in enumerate(unprocessed):
        # C-5g fix: live sheet uses 'title' (not 'title_ko') and has no
        # 'description' column. Use 'title' as the primary input.
        # Fall back to 'title_ko' for forward-compatibility if schema changes.
        title_ko = str(row.get("title") or row.get("title_ko", "")).strip()
        description = str(row.get("description", "")).strip()

        classification = classify_article(ai_client, title_ko, description)

        if classification.get("_error"):
            errors += 1

        results.append((sheet_row_num, classification))

        # Print progress every 10 rows.
        if (i + 1) % 10 == 0 or (i + 1) == len(unprocessed):
            print(f"  classified {i + 1}/{len(unprocessed)} rows...")

        # Rate limiting: sleep between calls (not after the last one).
        if i < len(unprocessed) - 1:
            time.sleep(_API_SLEEP_SECONDS)

    articles_processed = len(results)

    if args.dry_run:
        print()
        print("[DRY RUN] Classification complete — no Sheets write.")
        print("  First 5 results:")
        for sheet_row_num, cls in results[:5]:
            print(
                f"    row {sheet_row_num}: category={cls['category']} | "
                f"include_on_site={cls['include_on_site']} | "
                f"summary={cls['english_summary'][:80]!r}"
            )
        print(
            f"articles_processed: {articles_processed} | "
            f"written_to_sheets: 0 | "
            f"errors: {errors}"
        )
        sys.exit(0)

    # --- Write results back to Sheets in-place (L-4: one batch_update call) ---
    # Build a list of cell update dicts for gspread batch_update.
    # Each dict: {"range": "I3", "values": [["value"]]}
    cell_updates: list[dict] = []

    for sheet_row_num, cls in results:
        include_val = "TRUE" if cls["include_on_site"] else "FALSE"

        cell_updates.append({
            "range": _cell_ref(sheet_row_num, _COL_CATEGORY),
            "values": [[cls["category"]]],
        })
        cell_updates.append({
            "range": _cell_ref(sheet_row_num, _COL_ENGLISH_SUMMARY),
            "values": [[cls["english_summary"]]],
        })
        cell_updates.append({
            "range": _cell_ref(sheet_row_num, _COL_AI_PROCESSED_AT),
            "values": [[processed_at_ts]],
        })
        cell_updates.append({
            "range": _cell_ref(sheet_row_num, _COL_INCLUDE_ON_SITE),
            "values": [[include_val]],
        })

    # Execute all cell updates in a single batch_update call (L-4).
    ws.batch_update(cell_updates, value_input_option="USER_ENTERED")
    written_to_sheets = articles_processed

    print()
    print(f"  DONE: {written_to_sheets} rows updated in '{TARGET_TAB}'.")
    print(
        f"articles_processed: {articles_processed} | "
        f"written_to_sheets: {written_to_sheets} | "
        f"errors: {errors}"
    )


if __name__ == "__main__":
    main()
