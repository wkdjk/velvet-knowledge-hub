# library_data.py — read-side functions the dashboard build calls for the
# Library section (D1, Phase D rebuild, full build 2026-07-10).
#
# Design doc: Domain_Knowledge_staging/VKH_D1_library_scaffolding_proposal_2026-07-10.md
#
# Deep-module split (mirrors vkh_data.py / vkh_kpi.py / vkh_charts.py
# boundaries, kept from the C-12b audit, applied to the new sqlite spine):
#   library_schema.py — DDL (this section's "shape")
#   library_data.py   — THIS FILE: read queries for the build (this section's
#                        "what to show")
#   ingest_library.py — write path from Drive (this section's "how data
#                        arrives")
# No library_kpi.py / library_charts.py — Library has no numeric KPIs or
# charts, just a curated document list (YAGNI).
#
# Security: no credentials in this file.

import sqlite3

from scripts import vkh_sqlite
from scripts.library_schema import LIBRARY_DDL


def list_library_docs(
    conn: sqlite3.Connection,
    category: str | None = None,
) -> list[dict]:
    """
    Return published library_docs rows, most recent doc_date first.

    Returns plain dicts (not sqlite3.Row) so this works regardless of the
    caller's row_factory and so the result is directly Jinja2/JSON-safe —
    no join back to raw_library_files needed (title/date/category/tags/
    summary are all in library_docs already).
    """
    query = "SELECT id, file_ref, title, doc_date, category, tags, summary, curated_at, curated_by FROM library_docs"
    params: list = []
    if category:
        query += " WHERE category = ?"
        params.append(category)
    query += " ORDER BY doc_date DESC"

    cur = conn.execute(query, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def library_available(conn: sqlite3.Connection) -> bool:
    """
    Return True if library_docs has at least one row.

    Feeds the same graceful-degradation pattern every other VKH section
    uses (L-12): zero published docs renders a "reference library — coming
    soon" card, not an empty broken-looking section. Never touches
    raw_library_files — a raw file with no curated row is invisible here,
    by design (design doc's test-as-specification checklist).
    """
    try:
        count = conn.execute("SELECT COUNT(*) FROM library_docs").fetchone()[0]
    except sqlite3.OperationalError:
        # Table not yet migrated — treat as "not available", never crash the
        # build (ponytail: this is the same graceful-degradation posture as
        # a missing Sheets tab elsewhere in vkh_data.py).
        return False
    return count > 0


def assemble_library_section(config: dict) -> dict:
    """
    Build the "library" section dict for the dashboard build.

    Independent of vkh_data.assemble_sections()'s generic Sheets-tab loader
    (SECTION_SOURCE_MAP) because this section is sqlite-backed, not a Sheets
    tab at all — there is no tab_data entry to key off. Gated on the
    library_docs source's config.yaml `enabled` flag, same convention as
    every other optional section (e.g. market_presence).
    """
    sources_by_id = {s["id"]: s for s in config.get("sources", [])}
    source = sources_by_id.get("library_docs", {})

    if not source.get("enabled", False):
        return {"enabled": False, "data": [], "has_data": False, "last_updated": None}

    conn = vkh_sqlite.connect()
    try:
        vkh_sqlite.migrate(conn, LIBRARY_DDL)
        docs = list_library_docs(conn)
        has_data = library_available(conn)
    finally:
        conn.close()

    dates = [d["doc_date"] for d in docs if d.get("doc_date")]
    last_updated = max(dates) if dates else None

    return {
        "enabled": True,
        "data": docs,
        "has_data": has_data,
        "last_updated": last_updated,
    }
