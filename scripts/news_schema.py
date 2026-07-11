# news_schema.py — sqlite DDL for the News section (D3, Phase D rebuild).
#
# Design doc: Domain_Knowledge_staging/VKH_D3_news_scaffolding_proposal_2026-07-11.md
#
# Mirrors library_schema.py's role (single source of truth for a section's
# table shape) but for News. Two raw+canonical pairs:
#
#   raw_news_articles / news_articles — Naver collection -> relevance ->
#     classification -> semantic clustering (§2, §4).
#   raw_weekly_brief_drafts / weekly_briefs — auto-drafted weekly brief +
#     human publication gate, absorbed into the News section's read path (§5).
#
# Security: no credentials in this file.

# ---------------------------------------------------------------------------
# raw_news_articles — append-only. One row per article Naver's API returns,
# collapsed only on content_hash (the existing dedup key, ported as-is from
# collect_naver.py). A re-poll of an already-seen article is a no-op
# (INSERT OR IGNORE), not a new row.
#
# Columns are named for what they actually hold — the live KVN_Articles
# Sheet's title/url/content_hash column-swap bug (see the old
# classify_articles.py C-5h comment) is undone once, at migration time
# (scripts/migrate_news_to_sqlite.py), and never reappears here.
# ---------------------------------------------------------------------------
RAW_NEWS_ARTICLES_SQL = """
CREATE TABLE IF NOT EXISTS raw_news_articles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    content_hash    TEXT NOT NULL UNIQUE,   -- sha256(url + title_ko)[:16], ported unchanged
    url             TEXT NOT NULL,
    title_ko        TEXT NOT NULL,
    description     TEXT,
    published_date  TEXT,
    source_name     TEXT,
    source_domain   TEXT,
    keyword_matched TEXT,
    collected_at    TEXT NOT NULL,
    raw_metadata    TEXT
)
"""

# ---------------------------------------------------------------------------
# news_articles — canonical. One row per raw article that has entered the
# judgement pipeline. UNIQUE(raw_ref): a raw article is judged at most once
# per pipeline stage — schema-enforced, same guarantee D1 gave Library.
#
# relevant reflects the Haiku judgement ONLY — never overloaded to also mean
# "suppressed as a duplicate" (duplicate_of_raw_ref) or "hidden by a human"
# (hidden_by_commander). See §2 schema note for the NULL-vs-sentinel
# simplification this enables over the old string-sentinel Sheets column.
# ---------------------------------------------------------------------------
NEWS_ARTICLES_SQL = """
CREATE TABLE IF NOT EXISTS news_articles (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_ref                INTEGER NOT NULL UNIQUE REFERENCES raw_news_articles(id),

    relevant               INTEGER NOT NULL,      -- 0/1, Haiku judgement only
    relevance_judged_at    TEXT NOT NULL,

    category               TEXT,
    english_title          TEXT,
    english_summary        TEXT,
    classified_at          TEXT,

    duplicate_of_raw_ref   INTEGER REFERENCES raw_news_articles(id),  -- NULL = not a duplicate (or not yet judged — see dedup_judged_at)
    dedup_judged_at        TEXT,                  -- NULL = never judged by the clustering pass
    manual_override        INTEGER NOT NULL DEFAULT 0,  -- human-only; this pipeline reads it, never writes it
    hidden_by_commander    INTEGER NOT NULL DEFAULT 0   -- human-only; this pipeline reads it, never writes it
)
"""

_NEWS_ARTICLES_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_raw_news_articles_published ON raw_news_articles(published_date)",
    "CREATE INDEX IF NOT EXISTS idx_news_articles_relevant ON news_articles(relevant)",
]

