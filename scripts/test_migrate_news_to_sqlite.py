# Run as: PYTHONPATH=. python scripts/test_migrate_news_to_sqlite.py
#
# test_migrate_news_to_sqlite.py — coverage for scripts/migrate_news_to_sqlite.py
# (D3 Phase D rebuild, 2026-07-11). Fixture-based only — NEVER connects to
# the live KVN_Articles Sheet (see module docstring in the script under
# test). Synthetic fixture rows reproduce the documented failure modes from
# the D3 scaffolding proposal §3.
#
# Fixture rows use the LIVE sheet's column-swapped shape (title=URL,
# url=Korean title, content_hash=description) — exactly what
# migrate_pass1_raw()/migrate_pass2_canonical() are built to read.
#
# Covers:
#   (a) column-swap undone correctly (title/url/content_hash -> url/title_ko/description)
#   (b) a duplicate group (26 physical rows, one content_hash) with
#       inconsistent classification text across copies — verification count 1
#   (c) a dangling duplicate_of_article_id pointer — verification count 2
#   (d) a pointer-set-but-dedup_judged_at-empty row (difflib-era) — sentinel
#       backfill, verification count 3
#   (e) a blank/malformed article_id row — hard fail (N-6), verification count 4
#   N-1: a suppressed-duplicate mate must not be misclassified as irrelevant
#   N-4: per-column selection rule for collapsed groups (classification from
#        the visible row; manual_override = any TRUE wins)

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.migrate_news_to_sqlite import (  # noqa: E402
    find_malformed_article_ids,
    migrate_pass1_raw,
    migrate_pass2_canonical,
    run_migration,
    verify_manual_override_still_visible,
)
from scripts.news_schema import NEWS_DDL  # noqa: E402


def _fresh_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    for stmt in NEWS_DDL:
        conn.execute(stmt)
    return conn


def _live_row(
    article_id: str, title_url: str, url_title_ko: str, content_hash_description: str,
    published_date: str = "2026-05-28", source: str = "yna.co.kr",
    category: str = "", english_title: str = "", english_summary: str = "", ai_processed_at: str = "",
    include_on_site: str = "", duplicate_of_article_id: str = "", dedup_judged_at: str = "",
    manual_override: str = "", crawled_at: str = "2026-05-28T00:00:00Z",
) -> dict:
    """
    Build one live-Sheet-shaped row dict, matching the real column-swapped
    layout: 'title' holds the URL, 'url' holds the Korean title text,
    'content_hash' holds the description.
    """
    return {
        "article_id": article_id,
        "title": title_url,
        "url": url_title_ko,
        "content_hash": content_hash_description,
        "published_date": published_date,
        "source": source,
        "category": category,
        "english_title": english_title,
        "english_summary": english_summary,
        "ai_processed_at": ai_processed_at,
        "include_on_site": include_on_site,
        "crawled_at": crawled_at,
        "duplicate_of_article_id": duplicate_of_article_id,
        "dedup_judged_at": dedup_judged_at,
        "manual_override": manual_override,
    }


# ---------------------------------------------------------------------------
# (e) malformed article_id — hard fail, N-6
# ---------------------------------------------------------------------------

def test_malformed_article_id_hard_fails_before_any_insert():
    conn = _fresh_conn()
    rows = [
        _live_row("a1a1a1a1a1a1a1a1", "https://x.com/1", "headline one", "desc one"),
        _live_row("", "https://x.com/2", "headline two", "desc two"),  # blank article_id
        _live_row("not-hex!!", "https://x.com/3", "headline three", "desc three"),  # malformed
    ]
    malformed = find_malformed_article_ids(rows)
    assert len(malformed) == 2

    try:
        migrate_pass1_raw(conn, rows)
        raise AssertionError("Expected ValueError hard-fail on malformed article_id")
    except ValueError as exc:
        assert "malformed" in str(exc).lower() or "2 row" in str(exc)

    # Threshold zero: NOTHING must be inserted, not even the well-formed row.
    count = conn.execute("SELECT COUNT(*) FROM raw_news_articles").fetchone()[0]
    assert count == 0, "hard fail must abort before any row is written"


def test_well_formed_article_ids_do_not_hard_fail():
    conn = _fresh_conn()
    rows = [_live_row("a1a1a1a1a1a1a1a1", "https://x.com/1", "headline one", "desc one")]
    assert find_malformed_article_ids(rows) == []
    hash_to_raw_id = migrate_pass1_raw(conn, rows)
    assert len(hash_to_raw_id) == 1


# ---------------------------------------------------------------------------
# (a) column-swap undone correctly
# ---------------------------------------------------------------------------

