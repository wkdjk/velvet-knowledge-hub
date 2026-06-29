# Run as: PYTHONPATH=. python scripts/classify_articles.py [--dry-run] [--limit N] [--async]
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
#   PYTHONPATH=. python scripts/classify_articles.py --async
#
# L-1: PYTHONPATH=. ensures repo root is importable.
# L-2: .env must be at repo root (/Users/Qs/C/velvet-knowledge-hub/.env).
# L-3: GOOGLE_SERVICE_ACCOUNT_JSON must be single-line JSON in .env.
# L-4: get_all_records() called once; batch_update called once per batch of rows.
# L-11: ANTHROPIC_API_KEY validated — must start with "sk-ant-api03-".
#
# AI model: claude-haiku-4-5-20251001
# Sync rate limiting: 0.5s sleep between API calls.
# Async mode: asyncio.Semaphore(20) caps concurrency at 20 simultaneous requests.
#
# In-place update: uses gspread worksheet.batch_update() with A1 cell ranges.
# Header is row 1 → first data row is row 2.
#
# Security: no credentials or secrets in this file. All secrets from .env only.

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import gspread
from gspread.utils import rowcol_to_a1

# ---------------------------------------------------------------------------
# L-1: ensure repo root is on sys.path.
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.sheets_auth import _load_config, connect_sheets, resolve_sheet_id  # noqa: E402

logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger("classify_articles")

# Force unbuffered stdout so progress prints appear in real time when piped.
sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TARGET_TAB = "KVN_Articles"

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

# Async mode: max concurrent API requests.
# Set conservatively — the account limit is 50 RPM on Haiku.
# With Semaphore(3) and a token bucket at 40 tokens/min we stay well under quota.
_ASYNC_CONCURRENCY = 3

# Token bucket: max requests per minute for async mode.
# Set to 40 to leave 10 RPM headroom below the 50 RPM account limit.
_ASYNC_RPM_LIMIT = 40

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
# Anthropic API helpers — sync
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


def _parse_classification_response(raw_text: str, title_ko: str) -> dict:
    """
    Parse and normalise a raw JSON string from the Haiku classifier.

    Shared between sync and async paths. Returns a classification dict with
    keys: category, english_summary, include_on_site, _error.
    """
    # Strip markdown code fences if present (```json ... ``` or ``` ... ```).
    if raw_text.startswith("```"):
        raw_text = raw_text.strip("`").strip()
        if raw_text.lower().startswith("json"):
            raw_text = raw_text[4:].strip()

    result = json.loads(raw_text)

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


def classify_article(
    client: anthropic.Anthropic,
    title_ko: str,
    description: str,
) -> dict:
    """
    Call Haiku 4.5 to classify a single article (sync path).

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
        return _parse_classification_response(raw_text, title_ko)

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


# ---------------------------------------------------------------------------
# Anthropic API helpers — async
# ---------------------------------------------------------------------------

async def _classify_article_async(
    client: anthropic.AsyncAnthropic,
    semaphore: asyncio.Semaphore,
    sheet_row_num: int,
    row: dict,
    counter: list,
    counter_lock: asyncio.Lock,
    total: int,
) -> tuple[int, dict]:
    """
    Classify a single article asynchronously, bounded by semaphore.

    Returns (sheet_row_num, classification_result).
    Increments shared counter and prints progress every 100 rows.
    On any failure, returns safe defaults and sets ai_processed_at so the
    row is not reprocessed on the next run.
    """
    # C-5h fix: live KVN_Articles sheet has data written to wrong columns.
    # 'url' column holds the Korean article title; 'content_hash' holds description.
    title_ko = str(row.get("url") or row.get("title_ko", "")).strip()
    description = str(row.get("content_hash", "")).strip()
    user_msg = f"Title: {title_ko}\nDescription: {description}"

    async with semaphore:
        try:
            message = await client.messages.create(
                model=_HAIKU_MODEL,
                max_tokens=256,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_msg}],
            )
            raw_text = message.content[0].text.strip()
            result = _parse_classification_response(raw_text, title_ko)

        except json.JSONDecodeError as exc:
            logger.warning("JSON parse error for title='%s': %s", title_ko[:60], exc)
            result = {"category": "기타", "english_summary": "", "include_on_site": False, "_error": True}
        except anthropic.APIError as exc:
            logger.warning("Anthropic API error for title='%s': %s", title_ko[:60], exc)
            result = {"category": "기타", "english_summary": "", "include_on_site": False, "_error": True}
        except Exception as exc:  # noqa: BLE001
            logger.warning("Unexpected error for title='%s': %s", title_ko[:60], exc)
            result = {"category": "기타", "english_summary": "", "include_on_site": False, "_error": True}

    # Update shared progress counter (thread-safe via asyncio.Lock).
    async with counter_lock:
        counter[0] += 1
        done = counter[0]
        if done % 100 == 0 or done == total:
            print(f"  classified {done}/{total} rows...")

    return sheet_row_num, result


class _TokenBucket:
    """
    Simple async token bucket for rate limiting.

    Replenishes at rate tokens/second. Each acquire() waits until a token
    is available. Thread-safe via asyncio.Lock.
    """

    def __init__(self, rate_per_minute: int) -> None:
        self._rate = rate_per_minute / 60.0  # tokens per second
        self._tokens = float(rate_per_minute)
        self._max_tokens = float(rate_per_minute)
        self._last_refill = asyncio.get_event_loop().time()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Block until a token is available, then consume one token."""
        while True:
            async with self._lock:
                now = asyncio.get_event_loop().time()
                elapsed = now - self._last_refill
                self._tokens = min(
                    self._max_tokens,
                    self._tokens + elapsed * self._rate,
                )
                self._last_refill = now

                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return  # token acquired

            # No token available — wait a small amount then retry.
            wait_seconds = (1.0 - self._tokens) / self._rate
            await asyncio.sleep(max(0.05, wait_seconds))


