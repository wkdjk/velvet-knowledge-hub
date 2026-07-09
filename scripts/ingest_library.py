# ingest_library.py — Library ingest flow (D1, Phase D rebuild, full build
# 2026-07-10). Writes Drive file metadata to raw_library_files (sqlite) and
# syncs the library_curation Sheets tab to promote/update library_docs rows.
#
# Design doc: Domain_Knowledge_staging/VKH_D1_library_scaffolding_proposal_2026-07-10.md
#
# Flow (design doc §5, dispatch pattern matches every other ingest_*.py):
#   1. ingest_from_drive.py's "library" FOLDER_MAP key downloads each file in
#      the Drive subfolder and dispatches this script per file:
#        python scripts/ingest_library.py --file <path> \
#            --drive-file-id <id> --mime-type <mime> [--source-folder library]
#      ingest_one_file() writes ONE row to raw_library_files (dedup on
#      drive_file_id — a repeat poll of an already-seen file is a no-op).
#   2. Separately (cron or manual), --sync reads the library_curation Sheets
#      tab, pushes any newly-pending raw files into it (so the Commander sees
#      what needs curating), then promotes/updates library_docs for every row
#      with a non-blank title:
#        python scripts/ingest_library.py --sync
#
# promote_or_update_one() is the upsert (keyed on file_ref, which is
# UNIQUE on library_docs) that makes re-running --sync safe whether a row is
# being promoted for the first time or the Commander has edited an
# already-curated row (2026-07-10 pre-mortem, correction A).
#
# Security: no credentials in this file. All secrets from environment only
# (GOOGLE_SERVICE_ACCOUNT_JSON, used only by --sync via sheets_auth.py).

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from scripts import vkh_sqlite
from scripts.library_schema import LIBRARY_DDL

LIBRARY_CURATION_TAB = "library_curation"

# Drive mimeType -> short file_type label (design doc §7 item 5, ratified:
# Drive mimeType primary, filename-suffix fallback). Extend this map, don't
# add a dependency, if a new file type shows up (ponytail).
_MIME_TO_TYPE = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/msword": "doc",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.ms-excel": "xls",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "image/jpeg": "jpg",
    "image/png": "png",
}


def _resolve_file_type(mime_type: str, filename: str) -> str:
    """Drive mimeType primary, filename-suffix fallback (design doc, ratified)."""
    if mime_type in _MIME_TO_TYPE:
        return _MIME_TO_TYPE[mime_type]
    suffix = Path(filename).suffix.lstrip(".").lower()
    return suffix or "unknown"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Write path — Drive poll -> raw_library_files
# ---------------------------------------------------------------------------

def ingest_one_file(
    conn: sqlite3.Connection,
    drive_file_id: str,
    filename: str,
    source_folder: str,
    file_type: str,
    uploaded_at: str,
    raw_metadata_json: str,
) -> bool:
    """
    Write one row to raw_library_files. Returns True if inserted, False if
    drive_file_id already exists (dedup no-op — not an error).
    """
    cur = conn.execute(
        "INSERT OR IGNORE INTO raw_library_files "
        "(drive_file_id, filename, source_folder, file_type, uploaded_at, ingested_at, raw_metadata) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (drive_file_id, filename, source_folder, file_type, uploaded_at, _utc_now_iso(), raw_metadata_json),
    )
    conn.commit()
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Read path — curation queue
# ---------------------------------------------------------------------------

