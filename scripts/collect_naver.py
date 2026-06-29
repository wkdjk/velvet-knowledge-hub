# Run as: PYTHONPATH=. python scripts/collect_naver.py [--dry-run] [--limit N]
#
# collect_naver.py — Velvet Knowledge Hub Naver News API collection script
#
# What this script does:
#   1. Reads the keyword list from the _keywords Sheets tab (one API call).
#      If the tab is empty, seeds it with the default list and uses those keywords.
#   2. For each keyword, calls the Naver News API (display=100, sort=date).
#   3. Deduplicates articles using content_hash = sha256(url + title_ko)[:16].
#   4. Reads all existing KVN_Articles rows in ONE call (L-4 — never loop).
#   5. Appends only new rows using append_rows() (L-4 — single bulk write).
#
# Columns populated by this script (AI columns left blank for B-9):
#   content_hash | url | title_ko | description | published_date |
#   source_name | source_type | keyword_matched
#
# Security: no credentials in this file. All secrets from .env or environment.
# L-1: Run with PYTHONPATH=. to resolve project-level imports.
# L-2: .env must be in the repo root, not in Q-Submarine.
# L-3: GOOGLE_SERVICE_ACCOUNT_JSON must be a single-line JSON string.
# L-4: Sheets API called once for reads; once for writes — never inside a loop.

import argparse
import hashlib
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import gspread
import requests

# ---------------------------------------------------------------------------
# L-1: ensure repo root is on sys.path.
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.sheets_auth import FULL_SCOPES, _load_config, connect_sheets, resolve_sheet_id  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent

NAVER_API_URL = "https://openapi.naver.com/v1/search/news.json"
_HTML_TAG = re.compile(r"<[^>]+>")
_HTML_ENTITIES = {
    "&amp;": "&",
    "&lt;": "<",
    "&gt;": ">",
    "&quot;": '"',
    "&#39;": "'",
}

# Schema for the KVN_Articles tab — column order must match the Sheets header row.
# OI-3: added source_domain — publisher domain extracted from originallink
# (e.g. "yna.co.kr", "hankyung.com"). Populated for all new rows; existing
# rows are not backfilled. Do NOT backfill — the 5,586 existing rows keep
# empty source_domain by design.
KVN_ARTICLES_HEADER = [
    "content_hash",
    "url",
    "title_ko",
    "description",
    "published_date",
    "source_name",
    "source_type",
    "keyword_matched",
    "source_domain",
    "category",
    "english_summary",
    "ai_processed_at",
    "include_on_site",
]

# Schema for the _keywords tab.
KEYWORDS_HEADER = ["term", "type", "language"]

# Default keyword list — used when _keywords tab is empty.
# Also used to seed the tab on first run.
DEFAULT_KEYWORDS = [
    ("녹용", "primary", "ko"),
    ("뉴질랜드 녹용", "compound", "ko"),
    ("deer velvet Korea", "compound", "en"),
    ("녹용 수입", "compound", "ko"),
    ("녹용 건강기능식품", "compound", "ko"),
]

# Batch write size for append_rows (L-4: bulk write, never loop).
WRITE_BATCH_SIZE = 200
BATCH_SLEEP_SECONDS = 1.1


# ---------------------------------------------------------------------------
# Helpers — HTML cleaning and date parsing (reused from KVN naver.py)
# ---------------------------------------------------------------------------

def _clean(text: str) -> str:
    """Strip HTML tags and decode common HTML entities."""
    text = _HTML_TAG.sub("", text)
    for entity, char in _HTML_ENTITIES.items():
        text = text.replace(entity, char)
    return text.strip()


def _parse_date(pub_date: str) -> str:
    """Parse Naver pubDate string to YYYY-MM-DD. Returns '' on failure."""
    try:
        dt = datetime.strptime(pub_date, "%a, %d %b %Y %H:%M:%S %z")
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return ""


def _source_name_from_url(url: str) -> str:
    """Extract a short domain label from a URL (strip www.)."""
    try:
        return urlparse(url).netloc.replace("www.", "")
    except Exception:
        return ""


def _content_hash(url: str, title_ko: str) -> str:
    """Stable 16-char dedup key: sha256(url + title_ko)[:16]."""
    raw = (url + title_ko).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Step 1 — Configuration and credentials
# ---------------------------------------------------------------------------

def load_config() -> dict:
    """Read config.yaml from repo root."""
    return _load_config()


def get_naver_credentials() -> tuple[str, str]:
    """Return (client_id, client_secret) from environment. Exits if missing."""
    client_id = os.environ.get("NAVER_CLIENT_ID", "").strip()
    client_secret = os.environ.get("NAVER_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        print(
            "ERROR: NAVER_CLIENT_ID and NAVER_CLIENT_SECRET must be set in .env "
            "or GitHub Secrets.\n"
            "  Reuse the credentials from the KVN project — do NOT create a new app.",
            file=sys.stderr,
        )
        sys.exit(1)
    return client_id, client_secret


