# VKH D1 — Reference library: Commander download links + list/card redesign

**Author:** TechQ (worktree `velvet-knowledge-hub-d1-download-redesign`, branch `d1-library-download-redesign`)
**Date:** 2026-07-10
**Status:** Implemented, verified against live data, committed to branch — not merged to `main`.
**Base commit:** `419b869` (main, as stated in dispatch).
**Commit on branch:** `e554d0a`.

---

## 1. What changed

### A. `download_url` field, end to end

| File | Change |
|---|---|
| `scripts/library_schema.py` | `library_docs` DDL gains `download_url TEXT`, placed after `summary` (content fields together), before `curated_at`/`curated_by` (system fields). |
| `scripts/schema.py` | `LIBRARY_CURATION_HEADERS` gains `"download_url"` at the end, after `"summary"`. |
| `scripts/add_library_download_url_header.py` (new) | One-time migration mirroring `add_dedup_columns_header.py` exactly: writes `download_url` to cell `H1` of the live `library_curation` tab if not already present; exits 0 (no-op) if already correct; does not touch `A1:G1` or any data row. |
| `scripts/ingest_library.py` | `promote_one()`/`promote_or_update_one()` gain a `download_url` parameter (stored as-given, no validation at write time — a malformed paste must still be visible/fixable by the Commander in the Sheet). `sync_curation_tab()`'s push (`new_rows`) and pull (`promote_or_update_one()` call) both carry the 8th column through. |
| `scripts/library_data.py` | `list_library_docs()` selects `download_url` and runs it through the new `_sanitise_download_url()` guard before returning each doc dict. |

**Security guard (not in DesignQ spec, added per brief):** `_sanitise_download_url()` in `library_data.py` — allow-lists `http`/`https` scheme + requires a non-empty netloc (`urlparse`), else returns `None` (rendered identically to "absent" by the template). Fails closed and silently: a `WARNING` print to stderr, no exception, one bad URL never blocks the build. Validation is at the data layer (read path), not the template — the template only ever sees `None` or a URL known-safe for an `href`.

### B. Template + CSS — DesignQ spec implemented as written

- `templates/index.html.j2` section 5: `<table class="import-records-table">` replaced with `<ul class="library-list">`/`<li class="library-entry">` per spec §3, using real field names (`doc.title`, `doc.doc_date`, `doc.category`, `doc.tags`, `doc.summary`, `doc.download_url`) — matched the spec's illustrative names 1:1, no renaming needed.
- `assets/style.css`: added `.library-list`, `.library-entry`, `.library-body`, `.library-title`, `.library-summary`, `.library-tags`, `.library-tags-label`, `.library-meta`, `.library-meta-date`, `.library-meta-category`, `.library-download-link` (+ `:hover`), `.library-download-empty` — verbatim from spec §4/§5, using existing `--fs-caption`/`--fs-body`/`--accent`/`--text-primary`/`--text-secondary`/`--text-muted`/`--border`/`--surface-deep` tokens (none redefined).
- Mobile breakpoint (`@media max-width: 768px`): added `.library-entry`/`.library-meta` rules next to the existing `.article-entry`/`.article-meta` rules, verbatim from spec §6.
- `.import-records-table` CSS block and all its rules **left untouched** — still used by section 4. `.table-scroll-wrapper` dropped only from section 5's markup (removed the wrapping `<div>`), not from the CSS or section 4.
- Empty-table `else` branch (0 curated docs → "coming soon" placeholder) untouched, per spec §8.

## 2. Verification performed (real, not spec-only)

