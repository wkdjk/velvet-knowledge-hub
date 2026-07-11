# Run as: PYTHONPATH=. python scripts/test_news_pipeline.py
#
# test_news_pipeline.py — coverage for classify_articles.py / news_data.py
# (D3 Phase D rebuild, 2026-07-11).
#
# No test framework (matches test_dedup_logic.py/test_library_ingest.py
# convention). Assert-based, in-memory sqlite only — no network, no Sheets,
# no credentials required.
#
# Covers:
#   1. write_relevance_and_classification — the atomic single-INSERT write
#      path (§4 correction 1): one statement covers both relevance and
#      classification column groups; classification columns are NULL when
#      relevant=0.
#   2. list_stuck_classification / retry_stuck_classification — the "pending,
#      retry" self-heal path (§4 correction 1) is a real, callable query.
#   3. news_data.py's display predicate (N-3): relevant=1 AND
#      (duplicate_of_raw_ref IS NULL OR manual_override=1) AND
#      hidden_by_commander=0 — every branch.
#   4. run_semantic_clustering_pass — NULL-based judged/not-judged states,
#      same-batch matching, manual_override protection (ported from
#      test_dedup_logic.py's Sheets-based test 7).
#   5. run_canonical_succession — hidden_by_commander trigger, manual_override
#      exclusion, idempotency (ported from test_dedup_logic.py, new trigger).

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.classify_articles import (  # noqa: E402
    list_pending_relevance,
    list_stuck_classification,
    retry_stuck_classification,
    run_canonical_succession,
    run_semantic_clustering_pass,
    write_relevance_and_classification,
)
from scripts.news_data import list_news_articles, news_available  # noqa: E402
from scripts.news_schema import NEWS_DDL  # noqa: E402


def _fresh_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    for stmt in NEWS_DDL:
        conn.execute(stmt)
    return conn


def _insert_raw(conn: sqlite3.Connection, content_hash: str, title_ko: str, published_date: str = "2026-05-28") -> int:
    conn.execute(
        "INSERT INTO raw_news_articles (content_hash, url, title_ko, description, published_date, collected_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (content_hash, f"https://example.com/{content_hash}", title_ko, "a description", published_date, "2026-05-28T00:00:00Z"),
    )
    return conn.execute("SELECT id FROM raw_news_articles WHERE content_hash = ?", (content_hash,)).fetchone()[0]


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


# ---------------------------------------------------------------------------
# 1. Atomic single-INSERT write path (§4 correction 1)
# ---------------------------------------------------------------------------

def test_write_relevance_and_classification_relevant_row():
    conn = _fresh_conn()
    raw_id = _insert_raw(conn, "aaaa000000000001", "relevant headline")
    result = {"category": "무역시장", "english_title": "Title", "english_summary": "Summary", "include_on_site": True}
    write_relevance_and_classification(conn, raw_id, result, "2026-05-28T00:00:00Z")
    conn.commit()

    row = conn.execute(
        "SELECT relevant, category, english_title, english_summary, classified_at, relevance_judged_at "
        "FROM news_articles WHERE raw_ref = ?", (raw_id,)
    ).fetchone()
    assert row[0] == 1
    assert row[1] == "무역시장" and row[2] == "Title" and row[3] == "Summary"
    assert row[4] == "2026-05-28T00:00:00Z"  # classified_at populated
    assert row[5] == "2026-05-28T00:00:00Z"  # relevance_judged_at populated — same write


def test_write_relevance_and_classification_irrelevant_row_nulls_classification():
    conn = _fresh_conn()
    raw_id = _insert_raw(conn, "aaaa000000000002", "irrelevant headline")
    result = {"category": "기타", "english_title": "ignored", "english_summary": "ignored", "include_on_site": False}
    write_relevance_and_classification(conn, raw_id, result, "2026-05-28T00:00:00Z")
    conn.commit()

    row = conn.execute(
        "SELECT relevant, category, english_title, english_summary, classified_at "
        "FROM news_articles WHERE raw_ref = ?", (raw_id,)
    ).fetchone()
    assert row[0] == 0
    assert row[1] is None and row[2] is None and row[3] is None and row[4] is None, (
        "classification columns must be NULL when relevant=0 — Haiku's category/summary output is discarded, not stored"
    )


def test_unique_raw_ref_rejects_double_insert():
    conn = _fresh_conn()
    raw_id = _insert_raw(conn, "aaaa000000000003", "headline")
    write_relevance_and_classification(conn, raw_id, {"category": "기타", "english_title": "", "english_summary": "", "include_on_site": False}, "t1")
    conn.commit()
    try:
        write_relevance_and_classification(conn, raw_id, {"category": "기타", "english_title": "", "english_summary": "", "include_on_site": False}, "t2")
        raise AssertionError("Expected UNIQUE(raw_ref) to reject a second judgement")
    except sqlite3.IntegrityError:
        conn.rollback()


