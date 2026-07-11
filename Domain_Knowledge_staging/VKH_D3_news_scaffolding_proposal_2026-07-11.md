# VKH D3 — News section scaffolding proposal

**Author:** TechQ (worktree `d3-news-scaffold`, dispatched by CaptainQ)
**Date:** 2026-07-11
**Status:** PROPOSAL — revision 2, 2026-07-11. Design only, per the fleet's scaffolding-before-full-build rule. No sqlite tables created, no `config.yaml` changes, no live Sheets touched, nothing merged. Revision 2 incorporates CaptainQ's Pre-Mortem (`Bridge/2026-07-11_1306_from-captainq_vkh-d3-news-scaffolding-premortem.md`) and SurveyorQ's independent code-verified advisory (`Projects/Velvet_Knowledge_Hub/Domain_Knowledge/surveyorq_advisory_d3_news_premortem_2026-07-11.md`) — see §2, §3, §4, §6b, §7 for the resulting changes. For CaptainQ/Commander review before a Pre-Mortem opens D3's implementation phase.
**Scope:** News section (D3) of the Sheets→sqlite re-platform. Does not touch Library (D1, done) or Trade statistics (D2, done). Does not touch `velvet-knowledge-hub`'s frozen `main` branch.
**Follows the shape of:** `VKH_D1_library_scaffolding_proposal_2026-07-10.md` (schema, migration posture, resolved-vs-open split).

---

## 1. Summary

Two-table raw+canonical schema (`raw_news_articles` + `news_articles`), generalising the same pattern D1 established for Library — an append-only raw layer that accepts whatever Naver's API returns, and a canonical layer holding the derived/judged columns (relevance, category, clustering). Reuses `scripts/vkh_sqlite.py` unchanged.

The News section is a **port**, not a new build: `collect_naver.py`'s collection logic, `classify_articles.py`'s semantic clustering (Haiku pairwise match, `manual_override` protection, canonical succession), and `vkh_brief.py`'s weekly-brief generation/fact-check/approval-gate logic are all functionally sound today and carry over as-is per the rebuild decision. Three things change:

1. **Storage** — Sheets tab → sqlite raw+canonical pair, purging the `KVN` codename from every new table/column/variable name.
2. **A new relevance-filter step** becomes an explicit, separately-timestamped judgement (`relevant` / `relevance_judged_at`) rather than being folded invisibly into the same write as category/summary — closing the gap the Commander flagged, at no extra Haiku call cost (see §4).
3. **Weekly brief** stops being a standalone top-level section and becomes something the News section's own read path folds in, per the decision doc's visible-feature-changes table.

