# news_data.py — read-side functions the dashboard build calls for the News
# section (D3, Phase D rebuild).
#
# Design doc: Domain_Knowledge_staging/VKH_D3_news_scaffolding_proposal_2026-07-11.md
#
# Deep-module split (mirrors library_data.py's role for D1):
#   news_schema.py — DDL (this section's "shape")
#   news_data.py   — THIS FILE: read queries for the build (this section's
#                     "what to show"), including the weekly brief folded in
#                     per §5 (no longer a standalone build-level section).
#   collect_naver.py / classify_articles.py — write path (§4)
#   vkh_brief.py   — weekly brief write path + get_weekly_brief_context()
#
# Security: no credentials in this file.

import sqlite3

from scripts import vkh_sqlite
from scripts.news_schema import NEWS_DDL

# Display predicate (§4/§6b, revision-2 fix) — the ONLY place this predicate
# is allowed to be written. relevant=1 is the Haiku judgement; the OR clause
# keeps a manual_override=1 article visible even if the clustering pass
# pointed duplicate_of_raw_ref at another article; hidden_by_commander=0 is
# the human hide switch, always checked last and independently.
_DISPLAY_PREDICATE = (
    "n.relevant = 1 "
    "AND (n.duplicate_of_raw_ref IS NULL OR n.manual_override = 1) "
    "AND n.hidden_by_commander = 0"
)


def list_news_articles(conn: sqlite3.Connection) -> list[dict]:
    """
    Return published news_articles rows (display predicate applied),
    most recent published_date first, joined against raw_news_articles for
    the collected fields (url/title_ko/description/source_name/...).

    Returns plain dicts, JSON/Jinja2-safe.
    """
    query = f"""
        SELECT
            r.id AS raw_ref, r.url, r.title_ko, r.description, r.published_date,
            r.source_name, r.source_domain, r.keyword_matched, r.collected_at,
            n.id AS news_id, n.category, n.english_title, n.english_summary,
            n.classified_at, n.relevant, n.relevance_judged_at,
            n.duplicate_of_raw_ref, n.dedup_judged_at, n.manual_override,
            n.hidden_by_commander
        FROM news_articles n
        JOIN raw_news_articles r ON r.id = n.raw_ref
        WHERE {_DISPLAY_PREDICATE}
        ORDER BY r.published_date DESC
    """
    cur = conn.execute(query)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def news_available(conn: sqlite3.Connection) -> bool:
    """
    True if at least one news_articles row currently passes the display
    predicate. Table-not-yet-migrated degrades to False, never crashes the
    build (L-12), same posture as library_data.library_available().
    """
    try:
        count = conn.execute(
            f"SELECT COUNT(*) FROM news_articles n WHERE {_DISPLAY_PREDICATE}"
        ).fetchone()[0]
    except sqlite3.OperationalError:
        return False
    return count > 0


def assemble_news_section(config: dict) -> dict:
    """
    Build the "news_pulse" section dict for the dashboard build.

    Opens its own sqlite connection (mirrors library_data.assemble_library_section)
    and folds the weekly brief context into the same dict — per §5, the
    weekly brief is no longer a standalone build-level section; this is its
    only remaining call site.

    Gated on the news_articles source's config.yaml `enabled` flag (matches
    the D1 library_docs convention).
    """
    sources_by_id = {s["id"]: s for s in config.get("sources", [])}
    source = sources_by_id.get("news_articles", {})

    if not source.get("enabled", False):
        return {
            "enabled": False, "data": [], "has_data": False,
            "last_updated": None, "weekly_brief": {"enabled": False},
        }

    conn = vkh_sqlite.connect()
    try:
        vkh_sqlite.migrate(conn, NEWS_DDL)
        articles = list_news_articles(conn)
        has_data = news_available(conn)

        # Local import: avoids a module-level import cycle (vkh_brief imports
        # from classify_articles, which does not import news_data) and keeps
        # weekly_brief entirely optional if that module is ever unavailable.
        from scripts.vkh_brief import get_weekly_brief_context
        weekly_brief_ctx = get_weekly_brief_context(config, conn)
    finally:
        conn.close()

    dates = [a["published_date"] for a in articles if a.get("published_date")]
    last_updated = max(dates) if dates else None

    return {
        "enabled": True,
        "data": articles,
        "has_data": has_data,
        "last_updated": last_updated,
        "weekly_brief": weekly_brief_ctx,
    }