# ---------------------------------------------------------------------------
# 2. Pending / stuck queues — real, callable code paths
# ---------------------------------------------------------------------------

def test_list_pending_relevance_excludes_judged():
    conn = _fresh_conn()
    raw_1 = _insert_raw(conn, "bbbb000000000001", "a")
    _insert_raw(conn, "bbbb000000000002", "b")
    conn.commit()
    pending = list_pending_relevance(conn)
    assert {p["id"] for p in pending} == {raw_1, conn.execute("SELECT id FROM raw_news_articles WHERE content_hash='bbbb000000000002'").fetchone()[0]}

    write_relevance_and_classification(conn, raw_1, {"category": "기타", "english_title": "", "english_summary": "", "include_on_site": True}, "t1")
    conn.commit()
    pending = list_pending_relevance(conn)
    assert len(pending) == 1
    assert pending[0]["content_hash"] == "bbbb000000000002"


def test_stuck_classification_retry_path_is_real_and_callable():
    conn = _fresh_conn()
    raw_id = _insert_raw(conn, "cccc000000000001", "stuck headline")
    # Simulate a stuck row directly (relevant=1, classified_at IS NULL) —
    # the state this self-heal path exists for (§4 correction 1).
    conn.execute(
        "INSERT INTO news_articles (raw_ref, relevant, relevance_judged_at) VALUES (?, 1, ?)",
        (raw_id, "2026-05-28T00:00:00Z"),
    )
    conn.commit()

    stuck = list_stuck_classification(conn)
    assert len(stuck) == 1
    assert stuck[0]["raw_ref"] == raw_id

    retry_stuck_classification(
        conn, stuck[0]["news_id"],
        {"category": "무역시장", "english_title": "Healed", "english_summary": "Healed summary", "include_on_site": True},
        "2026-05-29T00:00:00Z",
    )
    conn.commit()

    assert list_stuck_classification(conn) == [], "row must no longer be stuck after retry"
    row = conn.execute("SELECT category, classified_at FROM news_articles WHERE raw_ref = ?", (raw_id,)).fetchone()
    assert row == ("무역시장", "2026-05-29T00:00:00Z")


# ---------------------------------------------------------------------------
# 3. Display predicate (N-3) — news_data.py
# ---------------------------------------------------------------------------

def test_display_predicate_every_branch():
    conn = _fresh_conn()

    # visible: relevant, no duplicate, not hidden
    r_visible = _insert_raw(conn, "dddd000000000001", "visible")
    conn.execute("INSERT INTO news_articles (raw_ref, relevant, relevance_judged_at) VALUES (?, 1, 't')", (r_visible,))

    # hidden: relevant=0
    r_irrelevant = _insert_raw(conn, "dddd000000000002", "irrelevant")
    conn.execute("INSERT INTO news_articles (raw_ref, relevant, relevance_judged_at) VALUES (?, 0, 't')", (r_irrelevant,))

    # hidden: suppressed duplicate, not protected
    r_canonical = _insert_raw(conn, "dddd000000000003", "canonical")
    conn.execute("INSERT INTO news_articles (raw_ref, relevant, relevance_judged_at) VALUES (?, 1, 't')", (r_canonical,))
    canonical_raw_ref = r_canonical
    r_dup = _insert_raw(conn, "dddd000000000004", "suppressed duplicate")
    conn.execute(
        "INSERT INTO news_articles (raw_ref, relevant, relevance_judged_at, duplicate_of_raw_ref, dedup_judged_at) "
        "VALUES (?, 1, 't', ?, 't')",
        (r_dup, canonical_raw_ref),
    )

    # visible: suppressed duplicate BUT manual_override=1
    r_protected = _insert_raw(conn, "dddd000000000005", "protected duplicate")
    conn.execute(
        "INSERT INTO news_articles (raw_ref, relevant, relevance_judged_at, duplicate_of_raw_ref, dedup_judged_at, manual_override) "
        "VALUES (?, 1, 't', ?, 't', 1)",
        (r_protected, canonical_raw_ref),
    )

    # hidden: hidden_by_commander=1, otherwise would be visible
    r_hidden = _insert_raw(conn, "dddd000000000006", "commander-hidden")
    conn.execute(
        "INSERT INTO news_articles (raw_ref, relevant, relevance_judged_at, hidden_by_commander) VALUES (?, 1, 't', 1)",
        (r_hidden,),
    )

    conn.commit()

    visible_titles = {a["title_ko"] for a in list_news_articles(conn)}
    assert visible_titles == {"visible", "canonical", "protected duplicate"}, visible_titles
    assert news_available(conn) is True