def list_pending(conn: sqlite3.Connection) -> list[dict]:
    """
    Return every raw_library_files row with no matching library_docs row —
    the curation queue (design doc §3 rationale: a query, not a table).
    """
    cur = conn.execute(
        "SELECT * FROM raw_library_files "
        "WHERE id NOT IN (SELECT file_ref FROM library_docs) "
        "ORDER BY ingested_at"
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# Promotion — raw_library_files -> library_docs
# ---------------------------------------------------------------------------

def promote_one(
    conn: sqlite3.Connection,
    raw_file_id: int,
    title: str,
    doc_date: str | None,
    category: str | None,
    tags: str | None,
    summary: str | None,
    curated_by: str = "Commander",
) -> None:
    """
    Insert-only promotion of one raw_library_files row to library_docs.

    UNIQUE(file_ref) is the schema-level guard against double-promotion.
    Raises RuntimeError with a Commander-readable message (naming the
    existing library_docs row) rather than letting sqlite3.IntegrityError's
    raw stack trace surface — see design doc's test-as-specification
    checklist item 5. Callers that want "insert or update" should use
    promote_or_update_one() instead (pre-mortem correction A).
    """
    try:
        conn.execute(
            "INSERT INTO library_docs (file_ref, title, doc_date, category, tags, summary, curated_at, curated_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (raw_file_id, title, doc_date, category, tags, summary, _utc_now_iso(), curated_by),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        existing = conn.execute(
            "SELECT id FROM library_docs WHERE file_ref = ?", (raw_file_id,)
        ).fetchone()
        row_note = f", see library_docs row {existing[0]}" if existing else ""
        raise RuntimeError(
            f"raw_library_files id {raw_file_id} already promoted{row_note}"
        ) from exc


def promote_or_update_one(
    conn: sqlite3.Connection,
    raw_file_id: int,
    title: str,
    doc_date: str | None,
    category: str | None,
    tags: str | None,
    summary: str | None,
    curated_by: str = "Commander",
) -> str:
    """
    Upsert into library_docs keyed on file_ref (UNIQUE).

    2026-07-10 pre-mortem correction A (binding): if the
    Commander edits an already-curated row in library_curation after first
    promotion, the sync must UPDATE the existing row, not attempt a second
    INSERT (which would hit UNIQUE(file_ref) and fail). This is the single
    upsert function sync_curation_tab() calls — no separate reconciliation
    service (ponytail).

    Returns "inserted" or "updated".
    """
    existing = conn.execute(
        "SELECT id FROM library_docs WHERE file_ref = ?", (raw_file_id,)
    ).fetchone()

    if existing is None:
        conn.execute(
            "INSERT INTO library_docs (file_ref, title, doc_date, category, tags, summary, curated_at, curated_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (raw_file_id, title, doc_date, category, tags, summary, _utc_now_iso(), curated_by),
        )
        conn.commit()
        return "inserted"

    conn.execute(
        "UPDATE library_docs SET title = ?, doc_date = ?, category = ?, tags = ?, summary = ? "
        "WHERE file_ref = ?",
        (title, doc_date, category, tags, summary, raw_file_id),
    )
    conn.commit()
    return "updated"


# ---------------------------------------------------------------------------
# Sync — library_curation Sheets tab <-> sqlite
# ---------------------------------------------------------------------------

def sync_curation_tab(conn: sqlite3.Connection, spreadsheet) -> dict:
    """
    Two-way sync between the library_curation Sheets tab and sqlite.

    1. Push: any raw_library_files row not yet in the curation tab gets a new
       row appended (drive_file_id/filename filled, everything else blank —
       the Commander fills in title etc. by hand).
    2. Pull: any curation-tab row with a non-blank title gets
       promote_or_update_one()'d — covers both first promotion and editing
       an already-curated row.

    Returns a summary dict: new_rows_added / promoted / updated / skipped_blank_title.
    """
    ws = spreadsheet.worksheet(LIBRARY_CURATION_TAB)
    rows = ws.get_all_records()

    existing_ids = {str(r.get("drive_file_id", "")) for r in rows if r.get("drive_file_id")}
    pending = list_pending(conn)
    new_rows = [
        [p["drive_file_id"], p["filename"], "", "", "", "", ""]
        for p in pending
        if str(p["drive_file_id"]) not in existing_ids
    ]
    if new_rows:
        ws.append_rows(new_rows, value_input_option="USER_ENTERED")

    promoted = 0
    updated = 0
    skipped_blank_title = 0
    skipped_no_raw_match = 0

    for row in rows:
        title = str(row.get("title", "")).strip()
        drive_file_id = str(row.get("drive_file_id", "")).strip()
        if not title or not drive_file_id:
            skipped_blank_title += 1
            continue

        raw = conn.execute(
            "SELECT id FROM raw_library_files WHERE drive_file_id = ?", (drive_file_id,)
        ).fetchone()
        if raw is None:
            # Curation row references a drive_file_id not (yet) in
            # raw_library_files — e.g. hand-typed row before the next Drive
            # poll. Not an error; just nothing to promote yet.
            skipped_no_raw_match += 1
            continue

        result = promote_or_update_one(
            conn,
            raw[0],
            title,
            row.get("doc_date") or None,
            row.get("category") or None,
            row.get("tags") or None,
            row.get("summary") or None,
        )
        if result == "inserted":
            promoted += 1
        else:
            updated += 1

    return {
        "new_rows_added": len(new_rows),
        "promoted": promoted,
        "updated": updated,
        "skipped_blank_title": skipped_blank_title,
        "skipped_no_raw_match": skipped_no_raw_match,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Ingest a Library file into raw_library_files, or sync the "
            "library_curation Sheets tab to promote/update library_docs."
        )
    )
    parser.add_argument("--file", type=Path, help="Path to a downloaded Drive file")
    parser.add_argument("--drive-file-id", help="Drive file ID (dedup key)")
    parser.add_argument("--mime-type", default="", help="Drive mimeType, for file_type resolution")
    parser.add_argument("--source-folder", default="library", help="Drive subfolder name/id")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--sync", action="store_true",
        help="Sync the library_curation Sheets tab instead of ingesting a file",
    )
    args = parser.parse_args()

    if args.sync:
        # Local import: only --sync needs Sheets credentials (L-3 pattern —
        # keep the file-ingest path importable/testable with no .env at all).
        from scripts.sheets_auth import connect_sheets, resolve_sheet_id

        sheet_id = resolve_sheet_id()
        spreadsheet = connect_sheets(sheet_id)
        conn = vkh_sqlite.connect()
        vkh_sqlite.migrate(conn, LIBRARY_DDL)
        try:
            result = sync_curation_tab(conn, spreadsheet)
        finally:
            conn.close()
        print(f"sync_curation_tab: {result}")
        return

    if not args.file or not args.drive_file_id:
        parser.error("--file and --drive-file-id are required unless --sync is used")

    file_type = _resolve_file_type(args.mime_type, args.file.name)
    raw_metadata_json = json.dumps({"mime_type": args.mime_type})

    if args.dry_run:
        print(
            f"[DRY-RUN] Would ingest {args.file.name} "
            f"(drive_file_id={args.drive_file_id}, file_type={file_type})"
        )
        return

    conn = vkh_sqlite.connect()
    vkh_sqlite.migrate(conn, LIBRARY_DDL)
    try:
        inserted = ingest_one_file(
            conn, args.drive_file_id, args.file.name, args.source_folder,
            file_type, "", raw_metadata_json,
        )
    finally:
        conn.close()
    print(f"ingest_library: {'inserted' if inserted else 'already present (no-op)'} — {args.file.name}")


if __name__ == "__main__":
    main()
