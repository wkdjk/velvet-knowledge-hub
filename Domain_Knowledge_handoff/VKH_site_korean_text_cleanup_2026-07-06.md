# VKH site Korean-text cleanup — 2026-07-06

Worktree: `/Users/Qs/C/velvet-knowledge-hub-worktrees/vkh-korean-text-cleanup`
Branch: `vkh-korean-text-cleanup`
Base commit: `3b22a1b` (main, "feat(c16): country/type trust-pipeline gate for VFI_Import_Records")
Files changed: `scripts/vkh_render.py`, `templates/index.html.j2` (no `.github/workflows/` touched, no new dependencies)

All 4 findings from the dispatch brief were pre-diagnosed and confirmed by CaptainQ before this task — no re-diagnosis was performed here, only implementation.

---

## Item 1 — News pulse category badges (Korean → English)

**Root cause:** `scripts/classify_articles.py`'s `_VALID_CATEGORIES` is a closed 6-value Korean enum (classifier prompt only ever returns one of these strings), stored as-is in the `category` column of `KVN_Articles`. The template rendered `article.category` directly with no translation layer.

**Fix location:** `scripts/vkh_render.py` — added `_CATEGORY_EN` dict (module-level constant, near the top, next to the other imports) and a translation block inside `render()` (before `import_records_total = ...`) that builds `sections["news_pulse"]["data"]` with each row carrying a new `_display_category` key. Falls back to the raw value if a category is somehow not in the dict (defensive; should never trigger since `classify_articles.py` already validates against `_VALID_CATEGORIES` before writing to the sheet).

`templates/index.html.j2` line ~992: changed `{{ article.category }}` → `{{ article._display_category }}` (and the guarding `{% if article.category %}` → `{% if article._display_category %}`).

**Wording chosen (final):**

| Korean | English |
|---|---|
| 규제정책 | Regulatory policy |
| 무역시장 | Trade market |
| 건강제품 | Health products |
| 수입유통 | Import & distribution |
| 업계소식 | Industry news |
| 기타 | Other |

**Not built:** a Google Sheets mapping tab / needs_review gate (the VFI country/company pattern). Rejected deliberately — that pattern is for open-ended free-text values arriving over time; this is a fixed 6-value enum the classifier prompt itself controls, so a plain Python dict is the correct-weight fix (ladder rung 6/7, not rung "build infrastructure").

**`classify_articles.py` untouched** per constraint — the Korean values stored in the sheet are unchanged; translation is display-layer only, in the same place `QIA_COUNTRY_MAP` does it for country names (client-side JS precedent), except this one is server-side Python since article category is server-rendered.

---

## Item 2 — Native month-picker locale (`<input type="month">` → year/month `<select>` pairs)

**Root cause confirmed:** Chromium's native month/date picker chrome follows the browser's own display-language setting, not the page's `<html lang="en">` attribute (already present, confirmed not the missing piece) and not anything settable via HTML/CSS. No cross-browser fix exists for forcing picker-UI language on `<input type="month">`.

**Fix:** Replaced all 4 elements —
- `a1-from` → `a1-from-year` + `a1-from-month`
- `a1-to` → `a1-to-year` + `a1-to-month`
- `a2-from` → `a2-from-year` + `a2-from-month`
- `a2-to` → `a2-to-year` + `a2-to-month`

Template locations: `templates/index.html.j2` lines ~704-709 (A1 toggle row) and ~770-775 (A2 toggle row). Each pair is two empty `<select>` elements populated by JS at page init (`aria-label="From year"` / `"From month"` etc. added for accessibility since the visible `<label>` now points at a compound control).

**New JS (added just after `last12Months()`, ~line 1216):**
- `MONTH_NAMES` — English month names, index 0 = January.
- `populateMonthYearSelects(prefix, startYear, endYear)` — fills `prefix-year` (startYear..endYear) and `prefix-month` (01-12, English labels). Guards against re-populating (checks `.options.length` first) so repeated calls are idempotent.
- `getMonthValue(prefix)` — reads `prefix-year` + `prefix-month`, returns `"YYYY-MM"` or `""` if either select is missing/unset.
- `setMonthValue(prefix, value)` — given `"YYYY-MM"`, sets both selects' `.value`.

**Year range:** 2021 (matches the existing "Data available from January 2021" note next to the controls, left as-is — not part of this fix) through the current year, computed as `parseInt(last12Months().end.split('-')[0], 10)` — reuses the existing helper's own current-year computation rather than hardcoding a value that would go stale, per the brief's instruction.

**Updated call sites (`window.a1Render`, `window.a2Render`, `init()` IIFE):** all 6 `document.getElementById('a1-from').value`-style reads/writes replaced with `getMonthValue(...)` / `setMonthValue(...)`. The inner chart-render functions (`function a1Render()` / `function a2Render()`, ~line 1491/1522) were untouched — they only ever read `a1State.from` / `a1State.to`, which are still plain `"YYYY-MM"` strings, so `filterRows()` and every other consumer needed no changes.

