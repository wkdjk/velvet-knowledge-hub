# Run as: PYTHONPATH=. python scripts/migrate_news_to_sqlite.py --live
#
# migrate_news_to_sqlite.py — one-off migration of the live KVN_Articles
# Sheet (~8,700 rows) into raw_news_articles + news_articles (sqlite).
#
# Design doc: Domain_Knowledge_staging/VKH_D3_news_scaffolding_proposal_2026-07-11.md §3
#
# NOT retired after running once like a legacy setup script — this belongs
# to the same category as D2's trade-stats migration tooling (§3, resolved).
#
# *** THIS SCRIPT MUST NOT BE RUN AGAINST THE LIVE KVN_Articles SHEET AS
# *** PART OF THE D3 BUILD DISPATCH. Building and unit-testing it against
# *** synthetic fixture data is the deliverable; running --live is a
# *** separate, later step after CaptainQ review (see D3 implementation
# *** report).
#
# Two-pass migration, reading the live Sheet's columns BY NAME
# (get_all_records()), never by position — the header names are correct,
# only the data is column-swapped relative to what the header implies (the
# old classify_articles.py C-5h bug). This script is the one place that
# swap gets corrected permanently.
#
# Security: no credentials in this file. All secrets from environment only.

import argparse
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import vkh_sqlite  # noqa: E402
from scripts.news_schema import NEWS_DDL  # noqa: E402

# 16-char hex — the shape of _content_hash()'s output (collect_naver.py).
_ARTICLE_ID_RE = re.compile(r"^[0-9a-f]{16}$")

# Old Sheets sentinel — read here only (migration input), never written by
# any code in this rebuild (news_schema.py's NULL-based states replace it).
_DEDUP_SENTINEL_NONE_LEGACY = "none"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _source_name_from_url(url: str) -> str:
    """Mirrors collect_naver.py's _source_name_from_url() — kept as its own
    copy (ponytail: two 3-line functions, not worth a shared-util import for
    a one-off migration script)."""
    try:
        return urlparse(url).netloc.replace("www.", "")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Pass 1 — raw layer
# ---------------------------------------------------------------------------

def find_malformed_article_ids(live_rows: list[dict]) -> list[str]:
    """
    Rows whose article_id is not a well-formed 16-char hex hash (N-6).
    Threshold zero — any hit means the whole run hard-fails in
    migrate_pass1_raw() before a single row is inserted.
    """
    bad = []
    for row in live_rows:
        aid = str(row.get("article_id", "")).strip()
        if not _ARTICLE_ID_RE.match(aid):
            bad.append(aid)
    return bad


def migrate_pass1_raw(conn: sqlite3.Connection, live_rows: list[dict]) -> dict[str, int]:
    """
    Insert one raw_news_articles row per distinct content_hash (article_id),
    undoing the live Sheet's title/url/content_hash column-swap (§3 table).

    Hard fails (raises ValueError) if ANY row has a malformed/blank
    article_id — checked BEFORE any insert (N-6): an empty or malformed hash
    would otherwise silently merge unrelated articles under
    UNIQUE(content_hash) grouping, which is much harder to spot after the
    fact than a failed migration run is to re-run.

    Returns content_hash -> raw_news_articles.id map, used by Pass 2 to
    resolve duplicate_of_article_id pointers.
    """
    malformed = find_malformed_article_ids(live_rows)
    if malformed:
        raise ValueError(
            f"migrate_news_to_sqlite: {len(malformed)} row(s) have a malformed/blank "
            f"article_id — hard fail (threshold zero), nothing written. "
            f"First few: {malformed[:5]!r}"
        )

    now = _utc_now_iso()
    seen: set[str] = set()
    for row in live_rows:
        content_hash = str(row["article_id"]).strip()
        if content_hash in seen:
            continue
        seen.add(content_hash)

        # Column-swap fix (§3 table): live 'title' holds the URL, live 'url'
        # holds the Korean title, live 'content_hash' holds the description.
        url = str(row.get("title", "")).strip()
        title_ko = str(row.get("url", "")).strip()
        description = str(row.get("content_hash", "")).strip()
        source_domain = _source_name_from_url(url)

        conn.execute(
            "INSERT OR IGNORE INTO raw_news_articles "
            "(content_hash, url, title_ko, description, published_date, source_name, "
            "source_domain, keyword_matched, collected_at, raw_metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL)",
            (
                content_hash, url, title_ko, description,
                str(row.get("published_date", "")), str(row.get("source", "")),
                source_domain, str(row.get("crawled_at", "")).strip() or now,
            ),
        )
    conn.commit()

    return {
        content_hash: raw_id
        for raw_id, content_hash in conn.execute("SELECT id, content_hash FROM raw_news_articles")
    }


# ---------------------------------------------------------------------------
# Pass 2 — canonical layer
# ---------------------------------------------------------------------------

