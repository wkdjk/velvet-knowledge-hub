# VKH D1 — Library section scaffolding proposal

**Author:** TechQ (worktree `d1-library-scaffold`, dispatched by CaptainQ, Commander asleep — automatic execution authorised)
**Date:** 2026-07-10
**Status:** PROPOSAL — scaffolding only, per the fleet's scaffolding-before-full-build rule. Nothing here is wired into the live site or merged to `main`. For CaptainQ/Commander review on wake.
**Scope:** Library section (D1) of the Sheets→sqlite re-platform. Does not touch Trade/News/Product sections, does not touch `velvet-knowledge-hub`'s frozen `main` branch.

---

## 1. Summary

Two-table sqlite schema (`raw_library_files` + `library_docs`) generalising the existing `raw_vfi` raw-store-then-map pattern to file metadata instead of import rows. Three flat skeleton modules follow the existing `vkh_data.py`/`vkh_kpi.py`/`vkh_charts.py` deep-module convention: `library_schema.py` (DDL), `library_data.py` (dashboard read queries — skeleton, `NotImplementedError`), `ingest_library.py` (Drive→raw write + manual promote — skeleton, `NotImplementedError`). One new shared module, `vkh_sqlite.py`, factors out the connect/migrate boilerplate every future section (D2–D4) will also need, rather than each section reopening its own `sqlite3.connect()`. `vkh.sqlite` itself proposed at the repo root, alongside `config.yaml`, not under `docs/` (a build artefact directory) or `scripts/` (code).

