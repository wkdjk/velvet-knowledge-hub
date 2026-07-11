# Run as: PYTHONPATH=. python scripts/test_validate_config.py
#
# test_validate_config.py — coverage for validate_config.py's validate().
#
# No test framework (matches test_dedup_logic.py/test_smoke.py convention —
# this repo has no pytest config). Assert-based script.
#
# Added 2026-07-10 alongside the tab/db_table fix (Phase D rebuild gap):
# validate_config.py used to require 'tab' unconditionally, which would
# reject any sqlite-backed source (D1 Library has no Sheets tab at all).

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.validate_config import validate  # noqa: E402

_DISPLAY_KINDS = ["timeseries", "records", "directory", "news", "note"]


def _base_config(sources: list[dict]) -> dict:
    return {"display_kinds": _DISPLAY_KINDS, "sources": sources}


def test_sheets_backed_source_valid():
    config = _base_config([
        {"id": "a", "tab": "Some_Tab", "kind": "records", "section": "s", "enabled": True}
    ])
    assert validate(config) == []


def test_sqlite_backed_source_valid():
    config = _base_config([
        {"id": "library_docs", "db_table": "library_docs", "kind": "directory",
         "section": "library", "enabled": False}
    ])
    assert validate(config) == []


def test_source_with_neither_tab_nor_db_table_fails():
    config = _base_config([
        {"id": "a", "kind": "records", "section": "s", "enabled": True}
    ])
    errors = validate(config)
    assert len(errors) == 1
    assert "tab" in errors[0] and "db_table" in errors[0]


def test_source_with_both_tab_and_db_table_is_not_an_error():
    # Not forbidden — a migration-in-progress source could plausibly have both.
    config = _base_config([
        {"id": "a", "tab": "Some_Tab", "db_table": "some_table", "kind": "records",
         "section": "s", "enabled": True}
    ])
    assert validate(config) == []


def test_empty_tab_and_missing_db_table_fails():
    config = _base_config([
        {"id": "a", "tab": "", "kind": "records", "section": "s", "enabled": True}
    ])
    errors = validate(config)
    assert len(errors) == 1


def test_missing_other_required_field_still_caught():
    config = _base_config([
        {"id": "a", "tab": "Some_Tab", "section": "s", "enabled": True}  # no 'kind'
    ])
    errors = validate(config)
    assert any("kind" in e for e in errors)


def test_unknown_kind_still_caught():
    config = _base_config([
        {"id": "a", "tab": "Some_Tab", "kind": "bogus", "section": "s", "enabled": True}
    ])
    errors = validate(config)
    assert any("not in display_kinds" in e for e in errors)


def main() -> None:
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    for test in tests:
        test()
        print(f"  PASS: {test.__name__}")
    print(f"test_validate_config.py: {len(tests)}/{len(tests)} passed")


if __name__ == "__main__":
    main()
