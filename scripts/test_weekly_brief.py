# Run as: PYTHONPATH=. python scripts/test_weekly_brief.py
#
# test_weekly_brief.py — assert-based checks for scripts/vkh_brief.py
# (C-14 item 4, 2026-07-05; storage rewired to sqlite, D3 Phase D rebuild,
# 2026-07-11 — see get_weekly_brief_context()'s new (config, conn) signature).
#
# No test framework in this repo (matches test_news_pipeline.py's
# convention). No network, no Sheets, no credentials — pure fixture-based
# checks of:
#   1. fact_check_draft() catches a deliberately wrong figure and passes a
#      correct one. UNCHANGED logic from C-14 — this coverage is unchanged.
#   2. generate_weekly_brief_draft()'s sqlite idempotency (UNIQUE(week_ending_date)).
#   3. get_weekly_brief_context() respects the weekly_brief.enabled kill
#      switch and returns available=False with no approved rows.
#   4. get_weekly_brief_context() picks the most recently *approved* week,
#      never an unapproved newer draft.
#   5. Staleness flips at stale_after_days.
#   6. sync_weekly_brief_approvals() promotes/updates weekly_briefs from a
#      fake Sheets curation surface.

import sqlite3
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.news_schema import NEWS_DDL  # noqa: E402
from scripts.vkh_brief import (  # noqa: E402
    fact_check_draft,
    generate_weekly_brief_draft,
    get_weekly_brief_context,
    sync_weekly_brief_approvals,
)
from scripts.vkh_data import _today_kst  # noqa: E402

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
    conn = sqlite3.connect(":memory:")
    for stmt in NEWS_DDL:
        conn.execute(stmt)
    return conn


_SAMPLE_KPI = {
    "qia_rolling12m_kg": "153,674",
    "qia_yoy_label": "▼ 18.3% vs Apr 2025",
    "articles_90d": "12",
    "food_imports_90d": "7",
    "nz_export_latest": "8,810.5",
    "nz_export_delta": "▲ 14.4%",
}
_SAMPLE_CHART_DATA = {"yoy_chip": {"label": "▼ 18.3%", "direction": "down"}, "triangulation": {}}


def test_fact_check_catches_wrong_number() -> None:
    print("test_fact_check_catches_wrong_number")
    correct = "Korea imports fell 18.3% to 153,674 kg over the last 12 months. News coverage held steady at 12 articles."
    status, detail = fact_check_draft(correct, _SAMPLE_KPI, _SAMPLE_CHART_DATA)
    _check("correct draft passes fact-check", status == "ok")

    wrong = "Korea imports fell 57.5% to 999,999 kg over the last 12 months."
    status, detail = fact_check_draft(wrong, _SAMPLE_KPI, _SAMPLE_CHART_DATA)
    _check("wrong draft flagged review_needed", status == "review_needed")
    _check("wrong draft detail names the bad figures", "57.5%" in detail and "999,999kg" in detail)


def test_fact_check_ignores_non_kpi_numbers() -> None:
    print("test_fact_check_ignores_non_kpi_numbers")
    text = "Food import declarations over the last 90 days held steady."
    status, _ = fact_check_draft(text, _SAMPLE_KPI, _SAMPLE_CHART_DATA)
    _check("window descriptor ('90 days') is not fact-checked", status == "ok")


def test_generate_weekly_brief_draft_idempotent_per_week() -> None:
    print("test_generate_weekly_brief_draft_idempotent_per_week")
    conn = _fresh_conn()
    conn.execute(
        "INSERT INTO raw_weekly_brief_drafts (week_ending_date, draft_text, fact_check_status, generated_at) "
        "VALUES ('2026-07-06', 'existing draft', 'ok', '2026-07-06T00:00:00Z')"
    )
    conn.commit()

    result = generate_weekly_brief_draft(
        {"weekly_brief": {"enabled": True}}, conn, _SAMPLE_KPI, _SAMPLE_CHART_DATA, {}, "2026-07-06",
    )
    _check("existing week is a no-op — never calls Haiku, never duplicates", result == {"new_draft": False})
    count = conn.execute("SELECT COUNT(*) FROM raw_weekly_brief_drafts").fetchone()[0]
    _check("still exactly one draft row for this week", count == 1)


