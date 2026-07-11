# Run as: PYTHONPATH=. python scripts/collect_naver.py [--dry-run] [--limit N]
#
# collect_naver.py — Velvet Knowledge Hub Naver News API collection script
# (D3, Phase D rebuild, sqlite storage — 2026-07-11; collection logic ported
# as-is from the pre-rebuild Sheets version, only storage changed).
#
# What this script does:
#   1. Reads the keyword list from the _keywords Sheets tab (one API call).
#      Stays Sheets-native — a handful of Commander-edited search terms, not
#      collected data (proposal §6b, resolved item 6).
#   2. For each keyword, calls the Naver News API (display=100, sort=date).
#   3. Deduplicates articles using content_hash = sha256(url + title_ko)[:16].
#   4. Inserts new articles into raw_news_articles (sqlite) via
#      INSERT OR IGNORE — UNIQUE(content_hash) is the authoritative dedup
#      guard; a re-poll of an already-seen article is a no-op.
#
# Columns are named for what they actually hold — no more column-swap
# workaround (the old KVN_Articles Sheet's title/url/content_hash swap, see
# classify_articles.py's historical C-5h comment, does not exist in this
# schema; it only ever existed because collect_naver.py's old row-writer
# built rows in the wrong order — see news_schema.py's module comment).
#
# Security: no credentials in this file. All secrets from .env or environment.
# L-1: Run with PYTHONPATH=. to resolve project-level imports.
# L-2: .env must be in the repo root, not in Q-Submarine.
# L-3: GOOGLE_SERVICE_ACCOUNT_JSON must be a single-line JSON string.

import argparse
import hashlib
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

# ---------------------------------------------------------------------------
# L-1: ensure repo root is on sys.path.
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import vkh_sqlite  # noqa: E402
from scripts.news_schema import NEWS_DDL  # noqa: E402
from scripts.sheets_auth import _load_config, connect_sheets, resolve_sheet_id  # noqa: E402

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

# Schema for the _keywords tab (still Sheets-native — proposal §6b).
KEYWORDS_HEADER = ["term", "type", "language"]

# Default keyword list — used when _keywords tab is empty.
DEFAULT_KEYWORDS = [
    ("녹용", "primary", "ko"),
    ("뉴질랜드 녹용", "compound", "ko"),
    ("deer velvet Korea", "compound", "en"),
    ("녹용 수입", "compound", "ko"),
    ("녹용 건강기능식품", "compound", "ko"),
]


# ---------------------------------------------------------------------------
# Helpers — HTML cleaning and date parsing (unchanged from the pre-rebuild version)
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


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
            "or GitHub Secrets.",
            file=sys.stderr,
        )
        sys.exit(1)
    return client_id, client_secret


# ---------------------------------------------------------------------------
# Step 2 — Keyword management (Sheets-native, unchanged)
# ---------------------------------------------------------------------------

def _get_or_create_worksheet(sheet, tab_name: str, headers: list[str]):
    """Return the named worksheet, creating it with the given headers if absent."""
    all_ws = {ws.title: ws for ws in sheet.worksheets()}
    if tab_name in all_ws:
        return all_ws[tab_name]

    print(f"  Creating missing tab: {tab_name}")
    ws = sheet.add_worksheet(title=tab_name, rows=200, cols=len(headers))
    ws.append_row(headers)
    return ws


def read_keywords(sheet, limit: int | None = None) -> list[str]:
    """
    Read keyword terms from the _keywords tab (one API call).
    If the tab is empty, seed it with defaults and return the default terms.
    """
    keywords_ws = _get_or_create_worksheet(sheet, "_keywords", KEYWORDS_HEADER)

    rows = keywords_ws.get_all_records()

    if rows:
        terms = [str(r.get("term", "")).strip() for r in rows if r.get("term")]
        terms = [t for t in terms if t]
        if terms:
            if limit:
                terms = terms[:limit]
            print(f"  keywords: {len(terms)} loaded from _keywords tab")
            return terms

    print("  _keywords tab is empty — seeding with defaults")
    seed_rows = [list(row) for row in DEFAULT_KEYWORDS]
    keywords_ws.append_rows(seed_rows, value_input_option="RAW")
    print(f"  seeded {len(seed_rows)} keywords")

    terms = [row[0] for row in DEFAULT_KEYWORDS]
    if limit:
        terms = terms[:limit]
    return terms


# ---------------------------------------------------------------------------
# Step 3 — Naver News API collection (unchanged)
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
# Step 4 — Write path: raw_news_articles (sqlite)
# ---------------------------------------------------------------------------

def ingest_articles(conn: sqlite3.Connection, articles: list[dict], dry_run: bool) -> dict:
    """
    Insert articles into raw_news_articles, one INSERT OR IGNORE per article.
    UNIQUE(content_hash) is the authoritative dedup guard — a duplicate
    within this batch OR already present from an earlier run is a no-op, not
    an error, and both land in the same skipped_duplicate count.

    Returns {"fetched": int, "inserted": int, "skipped_duplicate": int}.
    dry_run performs no writes.
    """
    if dry_run:
        return {"fetched": len(articles), "inserted": 0, "skipped_duplicate": 0}

    inserted = 0
    now = _utc_now_iso()
    for a in articles:
        cur = conn.execute(
            "INSERT OR IGNORE INTO raw_news_articles "
            "(content_hash, url, title_ko, description, published_date, source_name, "
            "source_domain, keyword_matched, collected_at, raw_metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                a["content_hash"], a["url"], a["title_ko"], a.get("description", ""),
                a.get("published_date", ""), a.get("source_name", ""), a.get("source_domain", ""),
                a.get("keyword_matched", ""), now, None,
            ),
        )
        if cur.rowcount > 0:
            inserted += 1
    conn.commit()
    return {"fetched": len(articles), "inserted": inserted, "skipped_duplicate": len(articles) - inserted}


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect Naver News articles into raw_news_articles (sqlite)."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch but do not write to sqlite.",
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

    print("collect_naver.py — VKH Naver News API collection (sqlite)")
    if args.dry_run:
        print("  mode: dry-run (no writes)")

    # --- Credentials ------------------------------------------------------
    config = load_config()
    sheet_id = resolve_sheet_id(config)
    sheet = connect_sheets(sheet_id)  # keywords only — see module docstring
    client_id, client_secret = get_naver_credentials()
    print(f"  keywords sheet: {sheet.title} ({sheet_id})")

    # --- Keywords -----------------------------------------------------------
    keywords = read_keywords(sheet, limit=args.limit)

    # --- Fetch ----------------------------------------------------------------
    all_articles = fetch_all_articles(keywords, client_id, client_secret)
    total_fetched = len(all_articles)

    # --- Write to sqlite --------------------------------------------------
    conn = vkh_sqlite.connect()
    vkh_sqlite.migrate(conn, NEWS_DDL)
    try:
        result = ingest_articles(conn, all_articles, dry_run=args.dry_run)
    finally:
        conn.close()

    # --- Summary line (required output format) --------------------------------
    print(
        f"keywords_searched: {len(keywords)} | "
        f"articles_fetched: {total_fetched} | "
        f"new_articles: {result['inserted']} | "
        f"skipped_duplicates: {result['skipped_duplicate']}"
    )
    if args.dry_run:
        print("  dry-run complete — no rows written")
    else:
        print(f"  rows written to raw_news_articles: {result['inserted']}")


if __name__ == "__main__":
    main()