def test_column_swap_undone_on_migration():
    conn = _fresh_conn()
    rows = [_live_row(
        "b1b1b1b1b1b1b1b1",
        title_url="https://publisher.co.kr/article/123",
        url_title_ko="녹용 수입 증가",
        content_hash_description="이 기사는 녹용 수입에 대한 내용입니다",
    )]
    migrate_pass1_raw(conn, rows)
    raw = conn.execute(
        "SELECT url, title_ko, description, source_domain FROM raw_news_articles WHERE content_hash = ?",
        ("b1b1b1b1b1b1b1b1",),
    ).fetchone()
    assert raw[0] == "https://publisher.co.kr/article/123", "url must come from live 'title' column"
    assert raw[1] == "녹용 수입 증가", "title_ko must come from live 'url' column"
    assert raw[2] == "이 기사는 녹용 수입에 대한 내용입니다", "description must come from live 'content_hash' column"
    assert raw[3] == "publisher.co.kr", "source_domain must be derived from the (corrected) url"


# ---------------------------------------------------------------------------
# (b) inconsistent classification text across up-to-26 physical copies —
#     verification count 1. Also exercises visible-row-wins selection (N-4).
# ---------------------------------------------------------------------------

def test_26_physical_copies_inconsistent_classification_text():
    conn = _fresh_conn()
    shared_hash = "c1c1c1c1c1c1c1c1"
    rows = []
    # 25 decoy copies, all suppressed, all with slightly different stale
    # classification text (pre-C-13 duplicate-insert debt scenario).
    for i in range(25):
        rows.append(_live_row(
            shared_hash, f"https://x.com/{i}", "headline", "description",
            category="기타", english_title=f"stale variant {i}", english_summary=f"stale summary {i}",
            ai_processed_at=f"2026-05-{20 + (i % 5):02d}T00:00:00Z", include_on_site="FALSE",
        ))
    # The 26th copy is the genuine, currently-visible one.
    rows.append(_live_row(
        shared_hash, "https://x.com/genuine", "headline", "description",
        category="무역시장", english_title="Genuine title", english_summary="Genuine summary",
        ai_processed_at="2026-05-28T00:00:00Z", include_on_site="TRUE",
    ))
    assert len(rows) == 26

    hash_to_raw_id = migrate_pass1_raw(conn, rows)
    assert len(hash_to_raw_id) == 1, "26 copies sharing one content_hash must collapse to ONE raw row"

    counts = migrate_pass2_canonical(conn, rows, hash_to_raw_id)
    assert counts["inconsistent_classification_groups"] == 1, counts

    canonical = conn.execute(
        "SELECT category, english_title, english_summary FROM news_articles"
    ).fetchone()
    assert canonical == ("무역시장", "Genuine title", "Genuine summary"), (
        "the currently-visible row must win the classification-field selection, not a stale decoy"
    )
    assert conn.execute("SELECT COUNT(*) FROM news_articles").fetchone()[0] == 1


# ---------------------------------------------------------------------------
# N-1: suppressed-duplicate mate must not be misclassified as irrelevant
# ---------------------------------------------------------------------------

def test_suppressed_duplicate_not_misclassified_as_irrelevant():
    conn = _fresh_conn()
    # Group X: both physical copies suppressed (include_on_site=FALSE), but
    # one carries a tracked pointer to a DIFFERENT article (Y) — it was
    # relevant, just suppressed as a same-story duplicate of Y.
    rows = [
        _live_row("d1d1d1d1d1d1d1d1", "https://x.com/x1", "headline X copy 1", "desc",
                  category="무역시장", english_title="X", english_summary="X summary",
                  ai_processed_at="2026-05-28T00:00:00Z", include_on_site="FALSE"),
        _live_row("d1d1d1d1d1d1d1d1", "https://x.com/x2", "headline X copy 2", "desc",
                  category="무역시장", english_title="X", english_summary="X summary",
                  ai_processed_at="2026-05-28T00:00:00Z", include_on_site="FALSE",
                  duplicate_of_article_id="d2d2d2d2d2d2d2d2", dedup_judged_at="2026-05-29T00:00:00Z"),
        # Y: the canonical this group's tracked row points at.
        _live_row("d2d2d2d2d2d2d2d2", "https://x.com/y", "headline Y", "desc",
                  category="무역시장", english_title="Y", english_summary="Y summary",
                  ai_processed_at="2026-05-28T00:00:00Z", include_on_site="TRUE"),
    ]
    hash_to_raw_id = migrate_pass1_raw(conn, rows)
    migrate_pass2_canonical(conn, rows, hash_to_raw_id)

    x_relevant, x_dup_of = conn.execute(
        "SELECT relevant, duplicate_of_raw_ref FROM news_articles WHERE raw_ref = ?",
        (hash_to_raw_id["d1d1d1d1d1d1d1d1"],),
    ).fetchone()
    assert x_relevant == 1, "a suppressed duplicate mate must NOT migrate as irrelevant"
    assert x_dup_of == hash_to_raw_id["d2d2d2d2d2d2d2d2"], "duplicate_of_raw_ref must resolve to Y's raw row"


