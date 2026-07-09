# ingest_library.py — SKELETON ONLY. Function signatures + docstrings for the
# Library ingest flow. Not implemented — see design doc for why:
# Domain_Knowledge_staging/VKH_D1_library_scaffolding_proposal_2026-07-10.md
#
# Intended shape (mirrors ingest_from_drive.py's existing Drive-polling
# pattern, reworked to write sqlite instead of a Sheets tab):
#
#   1. ingest_from_drive.py gains a "library" folder key in FOLDER_MAP,
#      pointed at whatever Drive subfolder holds Library-bound uploads
#      (Commander to confirm subfolder — see design doc open questions).
#   2. It dispatches this script per file, same as ingest_qia.py etc.
#   3. This script writes ONE row to raw_library_files per file (dedup by
#      drive_file_id — a repeat poll of an already-seen file is a no-op).
#   4. Promotion to library_docs (title/date/category/tags/summary) is a
#      SEPARATE, manual step — not run by this script or by any cron. The
#      decision doc's "Data entry" section is explicit that Library
#      curation is hand-done, unlike the other three sections' parsed CSVs.
#      promote_one() below is the function a small future admin CLI or
#      script would call once the Commander has decided the metadata for a
#      given raw row.
#
# Security: no credentials in this file. All secrets from environment only
# (this file will need GOOGLE_SERVICE_ACCOUNT_JSON once implemented, same
# as ingest_from_drive.py's _build_drive_service()).

import argparse
import sqlite3
from pathlib import Path


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

    NOT IMPLEMENTED. Intended body: INSERT OR IGNORE keyed on drive_file_id,
    ingested_at = current UTC ISO timestamp, then check conn.total_changes
    (or cursor.rowcount) to report whether a row was actually written.
    """
    raise NotImplementedError("Scaffolding only — see design doc for D1 build task")


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
    Promote one raw_library_files row to a canonical library_docs row.

    NOT IMPLEMENTED. Intended body: INSERT into library_docs with
    curated_at = current UTC ISO timestamp. UNIQUE(file_ref) on the table
    itself is the guard against double-promotion — this function does not
    need to check first, just handle the resulting sqlite3.IntegrityError
    with a clear message ("already promoted, see library_docs row N").
    """
    raise NotImplementedError("Scaffolding only — see design doc for D1 build task")


def list_pending(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """
    Return every raw_library_files row with no matching library_docs row —
    the curation queue. NOT IMPLEMENTED (trivial NOT IN query, deferred
    with the rest of this module so the whole ingest flow lands as one
    reviewable unit rather than piecemeal).
    """
    raise NotImplementedError("Scaffolding only — see design doc for D1 build task")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Ingest a file into raw_library_files (SKELETON — not implemented). "
            "See scripts/ingest_from_drive.py for the Drive-polling pattern this "
            "will plug into once built."
        )
    )
    parser.add_argument("--file", type=Path, help="Path to a downloaded Drive file")
    parser.add_argument("--source-folder", default="", help="Drive subfolder name/id")
    parser.add_argument("--dry-run", action="store_true")
    parser.parse_args()
    raise NotImplementedError("Scaffolding only — see design doc for D1 build task")


def demo() -> None:
    """
    Self-check for a skeleton file: every public function must correctly
    raise NotImplementedError (so a future partial implementation can't be
    silently skipped by a caller expecting a real return value).
    """
    conn = sqlite3.connect(":memory:")
    for fn, args in [
        (ingest_one_file, (conn, "id", "f.pdf", "folder", "pdf", "", "{}")),
        (promote_one, (conn, 1, "title", None, None, None, None)),
        (list_pending, (conn,)),
    ]:
        try:
            fn(*args)
            raise AssertionError(f"{fn.__name__} should have raised NotImplementedError")
        except NotImplementedError:
            pass
    print("ingest_library.py demo: OK — all skeleton functions raise NotImplementedError as expected")


if __name__ == "__main__":
    demo()
