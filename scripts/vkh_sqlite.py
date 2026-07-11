# vkh_sqlite.py — shared sqlite connect/migrate helper for the Phase D rebuild.
#
# Design doc: Domain_Knowledge_staging/VKH_D1_library_scaffolding_proposal_2026-07-10.md
#
# Deep-module rationale: every rebuilt section (library, trade_stats,
# news_articles, product pages) needs the same three lines — open the DB
# file, turn on foreign_keys, apply that section's CREATE TABLE statements —
# before it can do anything else. Putting this in one module means D2/D3/D4
# each write a schema.py-equivalent (list of DDL strings, see
# scripts/library_schema.py) and call migrate() here, instead of every
# ingest script re-opening sqlite3.connect() with its own pragma calls.
# Mirrors the role scripts/sheets_auth.py played for the old Sheets pipeline.
#
# DB location: repo root, alongside config.yaml — vkh.sqlite is the source
# registry's data counterpart, not a build artefact like docs/index.html
# (which is regenerated every run and is fine to live under docs/).
#
# Not yet wired into build.py / any ingest script — this is scaffolding.
#
# Security: no credentials in this file.

import sqlite3
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "vkh.sqlite"


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    """
    Open the VKH sqlite DB with foreign keys enforced and dict-like row access.

    sqlite3.Row gives column-name access (row["title"]) without adding a
    dependency — the ladder's rung 3 (stdlib) before rung 5 (a new package).

    busy_timeout (D3 side-observation, SurveyorQ advisory 2026-07-11, applies
    fleet-wide to D1/D2/D3): without this, a write from a scheduled
    collection run overlapping a manual classify/build run fails immediately
    with "database is locked" instead of waiting briefly. One line, cheap,
    solo-operator-safe — no behaviour change for the common case of one
    writer at a time.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def migrate(conn: sqlite3.Connection, ddl_statements: list[str]) -> None:
    """
    Apply a list of CREATE TABLE / CREATE INDEX statements idempotently.

    Every statement must be its own IF NOT EXISTS-guarded string (see
    library_schema.py) — this function does no diffing, it just re-applies
    DDL that is already safe to run every time.
    """
    for stmt in ddl_statements:
        conn.execute(stmt)
    conn.commit()


def demo() -> None:
    """Self-check: connect() + migrate() work end to end against a temp file."""
    import tempfile

    from scripts.library_schema import LIBRARY_DDL

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.sqlite"
        conn = connect(db_path)
        migrate(conn, LIBRARY_DDL)

        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {"raw_library_files", "library_docs"} <= tables, tables
        conn.close()
    print("vkh_sqlite.py demo: OK — connect() + migrate() created both tables")


if __name__ == "__main__":
    demo()
