# Run as: PYTHONPATH=. python scripts/test_library_ingest.py
#
# test_library_ingest.py — coverage for ingest_library.py / library_data.py
# (D1 full build, 2026-07-10).
#
# No test framework (matches test_news_pipeline.py/test_validate_config.py
# convention — this repo has no pytest config). Assert-based script,
# in-memory sqlite only — no network, no Sheets, no credentials required.
#
# sync_curation_tab() is tested against a minimal fake spreadsheet object
# (not real gspread) — this is the fixture-based simulation of the "Sheets
# curation tab" half of the pipeline documented in the D1 full implementation
# report as simulated, not live, per the dispatch brief's explicit allowance.

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.ingest_library import (  # noqa: E402
    _resolve_file_type,
    ingest_one_file,
    list_pending,
    promote_one,
    promote_or_update_one,
    sync_curation_tab,
)
from scripts.library_data import (  # noqa: E402
    _sanitise_download_url,
    library_available,
    list_library_docs,
)
from scripts.library_schema import LIBRARY_DDL  # noqa: E402


def _fresh_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    for stmt in LIBRARY_DDL:
        conn.execute(stmt)
    return conn


class _FakeWorksheet:
    """Minimal stand-in for a gspread Worksheet — get_all_records() + append_rows()."""

    def __init__(self, headers: list[str], rows: list[dict]):
        self.headers = headers
        self.rows = rows  # list of dicts, mutated in place like a live sheet

    def get_all_records(self) -> list[dict]:
        return self.rows

    def append_rows(self, new_rows: list[list], value_input_option: str = "USER_ENTERED") -> None:
        for values in new_rows:
            self.rows.append(dict(zip(self.headers, values)))


class _FakeSpreadsheet:
    def __init__(self, worksheet: _FakeWorksheet):
        self._worksheet = worksheet

    def worksheet(self, name: str) -> _FakeWorksheet:
        return self._worksheet


_CURATION_HEADERS = ["drive_file_id", "filename", "title", "doc_date", "category", "tags", "summary", "download_url"]


def test_dedup_on_drive_file_id():
    conn = _fresh_conn()
    inserted_1 = ingest_one_file(conn, "abc123", "report.pdf", "library", "pdf", "", "{}")
    inserted_2 = ingest_one_file(conn, "abc123", "report.pdf", "library", "pdf", "", "{}")
    assert inserted_1 is True
    assert inserted_2 is False
    count = conn.execute("SELECT COUNT(*) FROM raw_library_files").fetchone()[0]
    assert count == 1


def test_list_pending_excludes_promoted():
    conn = _fresh_conn()
    ingest_one_file(conn, "id-1", "a.pdf", "library", "pdf", "", "{}")
    ingest_one_file(conn, "id-2", "b.pdf", "library", "pdf", "", "{}")
    pending = list_pending(conn)
    assert {p["drive_file_id"] for p in pending} == {"id-1", "id-2"}

    raw_id = conn.execute(
        "SELECT id FROM raw_library_files WHERE drive_file_id = 'id-1'"
    ).fetchone()[0]
    promote_one(conn, raw_id, "Title A", None, None, None, None)

    pending = list_pending(conn)
    assert {p["drive_file_id"] for p in pending} == {"id-2"}


def test_unique_file_ref_still_enforced():
    conn = _fresh_conn()
    ingest_one_file(conn, "id-1", "a.pdf", "library", "pdf", "", "{}")
    raw_id = conn.execute("SELECT id FROM raw_library_files").fetchone()[0]
    promote_one(conn, raw_id, "First title", None, None, None, None)

    try:
        promote_one(conn, raw_id, "Second attempt", None, None, None, None)
        raise AssertionError("Expected RuntimeError on double promotion")
    except RuntimeError as exc:
        assert "already promoted" in str(exc)

    count = conn.execute("SELECT COUNT(*) FROM library_docs").fetchone()[0]
    assert count == 1


def test_promote_or_update_one_upsert_path():
    conn = _fresh_conn()
    ingest_one_file(conn, "id-1", "a.pdf", "library", "pdf", "", "{}")
    raw_id = conn.execute("SELECT id FROM raw_library_files").fetchone()[0]

    result_1 = promote_or_update_one(conn, raw_id, "Original title", "2026-01-01", "pricing", "kg", "sum")
    assert result_1 == "inserted"

    # Correction A: editing an already-curated row must UPDATE, not fail on
    # UNIQUE(file_ref).
    result_2 = promote_or_update_one(conn, raw_id, "Edited title", "2026-02-02", "yearbook", "kg,price", "sum2")
    assert result_2 == "updated"

    count = conn.execute("SELECT COUNT(*) FROM library_docs").fetchone()[0]
    assert count == 1
    row = conn.execute("SELECT title, doc_date, category FROM library_docs WHERE file_ref = ?", (raw_id,)).fetchone()
    assert row == ("Edited title", "2026-02-02", "yearbook")


def test_library_available_zero_rows():
    conn = _fresh_conn()
    assert library_available(conn) is False
    assert list_library_docs(conn) == []