One thing this migration also fixes as a side effect: the live `KVN_Articles` Sheet has a known, documented column-swap bug (`title` holds the URL, `url` holds the Korean title, `content_hash` holds the description — see `classify_articles.py`'s C-5h/A3 comments). The new sqlite columns are named for what they actually hold; the swap is undone once, at migration time, and never reappears in the rebuilt code (§3).

---

## 2. Schema (DDL)

```sql
-- raw_news_articles — append-only. One row per article Naver's API returns,
-- collapsed only on content_hash (the existing dedup key, ported as-is).
-- Never mutated after insert; a re-poll of an already-seen article is a
-- no-op (INSERT OR IGNORE), not a new row and not an edit.
CREATE TABLE IF NOT EXISTS raw_news_articles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    content_hash    TEXT NOT NULL UNIQUE,   -- sha256(url + title_ko)[:16], ported unchanged
    url             TEXT NOT NULL,
    title_ko        TEXT NOT NULL,
    description     TEXT,
    published_date  TEXT,
    source_name     TEXT,
    source_domain   TEXT,
    keyword_matched TEXT,                   -- which _keywords term returned this article (see 6a)
    collected_at    TEXT NOT NULL,
    raw_metadata    TEXT                    -- JSON overflow, e.g. full Naver API item — NULL until a
                                             -- query actually needs a field not yet promoted to a column
);

CREATE INDEX IF NOT EXISTS idx_raw_news_articles_published ON raw_news_articles(published_date);

-- news_articles — canonical. One row per raw article that has entered the
-- judgement pipeline (relevance, then — only if relevant — classification
-- and clustering). UNIQUE(raw_ref) makes "a raw article gets judged at most
-- once per pipeline stage" schema-enforced, same guarantee D1 gave Library
-- curation.
CREATE TABLE IF NOT EXISTS news_articles (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_ref                INTEGER NOT NULL UNIQUE REFERENCES raw_news_articles(id),

    -- Relevance filter (new explicit step — see §4). Written first, always.
    -- relevant reflects the Haiku judgement ONLY — never overloaded to also
    -- mean "suppressed as a duplicate" or "hidden by a human". Those are
    -- duplicate_of_raw_ref and hidden_by_commander respectively (see §3
    -- Pass 2 and the schema note below — this is the fix for a mapping
    -- bug the live `include_on_site` column conflates all three into).
    relevant               INTEGER NOT NULL,      -- 0/1
    relevance_judged_at    TEXT NOT NULL,

    -- Classification (only meaningful, and only populated, when relevant=1).
    category               TEXT,
    english_title          TEXT,
    english_summary        TEXT,
    classified_at          TEXT,

    -- Clustering (existing algorithm, ported as-is — only runs over relevant=1 rows).
    duplicate_of_raw_ref   INTEGER REFERENCES raw_news_articles(id),  -- NULL = not a duplicate (or not yet judged — see dedup_judged_at)
    dedup_judged_at        TEXT,                  -- NULL = never judged by the clustering pass
    manual_override        INTEGER NOT NULL DEFAULT 0,  -- human-only column; this pipeline reads it, never writes it
    hidden_by_commander    INTEGER NOT NULL DEFAULT 0   -- human-only column; this pipeline reads it, never writes it — same posture as manual_override (Commander decision 2026-07-11, §7.10)
);

CREATE INDEX IF NOT EXISTS idx_news_articles_relevant ON news_articles(relevant);
```

**Schema note — a deliberate simplification the sqlite move enables:** the old Sheet used a two-value string sentinel (`""` = never judged, `"none"` = judged-not-a-duplicate) on `duplicate_of_article_id` because Sheets cells have no real NULL. sqlite does, so `dedup_judged_at IS NULL` now means "never judged" and `dedup_judged_at IS NOT NULL AND duplicate_of_raw_ref IS NULL` means "judged, canonical" — the same three states, without a magic string. `run_semantic_clustering_pass()`/`run_canonical_succession()`'s control flow ports across unchanged; only the column reads/writes need updating from string-sentinel checks to NULL checks.

**Why `duplicate_of_raw_ref` points at `raw_news_articles`, not `news_articles`:** the existing algorithm's working key is `article_id` (= `content_hash`), which lives on the raw row. Pointing the FK at the raw table means clustering code that already reasons in terms of `content_hash`/`article_id` needs the smallest possible port — resolve `content_hash` → `raw_news_articles.id` once, not thread two different ID spaces through the clustering pass.

**Human control surface — `hidden_by_commander` (added revision 2, per Commander decision 2026-07-11):** the live `include_on_site` column doubles today as the Commander's manual hide/show switch, and `run_canonical_succession()`'s only trigger is a human flipping it in the Sheet. Once articles live in sqlite, there is no Commander-editable surface unless one is built — SurveyorQ's advisory (N-2) flagged this as blocking. `hidden_by_commander` is that surface's data-side half; §6b adds the Sheets tab and sync script that write it. Same posture as `manual_override`: this pipeline reads it, never writes it.

---

## 3. Migration plan — `KVN_Articles` (~8,700 rows)

**Two-pass migration, both passes reading the live Sheet's actual columns by name (`get_all_records()`), never by position** — the live sheet's *header names* are correct, only the *data* is column-swapped relative to what the header implies (per `classify_articles.py`'s C-5h comment). The migration is the one place this gets corrected permanently:

| New raw column | Read from live Sheet column | Why |
|---|---|---|
| `content_hash` | `article_id` | unaffected by the swap — this is where the dedup hash has always correctly landed |
| `url` | `title` | live `title` column holds the article URL |
| `title_ko` | `url` | live `url` column holds the Korean title text |
| `description` | `content_hash` | live `content_hash` column holds the description |
| `published_date`, `source_name` (← `source`), `collected_at` (← `crawled_at`) | as named | unaffected by the swap |
| `source_domain` | derived from `url` at migration time via the existing `_source_name_from_url()` helper in `collect_naver.py` | never written historically (dropped by `rows_to_write()`, per its own comment) — backfillable, not a gap |
| `keyword_matched`, `raw_metadata` | — | never captured historically → `NULL` for all migrated rows (see §6a for the forward-fix) |

**Pass 1 — raw layer.** Read all rows once. Before grouping, assert every `article_id` is well-formed — 16-char hex, matching `_content_hash()`'s output shape. Any row failing this check is a hard fail for the whole migration run (threshold zero, not logged-and-continue): an empty or malformed `article_id` would otherwise silently merge unrelated articles under `UNIQUE(content_hash)` grouping, and that corruption is much harder to spot after the fact than a failed migration run is to re-run. Once the assertion passes, group by `content_hash`. Insert one `raw_news_articles` row per distinct `content_hash` (`INSERT OR IGNORE`), building a `content_hash → raw id` map in memory. This step alone fixes the known duplicate-insert debt: `classify_articles.py`'s `run_canonical_succession()` docstring confirms live groups with up to 26 physical rows sharing one `content_hash` (pre-2026-07-03 dedup was broken). `UNIQUE(content_hash)` collapses each such group to one raw row for free.

**Pass 2 — canonical layer**, only for rows where `ai_processed_at` is non-empty (already classified):
- **`relevant`** (revised — CaptainQ Pre-Mortem + SurveyorQ N-1): **not** a plain `include_on_site` copy. Live, `include_on_site=FALSE` means any of (a) Haiku judged the article irrelevant, (b) the clustering pass suppressed it as a duplicate — `run_semantic_clustering_pass()` writes `include_on_site=FALSE` plus a pointer for suppressed mates too, not only for irrelevant articles — or (c) a human hid it manually. Mapping all three onto `relevant=0` would mark every suppressed duplicate mate as irrelevant, killing canonical succession for every historical cluster and corrupting the `relevant` column's meaning for any future relevance-precision measurement. Correct rule: `relevant = 1` if **any** physical row in the hash group has `include_on_site=TRUE`, **or** the group's `duplicate_of_article_id` is non-empty and not `"none"` (a suppressed mate was relevant by construction — it was suppressed for duplication, not irrelevance). Suppression is represented only by `duplicate_of_raw_ref`, never by `relevant`.
- `category` / `english_title` / `english_summary` / `classified_at` (← `ai_processed_at`) — taken from whichever physical row in the group is the one currently visible (`include_on_site=TRUE`), or the most-recently-`ai_processed_at` row if none is visible.
- `duplicate_of_raw_ref` (revised — SurveyorQ N-4) — resolve the old `duplicate_of_article_id` string through the Pass 1 `content_hash → raw id` map, sourced from **the tracked row in the group** (the one physical row with non-empty `duplicate_of_article_id`), if one exists, else `NULL`. A dangling pointer (points at an `article_id` no live row has — plausible after months of manual Sheet edits) is logged and left `NULL`, not treated as a migration failure (L-12 graceful degradation).
- `dedup_judged_at` (revised — SurveyorQ N-4, N-5) — sourced from the same tracked row as `duplicate_of_raw_ref`. **Sentinel backfill:** rows suppressed during the pre-2026-07-04 difflib era carry a duplicate pointer but predate the `dedup_judged_at` column, so they would otherwise migrate as "pointer set, `dedup_judged_at` NULL" — a state §2's new semantics defines as "never judged", which is contradictory. Migration must backfill a sentinel timestamp (the row's own `crawled_at`/collection date) wherever `duplicate_of_article_id` is non-empty, including `"none"`.
- `manual_override` (revised — SurveyorQ N-4) — **any `TRUE` in the group wins**, same conservative rule as the classification-visibility selection above: never drop a human protection by picking the wrong physical row.
- Rows where `ai_processed_at` is empty migrate into `raw_news_articles` only — no canonical row yet. This is not a gap to backfill; it is exactly the "awaiting judgement" state the pipeline already knows how to pick up (§4), identical in shape to Library's pending-curation query.

**Verification (mandatory, per the decision doc's Transition risk #2 — cannot be skipped; extended revision 2 per CaptainQ + SurveyorQ §4):** row-count check (distinct `content_hash` in the old sheet vs. `raw_news_articles` row count) plus a ≥20-row content spot-check diff against the 2026-07-06 backup figures. The migration script prints four counts, not one:
1. **Collapsed groups with inconsistent classification text** across their physical copies (CaptainQ Pre-Mortem #1) — a group where the 26 copies disagree on category/summary text, not just on `include_on_site`.
2. **Dangling `duplicate_of_raw_ref` pointers** — a pointer to an `article_id` no live row has. **>5 dangling pointers forces a re-review before go-live** (the 2026-07-04 canonical-succession incident this guards against involved exactly 8 wrongly-restored duplicates).
3. **Pointer-set-but-`dedup_judged_at`-empty rows** (N-5, pre-backfill count) — should be zero after the sentinel backfill runs; the pre-backfill count is printed so a discrepancy between it and the post-backfill zero is visible in the migration log.
4. **Blank/malformed `article_id` rows** (N-6) — threshold zero, hard fail, not logged-and-continue (see Pass 1 above; this count should always print 0 because the run would already have aborted, but it is printed as a confirmation, not a discovery step).

All four counts are **unknown until run against the live sheet**; not estimated in this document. One post-migration functional check, run after the counts above: every live `manual_override=TRUE` article that is visible today must still be visible in the new read path (§4's display predicate) — a direct regression check for N-3/N-4, not just a count.

**Two notes on what the counts do not mean (SurveyorQ N-7, N-8 — documentation only, no migration change):**
- **N-7 — distinct `content_hash` ≠ distinct stories.** OI-3 switched the preferred URL from Naver's `link` to `originallink`; since `content_hash` includes the URL, the same story collected before and after OI-3 hashes differently. This is correct behaviour (semantic clustering already covers the display-side collapse), not a bug — the row-count verification above must not be built on an assumption that distinct hashes imply distinct stories.
- **N-8 — backfilled `source_domain` is not directly comparable to forward-collected values.** Migrated `source_domain` is derived from the stored URL, which for pre-OI-3 rows is often an aggregator domain (e.g. `n.news.naver.com`) rather than the true publisher, whereas forward collection derives it from `originallink` specifically. Documented as a known migrated-data limitation; do not treat the two populations as one comparable series.

The migration script itself (`scripts/migrate_news_to_sqlite.py`, one-off, retired after running once) is not "kept" the way `ingest_*.py` scripts are — it belongs to the same category as D2's trade-stats migration script, not to the "drop one-off setup/seed/backfill scripts" list in the rebuild decision (that list refers to *legacy* one-off scripts being retired, not to migration tooling the rebuild itself needs).

---

## 4. Relevance-filter step — design

**What the decision doc flags as the gap:** "a missing relevance-filter step between keyword collection and clustering (keyword match → LLM relevance judgement → existing semantic clustering → display)." Reading `classify_articles.py` closely: relevance judgement *already happens* — it is the `include_on_site` boolean, produced by the same Haiku call that also assigns `category`/`english_title`/`english_summary`. The literal missing piece is not a missing LLM call, it is that **relevance has no identity of its own** — it shares one write (`ai_processed_at`) with three unrelated fields. That means:
- Re-tuning relevance criteria alone (e.g. adjusting the include/exclude examples in the system prompt) has no way to be re-run without also re-generating every summary and title — there is no `--force-relevance-only` the way `--force-cluster` already exists for clustering.
- There is no query that answers "how many articles were judged, and when, purely on relevance" independent of classification volume — useful for the same kind of cost/quality monitoring the fact-check step already does for the weekly brief.

**Proposed pipeline (reuses the existing call, does not add a new one):**

```
collect_naver.py (ported, writes raw_news_articles)
        │
        ▼
raw row with no matching news_articles row = "pending judgement"
  (same query shape as Library's pending-curation queue:
   SELECT * FROM raw_news_articles WHERE id NOT IN
     (SELECT raw_ref FROM news_articles))
        │
        ▼
classify_articles.py's existing Haiku call (unchanged prompt, unchanged
system prompt criteria — already well-tuned per classifier_guidance.md)
returns relevant + category + english_title + english_summary in one
response, as it already does today
        │
        ▼
ONE INSERT INTO news_articles covering both column groups — relevant +
relevance_judged_at, and category/english_title/english_summary/
classified_at (NULL unless relevant=1) — in a single statement, never an
INSERT-then-UPDATE pair (see atomicity note below)
        │
        ▼
run_semantic_clustering_pass() (ported as-is) — operates only over
relevant=1 rows, exactly as it operates over include_on_site=TRUE today
        │
        ▼
display — news_data.py reads relevant=1 AND (duplicate_of_raw_ref IS NULL
OR manual_override=1) AND hidden_by_commander=0 (see §4 predicate note
and §6b)
```

**Cost impact: none.** One Haiku call per unjudged article, same as today — the change is purely in how the result is written (two logical column groups with independent timestamps, instead of one). This is a schema change, not a pipeline-cost change.

**Atomicity — tightened, per CaptainQ's required correction #1 and SurveyorQ §4 (revision 2):** "a single INSERT" is only atomic if it is *literally* one statement. `vkh_sqlite.py`'s `connect()` uses Python's default `isolation_level`, so a lone INSERT is atomic on its own with no explicit `BEGIN`/`COMMIT` needed — but that stops being true the moment the port writes more than one statement per article. Three concrete rules, not just a principle:
1. **One INSERT statement per article**, covering both column groups (relevance + classification). Never an INSERT-then-UPDATE pair — that reintroduces exactly the partial-write failure mode (`relevant=1`, `classified_at IS NULL`) that today's design cannot produce (live writes are a single per-row `batch_update`).
2. **`conn.commit()` only at batch boundaries**, never between logical column groups within a batch. A crash before commit loses the whole in-flight batch — re-judged next run, which costs a duplicate Haiku call but corrupts nothing.
3. **The `relevant=1 AND classified_at IS NULL` "pending, retry" state must be wired into the pipeline as an actual query**, not just defined in prose: `SELECT * FROM news_articles WHERE relevant=1 AND classified_at IS NULL` feeds the same retry path as the "pending judgement" query above, so a crash mid-run self-heals on the next scheduled run rather than leaving the row stuck.

**Suggested separate small fix (not blocking D3, SurveyorQ side-observation, applies to D1/D2 too):** `vkh_sqlite.py`'s `connect()` sets no `busy_timeout`. If a scheduled collection run and a manual classify run ever overlap, a write fails immediately with "database is locked" instead of waiting briefly. One line — `conn.execute("PRAGMA busy_timeout = 5000")` in `connect()` — closes it; cheap, solo-operator-safe, worth doing regardless of D3.

**ponytail:** fold relevance + classification into the single existing call rather than splitting into two API calls — the schema already supports a future split (`relevance_judged_at` and `classified_at` are already independent columns) without a further migration. Upgrade trigger: if the Commander wants to retune relevance criteria on a faster cadence than category/summary criteria, add a second Haiku call scoped to relevance only, gated by a new `--force-relevance` flag mirroring `--force-cluster`. (For any reader outside the fleet's own convention: `ponytail:` marks a deliberate, scoped simplification — the comment names both the shortcut taken and the trigger that would justify upgrading it, not an error or a leftover placeholder.)

**What is explicitly not addressed here (open question, §5):** whether a cheap non-LLM pre-filter should sit before this Haiku call to cut call volume further. Today, "keyword match" already happens at collection (Naver is queried per term in `_keywords`) — every result returned by a keyword search goes to the Haiku call. Whether the Commander wants a *second*, stricter keyword/regex gate (e.g. requiring 녹용 in the title, not just anywhere in Naver's match) before spending a Haiku call is a cost/precision trade-off this document does not resolve.

---

## 5. Weekly brief absorption

**What changes:**
- **Storage** — `vkh_brief.py` currently reads/writes a standalone `weekly_brief` Sheets tab via `gspread` directly. In the rebuild this becomes a raw+canonical sqlite pair, mirroring the Library curation pattern already built in D1:
  - `raw_weekly_brief_drafts` — script-authored, append-only. One row per week's Haiku-generated draft + fact-check result (`draft_text`, `fact_check_status`, `fact_check_detail`, `week_ending_date` unique, `generated_at`). The script writes here directly — no Sheets round-trip needed for machine-authored data.
  - `weekly_briefs` — canonical. `UNIQUE(draft_ref)`. Holds only what a human decides: `approved`, `approved_at`, `published_text`, `notes`. A small curation Sheets tab (same shape as `setup_library_curation_tab.py`) is the Commander's edit surface; a sync script diffs it against `raw_weekly_brief_drafts` and promotes/updates `weekly_briefs`, the same two-way pattern D1 already established for Library (`promote_one()`).
- **Invocation** — `build.py` currently (per the decision doc) doesn't map `weekly_brief` to any of the four sections. In the rebuild, the News section's own data-read module (`news_data.py`, mirroring `library_data.py`) calls `get_weekly_brief_context()` internally and folds the result into the News section's template context — it is no longer a build-level peer of `news_articles`/`import_intelligence`/etc.

**What does not change:** the prompt-construction logic (`_build_prompt()`), the mechanical fact-check regex/number-matching (`fact_check_draft()`), the human-approval-only publication gate (no auto-publish fallback — Pre-Mortem #4 stands), the `stale_after_days` staleness warning, and the `weekly_brief.enabled` kill switch in `config.yaml`. All of this is Commander-approved logic with its own pre-mortem on file (`vkh_brief_gate_premortem_2026-07-04.md`) — none of it is being re-litigated by this rebuild, only re-plumbed.

---

## 6. Config and module structure

### 6a. `config.yaml` source block (drafted here, not yet added to the live file — same posture as D1's §6 before D1's real build)

```yaml
  # --- News (D3, Phase D rebuild) --------------------------------------------
  # sqlite-backed. enabled stays false until ingest/classify scripts are
  # rewritten against sqlite (this is still scaffolding).
  - id: news_articles
    db_table: news_articles
    kind: news
    section: news_pulse
    enabled: false
    description: >
      Korean-language deer velvet news via Naver News API. Raw collection in
      raw_news_articles; relevance judgement, classification, and semantic
      clustering land in news_articles. Weekly brief absorbed into this
      section's read path (see scripts/news_data.py). Schema + pipeline:
      scripts/news_schema.py,
      Domain_Knowledge/VKH_D3_news_scaffolding_proposal_2026-07-11.md.
```

`id: news_articles` — no predecessor codename, per the 2026-07-09 purge directive. `validate_config.py`'s `_LOCATION_FIELDS` check (fixed in D1 §6a) already accepts a `db_table`-only source with no `tab` — no further validator work needed for D3.

### 6b. Module structure (flat, matching the existing `scripts/` convention — no change from D1's rationale)

| File | Role | Mirrors |
|---|---|---|
| `scripts/news_schema.py` | DDL for both tables | `library_schema.py` |
| `scripts/news_data.py` | Dashboard read queries — `relevant=1 AND (duplicate_of_raw_ref IS NULL OR manual_override=1) AND hidden_by_commander=0` + folded-in weekly brief context (predicate revised revision 2, SurveyorQ N-3) | `library_data.py` |
| `scripts/collect_naver.py` | Ported: Naver collection → `raw_news_articles` insert. Keeps `_keywords` tab (still Sheets-native — a keyword list is a small, frequently-hand-edited setting, not a data table; no reason to move it into sqlite) | itself, rewired to sqlite |
| `scripts/classify_articles.py` | Ported: relevance + classification (§4), semantic clustering, canonical succession — same functions, sqlite reads/writes instead of `gspread` batch_update | itself, rewired to sqlite |
| `scripts/vkh_brief.py` | Ported: prompt/fact-check logic unchanged; storage retargeted per §5 | itself, rewired to sqlite |
| `scripts/vkh_sqlite.py` | Reused unchanged | already built (D1) |
| `scripts/setup_articles_curation_tab.py` | **New (revision 2, Commander decision 2026-07-11, closes SurveyorQ N-2).** One-off: creates the `articles_curation` Sheets tab — article_id/title for reference, `hidden` checkbox, `manual_override` checkbox — the Commander's post-migration edit surface for articles | `setup_library_curation_tab.py` |
| `scripts/sync_articles_curation.py` | **New (revision 2).** Diffs `articles_curation` against `news_articles` and updates `hidden_by_commander`/`manual_override` — this pipeline's only write path to those two human-only columns | D1's `promote_one()` pattern in `ingest_library.py` |

`vkh_charts.py` is not touched — News has no chart logic, consistent with the "must not keep accreting section-specific logic" caveat from the rebuild decision.

---

## 7. Resolved without a Commander decision

From existing decisions and the D1 precedent — no wake needed:

1. **Table/column naming** — `news_articles`/`raw_news_articles`, no `KVN` anywhere. Per the 2026-07-09 purge directive.
2. **Raw+canonical two-table pattern, `UNIQUE(raw_ref)`** — direct application of the decision doc's "raw store + mapping layer, generalised" principle and D1's precedent.
3. **Dedup key stays `content_hash`** — already the working, documented mechanism (per "domain rules kept: per-source dedup keys" in the decision doc); the only fix is undoing the column-swap bug at migration time, not changing the key itself.
4. **Clustering and canonical-succession logic ported as-is** — the decision doc explicitly says "keep clustering"; nothing in the algorithm depends on Sheets specifically once the NULL-vs-sentinel simplification (§2) is applied.
5. **Weekly brief prompt/fact-check/approval-gate logic ported as-is** — decision doc explicitly says "logic ported as-is, not a standalone section." Its own pre-mortem already covers the auto-publish-fallback question; not reopened here.
6. **`_keywords` tab stays in Sheets, not migrated to sqlite** — it is Commander-edited configuration (a handful of search terms), not collected data; moving it would add a sync step for no benefit. Consistent with "Sheets remains the human edit/view window" for anything a human directly maintains.
7. **`vkh_sqlite.py` reused unchanged** — built exactly for this in D1.
8. **`validate_config.py` needs no further change** — the `db_table`-only source path was already fixed for Library.
9. **Relevance judgement folds into the existing single Haiku call** (§4) rather than adding a second API call — cost-neutral, and the schema already supports splitting later without a further migration.
10. **Articles curation tab added, closing SurveyorQ N-2** — the Commander decided (2026-07-11) to add a human control surface for articles rather than drop manual hide/succession as dead code: `hidden_by_commander` column (§2), `articles_curation` Sheets tab + `setup_articles_curation_tab.py` + `sync_articles_curation.py` (§6b). This was raised as a candidate fifth open decision in SurveyorQ's advisory; the Commander resolved it directly, so it does not appear in §8.

## 8. Needs a Commander decision before full implementation starts

1. **Pre-LLM keyword pre-filter strictness (§4, closing paragraph).** Does "keyword match" already satisfy the Commander's intent (Naver search-term matching, as today), or is a second, stricter gate wanted before the Haiku call (e.g. requiring 녹용 in the title specifically) to cut call volume further? This changes whether a new pre-filter function needs writing for D3, or whether the existing collection step already is that filter.
2. **Weekly brief storage shape (§5).** Confirm the proposed raw+canonical sqlite pair + curation Sheets tab (mirroring Library) is wanted, versus a lighter option — e.g. the script keeps writing/reading a Sheets tab directly for `weekly_brief` (as today) and only the *site's read path* (not the draft-generation/approval workflow) goes through sqlite. The Architecture section's "Sheets syncs into sqlite; the site never reads Sheets directly" principle argues for the raw+canonical version, but weekly_brief is small, single-writer, and already has a working human-gate design — a lighter sync may be enough. Recommend the raw+canonical version for consistency with the rest of the rebuild, but this is Commander's call.
3. **Migration duplicate-group and dangling-pointer counts (§3)** — cannot be answered without a live read against the current `KVN_Articles` sheet. Once available, these numbers need Commander sign-off as part of the mandatory migration-verification spot-check (decision doc's Transition risk #2), same as D2's row-count check.
4. **Historical `keyword_matched`/`source_domain`/`raw_metadata` gaps (§3 table)** — confirmed acceptable to leave `NULL` for migrated rows (no re-collection planned), but flagging explicitly since it means historical articles can never be filtered/reported by which keyword found them, only future ones.

---

## 9. Pre-mortem gap check

Revision 1's gap check text is superseded by the two full reviews this revision incorporates. CaptainQ's Pre-Mortem (four findings, both required corrections) and SurveyorQ's independent advisory (four claims confirmed, N-1 through N-9) are addressed above: N-1/N-2 blocking (§2, §3, §6b), CaptainQ correction 1 and N-4/N-5/N-6 in §3/§4, N-3 in §6b's predicate, N-7/N-8/N-9 as documentation-only notes in §3/§4. No further gap identified beyond the open §8 Commander decisions (keyword pre-filter strictness, weekly-brief storage shape, migration counts, historical field gaps).

---

**Worktree:** `/Users/Qs/C/velvet-knowledge-hub-d3-news`
**Branch:** `d3-news-scaffold`
**`main` status:** untouched — no commits made to `main`, no files modified outside this worktree. No sqlite tables created, no `config.yaml` edits, no Sheets writes — this document is the only artefact produced.