def test_generate_weekly_brief_draft_kill_switch() -> None:
    print("test_generate_weekly_brief_draft_kill_switch")
    conn = _fresh_conn()
    result = generate_weekly_brief_draft(
        {"weekly_brief": {"enabled": False}}, conn, _SAMPLE_KPI, _SAMPLE_CHART_DATA, {}, "2026-07-06",
    )
    _check("enabled=False is a hard no-op", result == {"new_draft": False})
    count = conn.execute("SELECT COUNT(*) FROM raw_weekly_brief_drafts").fetchone()[0]
    _check("no row written when disabled", count == 0)


def test_kill_switch_and_no_approved_rows() -> None:
    print("test_kill_switch_and_no_approved_rows")
    conn = _fresh_conn()

    ctx = get_weekly_brief_context({"weekly_brief": {"enabled": False}}, conn)
    _check("enabled=False returns enabled=False, no other keys assumed", ctx == {"enabled": False})

    conn.execute(
        "INSERT INTO raw_weekly_brief_drafts (week_ending_date, draft_text, fact_check_status, generated_at) "
        "VALUES ('2026-07-06', 'draft only', 'ok', '2026-07-06T00:00:00Z')"
    )
    conn.commit()
    ctx = get_weekly_brief_context({"weekly_brief": {"enabled": True}}, conn)
    _check("enabled, no approved rows -> available=False", ctx == {"enabled": True, "available": False})


def test_picks_latest_approved_not_unapproved_newer_draft() -> None:
    print("test_picks_latest_approved_not_unapproved_newer_draft")
    conn = _fresh_conn()
    today = _today_kst()
    old_week = (today - timedelta(days=7)).isoformat()
    new_week = today.isoformat()

    conn.execute(
        "INSERT INTO raw_weekly_brief_drafts (week_ending_date, draft_text, fact_check_status, generated_at) "
        "VALUES (?, 'old approved brief', 'ok', ?)", (old_week, old_week),
    )
    old_draft_id = conn.execute("SELECT id FROM raw_weekly_brief_drafts WHERE week_ending_date = ?", (old_week,)).fetchone()[0]
    conn.execute(
        "INSERT INTO weekly_briefs (draft_ref, approved, approved_at) VALUES (?, 1, ?)", (old_draft_id, old_week),
    )
    conn.execute(
        "INSERT INTO raw_weekly_brief_drafts (week_ending_date, draft_text, fact_check_status, generated_at) "
        "VALUES (?, 'new unapproved draft', 'ok', ?)", (new_week, new_week),
    )
    conn.commit()

    ctx = get_weekly_brief_context({"weekly_brief": {"enabled": True}}, conn)
    _check("shows the approved week, not the newer unapproved draft", ctx["text"] == "old approved brief")
    _check("not stale at 7 days (default threshold 14)", ctx["is_stale"] is False)


def test_staleness_threshold() -> None:
    print("test_staleness_threshold")
    conn = _fresh_conn()
    today = _today_kst()
    old_week = (today - timedelta(days=20)).isoformat()

    conn.execute(
        "INSERT INTO raw_weekly_brief_drafts (week_ending_date, draft_text, fact_check_status, generated_at) "
        "VALUES (?, 'stale brief', 'ok', ?)", (old_week, old_week),
    )
    draft_id = conn.execute("SELECT id FROM raw_weekly_brief_drafts WHERE week_ending_date = ?", (old_week,)).fetchone()[0]
    conn.execute("INSERT INTO weekly_briefs (draft_ref, approved, approved_at) VALUES (?, 1, ?)", (draft_id, old_week))
    conn.commit()

    ctx = get_weekly_brief_context({"weekly_brief": {"enabled": True, "stale_after_days": 14}}, conn)
    _check("20 days old exceeds 14-day threshold -> is_stale=True", ctx["is_stale"] is True)
    _check("age_days computed correctly", ctx["age_days"] == 20)


