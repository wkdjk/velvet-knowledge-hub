# library_data.py — SKELETON ONLY. Read-side functions the dashboard build
# will call once the Library section is wired into vkh_render.py / templates.
# Not implemented — see design doc:
# Domain_Knowledge_staging/VKH_D1_library_scaffolding_proposal_2026-07-10.md
#
# Deep-module split (mirrors vkh_data.py / vkh_kpi.py / vkh_charts.py
# boundaries, kept from the C-12b audit, applied to the new sqlite spine):
#   library_schema.py — DDL (this section's "shape")
#   library_data.py   — THIS FILE: read queries for the build (this section's
#                        "what to show")
#   ingest_library.py — write path from Drive (this section's "how data
#                        arrives")
# No library_kpi.py / library_charts.py yet — Library has no numeric KPIs or
# charts, just a curated document list. Add those modules only when the
# section actually grows that kind of content (YAGNI) — don't scaffold empty
# files for a feature that isn't requested.
#
# Security: no credentials in this file.

import sqlite3


def list_library_docs(
    conn: sqlite3.Connection,
    category: str | None = None,
) -> list[sqlite3.Row]:
    """
    Return published library_docs rows, most recent doc_date first.

    NOT IMPLEMENTED. Intended body: simple SELECT ... ORDER BY doc_date DESC,
    optionally filtered by category. This is the only query the dashboard
    build needs for D1 — no join back to raw_library_files required (title/
    date/category/tags/summary are all in library_docs already).
    """
    raise NotImplementedError("Scaffolding only — see design doc for D1 build task")


def library_available(conn: sqlite3.Connection) -> bool:
    """
    Return True if library_docs has at least one row.

    Feeds the same graceful-degradation pattern every other VKH section
    uses (L-12): zero published docs renders a "reference library — coming
    soon" card, not an empty broken-looking section. NOT IMPLEMENTED.
    """
    raise NotImplementedError("Scaffolding only — see design doc for D1 build task")


def demo() -> None:
    """Self-check: both functions correctly raise NotImplementedError."""
    conn = sqlite3.connect(":memory:")
    for fn, args in [
        (list_library_docs, (conn,)),
        (library_available, (conn,)),
    ]:
        try:
            fn(*args)
            raise AssertionError(f"{fn.__name__} should have raised NotImplementedError")
        except NotImplementedError:
            pass
    print("library_data.py demo: OK — all skeleton functions raise NotImplementedError as expected")


if __name__ == "__main__":
    demo()