Curation is intentionally manual — a raw file with no matching `library_docs` row is simply "pending"; there is no third `needs_review`-style table, because Library categorisation is human judgement by design (decision doc's "Data entry" section), not an automatable name-matching problem like `map_companies`.

## 2. Files created in this worktree

| File | Purpose | Status |
|---|---|---|
| `scripts/library_schema.py` | DDL for both tables + `demo()` self-check | Runnable, tested |
| `scripts/vkh_sqlite.py` | Shared `connect()`/`migrate()` helper for all sections | Runnable, tested |
| `scripts/ingest_library.py` | Ingest flow function signatures | Skeleton — every function raises `NotImplementedError` by design |
| `scripts/library_data.py` | Dashboard read-query function signatures | Skeleton — every function raises `NotImplementedError` by design |

All four have a `demo()`/`__main__` self-check (ponytail discipline: no non-trivial file ships without one runnable check). The two real (non-skeleton) files were run and pass:

```
$ PYTHONPATH=. python3 scripts/library_schema.py
library_schema.py demo: OK — DDL valid, UNIQUE(file_ref) enforced
$ PYTHONPATH=. python3 scripts/vkh_sqlite.py
vkh_sqlite.py demo: OK — connect() + migrate() created both tables
```

The two skeleton files' `demo()` confirms every function raises `NotImplementedError` — this stops a future partial implementation from silently returning `None` where a caller expects real data.

## 3. Schema (DDL)

```sql
CREATE TABLE IF NOT EXISTS raw_library_files (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    drive_file_id   TEXT NOT NULL UNIQUE,
    filename        TEXT NOT NULL,
    source_folder   TEXT NOT NULL,
    file_type       TEXT NOT NULL,
    uploaded_at     TEXT,
    ingested_at     TEXT NOT NULL,
    raw_metadata    TEXT
);

CREATE TABLE IF NOT EXISTS library_docs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    file_ref        INTEGER NOT NULL UNIQUE REFERENCES raw_library_files(id),
    title           TEXT NOT NULL,
    doc_date        TEXT,
    category        TEXT,
    tags            TEXT,
    summary         TEXT,
    curated_at      TEXT NOT NULL,
    curated_by      TEXT NOT NULL DEFAULT 'Commander'
);

CREATE INDEX IF NOT EXISTS idx_library_docs_date ON library_docs(doc_date);
CREATE INDEX IF NOT EXISTS idx_library_docs_category ON library_docs(category);
```

**Rationale:**
- `raw_library_files` rejects nothing on shape — any file type, any folder, gets a row. `raw_metadata` is a JSON-text overflow column for whatever extra Drive file-resource fields turn up; a field only gets promoted to its own typed column once a query actually needs to filter on it (ponytail: don't pre-guess which Drive metadata fields matter).
- Dedup key is `drive_file_id` (`UNIQUE`), not a synthesised `(filename, date)` tuple — Drive file IDs are globally stable and unique, and re-polling an already-seen file must be a no-op, not a new audit-trail row (unlike `raw_vfi`, which is genuinely append-only per ingest run because MFDS/QIA source files can repeat the same date+company across different downloads).
- `library_docs.file_ref UNIQUE` makes "a human-tagged category survives a re-ingest of the same file" a schema-enforced guarantee, not an application check the ingest script has to remember: since `drive_file_id` dedup already blocks a duplicate raw row, and `file_ref UNIQUE` blocks a duplicate promotion, there is no code path that can produce two `library_docs` rows for one physical file.
- No `needs_review` table for Library. `map_companies`/`map_countries`/`map_types` exist because those are closed, resolvable sets (a company name either matches a known mapping or doesn't). Library categorisation has no such closed set — every raw file needs a human to actually read it and decide title/date/category. The "review queue" is just `SELECT * FROM raw_library_files WHERE id NOT IN (SELECT file_ref FROM library_docs)` — a query, not a table to keep in sync.

## 4. Folder / module structure

Kept flat in `scripts/`, matching the existing convention (no subfolders currently exist under `scripts/`, and `vkh_data.py`/`vkh_kpi.py`/`vkh_charts.py`/`vkh_render.py` are all flat):

- `scripts/library_schema.py` — this section's DDL (mirrors `schema.py`'s role: single source of truth for a section's "shape", but for sqlite tables rather than Sheets tab headers).
- `scripts/library_data.py` — this section's read queries for the dashboard build (mirrors `vkh_data.py`'s "what to show" responsibility).
- `scripts/ingest_library.py` — this section's write path from Drive (mirrors the `ingest_*.py` naming convention already used by `ingest_qia.py`, `ingest_nz_export.py`, `ingest_vfi_records.py`, etc., and would be dispatched from `ingest_from_drive.py`'s `FOLDER_MAP` the same way).
- No `library_kpi.py` / `library_charts.py` — Library has no numeric KPIs or chart data, only a curated document list. Per YAGNI, these modules get created only if the section grows content that actually needs them.
- `scripts/vkh_sqlite.py` — **new shared module**, not section-specific. Factors out `sqlite3.connect()` + `PRAGMA foreign_keys = ON` + apply-DDL-list, so D2 (`trade_schema.py`), D3 (`news_schema.py`), D4 (`product_schema.py`) each write their own DDL list and call `vkh_sqlite.migrate()`, rather than four sections each reopening the DB with their own pragma calls. Mirrors the role `sheets_auth.py` played for the old Sheets pipeline (one shared connect helper, many section-specific readers/writers on top of it).
- `vkh.sqlite` — proposed at the **repo root**, alongside `config.yaml`. Rationale: it is the data counterpart to the source registry (`config.yaml` describes what the sources are; `vkh.sqlite` holds what they contain) — not a regenerate-every-run build artefact like `docs/index.html`, so it does not belong under `docs/`, and it is data, not code, so it does not belong under `scripts/`.

This module split does **not** touch `vkh_charts.py` (already 1,227 lines, flagged in the rebuild decision as must-not-grow) — Library needs no chart logic, so this is a non-issue for D1, but the same discipline applies to D2 (trade triangulation charts must land in a new module, not accrete onto `vkh_charts.py`).

## 5. Ingest flow design

```
Commander drops a file in a Drive subfolder
        │
        ▼
ingest_from_drive.py polls the folder (existing pattern, FOLDER_MAP)
        │
        ▼
dispatches scripts/ingest_library.py --file <path> --source-folder <name>
        │
        ▼
ingest_one_file() : INSERT OR IGNORE INTO raw_library_files
  (dedup on drive_file_id — repeat poll of the same file = no-op)
        │
        ▼
raw file sits "pending" until a human curates it
  (no automatic promotion — Library curation is manual by design)
        │
        ▼
promote_one() : Commander (or CaptainQ on Commander's instruction) supplies
  title / doc_date / category / tags / summary → INSERT INTO library_docs
        │
        ▼
list_library_docs() reads published rows for the dashboard build
```

**Where `promote_one()` gets called from — decided (CaptainQ, 2026-07-10):** a Sheets tab, not an admin CLI. The rebuild decision doc's Architecture section is explicit that Sheets stays the human edit/view window for every section, not only the ones with a pre-existing tab. `promote_one()`'s real implementation reuses `gspread` (same pattern as every other ingest script): a small script reads a Library-curation Sheets tab (title/date/category/tags/summary columns, keyed to `raw_library_files.id` or `drive_file_id`), diffs it against `list_pending()`'s output, and calls `promote_one()` for any row the Commander has filled in.

## 6. config.yaml source block — added (2026-07-10, `enabled: false` still)

`validate_config.py` fixed (§6a) and the block below is now live in this worktree's `config.yaml` — no longer a draft:

```yaml
  # --- Library (D1, Phase D rebuild) -----------------------------------------
  # sqlite-backed, not a Sheets tab — first source of this kind, see
  # validate_config.py's tab/db_table location-field check (2026-07-10).
  # enabled stays false until ingest_library.py's real implementation and
  # dashboard rendering exist (D1 full build; this is still scaffolding).

  - id: library_docs
    db_table: library_docs
    kind: directory
    section: library
    enabled: false
    description: >
      Reference library — deer velvet supply-chain intelligence, pricing
      research, and industry yearbook excerpts. Files dropped in the Drive
      folder "DINZ data for Velvet Knowledge Hub" are auto-captured into
      raw_library_files (sqlite); a human (Commander) curates title/date/
      category into library_docs before a document appears on the
      dashboard. Schema + ingest flow: scripts/library_schema.py,
      Domain_Knowledge/VKH_D1_library_scaffolding_proposal_2026-07-10.md.
```

`id: library_docs` — plain descriptive name, no predecessor codename, per the 2026-07-09 purge directive.

### 6a. `validate_config.py` fix (was §7 item 2, now resolved)

`REQUIRED_SOURCE_FIELDS` used to hardcode `tab` as unconditionally required and non-empty — this would have rejected `library_docs` (no Sheets tab at all). Fixed:

- `REQUIRED_SOURCE_FIELDS` narrowed to `{id, kind, section, enabled}` (backing-store-agnostic).
- New `_LOCATION_FIELDS = ("tab", "db_table")` check: a source is valid if **at least one** of `tab`/`db_table` is non-empty — not both required, and having both is not an error either (covers a hypothetical future migration-in-progress source cleanly, at no extra cost).
- New test file `scripts/test_validate_config.py` (assert-based, matches `test_dedup_logic.py`/`test_smoke.py` convention — no existing test coverage for the validator was found before this fix). 7/7 pass:

```
$ PYTHONPATH=. python3 scripts/test_validate_config.py
  PASS: test_sheets_backed_source_valid
  PASS: test_sqlite_backed_source_valid
  PASS: test_source_with_neither_tab_nor_db_table_fails
  PASS: test_source_with_both_tab_and_db_table_is_not_an_error
  PASS: test_empty_tab_and_missing_db_table_fails
  PASS: test_missing_other_required_field_still_caught
  PASS: test_unknown_kind_still_caught
test_validate_config.py: 7/7 passed
```

- **Regression check** — ran the real validator against the full real `config.yaml` (all 7 existing sources + the new `library_docs` block), not just the new path:

```
$ PYTHONPATH=. python3 scripts/validate_config.py
validate_config.py — all checks passed
  display_kinds : 5 kinds defined
  sources total : 8
  enabled       : 6
  disabled      : 2
  disabled ids  : ['market_presence', 'library_docs']
  result        : OK
```

All existing Sheets-backed sources (`nz_export`, `korea_quarantine`, `kstat_api`, `vfi_import_records`, `vfi_price_annual`, `market_presence`, `kvn_articles`) still validate clean — the narrowed `REQUIRED_SOURCE_FIELDS` didn't silently loosen anything else, since every one of them still has a non-empty `tab`.

## 7. Open questions for CaptainQ / Commander

**Resolved 2026-07-10 (CaptainQ, from existing Commander decisions already on file):**
1. ~~Promotion interface~~ → Sheets tab, not an admin CLI (see §5).
2. ~~`validate_config.py` gap~~ → fixed, see §6a.
3. ~~Same-repo vs. new repo~~ → confirmed same repo (`velvet-knowledge-hub`). The whole rebuild (module boundaries kept, `actions/deploy-pages` kept, `ingest_from_drive.py` reworked in place) was always scoped to stay in this repo — there was never a new-repo option on the table.
5. ~~`file_type` source of truth~~ → ratified: Drive `mimeType` primary, filename-suffix fallback.

**Still genuinely open (Commander-only — cannot be resolved from existing decisions):**
4. **Drive subfolder for Library uploads** — `ingest_from_drive.py`'s `FOLDER_MAP` keys on specific named subfolders (`qia`, `nz`, `mfds_price`, etc.). Library needs its own subfolder ID before `ingest_from_drive.py` can gain a `library` key — this is the Commander's own Drive organisation and cannot be decided for them.

Neither open item blocks the scaffolding step itself.

## 8. Test-as-specification checklist (for the real D1 build, not this scaffolding step)

- [ ] A PDF dropped in the Library Drive folder appears as a `raw_library_files` row (unclassified) within one ingest cycle.
- [ ] Re-running the ingest cycle against the same file (same `drive_file_id`) does not create a second `raw_library_files` row.
- [ ] A human-tagged `library_docs` row survives a re-ingest of its source file — schema-enforced via `UNIQUE(file_ref)`, not an application-level check (verify this with a test that promotes, then re-ingests, then asserts `library_docs` still has exactly one row for that file).
- [ ] A `raw_library_files` row with no matching `library_docs` row does not crash the dashboard build — `library_available()` returns `True`/`False` correctly based on `library_docs` row count only, never touches `raw_library_files` for rendering.
- [ ] Attempting to promote the same raw file twice raises a clear error (`sqlite3.IntegrityError` on `UNIQUE(file_ref)`), not a silent duplicate or a crash with an unreadable stack trace — `promote_one()`'s real implementation must catch and re-raise with a Commander-readable message ("already promoted, see library_docs row N").
- [ ] `library_schema.py`'s `demo()` and `vkh_sqlite.py`'s `demo()` continue to pass unmodified once `ingest_library.py`/`library_data.py` are implemented — a real implementation must not require changing the schema's own self-check.

## 9. Pre-mortem gap check

Nothing missed from the 2026-07-09 pre-mortem specific to Library scaffolding. The `validate_config.py` required-fields gap flagged in the original version of this doc has now been fixed (§6a) — done once, before D2, rather than re-discovered there. Confirmed no regression: all 7 pre-existing sources plus the new `library_docs` block validate clean (§6a).

---

**Worktree:** `/Users/Qs/C/velvet-knowledge-hub-d1-library`
**Branch:** `d1-library-scaffold`
**`main` status:** untouched — no commits made to `main`, no files modified outside this worktree.