def migrate_pass2_canonical(
    conn: sqlite3.Connection,
    live_rows: list[dict],
    hash_to_raw_id: dict[str, int],
) -> dict:
    """
    Group live rows by content_hash (article_id). For each group with at
    least one non-empty ai_processed_at physical row, insert one
    news_articles row (§3 rules). Groups with no processed row migrate into
    raw_news_articles only (Pass 1) — "awaiting judgement", not a gap.

    Returns verification counts (§3, printed by run_migration()):
      inconsistent_classification_groups — count #1
      dangling_pointers                  — count #2
      sentinel_backfilled_prebackfill_count — count #3 (pre-backfill)
      malformed_article_id_count         — count #4 (always 0 here; Pass 1
                                            already hard-failed otherwise)
    """
    groups: dict[str, list[dict]] = {}
    for row in live_rows:
        groups.setdefault(str(row["article_id"]).strip(), []).append(row)

    inconsistent_groups = 0
    dangling_pointers = 0
    prebackfill_sentinel_count = 0
    now = _utc_now_iso()

    for content_hash, rows in groups.items():
        processed_rows = [r for r in rows if str(r.get("ai_processed_at", "")).strip()]
        if not processed_rows:
            continue  # not yet classified — raw-only, picked up by classify_articles.py's pending queue

        raw_id = hash_to_raw_id.get(content_hash)
        if raw_id is None:
            continue  # defensive — Pass 1 inserts a raw row for every distinct hash, should not happen

        # --- Verification count 1: classification text consistency ---------
        texts = {
            (str(r.get("category", "")).strip(), str(r.get("english_summary", "")).strip())
            for r in processed_rows
        }
        if len(texts) > 1:
            inconsistent_groups += 1

        # --- relevant (N-1 fix, §3 revised rule) ----------------------------
        # NOT a plain include_on_site copy: a suppressed duplicate mate
        # (duplicate_of_article_id set, not the "none" sentinel) was
        # relevant by construction — it was suppressed for being a
        # duplicate, not for being off-topic. Mapping it to relevant=0
        # would kill canonical succession for the whole historical cluster.
        any_visible = any(str(r.get("include_on_site", "")).strip().upper() == "TRUE" for r in rows)
        tracked = next((r for r in rows if str(r.get("duplicate_of_article_id", "")).strip()), None)
        tracked_dup_of = str(tracked.get("duplicate_of_article_id", "")).strip() if tracked else ""
        was_suppressed_mate = bool(tracked_dup_of) and tracked_dup_of != _DEDUP_SENTINEL_NONE_LEGACY
        relevant = 1 if (any_visible or was_suppressed_mate) else 0

        # --- classification fields — only meaningful when relevant=1 -------
        # (news_schema.py §2: never populated for a relevant=0 row, matching
        # the forward write path's write_relevance_and_classification()).
        if relevant:
            visible_row = next(
                (r for r in rows if str(r.get("include_on_site", "")).strip().upper() == "TRUE"), None
            )
            source_row = visible_row or max(processed_rows, key=lambda r: str(r.get("ai_processed_at", "")))
            category = str(source_row.get("category", "")).strip() or None
            english_title = str(source_row.get("english_title", "")).strip() or None
            english_summary = str(source_row.get("english_summary", "")).strip() or None
            classified_at = str(source_row.get("ai_processed_at", "")).strip() or None
        else:
            category = english_title = english_summary = classified_at = None

        # --- duplicate_of_raw_ref / dedup_judged_at (N-4, N-5) --------------
        duplicate_of_raw_ref = None
        dedup_judged_at = None
        if tracked is not None:
            if was_suppressed_mate:
                target_raw_id = hash_to_raw_id.get(tracked_dup_of)
                if target_raw_id is None:
                    dangling_pointers += 1  # verification count 2
                    duplicate_of_raw_ref = None
                else:
                    duplicate_of_raw_ref = target_raw_id
            # else: judged, not-a-duplicate ("none" sentinel) — duplicate_of_raw_ref stays NULL

            raw_dedup_judged_at = str(tracked.get("dedup_judged_at", "")).strip()
            if raw_dedup_judged_at:
                dedup_judged_at = raw_dedup_judged_at
            else:
                # Sentinel backfill (N-5): pointer set (or "none") but
                # dedup_judged_at empty — pre-2026-07-04 difflib-era row.
                # §2's NULL semantics defines "pointer + NULL judged_at" as
                # contradictory, so backfill a sentinel timestamp from the
                # row's own collection date.
                prebackfill_sentinel_count += 1  # verification count 3 (pre-backfill)
                dedup_judged_at = str(tracked.get("crawled_at", "")).strip() or now

        # --- manual_override: any TRUE in the group wins --------------------
        manual_override = 1 if any(
            str(r.get("manual_override", "")).strip().upper() == "TRUE" for r in rows
        ) else 0

        relevance_judged_at = classified_at or (
            str(processed_rows[0].get("ai_processed_at", "")).strip() or now
        )

        conn.execute(
            "INSERT INTO news_articles "
            "(raw_ref, relevant, relevance_judged_at, category, english_title, english_summary, "
            "classified_at, duplicate_of_raw_ref, dedup_judged_at, manual_override, hidden_by_commander) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (
                raw_id, relevant, relevance_judged_at, category, english_title, english_summary,
                classified_at, duplicate_of_raw_ref, dedup_judged_at, manual_override,
            ),
        )

    conn.commit()

    return {
        "inconsistent_classification_groups": inconsistent_groups,
        "dangling_pointers": dangling_pointers,
        "sentinel_backfilled_prebackfill_count": prebackfill_sentinel_count,
        "malformed_article_id_count": 0,  # Pass 1 would have hard-failed otherwise — confirmation, not discovery
    }