async def _classify_all_async(
    api_key: str,
    unprocessed: list[tuple[int, dict]],
) -> list[tuple[int, dict]]:
    """
    Classify all unprocessed rows concurrently using AsyncAnthropic.

    Rate-limiting strategy:
    - asyncio.Semaphore(_ASYNC_CONCURRENCY) caps simultaneous open connections.
    - _TokenBucket(_ASYNC_RPM_LIMIT) enforces requests-per-minute to stay
      within the account's 50 RPM Haiku limit.

    Results are collected in original order via asyncio.gather (index-preserving).
    Returns list of (sheet_row_num, classification_result) in input order.
    """
    client = anthropic.AsyncAnthropic(api_key=api_key)
    semaphore = asyncio.Semaphore(_ASYNC_CONCURRENCY)
    bucket = _TokenBucket(_ASYNC_RPM_LIMIT)
    counter = [0]  # mutable container for shared counter
    counter_lock = asyncio.Lock()
    total = len(unprocessed)

    async def _rate_limited_task(sheet_row_num: int, row: dict) -> tuple[int, dict]:
        # Acquire rate-limit token before acquiring semaphore (avoids holding
        # the semaphore slot while waiting for a rate-limit token).
        await bucket.acquire()
        return await _classify_article_async(
            client=client,
            semaphore=semaphore,
            sheet_row_num=sheet_row_num,
            row=row,
            counter=counter,
            counter_lock=counter_lock,
            total=total,
        )

    tasks = [
        _rate_limited_task(sheet_row_num, row)
        for sheet_row_num, row in unprocessed
    ]

    # gather preserves order — index i in results corresponds to tasks[i].
    results = await asyncio.gather(*tasks)
    await client.close()
    return list(results)


# ---------------------------------------------------------------------------
# Shared write-back helper
# ---------------------------------------------------------------------------

