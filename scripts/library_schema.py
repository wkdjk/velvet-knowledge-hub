# library_schema.py — sqlite DDL for the Library section (D1, Phase D rebuild).
#
# Design doc: Domain_Knowledge_staging/VKH_D1_library_scaffolding_proposal_2026-07-10.md
#
# Mirrors schema.py's role for the old Sheets pipeline (single source of
# truth for a section's column/table shape) but for sqlite tables instead of
# Sheets tab headers. Two tables, raw-store + mapping-layer pattern
# generalised from the existing raw_vfi tab (Commander directive 2026-07-09):
#
#   raw_library_files — append-only. Accepts whatever arrives (file metadata
#     only) without rejecting on shape. Never hand-edited.
#   library_docs      — canonical. A human (Commander) curates each row from
#     the raw layer: title, date, category, tags, summary.
#
# No code here writes to these tables yet — see scripts/ingest_library.py
# (skeleton signatures only, not implemented) and scripts/vkh_sqlite.py
# (shared connect/migrate helper, generic across every rebuilt section).
#
# Security: no credentials in this file.

# ---------------------------------------------------------------------------
# raw_library_files
#
# One row per file Drive-polling finds. Dedup key: drive_file_id (globally
# unique, stable across re-polls — a re-ingest of the same physical file is a
# no-op INSERT OR IGNORE, not a new row). raw_metadata is a JSON-string
# overflow column: whatever extra Drive file-resource fields show up land
# here without a migration, per the "don't reject on shape" principle.
# ponytail: overflow into raw_metadata as JSON text rather than adding a
# typed column per field seen so far — promote a field to its own column
# only once a query actually needs to filter/sort on it.
# ---------------------------------------------------------------------------
RAW_LIBRARY_FILES_SQL = """
CREATE TABLE IF NOT EXISTS raw_library_files (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    drive_file_id   TEXT NOT NULL UNIQUE,
    filename        TEXT NOT NULL,
    source_folder   TEXT NOT NULL,
    file_type       TEXT NOT NULL,
    uploaded_at     TEXT,
    ingested_at     TEXT NOT NULL,
    raw_metadata    TEXT
)
"""

# ---------------------------------------------------------------------------
# library_docs
#
# One row per curated document. UNIQUE(file_ref) means a raw file can be
# promoted at most once — this is what makes "re-ingest of the same file
# doesn't duplicate the curated row" a schema-level guarantee rather than an
# application-level check (see test-as-specification checklist in the
# design doc).
#
# A raw_library_files row with no matching library_docs row is simply
# "pending curation" — there is no separate needs_review table for Library.
# Unlike map_companies (which resolves a closed set of company/country/type
# names against automatic matching), Library curation is manual by design
# (decision doc's "Data entry" section) — the review queue IS
# "SELECT * FROM raw_library_files WHERE id NOT IN (SELECT file_ref FROM
# library_docs)", not a third table to keep in sync.
# ---------------------------------------------------------------------------
LIBRARY_DOCS_SQL = """
CREATE TABLE IF NOT EXISTS library_docs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    file_ref        INTEGER NOT NULL UNIQUE REFERENCES raw_library_files(id),
    title           TEXT NOT NULL,
    doc_date        TEXT,
    category        TEXT,
    tags            TEXT,
    summary         TEXT,
    curated_at      TEXT NOT NULL,
    curated_by      TEXT NOT NULL DEFAULT 'Commander'
)
"""

_LIBRARY_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_library_docs_date ON library_docs(doc_date)",
    "CREATE INDEX IF NOT EXISTS idx_library_docs_category ON library_docs(category)",
]

# Exported so scripts/vkh_sqlite.py's migrate() can apply every DDL statement
# for this section in one call, and so D2/D3/D4 each expose their own
# equivalent *_DDL list from their own schema module — no central "list every
# table in the whole app" file to keep in sync.
LIBRARY_DDL: list[str] = [RAW_LIBRARY_FILES_SQL, LIBRARY_DOCS_SQL, *_LIBRARY_INDEXES_SQL]


def demo() -> None:
    """Self-check: DDL is valid SQL and both tables are creatable in-memory."""
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    for stmt in LIBRARY_DDL:
        conn.execute(stmt)

    conn.execute(
        "INSERT INTO raw_library_files "
        "(drive_file_id, filename, source_folder, file_type, ingested_at) "
        "VALUES ('abc123', 'test.pdf', '02_Library', 'pdf', '2026-07-10T00:00:00')"
    )
    file_id = conn.execute("SELECT id FROM raw_library_files").fetchone()[0]
    conn.execute(
        "INSERT INTO library_docs (file_ref, title, curated_at) VALUES (?, ?, ?)",
        (file_id, "Test document", "2026-07-10T00:00:00"),
    )

    # UNIQUE(file_ref) must reject a second promotion of the same raw file.
    try:
        conn.execute(
            "INSERT INTO library_docs (file_ref, title, curated_at) VALUES (?, ?, ?)",
            (file_id, "Duplicate promotion", "2026-07-10T00:00:00"),
        )
        raise AssertionError("Expected UNIQUE(file_ref) to reject a second promotion")
    except sqlite3.IntegrityError:
        pass

    assert conn.execute("SELECT COUNT(*) FROM library_docs").fetchone()[0] == 1
    print("library_schema.py demo: OK — DDL valid, UNIQUE(file_ref) enforced")


if __name__ == "__main__":
    demo()