# ---------------------------------------------------------------------------
# raw_weekly_brief_drafts — script-authored, append-only. One row per week's
# Haiku-generated draft + fact-check result. UNIQUE(week_ending_date) gives
# "generate at most once per week" as a schema guarantee, matching the old
# Sheets-tab idempotency check in generate_weekly_brief_draft().
# ---------------------------------------------------------------------------
RAW_WEEKLY_BRIEF_DRAFTS_SQL = """
CREATE TABLE IF NOT EXISTS raw_weekly_brief_drafts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    week_ending_date    TEXT NOT NULL UNIQUE,
    draft_text          TEXT NOT NULL,
    fact_check_status   TEXT NOT NULL,
    fact_check_detail   TEXT,
    generated_at        TEXT NOT NULL
)
"""

# ---------------------------------------------------------------------------
# weekly_briefs — canonical. UNIQUE(draft_ref): holds only what a human
# decides (approved/approved_at/published_text/notes) — the Commander's
# publication gate. No auto-publish fallback exists anywhere in this
# pipeline (Pre-Mortem #4, ported as-is per §5).
# ---------------------------------------------------------------------------
WEEKLY_BRIEFS_SQL = """
CREATE TABLE IF NOT EXISTS weekly_briefs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_ref           INTEGER NOT NULL UNIQUE REFERENCES raw_weekly_brief_drafts(id),
    approved            INTEGER NOT NULL DEFAULT 0,
    approved_at         TEXT,
    published_text      TEXT,
    notes               TEXT
)
"""

# Exported so vkh_sqlite.py's migrate() can apply every DDL statement for
# this section in one call — same convention as library_schema.LIBRARY_DDL.
NEWS_DDL: list[str] = [
    RAW_NEWS_ARTICLES_SQL,
    NEWS_ARTICLES_SQL,
    *_NEWS_ARTICLES_INDEXES_SQL,
    RAW_WEEKLY_BRIEF_DRAFTS_SQL,
    WEEKLY_BRIEFS_SQL,
]


def demo() -> None:
    """Self-check: DDL is valid SQL, both raw+canonical pairs enforce UNIQUE(ref)."""
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    for stmt in NEWS_DDL:
        conn.execute(stmt)

    conn.execute(
        "INSERT INTO raw_news_articles (content_hash, url, title_ko, collected_at) "
        "VALUES ('abcdef0123456789', 'https://example.com/a', 'test headline', '2026-07-11T00:00:00Z')"
    )
    raw_id = conn.execute("SELECT id FROM raw_news_articles").fetchone()[0]
    conn.execute(
        "INSERT INTO news_articles (raw_ref, relevant, relevance_judged_at) VALUES (?, 1, '2026-07-11T00:00:00Z')",
        (raw_id,),
    )

    # UNIQUE(raw_ref) must reject a second judgement of the same raw article.
    try:
        conn.execute(
            "INSERT INTO news_articles (raw_ref, relevant, relevance_judged_at) VALUES (?, 0, '2026-07-11T00:00:00Z')",
            (raw_id,),
        )
        raise AssertionError("Expected UNIQUE(raw_ref) to reject a second judgement")
    except sqlite3.IntegrityError:
        pass

    conn.execute(
        "INSERT INTO raw_weekly_brief_drafts (week_ending_date, draft_text, fact_check_status, generated_at) "
        "VALUES ('2026-07-06', 'draft text', 'ok', '2026-07-11T00:00:00Z')"
    )
    draft_id = conn.execute("SELECT id FROM raw_weekly_brief_drafts").fetchone()[0]
    conn.execute("INSERT INTO weekly_briefs (draft_ref) VALUES (?)", (draft_id,))
    try:
        conn.execute("INSERT INTO weekly_briefs (draft_ref) VALUES (?)", (draft_id,))
        raise AssertionError("Expected UNIQUE(draft_ref) to reject a second promotion")
    except sqlite3.IntegrityError:
        pass

    assert conn.execute("SELECT COUNT(*) FROM news_articles").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM weekly_briefs").fetchone()[0] == 1
    print("news_schema.py demo: OK — DDL valid, UNIQUE(raw_ref) and UNIQUE(draft_ref) enforced")


if __name__ == "__main__":
    demo()
