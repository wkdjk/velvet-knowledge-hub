# Run as: PYTHONPATH=. python scripts/sync_articles_curation.py
#
# sync_articles_curation.py — two-way sync between the articles_curation
# Sheets tab and news_articles (sqlite). D3, revision 2, Commander decision
# 2026-07-11 — closes SurveyorQ N-2 (a human control surface for articles).
#
# This is this pipeline's ONLY write path to hidden_by_commander/
# manual_override (news_schema.py: "this pipeline reads it, never writes
# it" — this script IS "this pipeline" for those two columns specifically;
# every other script only reads them).
#
# Mirrors scripts/ingest_library.py's sync_curation_tab() / promote pattern:
#   1. Push: any relevant=1 news_articles row not yet in the curation tab
#      gets a new row appended (raw_ref/title_ko filled, hidden/
#      manual_override blank — the Commander fills those in by hand).
#   2. Pull: every curation-tab row updates hidden_by_commander/
#      manual_override on the matching news_articles row (keyed on raw_ref).
#      An UPDATE, not a promotion — the news_articles row already exists by
#      the time an article reaches this tab (it must be relevant=1 to be
#      pushed in the first place).
#
# Security: no credentials in this file. All secrets from environment only.

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import vkh_sqlite  # noqa: E402
from scripts.news_schema import NEWS_DDL  # noqa: E402

ARTICLES_CURATION_TAB = "articles_curation"


def _is_truthy(val) -> bool:
    if isinstance(val, bool):
        return val
    return str(val).strip().upper() in ("TRUE", "1", "YES")


def list_pending_curation(conn: sqlite3.Connection) -> list[dict]:
    """
    Relevant articles not yet pushed to the curation tab — the push queue.
    Caller passes the set of raw_ref values already present in the tab;
    this just returns every candidate so the caller can diff.
    """
    cur = conn.execute(
        "SELECT n.raw_ref, r.title_ko "
        "FROM news_articles n JOIN raw_news_articles r ON r.id = n.raw_ref "
        "WHERE n.relevant = 1 "
        "ORDER BY n.raw_ref"
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def apply_curation_row(conn: sqlite3.Connection, raw_ref: int, hidden: bool, manual_override: bool) -> bool:
    """
    UPDATE news_articles' hidden_by_commander/manual_override for the row
    matching raw_ref. Returns True if a row was updated, False if no
    news_articles row exists for this raw_ref (e.g. hand-typed row before
    the article was ever judged — not an error, just nothing to apply yet).
    """
    cur = conn.execute(
        "UPDATE news_articles SET hidden_by_commander = ?, manual_override = ? WHERE raw_ref = ?",
        (1 if hidden else 0, 1 if manual_override else 0, raw_ref),
    )
    return cur.rowcount > 0


def sync_articles_curation(conn: sqlite3.Connection, spreadsheet) -> dict:
    """
    Full two-way sync. Returns a summary dict: new_rows_added / applied /
    skipped_no_match / skipped_blank_raw_ref.
    """
    ws = spreadsheet.worksheet(ARTICLES_CURATION_TAB)
    rows = ws.get_all_records()

    existing_raw_refs = {str(r.get("raw_ref", "")) for r in rows if r.get("raw_ref")}
    pending = list_pending_curation(conn)
    new_rows = [
        [p["raw_ref"], p["title_ko"], "", ""]
        for p in pending
        if str(p["raw_ref"]) not in existing_raw_refs
    ]
    if new_rows:
        ws.append_rows(new_rows, value_input_option="USER_ENTERED")

    applied = 0
    skipped_no_match = 0
    skipped_blank_raw_ref = 0

    for row in rows:
        raw_ref_str = str(row.get("raw_ref", "")).strip()
        if not raw_ref_str:
            skipped_blank_raw_ref += 1
            continue
        try:
            raw_ref = int(float(raw_ref_str))  # Sheets may return numeric cells as float
        except ValueError:
            skipped_blank_raw_ref += 1
            continue

        hidden = _is_truthy(row.get("hidden"))
        manual_override = _is_truthy(row.get("manual_override"))

        if apply_curation_row(conn, raw_ref, hidden, manual_override):
            applied += 1
        else:
            skipped_no_match += 1

    conn.commit()

    return {
        "new_rows_added": len(new_rows),
        "applied": applied,
        "skipped_no_match": skipped_no_match,
        "skipped_blank_raw_ref": skipped_blank_raw_ref,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync the articles_curation Sheets tab with news_articles (sqlite)."
    )
    parser.parse_args()

    from scripts.sheets_auth import connect_sheets, resolve_sheet_id

    sheet_id = resolve_sheet_id()
    spreadsheet = connect_sheets(sheet_id)
    conn = vkh_sqlite.connect()
    vkh_sqlite.migrate(conn, NEWS_DDL)
    try:
        result = sync_articles_curation(conn, spreadsheet)
    finally:
        conn.close()
    print(f"sync_articles_curation: {result}")


if __name__ == "__main__":
    main()