**Dead CSS removed:** `.chart-toggle-row input[type="month"]` selectors (3 occurrences: base rule, `:focus` rule, mobile breakpoint) deleted since no `<input type="month">` remains — `.chart-toggle-row select` already covers the new elements with identical styling.

**No new dependency** — plain `<select>`, no date-picker library.

---

## Item 3 — Hardcoded Korean in source-citation prose

Replaced consistently across all 4 usages in `templates/index.html.j2` (lines ~821, ~830, ~1116, ~1117):
- `식품의약품통계연보` → **"Food & Drug Statistical Yearbook"**
- `수입식품 정보마루` → **"Imported Food Information Portal"**
- `MFDS` kept as-is (already the correct English acronym used site-wide).

Before/after (line ~821): `— 식품의약품통계연보 (Korean medicine yearbook), herbal medicine channel.` → `— Food & Drug Statistical Yearbook, herbal medicine channel.`

---

## Item 4 — Internal jargon in HTML comment

`templates/index.html.j2` line ~162 (CSS comment above `.estimate-badge`): `per the 잠망경 pre-mortem's Appendix B caveat requirement.` → reworded to describe the actual requirement in plain English: *"every derived (non-source-reported) figure in this section carries one, so readers can tell a calculated estimate from a directly-sourced number at a glance."* No fleet-internal codename retained.

---

## Verification method (worktree has no browser / no live Sheets credentials)

1. **Fixture render test** (`scripts/vkh_render.render()` called directly with hand-built fixture `sections` dict — no Sheets connection): confirmed via assertions that
   - `건강제품`/raw Korean category strings do not appear in the rendered HTML; `Health products` / `Trade market` do; an unmapped category value falls back to itself (proves the defensive fallback path).
   - No `<input type="month">` element remains in the rendered output; `a1-from-year` / `a1-from-month` element IDs are present.
   - `Food & Drug Statistical Yearbook` / `Imported Food Information Portal` present; `식품의약품통계연보` / `수입식품 정보마루` absent.
   - `잠망경` absent from the output.
   - Full `[가-힣]` grep of the rendered HTML: only the pre-existing `QIA_COUNTRY_MAP` Korean keys remain (`뉴질랜드`, `중국`, `러시아`, `홍콩`, `카자흐스탄`, `호주`, `몽골`, `미국`, `싱가포르`) — this is the established client-side lookup precedent referenced in the brief, not a bug. **`docs/index.html` was restored via `git checkout` after this test** — the fixture render is not a live rebuild and was not left in the repo (this repo has no live Sheets credentials in this worktree, so a full `PYTHONPATH=. python scripts/build.py` was not run).
   - This test script is not part of the repo; it lived in the scratchpad directory only and is not committed.

2. **JS logic verification via Node** (no browser access, so this is the highest-confidence check available short of a real browser): extracted the exact `populateMonthYearSelects` / `getMonthValue` / `setMonthValue` function bodies added to the template, ran them against a minimal DOM stub in Node.js. Confirmed: year range produces the expected option count, month values are zero-padded ("01".."12") with English `textContent`, `setMonthValue` → `getMonthValue` round-trips the exact `"YYYY-MM"` string, missing/unstubbed elements degrade to `""` without throwing (matches the `if (!sel) return` / `if (!yearSel || !monthSel...)` guards already in the code), and repeated `populateMonthYearSelects` calls do not duplicate options (idempotency check for the `init()` IIFE only being called once, but defensive regardless).
   **Not verified:** actual on-screen rendering, click/onchange event wiring through a real browser DOM, or the segmented-control / Chart.js interaction. This is a code-logic verification, not a rendered-pixel verification — flagging as residual risk below.

3. **Existing test suite:** ran all 6 test scripts in `scripts/` (`test_smoke.py`, `test_dedup_logic.py`, `test_kpi_date_normalisation.py`, `test_trust_pipeline.py`, `test_weekly_brief.py`) — all pass, 0 regressions. None of these exercise `vkh_render.py` or the template directly (pre-existing gap, not introduced by this change).

4. **Import/compile check:** `python3 -m py_compile scripts/vkh_render.py` and `PYTHONPATH=. python3 -c "import scripts.vkh_render; import scripts.vkh_data; import scripts.build"` — all clean.

---

## Residual risk

- The month-select replacement has not been visually verified in an actual browser (no browser access in this worktree). The JS-logic test gives high confidence the data plumbing is correct; it does not confirm layout/spacing of 4 selects per toggle row reads well at the `768px` mobile breakpoint (the `.chart-toggle-row select { width: 100% }` mobile rule now applies to 4 stacked selects per From/To pair instead of 1 input — visually untested). Recommend DesignQ/CaptainQ spot-check on first live deploy per the "post-deploy verification" lesson (green build ≠ correct rendered output).
- `docs/index.html` in this worktree still contains the pre-fix build (unchanged, restored to `main`'s version) — the real fix will only appear in `docs/index.html` after the next scheduled/triggered `build.py` run against live Sheets data, post-merge.
