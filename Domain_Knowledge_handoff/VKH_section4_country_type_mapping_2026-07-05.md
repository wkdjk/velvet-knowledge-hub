# VKH §4 country/type trust-pipeline gate — C-16

**Date:** 2026-07-05 | **Author:** TechQ | **Worktree branch:** `c-vfi-country-type-mapping` (repo `/Users/Qs/C/velvet-knowledge-hub-worktrees/c-vfi-country-type-mapping`, not merged to `main`)

Follow-up to `VKH_section4_trust_pipeline_2026-07-05.md` (C-15). Scope: `country_origin_en`, `country_export_en`, `product_type_en` on `VFI_Import_Records` only.

---

## 1. Root cause recap (already confirmed by CaptainQ before dispatch, not re-diagnosed here)

`_parse_mfds()` in `ingest_vfi_records.py` (pre-fix, lines ~421-425) unconditionally set:

```python
row["importer_en"] = ""
row["product_type_en"] = ""
row["country_origin_en"] = ""
row["country_export_en"] = ""
```

`importer_en` was fixed C-15 via the `map_companies` gate. `product_type_en`/`country_origin_en`/`country_export_en` were left as a known, accepted gap (comment referenced "G-2" — no translation table shipped) — this is the gap this dispatch closes. The Korean-language source fields (`country_origin_ko`, `country_export_ko`, `product_type_ko`) were already populated correctly by `_MFDS_COLUMN_MAP` — this was a translation gap, not a data-collection gap. `vkh_render.py` renders only `_en` fields with a bare `or "—"` fallback (unchanged, correct render-layer behaviour — fix belongs at ingest time).

## 2. Design decision — tab shape

**Two tabs** (`map_countries`, `map_types`), not one generic `map_terms` tab with a `category` column.

Reasoning: `ingest_common.py`'s `load_company_mapping()`/`resolve_company()` are already fully generic — they read only `match_key`, `source_name_kr` (fallback), and `canonical_name_en`, with no company-specific logic beyond the function names. A single `map_terms` tab would need an extra category filter (to stop a country key and a type key colliding in one dict) before calling these functions — that filter is code that doesn't need to exist. Two tabs shaped identically to `map_companies` (minus the company-only `public_display_name`/`country` columns) let both functions be reused **completely unchanged** — zero new code in `ingest_common.py`, only a docstring note added recording the broader reuse. This is the "reuses more of ingest_common.py's existing generic-shaped functions" option the dispatch brief asked to prefer.

`map_countries` covers **both** `country_origin_en` and `country_export_en` — same underlying set of country names, one tab, seeded from both column-pairs merged.

Headers (`schema.py`, `MAP_COUNTRIES_HEADERS`/`MAP_TYPES_HEADERS`): `source_name_kr, match_key, canonical_name_en, notes`.

## 3. Files changed

| File | Change | Lines |
|---|---|---|
| `scripts/schema.py` | Added `MAP_COUNTRIES_HEADERS`, `MAP_TYPES_HEADERS` | +23 |
| `scripts/ingest_common.py` | Docstring only — records that `normalise_company_key`/`load_company_mapping`/`resolve_company` are now reused for country/type mapping unchanged. **No functional change.** | +8 |
| `scripts/setup_trust_pipeline_tabs.py` | Added `map_countries`/`map_types` to `TABS` list | +18/-4 |
| `scripts/seed_map_terms.py` (new) | One-time seed script — mirrors `seed_map_companies.py`, generalised to accept multiple `(ko_field, en_field)` pairs so `map_countries` merges `country_origin` + `country_export` columns. Idempotent per tab. | 116 |
| `scripts/ingest_vfi_records.py` | `main()`: added a second trust-pipeline gate block (after the existing `importer_en` gate) resolving `country_origin_en`/`country_export_en`/`product_type_en` via `map_countries`/`map_types`, using the same `resolve_company()`/`needs_review` pattern. Updated the top-of-file comment block. `_parse_mfds()` itself untouched — it still initialises the 3 fields to `""`; resolution happens in `main()`, same as `importer_en`. | +58/-4 |
| `scripts/backfill_term_mapping.py` (new) | Patches already-ingested live `VFI_Import_Records` rows with blank `_en` values for the 3 fields — the actual fix for what is currently showing "—" on the live site. Mirrors `backfill_company_mapping.py`. Idempotent (only touches currently-blank cells). | 122 |
| `scripts/test_trust_pipeline.py` | Extended (not a new file) — 3 new tests: `resolve_company`/`load_company_mapping` reused unchanged for a country-shaped fake worksheet; `seed_map_terms.build_seed_rows` merges origin+export columns into one key; skips never-mapped terms. | +67/-2 |