1. **Unit tests** — `PYTHONPATH=. python3 scripts/test_library_ingest.py`: 10/10 passed (8 pre-existing + 2 new: `test_download_url_round_trips_through_promote_and_update`, `test_download_url_scheme_guard_at_read_time` — covers insert path, the correction-A update path, a valid `https://` URL passing through unchanged, and `javascript:`/`ftp://`/blank/whitespace/`None`/garbage strings all normalising to `None`).
2. `PYTHONPATH=. python3 scripts/library_schema.py` and `scripts/vkh_sqlite.py` — both `demo()` self-checks pass unmodified (DDL change doesn't break either).
3. `PYTHONPATH=. python3 scripts/test_smoke.py` — 32/32 checks pass, no regression.
4. `PYTHONPATH=. python3 scripts/validate_config.py` — all checks pass, 7 enabled / 1 disabled (`market_presence`), matching pre-change baseline.
5. **Live migration** — copied `.env` from `/Users/Qs/C/velvet-knowledge-hub/.env`, ran `add_library_download_url_header.py` against the real `library_curation` tab:
   - **Before:** 7 headers (`drive_file_id..summary`), 1 real row (`Export Guide_final.pdf` / "DINZ Velvet Exporter Guidebook") — read and printed in full before touching anything.
   - **After:** header row gained `download_url` at `H1`; the existing row's 7 fields (`drive_file_id`, `filename`, `title`, `doc_date`, `category`, `tags`, `summary`) are byte-identical to the pre-migration read; new `H` cell is empty for that row.
   - Re-ran the script a second time: printed "already contains 'download_url' — nothing to do" and exited 0 (idempotency confirmed).
6. **Live build** — `ingest_library.py --sync` against the live sheet, then `scripts/build.py`, then Playwright screenshots (`file://` against `docs/index.html`, viewport 1280×1400, `color_scheme="light"` and `"dark"`):
   - `#section-5` renders the real "DINZ Velvet Exporter Guidebook" entry in the new list layout: title, full-width summary, tags line, `—` date, "EXPORT GUIDE" category badge, and the italic muted "No link yet" empty state (no `download_url` was set — correctly not populated by me, that is the Commander's data entry).
   - `#section-4` (import records table) screenshotted as a regression check — unchanged 6-column table, `Show more`/`CSV — request by email` controls, channel-scope accordion all intact.
   - Light and dark screenshots are byte-identical (`md5` match) for both sections — **this codebase has no dark-mode CSS at all** (no `prefers-color-scheme` block exists anywhere in `style.css`; confirmed by grep before screenshotting). This is expected given the Commander's confirmed single-theme B&W design (`Shared_Skills`/memory), not a bug in this change — noted as a deviation from the brief's literal "light + dark" framing, not from the DesignQ spec (which doesn't mention dark mode).
7. Deleted `.env` afterwards; confirmed absent (`ls` errors "No such file or directory"); grepped the diff and touched files for API-key/service-account patterns — none found.

## 3. Live migration outcome for the existing curated row

**No data loss.** The one production row (`drive_file_id=1WN8cq8EX8e4vVrePQJQOerqoP0yUnnm2`, "DINZ Velvet Exporter Guidebook") kept all 7 existing field values exactly as they were; it now also has an empty `download_url` cell, ready for the Commander to paste a link into.

**Local-sqlite caveat (not a migration issue, worth flagging):** `vkh.sqlite` is git-ignored and does not exist by default in a fresh worktree — this worktree's `raw_library_files` had no row for the real Drive file (this worktree never ran the real Drive-poll ingest). To exercise the real curated row through the full pipeline for the screenshot, I seeded one `raw_library_files` row with the real `drive_file_id`/filename via `ingest_one_file()` (a simulated Drive-poll result, same pattern the original D1 report used for its own live verification) before running `--sync`. This is local-only, gitignored state — it does not touch the Sheet, does not persist anywhere committed, and does not affect the live production Sheets data (verified in §2.5 above, checked before this step).

## 4. Deviations from the DesignQ spec / dispatch brief

1. **Migration approach** — used the "small one-time script" option (mirroring `add_dedup_columns_header.py`) rather than extending `setup_library_curation_tab.py`. Reason: `setup_library_curation_tab.py` calls `create_tab()`, which skips entirely if the tab already exists (verified by reading `setup_sheets.py`) — it has no "add a missing column to an existing tab" code path to extend, so the one-time-script pattern is the only one of the brief's two named options that actually fits an existing tab.
2. **Light/dark screenshots identical** — see §2.6. Not a spec deviation (DesignQ's spec has no dark-mode content); flagging only because the brief asked for both explicitly.
3. No other deviations — HTML structure, CSS values, and empty-state rules match spec §3/§4/§5/§6/§9 verbatim.

## 5. Files touched

- `scripts/library_schema.py`, `scripts/schema.py`, `scripts/ingest_library.py`, `scripts/library_data.py`, `scripts/test_library_ingest.py`
- `scripts/add_library_download_url_header.py` (new)
- `templates/index.html.j2`, `assets/style.css`, `docs/assets/style.css` (build-copied), `docs/index.html` (rebuilt)

Branch: `d1-library-download-redesign`, commit `e554d0a`. Worktree: `/Users/Qs/C/velvet-knowledge-hub-d1-download-redesign`.
