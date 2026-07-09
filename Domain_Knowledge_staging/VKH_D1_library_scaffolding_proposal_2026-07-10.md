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

**Where `promote_one()` gets called from** is an open question (see §7) — options are (a) a small new admin CLI script the Commander runs after reading `list_pending()` output, (b) a Google Sheet the Commander edits (title/date/category columns) that a script diffs against `list_pending()` and promotes automatically once filled in — closer to the existing Sheets-editing UX the Commander already knows, and would reuse `gspread` rather than requiring a new admin surface. Option (b) more closely matches "Google Sheets remains the human edit/view window" from the rebuild decision's Architecture section — flagging as the likely right answer, pending confirmation.

## 6. config.yaml source block draft

```yaml
  # --- Library (D1, Phase D rebuild, sqlite-backed — not a Sheets tab) -----
  - id: library_docs
    tab: null  # NOTE: validate_config.py currently requires 'tab' to be
               # non-empty for every source (REQUIRED_SOURCE_FIELDS). This
               # source has no Sheets tab. validate_config.py needs a small
               # follow-up patch (accept 'db_table' as an alternative to
               # 'tab') before this block can be enabled — flagged in §7,
               # not fixed here (out of scope for scaffolding).
    db_table: library_docs
    kind: directory  # closest existing kind (stable, low-churn curated
                      # rows). Revisit if Library's display needs diverge
                      # enough from "directory" once D1 is actually built —
                      # a new "library" kind is one line to add if so.
    section: library
    enabled: false  # flip true once ingest_library.py + dashboard
                    # rendering exist (D1 full build, not this scaffolding
                    # step)
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

## 7. Open questions for CaptainQ / Commander

1. **Promotion interface** — admin CLI vs. a Sheets tab the Commander fills in (§5). Sheets tab is likely the better fit (matches "Sheets stays the human edit/view window" from the decision doc) but needs confirmation before `ingest_library.py`'s real implementation is written.
2. **`validate_config.py` gap** — the validator's `REQUIRED_SOURCE_FIELDS` set (`id, tab, kind, section, enabled`) assumes every source has a Sheets tab. A sqlite-backed source needs either a placeholder `tab` value or a validator patch (accept `db_table` as an alternative to `tab`). This will affect every one of D1–D4, not just Library — worth deciding once, here, rather than re-deciding at D2.
3. **Same-repo vs. new repo** — this worktree adds new files to `velvet-knowledge-hub` (the same repo whose `main` is frozen for feature work). None of the new files touch anything frozen, so this should be safe to merge under the freeze policy, but confirming explicitly: is the sqlite rebuild meant to land as new files in this same repo (assumed here), or a separate repo entirely?
4. **Drive subfolder for Library uploads** — decision doc references the existing intake folder `DINZ data for Velvet Knowledge Hub` generally; `ingest_from_drive.py`'s `FOLDER_MAP` keys on specific named subfolders (`qia`, `nz`, `mfds_price`, etc.). Library needs its own subfolder ID before `ingest_from_drive.py` can gain a `library` key — likely a new Drive subfolder the Commander creates, e.g. matching the `02_Library` placeholder name used in this doc's demo code, or reusing an existing one if the staged reference material (`VKH_library_staging_2026-07-09.md`) already has a home.
5. **`file_type` source of truth** — Drive's own `mimeType` (e.g. `application/pdf`) vs. filename suffix (`.pdf`). Recommend deriving from `mimeType` where present (more reliable than a user-supplied filename) with suffix as fallback — not yet decided in code, flagging for the real implementation.

None of these block the scaffolding step itself — all five are implementation-time decisions for the next dispatch once this proposal is confirmed.

## 8. Test-as-specification checklist (for the real D1 build, not this scaffolding step)

- [ ] A PDF dropped in the Library Drive folder appears as a `raw_library_files` row (unclassified) within one ingest cycle.
- [ ] Re-running the ingest cycle against the same file (same `drive_file_id`) does not create a second `raw_library_files` row.
- [ ] A human-tagged `library_docs` row survives a re-ingest of its source file — schema-enforced via `UNIQUE(file_ref)`, not an application-level check (verify this with a test that promotes, then re-ingests, then asserts `library_docs` still has exactly one row for that file).
- [ ] A `raw_library_files` row with no matching `library_docs` row does not crash the dashboard build — `library_available()` returns `True`/`False` correctly based on `library_docs` row count only, never touches `raw_library_files` for rendering.
- [ ] Attempting to promote the same raw file twice raises a clear error (`sqlite3.IntegrityError` on `UNIQUE(file_ref)`), not a silent duplicate or a crash with an unreadable stack trace — `promote_one()`'s real implementation must catch and re-raise with a Commander-readable message ("already promoted, see library_docs row N").
- [ ] `library_schema.py`'s `demo()` and `vkh_sqlite.py`'s `demo()` continue to pass unmodified once `ingest_library.py`/`library_data.py` are implemented — a real implementation must not require changing the schema's own self-check.

## 9. Pre-mortem gap check

Nothing missed from the 2026-07-09 pre-mortem specific to Library scaffolding. One addition worth flagging to CaptainQ: the pre-mortem covered the rebuild decision generally but the `validate_config.py` required-fields gap (§7 item 2) is a concrete blocker that will hit D2 identically — worth fixing once, before D2 starts, rather than re-discovering it there.

---

**Worktree:** `/Users/Qs/C/velvet-knowledge-hub-d1-library`
**Branch:** `d1-library-scaffold`
**`main` status:** untouched — no commits made to `main`, no files modified outside this worktree.