def test_library_available_after_promotion():
    conn = _fresh_conn()
    ingest_one_file(conn, "id-1", "a.pdf", "library", "pdf", "", "{}")
    raw_id = conn.execute("SELECT id FROM raw_library_files").fetchone()[0]
    promote_one(conn, raw_id, "Title A", "2026-01-01", "pricing", None, "summary text")
    assert library_available(conn) is True
    docs = list_library_docs(conn)
    assert len(docs) == 1
    assert docs[0]["title"] == "Title A"


def test_resolve_file_type_mime_primary_suffix_fallback():
    assert _resolve_file_type("application/pdf", "whatever.bin") == "pdf"
    assert _resolve_file_type("application/octet-stream", "report.docx") == "docx"
    assert _resolve_file_type("", "no_suffix") == "unknown"


def test_sync_curation_tab_push_then_promote_then_update():
    conn = _fresh_conn()
    # Simulated Drive poll: one file lands in raw_library_files (fixture,
    # not a live Drive call — see test module docstring).
    ingest_one_file(conn, "drive-id-1", "yearbook.pdf", "library", "pdf", "", "{}")

    ws = _FakeWorksheet(_CURATION_HEADERS, rows=[])
    spreadsheet = _FakeSpreadsheet(ws)

    # First sync: pushes the pending file into the curation tab (title blank).
    result_1 = sync_curation_tab(conn, spreadsheet)
    assert result_1["new_rows_added"] == 1
    assert result_1["promoted"] == 0
    assert len(ws.rows) == 1
    assert ws.rows[0]["drive_file_id"] == "drive-id-1"
    assert ws.rows[0]["title"] == ""

    # Commander fills in the title (simulated Sheets edit).
    ws.rows[0]["title"] = "2025 velvet pricing yearbook"
    ws.rows[0]["category"] = "pricing"

    # Second sync: promotes the now-titled row.
    result_2 = sync_curation_tab(conn, spreadsheet)
    assert result_2["new_rows_added"] == 0
    assert result_2["promoted"] == 1
    assert library_available(conn) is True
    docs = list_library_docs(conn)
    assert docs[0]["title"] == "2025 velvet pricing yearbook"

    # Commander edits the title after first promotion (correction A path).
    ws.rows[0]["title"] = "2025 velvet pricing yearbook (revised)"
    result_3 = sync_curation_tab(conn, spreadsheet)
    assert result_3["updated"] == 1
    assert result_3["promoted"] == 0
    docs = list_library_docs(conn)
    assert docs[0]["title"] == "2025 velvet pricing yearbook (revised)"
    assert len(docs) == 1  # still one row, not a duplicate


def test_download_url_round_trips_through_promote_and_update():
    conn = _fresh_conn()
    ingest_one_file(conn, "id-1", "a.pdf", "library", "pdf", "", "{}")
    raw_id = conn.execute("SELECT id FROM raw_library_files").fetchone()[0]

    promote_or_update_one(
        conn, raw_id, "Title A", "2026-01-01", "pricing", None, "summary",
        download_url="https://example.com/report.pdf",
    )
    docs = list_library_docs(conn)
    assert docs[0]["download_url"] == "https://example.com/report.pdf"

    # Editing an already-curated row (correction A path) must also carry
    # download_url through the UPDATE branch, not just the INSERT branch.
    promote_or_update_one(
        conn, raw_id, "Title A", "2026-01-01", "pricing", None, "summary",
        download_url="https://example.com/v2.pdf",
    )
    docs = list_library_docs(conn)
    assert len(docs) == 1
    assert docs[0]["download_url"] == "https://example.com/v2.pdf"


def test_download_url_scheme_guard_at_read_time():
    conn = _fresh_conn()
    ingest_one_file(conn, "id-1", "a.pdf", "library", "pdf", "", "{}")
    raw_id = conn.execute("SELECT id FROM raw_library_files").fetchone()[0]

    # A garbage-scheme URL is stored as given (curation-tab data is never
    # rejected on write — see promote_or_update_one() docstring) but must be
    # treated as absent by the read path that feeds the template.
    promote_or_update_one(
        conn, raw_id, "Title A", None, None, None, None,
        download_url="javascript:alert(1)",
    )
    docs = list_library_docs(conn)
    assert docs[0]["download_url"] is None

    # A well-formed https URL passes through unchanged.
    assert _sanitise_download_url("https://dinz.co.nz/guide.pdf") == "https://dinz.co.nz/guide.pdf"
    # Disallowed/malformed schemes and blank/whitespace-only strings are
    # treated as absent, not crash the build.
    assert _sanitise_download_url("javascript:alert(1)") is None
    assert _sanitise_download_url("ftp://example.com/x") is None
    assert _sanitise_download_url("") is None
    assert _sanitise_download_url("   ") is None
    assert _sanitise_download_url(None) is None
    assert _sanitise_download_url("not a url at all") is None


def main() -> None:
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    for test in tests:
        test()
        print(f"  PASS: {test.__name__}")
    print(f"test_library_ingest.py: {len(tests)}/{len(tests)} passed")


if __name__ == "__main__":
    main()