def test_published_text_overrides_draft() -> None:
    print("test_published_text_overrides_draft")
    conn = _fresh_conn()
    today_iso = _today_kst().isoformat()
    conn.execute(
        "INSERT INTO raw_weekly_brief_drafts (week_ending_date, draft_text, fact_check_status, generated_at) "
        "VALUES (?, 'auto draft', 'ok', ?)", (today_iso, today_iso),
    )
    draft_id = conn.execute("SELECT id FROM raw_weekly_brief_drafts WHERE week_ending_date = ?", (today_iso,)).fetchone()[0]
    conn.execute(
        "INSERT INTO weekly_briefs (draft_ref, approved, approved_at, published_text) VALUES (?, 1, ?, ?)",
        (draft_id, today_iso, "Commander-edited final text"),
    )
    conn.commit()

    ctx = get_weekly_brief_context({"weekly_brief": {"enabled": True}}, conn)
    _check("published_text overrides draft_text when set", ctx["text"] == "Commander-edited final text")


class _FakeWorksheet:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows
        self._header = [
            "week_ending_date", "draft_text", "fact_check_status", "fact_check_detail",
            "approved", "approved_at", "published_text", "notes",
        ]

    def get_all_records(self) -> list[dict]:
        return [dict(r) for r in self._rows]

    def row_values(self, n: int) -> list[str]:
        return self._header

    def batch_update(self, *a, **kw) -> None:
        pass

    def append_rows(self, *a, **kw) -> None:
        pass


class _FakeSheet:
    def __init__(self, ws: "_FakeWorksheet | None") -> None:
        self._ws = ws

    def worksheet(self, name: str):
        if self._ws is None:
            import gspread
            raise gspread.exceptions.WorksheetNotFound(name)
        return self._ws


def test_sync_weekly_brief_approvals_promotes_and_updates() -> None:
    print("test_sync_weekly_brief_approvals_promotes_and_updates")
    conn = _fresh_conn()
    conn.execute(
        "INSERT INTO raw_weekly_brief_drafts (week_ending_date, draft_text, fact_check_status, generated_at) "
        "VALUES ('2026-07-06', 'draft text', 'ok', '2026-07-06T00:00:00Z')"
    )
    conn.commit()

    ws = _FakeWorksheet([
        {"week_ending_date": "2026-07-06", "draft_text": "draft text", "fact_check_status": "ok",
         "fact_check_detail": "", "approved": "TRUE", "approved_at": "2026-07-07",
         "published_text": "", "notes": "looks good"},
    ])
    result = sync_weekly_brief_approvals(conn, _FakeSheet(ws))
    _check("first sync promotes (inserts) weekly_briefs", result["promoted"] == 1 and result["updated"] == 0)

    row = conn.execute("SELECT approved, notes FROM weekly_briefs").fetchone()
    _check("approved=1 and notes carried through", row == (1, "looks good"))

    # Commander edits notes after first promotion — must UPDATE, not fail on UNIQUE(draft_ref).
    ws._rows[0]["notes"] = "revised notes"
    result_2 = sync_weekly_brief_approvals(conn, _FakeSheet(ws))
    _check("second sync updates, not re-promotes", result_2["updated"] == 1 and result_2["promoted"] == 0)
    row = conn.execute("SELECT notes FROM weekly_briefs").fetchone()
    _check("notes carry the edit", row[0] == "revised notes")


def test_sync_weekly_brief_approvals_skips_row_with_no_matching_draft() -> None:
    print("test_sync_weekly_brief_approvals_skips_row_with_no_matching_draft")
    conn = _fresh_conn()
    ws = _FakeWorksheet([
        {"week_ending_date": "2099-01-01", "draft_text": "", "fact_check_status": "", "fact_check_detail": "",
         "approved": "", "approved_at": "", "published_text": "", "notes": ""},
    ])
    result = sync_weekly_brief_approvals(conn, _FakeSheet(ws))
    _check("no matching raw draft -> skipped, not an error", result["skipped_no_draft"] == 1)


def main() -> None:
    test_fact_check_catches_wrong_number()
    test_fact_check_ignores_non_kpi_numbers()
    test_generate_weekly_brief_draft_idempotent_per_week()
    test_generate_weekly_brief_draft_kill_switch()
    test_kill_switch_and_no_approved_rows()
    test_picks_latest_approved_not_unapproved_newer_draft()
    test_staleness_threshold()
    test_published_text_overrides_draft()
    test_sync_weekly_brief_approvals_promotes_and_updates()
    test_sync_weekly_brief_approvals_skips_row_with_no_matching_draft()

    print(f"\n{_checks_run} checks run, {_checks_failed} failed.")
    if _checks_failed:
        sys.exit(1)
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