def test_naively_all_false_no_tracked_pointer_is_irrelevant():
    """Contrast case: a group that is genuinely irrelevant (no visible row,
    no duplicate tracking pointer at all) must migrate as relevant=0."""
    conn = _fresh_conn()
    rows = [_live_row(
        "e1e1e1e1e1e1e1e1", "https://x.com/1", "headline", "desc",
        category="기타", english_title="", english_summary="",
        ai_processed_at="2026-05-28T00:00:00Z", include_on_site="FALSE",
    )]
    hash_to_raw_id = migrate_pass1_raw(conn, rows)
    migrate_pass2_canonical(conn, rows, hash_to_raw_id)
    relevant, category = conn.execute(
        "SELECT relevant, category FROM news_articles WHERE raw_ref = ?", (hash_to_raw_id["e1e1e1e1e1e1e1e1"],)
    ).fetchone()
    assert relevant == 0
    assert category is None, "classification columns must be NULL for a relevant=0 migrated row"


# ---------------------------------------------------------------------------
# (c) dangling duplicate_of_article_id pointer — verification count 2
# ---------------------------------------------------------------------------

def test_dangling_pointer_counted_and_left_null():
    conn = _fresh_conn()
    rows = [_live_row(
        "f1f1f1f1f1f1f1f1", "https://x.com/1", "headline", "desc",
        category="무역시장", english_title="T", english_summary="S",
        ai_processed_at="2026-05-28T00:00:00Z", include_on_site="FALSE",
        duplicate_of_article_id="ffffffffffffffff",  # no live row has this article_id
        dedup_judged_at="2026-05-28T00:00:00Z",
    )]
    hash_to_raw_id = migrate_pass1_raw(conn, rows)
    counts = migrate_pass2_canonical(conn, rows, hash_to_raw_id)
    assert counts["dangling_pointers"] == 1, counts

    dup_of = conn.execute(
        "SELECT duplicate_of_raw_ref FROM news_articles WHERE raw_ref = ?", (hash_to_raw_id["f1f1f1f1f1f1f1f1"],)
    ).fetchone()[0]
    assert dup_of is None, "a dangling pointer must migrate as NULL, not crash the run (L-12)"


# ---------------------------------------------------------------------------
# (d) pointer-set-but-dedup_judged_at-empty (difflib-era) — sentinel backfill, count 3
# ---------------------------------------------------------------------------

def test_sentinel_backfill_for_difflib_era_row():
    conn = _fresh_conn()
    rows = [
        _live_row("a3a3a3a3a3a3a3a3", "https://x.com/canon", "canon headline", "desc",
                  category="무역시장", english_title="T", english_summary="S",
                  ai_processed_at="2026-05-20T00:00:00Z", include_on_site="TRUE"),
        _live_row("a2a2a2a2a2a2a2a2", "https://x.com/1", "headline", "desc",
                  category="무역시장", english_title="T", english_summary="S",
                  ai_processed_at="2026-05-20T00:00:00Z", include_on_site="FALSE",
                  duplicate_of_article_id="a3a3a3a3a3a3a3a3", dedup_judged_at="",  # difflib-era: no timestamp
                  crawled_at="2026-05-19T00:00:00Z"),
    ]
    hash_to_raw_id = migrate_pass1_raw(conn, rows)
    counts = migrate_pass2_canonical(conn, rows, hash_to_raw_id)
    assert counts["sentinel_backfilled_prebackfill_count"] == 1, counts

    dedup_judged_at = conn.execute(
        "SELECT dedup_judged_at FROM news_articles WHERE raw_ref = ?", (hash_to_raw_id["a2a2a2a2a2a2a2a2"],)
    ).fetchone()[0]
    assert dedup_judged_at == "2026-05-19T00:00:00Z", "backfilled sentinel must come from the row's own crawled_at"
    assert dedup_judged_at is not None, "post-backfill, dedup_judged_at must never be NULL when a pointer is set"


