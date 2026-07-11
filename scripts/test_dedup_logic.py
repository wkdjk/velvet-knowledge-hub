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
#   4. run_canonical_succession — duplicate-article_id regression (bug found
#      on the first live run, 2026-07-04): multiple physical rows sharing
#      one article_id must not cause a false "canonical was suppressed".
#   5. run_canonical_succession — SurveyorQ (a): a manual_override=TRUE mate
#      must never be promoted; the next eligible mate is promoted instead.
#   6. run_canonical_succession — SurveyorQ F3: same-published_date mates
#      must resolve deterministically via an article_id tie-break.
#   7. run_semantic_clustering_pass — full pass against a fake worksheet AND
#      a fake Anthropic client (always reports "match: 1"): same-batch
#      matching (Task 1a) and manual_override protection (Task 1, Part B §c).

import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.classify_articles import (  # noqa: E402
    _fallback_ratio_match,
    _within_cluster_window,
    run_canonical_succession,
    run_semantic_clustering_pass,
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


class _FakeMessage:
    def __init__(self, text: str) -> None:
        self.content = [type("Block", (), {"text": text})()]


class _FakeMessages:
    """Always reports the new headline matches candidate #1 (1-based)."""

    def create(self, **kwargs) -> _FakeMessage:
        return _FakeMessage(json.dumps({"match": 1}))


class _FakeAnthropicClient:
    def __init__(self) -> None:
        self.messages = _FakeMessages()


def demo() -> None:
    # --- 1. date window ----------------------------------------------------
    assert _within_cluster_window(date(2026, 5, 28), date(2026, 5, 31)) is True   # 3 days, inclusive
    assert _within_cluster_window(date(2026, 5, 28), date(2026, 6, 1)) is False   # 4 days
    assert _within_cluster_window(date(2026, 6, 1), date(2026, 5, 28)) is False   # order-independent
    print("  [1/7] _within_cluster_window: PASS")

    # --- 2. fallback ratio match --------------------------------------------
    idx = _fallback_ratio_match("조아제약, 몽진환 마인 출시", ["조아제약, 몽진환 마인 신제품 출시"])
    assert idx == 0, "near-verbatim titles should match under the strict fallback threshold"
    idx = _fallback_ratio_match("조아제약 신제품 출시", ["완전히 다른 뉴스 헤드라인입니다"])
    assert idx is None, "unrelated titles should not match"
    print("  [2/7] _fallback_ratio_match: PASS")

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
    assert len(promotions) == 1, f"expected 1 promotion, got {len(promotions)}"

    result = {r["article_id"]: r for r in ws.get_all_records()}
    assert result["B"]["include_on_site"] == "TRUE", "earliest surviving mate must be promoted"
    assert result["B"]["duplicate_of_article_id"] == "none", "promoted mate becomes its own canonical"
    assert result["C"]["duplicate_of_article_id"] == "B", "remaining mate must be repointed to the new canonical"
    assert result["A"]["include_on_site"] == "FALSE", "the manually-suppressed row is never auto-revived"
    assert result["A"]["duplicate_of_article_id"] == "B", (
        "SurveyorQ (b): the old canonical itself must also be repointed to the new one"
    )

    # Idempotency: running again with succession already applied must be a no-op.
    promotions_again = run_canonical_succession(ws, dry_run=False)
    assert len(promotions_again) == 0, "succession must not re-fire once a mate has already been promoted"
    print("  [3/7] run_canonical_succession: PASS")

    # --- 4. duplicate-article_id regression (2026-07-04 live bug) -----------
    # "X" has TWO physical rows sharing article_id "X" — a decoy copy (FALSE,
    # never touched by dedup, e.g. pre-C-13 duplicate-insert debt) and the
    # genuine canonical (TRUE). "Y" is a real, distinct story wrongly judged
    # a duplicate of "X" and correctly suppressed by clustering. Succession
    # must see that SOME row with article_id "X" is still TRUE and do
    # nothing — promoting "Y" here would surface a duplicate on the site.
    rows_dup_id = [
        {"article_id": "X", "url": "decoy copy, never live", "published_date": "2026-05-20",
         "include_on_site": "FALSE", "duplicate_of_article_id": "", "dedup_judged_at": ""},
        {"article_id": "X", "url": "genuine canonical, still live", "published_date": "2026-05-28",
         "include_on_site": "TRUE", "duplicate_of_article_id": "none", "dedup_judged_at": "2026-05-28T00:00:00Z"},
        {"article_id": "Y", "url": "distinct story, correctly suppressed", "published_date": "2026-05-29",
         "include_on_site": "FALSE", "duplicate_of_article_id": "X", "dedup_judged_at": "2026-05-29T00:00:00Z"},
    ]
    ws_dup_id = _FakeWorksheet(rows_dup_id)
    promotions_dup_id = run_canonical_succession(ws_dup_id, dry_run=False)
    assert len(promotions_dup_id) == 0, (
        f"expected 0 promotions when the canonical is still TRUE via another "
        f"physical row sharing its article_id, got {len(promotions_dup_id)}"
    )
    result_dup_id = ws_dup_id.get_all_records()
    y_row = next(r for r in result_dup_id if r["url"] == "distinct story, correctly suppressed")
    assert y_row["include_on_site"] == "FALSE", "correctly-suppressed duplicate must not be resurrected"
    print("  [4/7] run_canonical_succession (duplicate article_id): PASS")

    # --- 5. SurveyorQ (a): manual_override excludes a mate from succession ---
    # Cluster: "P" (canonical, manually suppressed), "Q" (earliest mate, but
    # manual_override=TRUE — a human specifically wants THIS one to stay
    # suppressed, not to become the new canonical), "R" (later mate, no
    # override — must be promoted instead, skipping over Q).
    rows_override = [
        {"article_id": "P", "url": "canonical, manually suppressed", "published_date": "2026-05-28",
         "include_on_site": "FALSE", "duplicate_of_article_id": "none", "dedup_judged_at": "2026-05-28T00:00:00Z"},
        {"article_id": "Q", "url": "protected mate, must stay suppressed", "published_date": "2026-05-29",
         "include_on_site": "FALSE", "duplicate_of_article_id": "P", "dedup_judged_at": "2026-05-29T00:00:00Z",
         "manual_override": "TRUE"},
        {"article_id": "R", "url": "unprotected mate, eligible", "published_date": "2026-05-30",
         "include_on_site": "FALSE", "duplicate_of_article_id": "P", "dedup_judged_at": "2026-05-30T00:00:00Z"},
    ]
    ws_override = _FakeWorksheet(rows_override)
    promotions_override = run_canonical_succession(ws_override, dry_run=False)
    assert len(promotions_override) == 1, f"expected 1 promotion, got {len(promotions_override)}"
    result_override = {r["article_id"]: r for r in ws_override.get_all_records()}
    assert result_override["Q"]["include_on_site"] == "FALSE", "manual_override=TRUE mate must never be promoted"
    assert result_override["R"]["include_on_site"] == "TRUE", "next-eligible (unprotected) mate must be promoted instead"
    print("  [5/7] run_canonical_succession (manual_override excluded): PASS")

    # --- 6. SurveyorQ F3: same-date tie-break is deterministic ---------------
    # "S" and "T" share the exact same published_date; only article_id order
    # (S < T) should decide which one is promoted, both directions.
    def _build_same_date_cluster():
        return _FakeWorksheet([
            {"article_id": "M", "url": "canonical, manually suppressed", "published_date": "2026-05-28",
             "include_on_site": "FALSE", "duplicate_of_article_id": "none", "dedup_judged_at": "2026-05-28T00:00:00Z"},
            {"article_id": "T", "url": "same-date mate T", "published_date": "2026-05-29",
             "include_on_site": "FALSE", "duplicate_of_article_id": "M", "dedup_judged_at": "2026-05-29T00:00:00Z"},
            {"article_id": "S", "url": "same-date mate S", "published_date": "2026-05-29",
             "include_on_site": "FALSE", "duplicate_of_article_id": "M", "dedup_judged_at": "2026-05-29T00:00:00Z"},
        ])

    for _ in range(3):  # repeat — a flaky tie-break would show up as run-to-run variance
        ws_tie = _build_same_date_cluster()
        run_canonical_succession(ws_tie, dry_run=False)
        result_tie = {r["article_id"]: r for r in ws_tie.get_all_records()}
        assert result_tie["S"]["include_on_site"] == "TRUE", "article_id-ascending tie-break must always pick S over T"
        assert result_tie["T"]["include_on_site"] == "FALSE"
    print("  [6/7] run_canonical_succession (same-date tie-break): PASS")

    # --- 7. semantic clustering pass -----------------------------------------
    # D (2026-05-28): arrives first in this pass, no candidates yet — becomes
    #   canonical (settled) before E or F are processed (Task 1a requires
    #   ascending published_date order to make this deterministic).
    # E (2026-05-29): brand new, within window of D — fake client always
    #   says "match candidate 1", so E matches D and gets suppressed.
    # F (2026-05-29): also new and within window, manual_override=TRUE —
    #   fake client says it matches D too, but protection must stop the
    #   suppression: F stays include_on_site=TRUE, verdict still cached.
    rows = [
        {"article_id": "D", "url": "headline D", "published_date": "2026-05-28",
         "include_on_site": "TRUE", "duplicate_of_article_id": "", "dedup_judged_at": ""},
        {"article_id": "E", "url": "headline E", "published_date": "2026-05-29",
         "include_on_site": "TRUE", "duplicate_of_article_id": "", "dedup_judged_at": ""},
        {"article_id": "F", "url": "headline F", "published_date": "2026-05-29",
         "include_on_site": "TRUE", "duplicate_of_article_id": "", "dedup_judged_at": "",
         "manual_override": "TRUE"},
    ]
    ws = _FakeWorksheet(rows)
    client = _FakeAnthropicClient()
    stats = run_semantic_clustering_pass(ws, client, dry_run=False)
    assert stats["judged"] == 3 and stats["suppressed"] == 1, stats

    result = {r["article_id"]: r for r in ws.get_all_records()}
    assert result["D"]["include_on_site"] == "TRUE", "first-processed row with no candidates must stay canonical"
    assert result["D"]["duplicate_of_article_id"] == "none"
    assert result["E"]["include_on_site"] == "FALSE", "Task 1a: E must match D even though D was only settled THIS pass"
    assert result["E"]["duplicate_of_article_id"] == "D"
    assert result["F"]["include_on_site"] == "TRUE", "manual_override=TRUE must block suppression"
    assert result["F"]["duplicate_of_article_id"] == "D", "verdict is still cached even when not applied"
    print("  [7/7] run_semantic_clustering_pass (Task 1a + manual_override): PASS")

    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    demo()
