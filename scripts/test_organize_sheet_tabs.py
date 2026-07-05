# Run as: PYTHONPATH=. python scripts/test_organize_sheet_tabs.py
#
# test_organize_sheet_tabs.py — assert-based checks for
# scripts/organize_sheet_tabs.py's build_requests() diff logic (Phase 1
# sheet cleanup, 2026-07-06). No network, no Sheets, no credentials —
# matches test_weekly_brief.py's fixture convention (no test framework in
# this repo, per test_smoke.py).
#
# Covers the one non-trivial branch: build_requests() must emit a request
# only for a property that actually differs from desired state, and no
# request at all once a tab already matches (idempotency — a second run
# is a clean no-op).

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.organize_sheet_tabs import build_requests  # noqa: E402

_checks_run = 0
_checks_failed = 0


def _check(label: str, condition: bool) -> None:
    global _checks_run, _checks_failed
    _checks_run += 1
    status = "ok" if condition else "FAIL"
    print(f"  [{status}] {label}")
    if not condition:
        _checks_failed += 1


class _FakeWorksheet:
    def __init__(self, sheet_id: int, title: str, index: int, tab_color: str | None, hidden: bool) -> None:
        self.id = sheet_id
        self.title = title
        self.index = index
        self.tab_color = tab_color
        self.isSheetHidden = hidden


class _FakeSpreadsheet:
    def __init__(self, worksheets: list[_FakeWorksheet]) -> None:
        self._worksheets = worksheets

    def worksheets(self):
        return self._worksheets


def test_all_properties_differ_produces_full_request() -> None:
    print("test_all_properties_differ_produces_full_request")
    ws = _FakeWorksheet(sheet_id=111, title="raw_vfi", index=5, tab_color="#FFFFFF", hidden=False)
    spreadsheet = _FakeSpreadsheet([ws])
    desired = [("raw_vfi", 10, "#666666", True)]

    requests, plan = build_requests(spreadsheet, desired)

    _check("one request emitted", len(requests) == 1)
    props = requests[0]["updateSheetProperties"]["properties"]
    _check("sheetId carried through", props["sheetId"] == 111)
    _check("index included in changes", props["index"] == 10)
    _check("hidden included in changes", props["hidden"] is True)
    _check("tabColorStyle included in changes", "tabColorStyle" in props)
    fields = requests[0]["updateSheetProperties"]["fields"]
    _check("fields mask lists all three changed properties", set(fields.split(",")) == {"index", "tabColorStyle", "hidden"})


def test_already_matching_tab_is_a_noop() -> None:
    print("test_already_matching_tab_is_a_noop")
    ws = _FakeWorksheet(sheet_id=222, title="weekly_brief", index=13, tab_color="#8E7CC3", hidden=False)
    spreadsheet = _FakeSpreadsheet([ws])
    desired = [("weekly_brief", 13, "#8E7CC3", False)]

    requests, plan = build_requests(spreadsheet, desired)

    _check("no request emitted when state already matches", requests == [])
    _check("plan reports no-op", "no-op" in plan[0])


def test_partial_diff_only_includes_changed_fields() -> None:
    print("test_partial_diff_only_includes_changed_fields")
    # Color and hidden already correct; only the index needs to move.
    ws = _FakeWorksheet(sheet_id=333, title="map_companies", index=2, tab_color="#F1C232", hidden=False)
    spreadsheet = _FakeSpreadsheet([ws])
    desired = [("map_companies", 7, "#F1C232", False)]

    requests, plan = build_requests(spreadsheet, desired)

    _check("one request emitted", len(requests) == 1)
    props = requests[0]["updateSheetProperties"]["properties"]
    _check("only index is present in the changed properties", set(props.keys()) == {"sheetId", "index"})


def test_missing_tab_is_skipped_not_raised() -> None:
    print("test_missing_tab_is_skipped_not_raised")
    spreadsheet = _FakeSpreadsheet([])
    desired = [("does_not_exist", 0, "#000000", False)]

    requests, plan = build_requests(spreadsheet, desired)

    _check("missing tab produces no request and does not raise", requests == [])


def main() -> None:
    test_all_properties_differ_produces_full_request()
    test_already_matching_tab_is_a_noop()
    test_partial_diff_only_includes_changed_fields()
    test_missing_tab_is_skipped_not_raised()

    print(f"\n{_checks_run} checks run, {_checks_failed} failed.")
    if _checks_failed:
        sys.exit(1)
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
