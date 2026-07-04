# Run as: PYTHONPATH=. python scripts/test_dedup_logic.py
#
# test_dedup_logic.py — offline smoke test for C-13 Task 1 semantic dedup.
#
# No network, no Sheets, no ANTHROPIC_API_KEY required — exercises the pure
# logic pieces of classify_articles.py's dedup code with synthetic data.
# This repo has no test framework (checked: no test_*.py, no pytest config
# before this file); a single assert-based script matches the existing
# `if __name__ == "__main__"` convention used by every other script here.
#
# Covers:
#   1. _within_cluster_window — date-window boundary behaviour.
#   2. _fallback_ratio_match — difflib fallback used when an LLM call errors.
#   3. run_canonical_succession — full pass against a fake in-memory
#      worksheet (no LLM involved in this function, so it's fully testable
#      offline): manual-suppression promotion + multi-mate repointing.

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.classify_articles import (  # noqa: E402
    _fallback_ratio_match,
    _within_cluster_window,
    run_canonical_succession,
)
from scripts.schema import KVN_ARTICLES_HEADERS  # noqa: E402


class _FakeWorksheet:
    """
    Minimal in-memory stand-in for the two gspread calls
    run_canonical_succession uses.

    Uses the REAL 15-column KVN_ARTICLES_HEADERS layout (not a trimmed
    subset) — production code writes cells by absolute column index
    (_COL_INCLUDE_ON_SITE etc., matching schema.py), so the fake row width
    must match the real schema or a batch_update() write lands on the wrong
    field once get_all_records() zips headers back onto row values.
    """

    def __init__(self, rows_by_field: list[dict]) -> None:
        self._rows = [
            [row.get(h, "") for h in KVN_ARTICLES_HEADERS]
            for row in rows_by_field
        ]

    def get_all_records(self) -> list[dict]:
        return [dict(zip(KVN_ARTICLES_HEADERS, row)) for row in self._rows]

    def batch_update(self, cell_updates: list[dict], value_input_option: str = "USER_ENTERED") -> None:
        import re
        for update in cell_updates:
            match = re.match(r"^([A-Z]+)(\d+)$", update["range"])
            col_letters, row_num = match.group(1), int(match.group(2))
            col_idx = 0
            for ch in col_letters:
                col_idx = col_idx * 26 + (ord(ch) - ord("A") + 1)
            row_idx = row_num - 2  # header is row 1
            value = update["values"][0][0]
            self._rows[row_idx][col_idx - 1] = value


def demo() -> None:
    # --- 1. date window ----------------------------------------------------
    assert _within_cluster_window(date(2026, 5, 28), date(2026, 5, 31)) is True   # 3 days, inclusive
    assert _within_cluster_window(date(2026, 5, 28), date(2026, 6, 1)) is False   # 4 days
    assert _within_cluster_window(date(2026, 6, 1), date(2026, 5, 28)) is False   # order-independent
    print("  [1/3] _within_cluster_window: PASS")

    # --- 2. fallback ratio match --------------------------------------------
    idx = _fallback_ratio_match("조아제약, 몽진환 마인 출시", ["조아제약, 몽진환 마인 신제품 출시"])
    assert idx == 0, "near-verbatim titles should match under the strict fallback threshold"
    idx = _fallback_ratio_match("조아제약 신제품 출시", ["완전히 다른 뉴스 헤드라인입니다"])
    assert idx is None, "unrelated titles should not match"
    print("  [2/3] _fallback_ratio_match: PASS")

    # --- 3. canonical succession --------------------------------------------
    # Cluster: article_id "A" (canonical, manually suppressed by a human),
    # "B" (earliest mate, still suppressed — should be promoted),
    # "C" (later mate, still suppressed — should be repointed to B).
    rows = [
        {"article_id": "A", "url": "canonical headline", "published_date": "2026-05-28",
         "include_on_site": "FALSE", "duplicate_of_article_id": "none", "dedup_judged_at": "2026-05-28T00:00:00Z"},
        {"article_id": "B", "url": "mate headline one", "published_date": "2026-05-29",
         "include_on_site": "FALSE", "duplicate_of_article_id": "A", "dedup_judged_at": "2026-05-29T00:00:00Z"},
        {"article_id": "C", "url": "mate headline two", "published_date": "2026-05-30",
         "include_on_site": "FALSE", "duplicate_of_article_id": "A", "dedup_judged_at": "2026-05-30T00:00:00Z"},
    ]
    ws = _FakeWorksheet(rows)
    promotions = run_canonical_succession(ws, dry_run=False)
    assert promotions == 1, f"expected 1 promotion, got {promotions}"

    result = {r["article_id"]: r for r in ws.get_all_records()}
    assert result["B"]["include_on_site"] == "TRUE", "earliest surviving mate must be promoted"
    assert result["B"]["duplicate_of_article_id"] == "none", "promoted mate becomes its own canonical"
    assert result["C"]["duplicate_of_article_id"] == "B", "remaining mate must be repointed to the new canonical"
    assert result["A"]["include_on_site"] == "FALSE", "the manually-suppressed row is never auto-revived"

    # Idempotency: running again with succession already applied must be a no-op.
    promotions_again = run_canonical_succession(ws, dry_run=False)
    assert promotions_again == 0, "succession must not re-fire once a mate has already been promoted"
    print("  [3/3] run_canonical_succession: PASS")

    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    demo()