Total diff: 7 files, +395/-17 lines (`git diff --stat` on the worktree commit `8b9cc3f`).

## 4. Seed script — test method

**No real VFI historical `.xlsx` exists in this repo** (Downloads/ is transit-only per fleet rule; the original file was never committed, matching how `seed_map_companies.py` was itself tested in C-15 — that script's tests also use synthetic dict records, not a real file read). `seed_map_terms.build_seed_rows()` was verified the same way: synthetic KO/EN record dicts replicating the merge-two-columns and skip-unmapped cases (see `test_trust_pipeline.py` additions). This was **not** run against the live sheet or a real historical export — no such fixture exists to run it against, and running it live was out of scope per the dispatch's "do not run anything that writes to the live Sheet" constraint.

If the Commander wants a live pair-count, the safe next step is: `PYTHONPATH=. python scripts/seed_map_terms.py --dry-run` against the actual `VKH_Data` sheet (read-only — `get_all_values()`/`get_all_records()` only, no write in `--dry-run` mode) to print real distinct-key counts before deciding to run it for real.

## 5. Outstanding production step — NOT run by this dispatch

Two live-sheet commands still need to run against production, **neither was run in this session** (constraint: no live writes without Commander confirmation):

1. `PYTHONPATH=. python scripts/setup_trust_pipeline_tabs.py` — creates `map_countries`/`map_types` tabs (idempotent, safe to run first; no-ops if tabs already exist).
2. `PYTHONPATH=. python scripts/seed_map_terms.py` — seeds both tabs from the 576 historical rows (idempotent — no-ops if either tab already has data rows; run `--dry-run` first to see counts).
3. `PYTHONPATH=. python scripts/backfill_term_mapping.py` — the actual fix for the live "—" dashes. Patches every MFDS-ingested row currently sitting with blank `country_origin_en`/`country_export_en`/`product_type_en`. Idempotent, safe to re-run. Run `--dry-run` first to see resolved/unmatched counts before the real run.

Recommended order: 1 → 2 (or 2 `--dry-run` first) → 3 `--dry-run` → 3 (real).

## 6. Test results

- `PYTHONPATH=. python3 scripts/test_trust_pipeline.py` — **10/10 pass** (7 existing + 3 new).
- `PYTHONPATH=. python3 scripts/test_smoke.py` — 32/32 pass, no regression.
- `PYTHONPATH=. python3 scripts/test_dedup_logic.py` — pass (7/7 steps), no regression.
- `PYTHONPATH=. python3 scripts/test_kpi_date_normalisation.py` — pass, no regression.
- `PYTHONPATH=. python3 scripts/test_weekly_brief.py` — 11/11 pass, no regression.
- `python3 -m py_compile` on all 6 changed/new `.py` files — clean.
- L-26 import-existence check: `PYTHONPATH=. python3 -c "import scripts.<module>"` run for every changed/new file — all import cleanly (module-level only, no network).

No test connects to a live Google Sheet — this repo's whole test suite is synthetic-data/assert-based (per its own documented convention), consistent with C-15's test methodology.

## 7. Constraints honoured

- No new dependencies.
- No secrets/credentials in any new or changed file.
- No live Sheets writes performed (`--dry-run` flags exist on both new scripts; neither was run for real).
- `vkh_render.py` untouched beyond what the brief allowed (nothing — fix is entirely at ingest time).
- `.github/workflows/` untouched.
- Not merged/pushed to `main` — worktree branch only.