# ---------------------------------------------------------------------------
# Post-migration functional check (§3): every live manual_override=TRUE
# article visible today must still be visible in the new read path.
# ---------------------------------------------------------------------------

def verify_manual_override_still_visible(conn: sqlite3.Connection, live_rows: list[dict]) -> list[str]:
    """
    For every live article_id with manual_override=TRUE on at least one
    physical row AND at least one physical row include_on_site=TRUE, confirm
    the migrated row still satisfies news_data.py's display predicate.

    Returns a list of article_id strings that FAIL this check (empty list =
    all clear). Direct regression check for N-3/N-4, not just a count.
    """
    from scripts.news_data import _DISPLAY_PREDICATE  # local import — avoids a module-level cycle

    live_visible_overrides = {
        str(row["article_id"]).strip()
        for row in live_rows
        if str(row.get("manual_override", "")).strip().upper() == "TRUE"
        and str(row.get("include_on_site", "")).strip().upper() == "TRUE"
    }

    failures = []
    for content_hash in live_visible_overrides:
        row = conn.execute(
            f"SELECT n.id FROM news_articles n JOIN raw_news_articles r ON r.id = n.raw_ref "
            f"WHERE r.content_hash = ? AND {_DISPLAY_PREDICATE}",
            (content_hash,),
        ).fetchone()
        if row is None:
            failures.append(content_hash)
    return failures


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run_migration(conn: sqlite3.Connection, live_rows: list[dict]) -> dict:
    """
    Run both passes and print the four mandatory verification counts (§3)
    plus the post-migration functional check. Returns a summary dict.
    """
    print(f"migrate_news_to_sqlite: {len(live_rows)} live rows read")

    hash_to_raw_id = migrate_pass1_raw(conn, live_rows)
    print(f"  Pass 1: {len(hash_to_raw_id)} distinct raw_news_articles row(s) inserted (or already present)")

    counts = migrate_pass2_canonical(conn, live_rows, hash_to_raw_id)
    canonical_count = conn.execute("SELECT COUNT(*) FROM news_articles").fetchone()[0]
    print(f"  Pass 2: {canonical_count} news_articles row(s) inserted (or already present)")

    print("  Verification counts:")
    print(f"    1. collapsed groups with inconsistent classification text: {counts['inconsistent_classification_groups']}")
    print(f"    2. dangling duplicate_of_raw_ref pointers: {counts['dangling_pointers']}"
          + (" — >5: FORCE RE-REVIEW BEFORE GO-LIVE" if counts['dangling_pointers'] > 5 else ""))
    print(f"    3. pointer-set-but-dedup_judged_at-empty rows (pre-backfill): {counts['sentinel_backfilled_prebackfill_count']}")
    print(f"    4. blank/malformed article_id rows: {counts['malformed_article_id_count']} (confirmation — Pass 1 hard-fails otherwise)")

    override_failures = verify_manual_override_still_visible(conn, live_rows)
    if override_failures:
        print(f"  WARNING: {len(override_failures)} manual_override=TRUE, live-visible article(s) "
              f"are NOT visible after migration: {override_failures[:10]}")
    else:
        print("  Post-migration check: every live manual_override=TRUE visible article is still visible. OK.")

    return {
        "live_rows": len(live_rows),
        "raw_rows": len(hash_to_raw_id),
        "canonical_rows": canonical_count,
        **counts,
        "manual_override_regressions": len(override_failures),
    }


# ---------------------------------------------------------------------------
# CLI — NOT invoked against the live sheet in this dispatch (see module header)
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="One-off migration of the live KVN_Articles Sheet into raw_news_articles/news_articles (sqlite)."
    )
    parser.add_argument(
        "--live", action="store_true",
        help="Actually connect to the live Sheet and run the migration. Without this flag, the script "
             "prints usage and exits — it will not touch the live Sheet by accident.",
    )
    args = parser.parse_args()

    if not args.live:
        print(
            "migrate_news_to_sqlite.py — one-off migration script.\n"
            "  Re-run with --live to connect to the live KVN_Articles Sheet and migrate.\n"
            "  Do NOT run --live until CaptainQ has reviewed the D3 implementation report."
        )
        return

    from scripts.sheets_auth import connect_sheets, resolve_sheet_id

    sheet_id = resolve_sheet_id()
    spreadsheet = connect_sheets(sheet_id)
    ws = spreadsheet.worksheet("KVN_Articles")
    live_rows = ws.get_all_records()

    conn = vkh_sqlite.connect()
    vkh_sqlite.migrate(conn, NEWS_DDL)
    try:
        run_migration(conn, live_rows)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