def test_judged_not_duplicate_sentinel_needs_no_backfill():
    """A row judged 'not a duplicate' (old 'none' sentinel) with a real
    dedup_judged_at timestamp must NOT be counted as needing backfill."""
    conn = _fresh_conn()
    rows = [_live_row(
        "a4a4a4a4a4a4a4a4", "https://x.com/1", "headline", "desc",
        category="무역시장", english_title="T", english_summary="S",
        ai_processed_at="2026-05-20T00:00:00Z", include_on_site="TRUE",
        duplicate_of_article_id="none", dedup_judged_at="2026-05-20T00:00:00Z",
    )]
    hash_to_raw_id = migrate_pass1_raw(conn, rows)
    counts = migrate_pass2_canonical(conn, rows, hash_to_raw_id)
    assert counts["sentinel_backfilled_prebackfill_count"] == 0, counts
    dup_of, judged_at = conn.execute(
        "SELECT duplicate_of_raw_ref, dedup_judged_at FROM news_articles WHERE raw_ref = ?",
        (hash_to_raw_id["a4a4a4a4a4a4a4a4"],),
    ).fetchone()
    assert dup_of is None and judged_at == "2026-05-20T00:00:00Z"


# ---------------------------------------------------------------------------
# N-4: manual_override = any TRUE in the group wins
# ---------------------------------------------------------------------------

def test_manual_override_any_true_wins():
    conn = _fresh_conn()
    rows = [
        _live_row("a5a5a5a5a5a5a5a5", "https://x.com/1", "headline", "desc",
                  category="무역시장", english_title="T", english_summary="S",
                  ai_processed_at="2026-05-20T00:00:00Z", include_on_site="TRUE", manual_override=""),
        _live_row("a5a5a5a5a5a5a5a5", "https://x.com/2", "headline", "desc",
                  category="무역시장", english_title="T", english_summary="S",
                  ai_processed_at="2026-05-20T00:00:00Z", include_on_site="FALSE", manual_override="TRUE"),
    ]
    hash_to_raw_id = migrate_pass1_raw(conn, rows)
    migrate_pass2_canonical(conn, rows, hash_to_raw_id)
    manual_override = conn.execute(
        "SELECT manual_override FROM news_articles WHERE raw_ref = ?", (hash_to_raw_id["a5a5a5a5a5a5a5a5"],)
    ).fetchone()[0]
    assert manual_override == 1, "any TRUE in the physical group must win, never dropped by picking the wrong row"


# ---------------------------------------------------------------------------
# Unprocessed groups migrate raw-only
# ---------------------------------------------------------------------------

def test_unprocessed_group_migrates_raw_only():
    conn = _fresh_conn()
    rows = [_live_row("a6a6a6a6a6a6a6a6", "https://x.com/1", "headline", "desc", ai_processed_at="")]
    hash_to_raw_id = migrate_pass1_raw(conn, rows)
    counts = migrate_pass2_canonical(conn, rows, hash_to_raw_id)
    assert conn.execute("SELECT COUNT(*) FROM news_articles").fetchone()[0] == 0
    assert counts["inconsistent_classification_groups"] == 0


# ---------------------------------------------------------------------------
# Post-migration functional check — manual_override regression guard
# ---------------------------------------------------------------------------

def test_manual_override_visible_regression_check_passes():
    conn = _fresh_conn()
    rows = [_live_row(
        "a7a7a7a7a7a7a7a7", "https://x.com/1", "headline", "desc",
        category="무역시장", english_title="T", english_summary="S",
        ai_processed_at="2026-05-20T00:00:00Z", include_on_site="TRUE", manual_override="TRUE",
    )]
    hash_to_raw_id = migrate_pass1_raw(conn, rows)
    migrate_pass2_canonical(conn, rows, hash_to_raw_id)
    failures = verify_manual_override_still_visible(conn, rows)
    assert failures == [], f"expected no regressions, got {failures}"


# ---------------------------------------------------------------------------
# End-to-end orchestration smoke test
# ---------------------------------------------------------------------------

def test_run_migration_end_to_end_prints_and_returns_counts():
    conn = _fresh_conn()
    rows = [
        _live_row("a8a8a8a8a8a8a8a8", "https://x.com/1", "headline one", "desc one",
                  category="무역시장", english_title="T1", english_summary="S1",
                  ai_processed_at="2026-05-20T00:00:00Z", include_on_site="TRUE"),
        _live_row("a9a9a9a9a9a9a9a9", "https://x.com/2", "headline two", "desc two"),  # unprocessed
    ]
    summary = run_migration(conn, rows)
    assert summary["live_rows"] == 2
    assert summary["raw_rows"] == 2
    assert summary["canonical_rows"] == 1
    assert summary["malformed_article_id_count"] == 0
    assert summary["manual_override_regressions"] == 0


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for test in tests:
        test()
        print(f"  PASS: {test.__name__}")
    print(f"test_migrate_news_to_sqlite.py: {len(tests)}/{len(tests)} passed")


if __name__ == "__main__":
    main()