def _write_results_to_sheets(
    ws,
    results: list[tuple[int, dict]],
    processed_at_ts: str,
) -> int:
    """
    Write classification results back to Google Sheets via chunked batch_update.

    Returns the number of rows written.
    Splits into chunks of 200 rows (800 cell dicts each) with 1.1s inter-chunk
    sleep to stay within Sheets write quota (300 write requests/min per project).
    """
    _WRITE_CHUNK_ROWS = 200
    cells_per_row = 4  # category + english_summary + ai_processed_at + include_on_site

    cell_updates: list[dict] = []

    for sheet_row_num, cls in results:
        include_val = "TRUE" if cls["include_on_site"] else "FALSE"

        cell_updates.append({
            "range": rowcol_to_a1(sheet_row_num, _COL_CATEGORY + 1),
            "values": [[cls["category"]]],
        })
        cell_updates.append({
            "range": rowcol_to_a1(sheet_row_num, _COL_ENGLISH_SUMMARY + 1),
            "values": [[cls["english_summary"]]],
        })
        cell_updates.append({
            "range": rowcol_to_a1(sheet_row_num, _COL_AI_PROCESSED_AT + 1),
            "values": [[processed_at_ts]],
        })
        cell_updates.append({
            "range": rowcol_to_a1(sheet_row_num, _COL_INCLUDE_ON_SITE + 1),
            "values": [[include_val]],
        })

    chunk_size = _WRITE_CHUNK_ROWS * cells_per_row
    total_chunks = (len(cell_updates) + chunk_size - 1) // chunk_size

    for chunk_idx in range(total_chunks):
        chunk = cell_updates[chunk_idx * chunk_size: (chunk_idx + 1) * chunk_size]
        ws.batch_update(chunk, value_input_option="USER_ENTERED")
        print(f"  wrote chunk {chunk_idx + 1}/{total_chunks} ({len(chunk) // cells_per_row} rows)...")
        if chunk_idx < total_chunks - 1:
            time.sleep(1.1)

    return len(results)


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
    parser.add_argument(
        "--async",
        dest="async_mode",
        action="store_true",
        help=(
            "Use AsyncAnthropic with Semaphore(3) + token bucket (40 RPM) "
            "for rate-limited concurrent classification. "
            "Sync path (default) uses 0.5s sleep between calls."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Re-classify ALL rows, including those already processed. "
            "Use after a failed run where rows were written with error defaults."
        ),
    )
    args = parser.parse_args()

    print("classify_articles.py — VKH article AI classification")
    print(f"  model: {_HAIKU_MODEL}")
    print(f"  mode: {'async (Semaphore 3, 40 RPM bucket)' if args.async_mode else 'sync (0.5s sleep)'}")
    print(f"  dry-run: {args.dry_run}")
    print(f"  force: {args.force}")
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
        if args.force or _is_unprocessed(row):
            sheet_row_number = idx + 2  # header is row 1
            unprocessed.append((sheet_row_number, row))

    label = "all rows (--force)" if args.force else "unprocessed rows (empty ai_processed_at)"
    print(f"  {label}: {len(unprocessed)}")

    if not unprocessed:
        print("  All rows already classified — nothing to do.")
        print("articles_processed: 0 | written_to_sheets: 0 | errors: 0")
        sys.exit(0)

    # Apply --limit if set.
    if args.limit is not None:
        unprocessed = unprocessed[: args.limit]
        print(f"  applying --limit: processing {len(unprocessed)} rows")

    processed_at_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # --- ASYNC PATH -----------------------------------------------------------
    if args.async_mode:
        print(f"  launching async classification ({_ASYNC_CONCURRENCY} concurrent)...")
        t_start = time.monotonic()

        results: list[tuple[int, dict]] = asyncio.run(
            _classify_all_async(anthropic_key, unprocessed)
        )

        elapsed = time.monotonic() - t_start
        errors = sum(1 for _, cls in results if cls.get("_error"))
        articles_processed = len(results)

        print(f"  async classification done in {elapsed:.1f}s — {articles_processed} rows, {errors} errors")

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

        written_to_sheets = _write_results_to_sheets(ws, results, processed_at_ts)

        print()
        print(f"  DONE: {written_to_sheets} rows updated in '{TARGET_TAB}'.")
        print(
            f"articles_processed: {articles_processed} | "
            f"written_to_sheets: {written_to_sheets} | "
            f"errors: {errors}"
        )
        return

    # --- SYNC PATH (default) --------------------------------------------------
    # Unchanged from original implementation. --dry-run and --limit work here too.

    ai_client = anthropic.Anthropic(api_key=anthropic_key)
    sync_results: list[tuple[int, dict]] = []
    errors = 0

    for i, (sheet_row_num, row) in enumerate(unprocessed):
        # C-5h fix: live KVN_Articles sheet has data written to wrong columns by
        # the original collector. The header names do not match the stored values:
        #   'title' column (col B) → holds the article URL (http://...)
        #   'url' column (col C)   → holds the Korean article title text
        #   'content_hash' (col D) → holds the article description/content snippet
        # Verified 2026-06-05 by direct row inspection (all 5,586 rows confirmed).
        # Read 'url' for the Korean title and 'content_hash' for description.
        title_ko = str(row.get("url") or row.get("title_ko", "")).strip()
        description = str(row.get("content_hash", "")).strip()

        classification = classify_article(ai_client, title_ko, description)

        if classification.get("_error"):
            errors += 1

        sync_results.append((sheet_row_num, classification))

        # Print progress every 10 rows.
        if (i + 1) % 10 == 0 or (i + 1) == len(unprocessed):
            print(f"  classified {i + 1}/{len(unprocessed)} rows...")

        # Rate limiting: sleep between calls (not after the last one).
        if i < len(unprocessed) - 1:
            time.sleep(_API_SLEEP_SECONDS)

    articles_processed = len(sync_results)

    if args.dry_run:
        print()
        print("[DRY RUN] Classification complete — no Sheets write.")
        print("  First 5 results:")
        for sheet_row_num, cls in sync_results[:5]:
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

    written_to_sheets = _write_results_to_sheets(ws, sync_results, processed_at_ts)

    print()
    print(f"  DONE: {written_to_sheets} rows updated in '{TARGET_TAB}'.")
    print(
        f"articles_processed: {articles_processed} | "
        f"written_to_sheets: {written_to_sheets} | "
        f"errors: {errors}"
    )


if __name__ == "__main__":
    main()
