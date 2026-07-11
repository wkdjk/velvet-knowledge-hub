# VKH D3 — News section build + test report

**Author:** TechQ (worktree `d3-news-scaffold`, dispatched by CaptainQ)
**Date:** 2026-07-11
**Status:** Build + unit-test complete against the finalised revision-2 spec. **Not merged to `main`. Live Sheets untouched. Live migration NOT run.** `config.yaml`'s `news_articles` source is `enabled: false`, matching D1's initial posture.
**Spec:** `Domain_Knowledge_staging/VKH_D3_news_scaffolding_proposal_2026-07-11.md` (revision 2, CaptainQ Pre-Mortem + SurveyorQ advisory incorporated).

---

## 1. Summary

Implements items 1–9 of the dispatch: schema, read path, `collect_naver.py`/`classify_articles.py`/`vkh_brief.py` rewired to sqlite storage, the new `articles_curation` Sheets curation tab + sync script, the one-off migration script (built and fixture-tested, **not run against live data**), `config.yaml` wiring, and new test coverage. `build.py`/`templates/index.html.j2` are **not** touched — this dispatch was scoped to the scripts layer only (D1's "full build" wired build.py/templates; this D3 dispatch's numbered list did not include that step, so it is deferred, not forgotten — see §5).

This is a genuine port, not a rewrite of the domain logic: `collect_naver.py`'s Naver collection, `classify_articles.py`'s Haiku classification + semantic clustering + canonical succession, and `vkh_brief.py`'s prompt/fact-check/approval-gate logic are functionally the same algorithms as the pre-rebuild version — only the storage layer changed from Sheets cell writes to sqlite rows, per the rebuild decision.

## 2. Files created/modified in this worktree

| File | Change |
|---|---|
| `scripts/news_schema.py` | New — DDL for `raw_news_articles`/`news_articles` (§2) and `raw_weekly_brief_drafts`/`weekly_briefs` (§5), mirroring `library_schema.py`'s structure/conventions |
| `scripts/news_data.py` | New — `list_news_articles()` (display predicate, §4/§6b), `news_available()`, `assemble_news_section()` (folds `get_weekly_brief_context()` in per §5) |
| `scripts/collect_naver.py` | Rewired — storage is now `raw_news_articles` (sqlite) via `ingest_articles()` (`INSERT OR IGNORE`, one statement per article). `_keywords` tab stays Sheets-native (§6b). Column-swap workaround removed — no longer needed |
| `scripts/classify_articles.py` | Rewired — `list_pending_relevance()`/`list_stuck_classification()` (the "pending judgement"/"pending, retry" queues, §4), `write_relevance_and_classification()` (one INSERT, both column groups, §4 correction 1), `retry_stuck_classification()` (UPDATE self-heal path), `run_semantic_clustering_pass()`/`run_canonical_succession()` ported to NULL-based judged states operating on sqlite. Sheets connection removed entirely — this script no longer touches Sheets at all |
| `scripts/vkh_brief.py` | Rewired — `generate_weekly_brief_draft()` writes `raw_weekly_brief_drafts` (sqlite, `UNIQUE(week_ending_date)` idempotency); `push_pending_drafts_to_sheet()`/`sync_weekly_brief_approvals()` (new) implement the human curation surface (see §4 deviation note); `get_weekly_brief_context(config, conn)` — signature changed from `(config, sheet)`. `fact_check_draft()`/`_build_prompt()`/`_call_haiku()` UNCHANGED |
| `scripts/schema.py` | Added `ARTICLES_CURATION_HEADERS` |
| `scripts/setup_articles_curation_tab.py` | New — mirrors `setup_library_curation_tab.py` exactly |
| `scripts/sync_articles_curation.py` | New — mirrors `ingest_library.py`'s `sync_curation_tab()` pattern; this pipeline's only write path to `hidden_by_commander`/`manual_override` |
| `scripts/migrate_news_to_sqlite.py` | New — one-off migration script. `--live` flag required to touch a real Sheet; default run just prints usage and exits. **Not invoked with `--live` in this dispatch** |
| `scripts/vkh_sqlite.py` | Small, separate change: `connect()` now sets `PRAGMA busy_timeout = 5000` (SurveyorQ side-observation, optional item, fleet-wide D1/D2/D3 fix) — see §6 |
| `config.yaml` | Added `news_articles` source block, `enabled: false` |
| `scripts/test_news_pipeline.py` | New — 13 tests: atomic single-INSERT write path, pending/stuck queues, display predicate (every branch), semantic clustering (NULL states), canonical succession (`hidden_by_commander` trigger) |
| `scripts/test_migrate_news_to_sqlite.py` | New — 13 tests: column-swap correction, 26-copy inconsistent-classification group, N-1 relevant derivation, dangling pointer, sentinel backfill, malformed-`article_id` hard fail, manual_override any-TRUE-wins, end-to-end orchestration |
| `scripts/test_weekly_brief.py` | Rewritten for the new `(config, conn)` signature; `fact_check_draft()` coverage carried over unchanged; added `sync_weekly_brief_approvals()` coverage |
| `scripts/test_dedup_logic.py` | **Deleted** — tested the old Sheets-based `classify_articles.py` API (`_FakeWorksheet`), fully superseded by `test_news_pipeline.py` against the new sqlite API. Stale filename references in other test files' docstrings updated to point at the replacement |