# ---------------------------------------------------------------------------
# Step 2 — Keyword management
# ---------------------------------------------------------------------------

def _get_or_create_worksheet(sheet, tab_name: str, headers: list[str]):
    """
    Return the named worksheet, creating it with the given headers if absent.
    Uses batchUpdate for creation (L-5: Drive API absent on velvet-trade-watch GCP).
    """
    all_ws = {ws.title: ws for ws in sheet.worksheets()}
    if tab_name in all_ws:
        return all_ws[tab_name]

    print(f"  Creating missing tab: {tab_name}")
    ws = sheet.add_worksheet(title=tab_name, rows=200, cols=len(headers))
    ws.append_row(headers)
    return ws


def read_keywords(sheet, limit: int | None = None) -> list[str]:
    """
    Read keyword terms from the _keywords tab (one API call — L-4).
    If the tab is empty (no data rows beyond header), seed it with defaults
    and return the default term list.
    Returns a plain list of term strings.
    """
    keywords_ws = _get_or_create_worksheet(sheet, "_keywords", KEYWORDS_HEADER)

    # L-4: single bulk read.
    rows = keywords_ws.get_all_records()

    if rows:
        terms = [str(r.get("term", "")).strip() for r in rows if r.get("term")]
        terms = [t for t in terms if t]
        if terms:
            if limit:
                terms = terms[:limit]
            print(f"  keywords: {len(terms)} loaded from _keywords tab")
            return terms

    # Tab is empty — seed it with defaults (one bulk write).
    print("  _keywords tab is empty — seeding with defaults")
    seed_rows = [list(row) for row in DEFAULT_KEYWORDS]
    keywords_ws.append_rows(seed_rows, value_input_option="RAW")
    print(f"  seeded {len(seed_rows)} keywords")

    terms = [row[0] for row in DEFAULT_KEYWORDS]
    if limit:
        terms = terms[:limit]
    return terms


# ---------------------------------------------------------------------------
# Step 3 — Naver News API collection
# ---------------------------------------------------------------------------

def search_naver(keyword: str, client_id: str, client_secret: str) -> list[dict]:
    """
    Call the Naver News API for one keyword (display=100, sort=date).
    Returns list of article dicts with normalised fields.
    Raises requests.HTTPError on non-2xx response.
    """
    headers = {
        "X-Naver-Client-Id": client_id,
        "X-Naver-Client-Secret": client_secret,
    }
    params = {"query": keyword, "display": 100, "sort": "date"}
    resp = requests.get(NAVER_API_URL, headers=headers, params=params, timeout=10)
    resp.raise_for_status()

    articles = []
    for item in resp.json().get("items", []):
        # OI-3: use originallink (publisher URL) as preferred URL source.
        # Naver API always returns originallink for news items.
        # _source_name_from_url() extracts the domain (strips www.) from it,
        # giving the publisher domain (e.g. "yna.co.kr"), not the search platform.
        original_url = item.get("originallink", "")
        link_url = item.get("link", "")
        url = original_url or link_url
        source_domain = _source_name_from_url(original_url) if original_url else ""
        title_ko = _clean(item.get("title", ""))
        articles.append({
            "url": url,
            "title_ko": title_ko,
            "description": _clean(item.get("description", "")),
            "published_date": _parse_date(item.get("pubDate", "")),
            "source_name": _source_name_from_url(url),
            "source_domain": source_domain,
            "content_hash": _content_hash(url, title_ko),
            "keyword_matched": keyword,
        })
    return articles


def fetch_all_articles(
    keywords: list[str], client_id: str, client_secret: str
) -> list[dict]:
    """
    Call search_naver() for every keyword. Returns combined list.
    Adds a brief sleep between keywords to avoid Naver rate-limits.
    """
    all_articles: list[dict] = []
    for i, keyword in enumerate(keywords):
        try:
            results = search_naver(keyword, client_id, client_secret)
            all_articles.extend(results)
            print(f"  [{i + 1}/{len(keywords)}] '{keyword}': {len(results)} articles")
        except requests.HTTPError as exc:
            print(
                f"  WARNING: Naver API error for keyword '{keyword}' — {exc}",
                file=sys.stderr,
            )
        except requests.RequestException as exc:
            print(
                f"  WARNING: Network error for keyword '{keyword}' — {exc}",
                file=sys.stderr,
            )
        if i < len(keywords) - 1:
            time.sleep(0.5)  # polite pause between keyword requests
    return all_articles


# ---------------------------------------------------------------------------
# Step 4 — Deduplication
# ---------------------------------------------------------------------------

