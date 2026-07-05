# Run as: PYTHONPATH=. python scripts/test_weekly_brief.py
#
# test_weekly_brief.py — assert-based checks for scripts/vkh_brief.py
# (C-14 item 4, 2026-07-05).
#
# No test framework in this repo (matches test_smoke.py's convention). No
# network, no Sheets, no credentials — pure fixture-based checks of:
#   1. fact_check_draft() catches a deliberately wrong figure and passes a
#      correct one.
#   2. get_weekly_brief_context() respects the weekly_brief.enabled kill
#      switch and returns available=False with no approved rows.
#   3. get_weekly_brief_context() picks the most recently *approved* week,
#      never an unapproved newer draft.
#   4. Staleness flips at stale_after_days.

import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.vkh_brief import fact_check_draft, get_weekly_brief_context  # noqa: E402
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
    # "90 days" is a fixed window descriptor, not a cited KPI figure — must
    # not be flagged just because "90" has no matching reference number.
    text = "Food import declarations over the last 90 days held steady."
    status, _ = fact_check_draft(text, _SAMPLE_KPI, _SAMPLE_CHART_DATA)
    _check("window descriptor ('90 days') is not fact-checked", status == "ok")


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


class _FakeSheet:
    def __init__(self, ws: "_FakeWorksheet | None") -> None:
        self._ws = ws

    def worksheet(self, name: str):
        if self._ws is None:
            import gspread
            raise gspread.exceptions.WorksheetNotFound(name)
        return self._ws


def test_kill_switch_and_no_approved_rows() -> None:
    print("test_kill_switch_and_no_approved_rows")

    ctx = get_weekly_brief_context({"weekly_brief": {"enabled": False}}, _FakeSheet(_FakeWorksheet([])))
    _check("enabled=False returns enabled=False, no other keys assumed", ctx == {"enabled": False})

    ws = _FakeWorksheet([
        {"week_ending_date": "2026-07-06", "draft_text": "draft only", "fact_check_status": "ok",
         "fact_check_detail": "", "approved": "", "approved_at": "", "published_text": "", "notes": ""},
    ])
    ctx = get_weekly_brief_context({"weekly_brief": {"enabled": True}}, _FakeSheet(ws))
    _check("enabled, no approved rows -> available=False", ctx == {"enabled": True, "available": False})


def test_picks_latest_approved_not_unapproved_newer_draft() -> None:
    print("test_picks_latest_approved_not_unapproved_newer_draft")
    today = _today_kst()
    old_week = (today - timedelta(days=7)).isoformat()
    new_week = today.isoformat()

    ws = _FakeWorksheet([
        {"week_ending_date": old_week, "draft_text": "old approved brief", "fact_check_status": "ok",
         "fact_check_detail": "", "approved": "TRUE", "approved_at": old_week, "published_text": "", "notes": ""},
        {"week_ending_date": new_week, "draft_text": "new unapproved draft", "fact_check_status": "ok",
         "fact_check_detail": "", "approved": "", "approved_at": "", "published_text": "", "notes": ""},
    ])
    ctx = get_weekly_brief_context({"weekly_brief": {"enabled": True}}, _FakeSheet(ws))
    _check("shows the approved week, not the newer unapproved draft", ctx["text"] == "old approved brief")
    _check("not stale at 7 days (default threshold 14)", ctx["is_stale"] is False)


def test_staleness_threshold() -> None:
    print("test_staleness_threshold")
    today = _today_kst()
    old_week = (today - timedelta(days=20)).isoformat()

    ws = _FakeWorksheet([
        {"week_ending_date": old_week, "draft_text": "stale brief", "fact_check_status": "ok",
         "fact_check_detail": "", "approved": "TRUE", "approved_at": old_week, "published_text": "", "notes": ""},
    ])
    ctx = get_weekly_brief_context({"weekly_brief": {"enabled": True, "stale_after_days": 14}}, _FakeSheet(ws))
    _check("20 days old exceeds 14-day threshold -> is_stale=True", ctx["is_stale"] is True)
    _check("age_days computed correctly", ctx["age_days"] == 20)


def test_published_text_overrides_draft() -> None:
    print("test_published_text_overrides_draft")
    today_iso = _today_kst().isoformat()
    ws = _FakeWorksheet([
        {"week_ending_date": today_iso, "draft_text": "auto draft", "fact_check_status": "ok",
         "fact_check_detail": "", "approved": "TRUE", "approved_at": today_iso,
         "published_text": "Commander-edited final text", "notes": ""},
    ])
    ctx = get_weekly_brief_context({"weekly_brief": {"enabled": True}}, _FakeSheet(ws))
    _check("published_text overrides draft_text when set", ctx["text"] == "Commander-edited final text")


def main() -> None:
    test_fact_check_catches_wrong_number()
    test_fact_check_ignores_non_kpi_numbers()
    test_kill_switch_and_no_approved_rows()
    test_picks_latest_approved_not_unapproved_newer_draft()
    test_staleness_threshold()
    test_published_text_overrides_draft()

    print(f"\n{_checks_run} checks run, {_checks_failed} failed.")
    if _checks_failed:
        sys.exit(1)
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