No changes to `build.py`, `templates/index.html.j2`, `.github/workflows/*`, or D1/D2 files (`library_*.py` untouched; `vkh_sqlite.py`'s one-line change is additive and backward-compatible with D1/D2's existing usage).

## 3. Deviation from spec, and why

**§5 weekly brief curation surface: reused the existing `weekly_brief` Sheets tab instead of a new tab.** The spec's §5 prose says "a small curation Sheets tab (same shape as `setup_library_curation_tab.py`)" but the dispatch's numbered "what to build" list (item 5) names only `vkh_brief.py` for this — no `setup_weekly_brief_curation_tab.py` is listed alongside `setup_articles_curation_tab.py`/`sync_articles_curation.py` (item 6), which *are* explicitly named. Reusing the existing `weekly_brief` tab (already created by `setup_weekly_brief_tab.py`, already the Commander's known approval surface) satisfies §5's substance — storage retargeted to `raw_weekly_brief_drafts`/`weekly_briefs`, approval gate logic unchanged, no new tab for the Commander to learn — without inventing a file the numbered list didn't ask for (ponytail: reuse before writing). `push_pending_drafts_to_sheet()` and `sync_weekly_brief_approvals()` in `vkh_brief.py` implement the two-way sync the spec describes, just against the pre-existing tab rather than a new one. Flagging this for CaptainQ review — if a dedicated `weekly_brief_curation` tab is wanted after all, it is a small follow-up (new tab + point the two sync functions at it instead).

## 4. Design decision not fully specified by the proposal: canonical succession's new trigger

`run_canonical_succession()`'s pre-rebuild trigger was "a human flips `include_on_site=FALSE` directly on the canonical row in the Sheet." In the new schema, `relevant` is explicitly Haiku-only (§2: "never overloaded to also mean... hidden by a human") — there is no equivalent human-writable field for "hide this specific canonical row" except `hidden_by_commander`, the human control surface §2/§6b introduced for exactly this purpose. I implemented succession's trigger as `hidden_by_commander=1` on the canonical row, with `manual_override=1` mates excluded from promotion candidacy (same rule as the pre-rebuild version, restated for the new predicate — a protected mate is already visible via the display predicate regardless of `duplicate_of_raw_ref`, so it needs no promotion). This is a direct, spec-consistent adaptation of an existing rule to the new schema's field, not a new design — but the proposal doesn't state it explicitly, so flagging it here per the dispatch's deviation-disclosure instruction.

One simplification the new schema *removes*, not adds: the pre-rebuild `run_canonical_succession()` had defensive logic for "multiple physical rows sharing one `article_id`" (the pre-C-13 duplicate-insert-debt bug). `UNIQUE(raw_ref)` on `news_articles` makes that whole bug class schema-enforced impossible going forward — the defensive lookup was dropped, not ported, with a comment explaining why.

## 5. Deliberately deferred (not part of this dispatch's numbered list)

- **`build.py`/`templates/index.html.j2` wiring** — `news_articles` is not yet added to any `SECTION_SOURCE_MAP`-equivalent or rendered. `assemble_news_section()` is built and tested standalone (§7 below) but has no call site in `build.py` yet. This mirrors D1's scaffolding step before its separate "full build" dispatch.
- **Running `scripts/migrate_news_to_sqlite.py --live`** against the real `KVN_Articles` Sheet — explicitly out of scope for this dispatch (Commander/CaptainQ review gate).
- **Flipping `config.yaml`'s `news_articles.enabled` to `true`** — depends on the live migration having run and been verified (mirrors D1's `enabled: false → true` only after verification, §6 item 6 of that report).
- **A dedicated `weekly_brief_curation` Sheets tab** — see §3 deviation note.

## 6. Optional side item — `busy_timeout` (separate from D3 feature work)

Added `conn.execute("PRAGMA busy_timeout = 5000")` to `vkh_sqlite.py`'s `connect()` (SurveyorQ's advisory, one line, fleet-wide D1/D2/D3 benefit — a scheduled collection run overlapping a manual classify/build run currently fails immediately with "database is locked" instead of waiting briefly). This is a one-line, backward-compatible addition with no behaviour change for the single-writer case D1/D2 already exercise; `library_schema.py`'s and its own `demo()` (re-run, §7) still pass unmodified. Noted here as instructed — a separate concern from the D3 feature commits, not mixed into them.