def dedup_against_sheet(
    articles: list[dict], existing_hashes: set[str]
) -> tuple[list[dict], int]:
    """
    Remove articles whose content_hash already exists in the sheet or appeared
    earlier in this batch.

    Returns (new_articles, skipped_count).
    """
    seen: set[str] = set()
    new_articles: list[dict] = []
    skipped = 0

    for article in articles:
        h = article["content_hash"]
        if h in existing_hashes or h in seen:
            skipped += 1
            continue
        seen.add(h)
        new_articles.append(article)

    return new_articles, skipped


# ---------------------------------------------------------------------------
# Step 5 — Sheets read and write
# ---------------------------------------------------------------------------

def read_existing_hashes(sheet) -> set[str]:
    """
    Read the KVN_Articles tab once (L-4). Extract all content_hash values.
    Returns an empty set if the tab is missing or has no rows.
    """
    articles_ws = _get_or_create_worksheet(sheet, "KVN_Articles", KVN_ARTICLES_HEADER)

    # L-4: single bulk read.
    rows = articles_ws.get_all_records()
    hashes = {str(r.get("content_hash", "")) for r in rows if r.get("content_hash")}
    print(f"  existing KVN_Articles rows: {len(rows)} | known hashes: {len(hashes)}")
    return hashes


def rows_to_write(articles: list[dict]) -> list[list]:
    """
    Convert article dicts to ordered rows matching KVN_ARTICLES_HEADER.
    AI columns (category, english_summary, ai_processed_at, include_on_site) are blank.

    OI-3: source_domain is populated from originallink domain (e.g. "yna.co.kr").
    This identifies the publishing media company, not the search platform.
    Only new rows receive source_domain — existing 5,586 rows are not backfilled.
    """
    output_rows = []
    for a in articles:
        row = [
            a.get("content_hash", ""),
            a.get("url", ""),
            a.get("title_ko", ""),
            a.get("description", ""),
            a.get("published_date", ""),
            a.get("source_name", ""),
            "naver_api",          # source_type — fixed for this script
            a.get("keyword_matched", ""),
            a.get("source_domain", ""),  # OI-3: publisher domain from originallink
            "",                   # category — filled by B-9
            "",                   # english_summary — filled by B-9
            "",                   # ai_processed_at — filled by B-9
            "",                   # include_on_site — filled by B-9
        ]
        output_rows.append(row)
    return output_rows


def write_new_articles(sheet, articles: list[dict], dry_run: bool) -> int:
    """
    Append new article rows to KVN_Articles in batches (L-4).
    Returns the number of rows written (0 on dry_run).
    """
    if not articles:
        print("  no new articles to write")
        return 0

    output_rows = rows_to_write(articles)

    if dry_run:
        print(f"  [dry-run] would write {len(output_rows)} rows to KVN_Articles")
        return 0

    articles_ws = _get_or_create_worksheet(sheet, "KVN_Articles", KVN_ARTICLES_HEADER)

    # Batch write in chunks of WRITE_BATCH_SIZE (L-4 pattern from VFI A-3).
    written = 0
    for i in range(0, len(output_rows), WRITE_BATCH_SIZE):
        batch = output_rows[i : i + WRITE_BATCH_SIZE]
        articles_ws.append_rows(batch, value_input_option="RAW")
        written += len(batch)
        if i + WRITE_BATCH_SIZE < len(output_rows):
            time.sleep(BATCH_SLEEP_SECONDS)

    return written


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect Naver News articles into VKH KVN_Articles Sheets tab."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and deduplicate but do not write to Sheets.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Only process first N keywords (for testing).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    print("collect_naver.py — VKH Naver News API collection")
    if args.dry_run:
        print("  mode: dry-run (no writes)")

    # --- Credentials ----------------------------------------------------------
    config = load_config()
    sheet_id = resolve_sheet_id(config)
    sheet = connect_sheets(sheet_id, scopes=FULL_SCOPES)
    client_id, client_secret = get_naver_credentials()
    print(f"  sheet: {sheet.title} ({sheet_id})")

    # --- Keywords -------------------------------------------------------------
    keywords = read_keywords(sheet, limit=args.limit)

    # --- Fetch ----------------------------------------------------------------
    all_articles = fetch_all_articles(keywords, client_id, client_secret)
    total_fetched = len(all_articles)

    # --- Dedup against sheet --------------------------------------------------
    existing_hashes = read_existing_hashes(sheet)
    new_articles, skipped = dedup_against_sheet(all_articles, existing_hashes)

    # --- Write ----------------------------------------------------------------
    written = write_new_articles(sheet, new_articles, dry_run=args.dry_run)

    # --- Summary line (required output format) --------------------------------
    print(
        f"keywords_searched: {len(keywords)} | "
        f"articles_fetched: {total_fetched} | "
        f"new_articles: {len(new_articles)} | "
        f"skipped_duplicates: {skipped}"
    )
    if args.dry_run:
        print("  dry-run complete — no rows written")
    else:
        print(f"  rows written to KVN_Articles: {written}")


if __name__ == "__main__":
    main()