def test_news_available_false_when_nothing_passes_predicate():
    conn = _fresh_conn()
    raw_id = _insert_raw(conn, "eeee000000000001", "irrelevant only")
    conn.execute("INSERT INTO news_articles (raw_ref, relevant, relevance_judged_at) VALUES (?, 0, 't')", (raw_id,))
    conn.commit()
    assert news_available(conn) is False
    assert list_news_articles(conn) == []


def test_news_available_false_table_not_migrated():
    conn = sqlite3.connect(":memory:")  # no DDL applied
    assert news_available(conn) is False


# ---------------------------------------------------------------------------
# 4. Semantic clustering pass — NULL-based states
# ---------------------------------------------------------------------------

def test_semantic_clustering_pass_same_batch_and_manual_override():
    conn = _fresh_conn()
    # D: first-processed, no candidates yet -> becomes canonical (settled)
    #   before E/F are processed.
    # E: within window of D, fake client always matches candidate 1 -> suppressed.
    # F: within window, manual_override=1 -> verdict cached but display
    #    predicate keeps it visible regardless.
    raw_d = _insert_raw(conn, "ffff000000000001", "headline D", "2026-05-28")
    raw_e = _insert_raw(conn, "ffff000000000002", "headline E", "2026-05-29")
    raw_f = _insert_raw(conn, "ffff000000000003", "headline F", "2026-05-29")
    conn.execute("INSERT INTO news_articles (raw_ref, relevant, relevance_judged_at) VALUES (?, 1, 't')", (raw_d,))
    conn.execute("INSERT INTO news_articles (raw_ref, relevant, relevance_judged_at) VALUES (?, 1, 't')", (raw_e,))
    conn.execute(
        "INSERT INTO news_articles (raw_ref, relevant, relevance_judged_at, manual_override) VALUES (?, 1, 't', 1)",
        (raw_f,),
    )
    conn.commit()

    stats = run_semantic_clustering_pass(conn, _FakeAnthropicClient(), dry_run=False)
    assert stats["judged"] == 3 and stats["suppressed"] == 1, stats

    d_row = conn.execute("SELECT duplicate_of_raw_ref, dedup_judged_at FROM news_articles WHERE raw_ref = ?", (raw_d,)).fetchone()
    assert d_row == (None, d_row[1]) and d_row[1] is not None, "D must be judged-canonical (NULL dup, non-NULL judged_at)"

    e_row = conn.execute("SELECT duplicate_of_raw_ref FROM news_articles WHERE raw_ref = ?", (raw_e,)).fetchone()
    assert e_row[0] == raw_d, "E must match D even though D was only settled this pass"

    f_row = conn.execute("SELECT duplicate_of_raw_ref FROM news_articles WHERE raw_ref = ?", (raw_f,)).fetchone()
    assert f_row[0] == raw_d, "verdict is still cached for a protected row"

    # Display predicate: E hidden, F still visible (manual_override).
    visible = {a["raw_ref"] for a in list_news_articles(conn)}
    assert raw_d in visible and raw_f in visible and raw_e not in visible


def test_semantic_clustering_pass_dry_run_writes_nothing():
    conn = _fresh_conn()
    raw_d = _insert_raw(conn, "ffff100000000001", "headline D", "2026-05-28")
    raw_e = _insert_raw(conn, "ffff100000000002", "headline E", "2026-05-29")
    conn.execute("INSERT INTO news_articles (raw_ref, relevant, relevance_judged_at) VALUES (?, 1, 't')", (raw_d,))
    conn.execute("INSERT INTO news_articles (raw_ref, relevant, relevance_judged_at) VALUES (?, 1, 't')", (raw_e,))
    conn.commit()

    run_semantic_clustering_pass(conn, _FakeAnthropicClient(), dry_run=True)
    row = conn.execute("SELECT dedup_judged_at FROM news_articles WHERE raw_ref = ?", (raw_d,)).fetchone()
    assert row[0] is None, "dry-run must not persist any dedup verdict"


# ---------------------------------------------------------------------------
# 5. Canonical succession — hidden_by_commander trigger (new in D3)
# ---------------------------------------------------------------------------