## 7. Test results (unit, in-memory sqlite — no network, no Sheets, no live migration)

New D3 coverage:
```
$ PYTHONPATH=. python3 scripts/test_news_pipeline.py
  ... 13 tests ...
test_news_pipeline.py: 13/13 passed

$ PYTHONPATH=. python3 scripts/test_migrate_news_to_sqlite.py
  ... 13 tests ...
test_migrate_news_to_sqlite.py: 13/13 passed

$ PYTHONPATH=. python3 scripts/test_weekly_brief.py
20 checks run, 0 failed.
ALL CHECKS PASSED
```

Self-checks:
```
$ PYTHONPATH=. python3 scripts/news_schema.py
news_schema.py demo: OK — DDL valid, UNIQUE(raw_ref) and UNIQUE(draft_ref) enforced

$ PYTHONPATH=. python3 scripts/vkh_sqlite.py
vkh_sqlite.py demo: OK — connect() + migrate() created both tables
```

Full regression sweep — every pre-existing suite still passes (test_dedup_logic.py retired, see §2):
```
$ PYTHONPATH=. python3 scripts/test_kpi_date_normalisation.py     # 2 checks, 0 failed
$ PYTHONPATH=. python3 scripts/test_library_ingest.py             # 10/10 passed
$ PYTHONPATH=. python3 scripts/test_organize_sheet_tabs.py        # 11 checks, 0 failed
$ PYTHONPATH=. python3 scripts/test_smoke.py                      # 32 checks, 0 failed
$ PYTHONPATH=. python3 scripts/test_trust_pipeline.py             # 4 checks, 0 failed
$ PYTHONPATH=. python3 scripts/test_validate_config.py            # 7/7 passed
$ PYTHONPATH=. python3 scripts/validate_config.py                 # OK, 9 sources, 7 enabled, 2 disabled (market_presence, news_articles)
```

**Total: 9 test files, 0 failures.** New/changed coverage this dispatch: 13 + 13 + 20 = 46 checks, all passing. `python3 -m py_compile scripts/*.py` clean across the whole `scripts/` directory.

End-to-end smoke test of `news_data.assemble_news_section()` (not a `test_*.py` file — a one-off verification run against a temp sqlite file, not the repo's real `vkh.sqlite`, and not committed):
- `enabled=False` in `config.yaml` (the live posture right now): returns immediately, **confirmed no `vkh.sqlite` file is created** — safe to run against the real repo config today.
- `enabled=True` against an empty temp DB: `{'data': [], 'has_data': False, ...}` (graceful empty state).
- `enabled=True` with one seeded visible article: `has_data=True`, 1 article returned, `last_updated` correctly derived, `weekly_brief` context folded in per §5.

## 8. Test-as-specification checklist (stated per the dispatch's success-conditions requirement)

| # | Must pass | Verified by |
|---|---|---|
| 1 | `relevant` derivation (N-1): a suppressed-duplicate mate is not migrated as irrelevant | `test_suppressed_duplicate_not_misclassified_as_irrelevant` |
| 2 | Display predicate (N-3), every branch | `test_display_predicate_every_branch` |
| 3 | Migration per-column selection for collapsed groups (N-4): visible row wins classification; any-TRUE wins `manual_override` | `test_26_physical_copies_inconsistent_classification_text`, `test_manual_override_any_true_wins` |
| 4 | Sentinel-backfill rule (N-5) | `test_sentinel_backfill_for_difflib_era_row`, contrast case `test_judged_not_duplicate_sentinel_needs_no_backfill` |
| 5 | Malformed/blank `article_id` hard-fails, threshold zero, nothing written | `test_malformed_article_id_hard_fails_before_any_insert` |
| 6 | Atomic single-INSERT write path (correction 1); classification columns NULL when `relevant=0` | `test_write_relevance_and_classification_relevant_row`, `test_write_relevance_and_classification_irrelevant_row_nulls_classification` |
| 7 | "Pending, retry" state is a real, callable query, not just documented | `test_stuck_classification_retry_path_is_real_and_callable` |
| 8 | Dangling `duplicate_of_raw_ref` pointer counted, migrated as NULL, never crashes | `test_dangling_pointer_counted_and_left_null` |
| 9 | Post-migration functional check: manual_override-visible articles stay visible | `test_manual_override_visible_regression_check_passes` |

All nine pass against synthetic fixtures. Verified against synthetic data only, per the dispatch's explicit "do not run against live Sheets" constraint — the live-data verification (four printed counts against the real 8,700-row sheet) is the separate, later step.

---

**Worktree:** `/Users/Qs/C/velvet-knowledge-hub-d3-news`
**Branch:** `d3-news-scaffold`
**`main` status:** untouched. No live Sheets writes, no live migration run, `config.yaml`'s `news_articles.enabled` stays `false`.
