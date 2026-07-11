# Run as: PYTHONPATH=. python scripts/test_build_weekly_brief.py
#
# test_build_weekly_brief.py — assert-based checks for
# scripts/build.py's run_weekly_brief_step() (SurveyorQ T3 re-merge audit,
# 2026-07-11: the class of bug that escaped review twice was "the new
# module's own tests pass but nothing tests the real caller in build.py" —
# scripts/test_weekly_brief.py exercises vkh_brief.py's functions directly;
# this file exercises build.py's own call site instead, with fakes for both
# the sqlite connection and the Sheet.
#
# No test framework in this repo (matches test_weekly_brief.py's
# convention). No network, no Sheets, no credentials, no Anthropic key
# (ANTHROPIC_API_KEY is popped from the environment for the duration of
# each check so a locally/CI-set key can never turn this into a live API
# call — vkh_brief._call_haiku() degrades to no-op without a key, L-12).
#
# Checks:
#   1. run_weekly_brief_step() rehydrates raw_weekly_brief_drafts from the
#      Sheet, runs the approvals sync, and returns the approved week's
#      context — reproduces SurveyorQ B-4 (approved brief must survive an
#      empty/ephemeral sqlite build cache).
#   2. run_weekly_brief_step() pushes a raw draft not yet present in the
#      Sheet — reproduces SurveyorQ B-2 (the Sheet round-trip must actually
#      be invoked, not just unit-tested in isolation).
#   3. weekly_brief.enabled=False short-circuits with no reads or writes.

import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.build import run_weekly_brief_step  # noqa: E402
from scripts.news_schema import NEWS_DDL  # noqa: E402

_checks_run = 0
_checks_failed = 0


def _check(label: str, condition: bool) -> None:
    global _checks_run, _checks_failed
    _checks_run += 1
    status = "ok" if condition else "FAIL"
    print(f"  [{status}] {label}")
    if not condition:
        _checks_failed += 1


def _fresh_conn() -> sqlite3.Connection:
    # row_factory = sqlite3.Row matches vkh_sqlite.connect() (the real conn
    # build.py passes in) — push_pending_drafts_to_sheet() reads columns by
    # name, so a plain tuple-row conn breaks it.
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    for stmt in NEWS_DDL:
        conn.execute(stmt)
    return conn


class _FakeWorksheet:
    """Tracks appended/updated rows for real, unlike a pure stub — this
    file's checks need to observe push_pending_drafts_to_sheet()'s effect."""

    def __init__(self, rows: list[dict]) -> None:
        self._header = [
            "week_ending_date", "draft_text", "fact_check_status", "fact_check_detail",
            "approved", "approved_at", "published_text", "notes",
        ]
        self._rows = [dict(r) for r in rows]

    def get_all_records(self) -> list[dict]:
        return [dict(r) for r in self._rows]

    def row_values(self, n: int) -> list[str]:
        return self._header

    def batch_update(self, *a, **kw) -> None:
        pass

    def append_rows(self, rows: list[list], value_input_option=None) -> None:
        for r in rows:
            self._rows.append(dict(zip(self._header, r)))


class _FakeSheet:
    def __init__(self, ws: _FakeWorksheet) -> None:
        self._ws = ws

    def worksheet(self, name: str):
        return self._ws


def _without_anthropic_key():
    """Context-manager-free pop/restore — no key means _call_haiku() no-ops."""
    return os.environ.pop("ANTHROPIC_API_KEY", None)


def _restore_anthropic_key(saved) -> None:
    if saved is not None:
        os.environ["ANTHROPIC_API_KEY"] = saved


def test_rehydrate_sync_and_read_approved_brief() -> None:
    print("test_rehydrate_sync_and_read_approved_brief")
    saved_key = _without_anthropic_key()
    try:
        conn = _fresh_conn()
        ws = _FakeWorksheet([
            {"week_ending_date": "2026-06-29", "draft_text": "Prior week text",
             "fact_check_status": "ok", "fact_check_detail": "",
             "approved": "TRUE", "approved_at": "2026-06-30",
             "published_text": "", "notes": ""},
        ])
        sheet = _FakeSheet(ws)
        config = {"weekly_brief": {"enabled": True}}

        brief_notice, weekly_brief = run_weekly_brief_step(
            config, sheet, conn, {}, {}, {}, "2026-07-11"
        )

        raw_count = conn.execute("SELECT COUNT(*) FROM raw_weekly_brief_drafts").fetchone()[0]
        _check("prior week rehydrated into raw_weekly_brief_drafts", raw_count == 1)

        promoted = conn.execute("SELECT approved FROM weekly_briefs").fetchone()
        _check("approvals sync promoted the rehydrated draft", tuple(promoted) == (1,))

        _check("no draft generated (no ANTHROPIC_API_KEY)", brief_notice == {"new_draft": False})
        _check("weekly_brief enabled", weekly_brief.get("enabled") is True)
        _check("approved brief is available", weekly_brief.get("available") is True)
        _check("approved brief text matches the rehydrated draft", weekly_brief.get("text") == "Prior week text")
        _check("approved brief week matches", weekly_brief.get("week_ending_date") == "2026-06-29")
    finally:
        _restore_anthropic_key(saved_key)


def test_push_unpushed_draft_to_sheet() -> None:
    print("test_push_unpushed_draft_to_sheet")
    saved_key = _without_anthropic_key()
    try:
        conn = _fresh_conn()
        conn.execute(
            "INSERT INTO raw_weekly_brief_drafts "
            "(week_ending_date, draft_text, fact_check_status, fact_check_detail, generated_at) "
            "VALUES ('2026-07-04', 'not yet on the sheet', 'ok', '', '2026-07-04T00:00:00Z')"
        )
        conn.commit()
        ws = _FakeWorksheet([])
        sheet = _FakeSheet(ws)
        config = {"weekly_brief": {"enabled": True}}

        run_weekly_brief_step(config, sheet, conn, {}, {}, {}, "2026-07-11")

        pushed_weeks = {r["week_ending_date"] for r in ws.get_all_records()}
        _check("existing unpushed draft reached the Sheet", "2026-07-04" in pushed_weeks)
    finally:
        _restore_anthropic_key(saved_key)


def test_kill_switch_no_reads_or_writes() -> None:
    print("test_kill_switch_no_reads_or_writes")
    conn = _fresh_conn()
    ws = _FakeWorksheet([
        {"week_ending_date": "2026-06-29", "draft_text": "Prior week text",
         "fact_check_status": "ok", "fact_check_detail": "",
         "approved": "TRUE", "approved_at": "2026-06-30",
         "published_text": "", "notes": ""},
    ])
    sheet = _FakeSheet(ws)
    config = {"weekly_brief": {"enabled": False}}

    brief_notice, weekly_brief = run_weekly_brief_step(config, sheet, conn, {}, {}, {}, "2026-07-11")

    _check("disabled: no draft generated", brief_notice == {"new_draft": False})
    _check("disabled: weekly_brief context reports enabled=False", weekly_brief == {"enabled": False})
    raw_count = conn.execute("SELECT COUNT(*) FROM raw_weekly_brief_drafts").fetchone()[0]
    _check("disabled: rehydrate/sync still ran (harmless — only the draft+read gate is off)", raw_count == 1)


def main() -> None:
    test_rehydrate_sync_and_read_approved_brief()
    test_push_unpushed_draft_to_sheet()
    test_kill_switch_no_reads_or_writes()

    print(f"\n{_checks_run} checks run, {_checks_failed} failed.")
    if _checks_failed:
        sys.exit(1)
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