def test_canonical_succession_promotes_earliest_eligible_mate():
    conn = _fresh_conn()
    raw_a = _insert_raw(conn, "gggg000000000001", "canonical, hidden by commander", "2026-05-28")
    raw_b = _insert_raw(conn, "gggg000000000002", "mate one, earliest", "2026-05-29")
    raw_c = _insert_raw(conn, "gggg000000000003", "mate two, later", "2026-05-30")

    conn.execute(
        "INSERT INTO news_articles (raw_ref, relevant, relevance_judged_at, dedup_judged_at, hidden_by_commander) "
        "VALUES (?, 1, 't', 't', 1)", (raw_a,),
    )
    conn.execute(
        "INSERT INTO news_articles (raw_ref, relevant, relevance_judged_at, duplicate_of_raw_ref, dedup_judged_at) "
        "VALUES (?, 1, 't', ?, 't')", (raw_b, raw_a),
    )
    conn.execute(
        "INSERT INTO news_articles (raw_ref, relevant, relevance_judged_at, duplicate_of_raw_ref, dedup_judged_at) "
        "VALUES (?, 1, 't', ?, 't')", (raw_c, raw_a),
    )
    conn.commit()

    promotions = run_canonical_succession(conn, dry_run=False)
    assert len(promotions) == 1

    b_row = conn.execute("SELECT duplicate_of_raw_ref FROM news_articles WHERE raw_ref = ?", (raw_b,)).fetchone()
    assert b_row[0] is None, "earliest surviving mate must become the new canonical"
    c_row = conn.execute("SELECT duplicate_of_raw_ref FROM news_articles WHERE raw_ref = ?", (raw_c,)).fetchone()
    assert c_row[0] == raw_b, "remaining mate must be repointed to the new canonical"
    a_row = conn.execute("SELECT duplicate_of_raw_ref FROM news_articles WHERE raw_ref = ?", (raw_a,)).fetchone()
    assert a_row[0] == raw_b, "old canonical itself must also be repointed"

    # Idempotency.
    assert run_canonical_succession(conn, dry_run=False) == []


def test_canonical_succession_excludes_manual_override_mate():
    conn = _fresh_conn()
    raw_p = _insert_raw(conn, "hhhh000000000001", "canonical, hidden", "2026-05-28")
    raw_q = _insert_raw(conn, "hhhh000000000002", "protected mate, earliest", "2026-05-29")
    raw_r = _insert_raw(conn, "hhhh000000000003", "unprotected mate", "2026-05-30")

    conn.execute(
        "INSERT INTO news_articles (raw_ref, relevant, relevance_judged_at, dedup_judged_at, hidden_by_commander) "
        "VALUES (?, 1, 't', 't', 1)", (raw_p,),
    )
    conn.execute(
        "INSERT INTO news_articles (raw_ref, relevant, relevance_judged_at, duplicate_of_raw_ref, dedup_judged_at, manual_override) "
        "VALUES (?, 1, 't', ?, 't', 1)", (raw_q, raw_p),
    )
    conn.execute(
        "INSERT INTO news_articles (raw_ref, relevant, relevance_judged_at, duplicate_of_raw_ref, dedup_judged_at) "
        "VALUES (?, 1, 't', ?, 't')", (raw_r, raw_p),
    )
    conn.commit()

    promotions = run_canonical_succession(conn, dry_run=False)
    assert len(promotions) == 1
    q_row = conn.execute("SELECT duplicate_of_raw_ref FROM news_articles WHERE raw_ref = ?", (raw_q,)).fetchone()
    assert q_row[0] == raw_p, "manual_override mate must never be promoted"
    r_row = conn.execute("SELECT duplicate_of_raw_ref FROM news_articles WHERE raw_ref = ?", (raw_r,)).fetchone()
    assert r_row[0] is None, "next-eligible (unprotected) mate must be promoted instead"


def test_canonical_succession_no_op_when_canonical_still_visible():
    conn = _fresh_conn()
    raw_x = _insert_raw(conn, "iiii000000000001", "canonical, still visible", "2026-05-28")
    raw_y = _insert_raw(conn, "iiii000000000002", "correctly suppressed", "2026-05-29")
    conn.execute(
        "INSERT INTO news_articles (raw_ref, relevant, relevance_judged_at, dedup_judged_at) VALUES (?, 1, 't', 't')",
        (raw_x,),
    )
    conn.execute(
        "INSERT INTO news_articles (raw_ref, relevant, relevance_judged_at, duplicate_of_raw_ref, dedup_judged_at) "
        "VALUES (?, 1, 't', ?, 't')", (raw_y, raw_x),
    )
    conn.commit()

    promotions = run_canonical_succession(conn, dry_run=False)
    assert promotions == []
    y_row = conn.execute("SELECT duplicate_of_raw_ref FROM news_articles WHERE raw_ref = ?", (raw_y,)).fetchone()
    assert y_row[0] == raw_x, "correctly-suppressed duplicate must not be resurrected"


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for test in tests:
        test()
        print(f"  PASS: {test.__name__}")
    print(f"test_news_pipeline.py: {len(tests)}/{len(tests)} passed")


if __name__ == "__main__":
    main()
