# schema.py — single source of truth for the KVN_Articles tab header row.
#
# A3 fix (2026-07-03): collect_naver.py, classify_articles.py, and
# setup_sheets.py each hardcoded their own copy of this header list and had
# drifted out of sync (different column count, order, and names). This
# module is now the only place the header is defined; the other three
# scripts import it and verify the live sheet matches before writing.
#
# Security: no credentials in this file.

# Live KVN_Articles header, row 1 (15 columns). Matches the sheet as
# hand-verified 2026-06-05 (C-5g/C-5h) plus english_title added C-8 P0b,
# plus the 3 semantic-dedup columns added C-13 Task 1 (2026-07-04).
# 'title' holds the article URL and 'url' holds the Korean title text — a
# historical column-swap from the original collector; kept as-is because
# ~5,600 live rows already use this layout and a rename would require a
# full re-ingest for no functional gain.
KVN_ARTICLES_HEADERS = [
    "article_id",
    "title",
    "url",
    "content_hash",
    "published_date",
    "source",
    "category",
    "english_summary",
    "ai_processed_at",
    "include_on_site",
    "crawled_at",
    "english_title",
    # C-13 Task 1 (2026-07-04): semantic near-duplicate cache + manual
    # protection columns. Additive, same pattern as english_title (C-8 P0b) —
    # run scripts/add_dedup_columns_header.py once against the live sheet
    # before running classify_articles.py's semantic clustering pass.
    "duplicate_of_article_id",  # "" = never judged; "none" = judged, not a duplicate; else = article_id of the canonical row this row duplicates.
    "dedup_judged_at",          # ISO timestamp of the last dedup judgment (own row), mirrors ai_processed_at's empty-means-unprocessed convention.
    "manual_override",          # "TRUE" set by a human only — protects this row from ever being auto-suppressed by the clustering pass.
]


# ---------------------------------------------------------------------------
# Trust pipeline (C-15, 2026-07-05) — raw -> mapping -> master + needs_review
# gate, per VKH_improvement_directive_2026-07-03.md §4. VFI_Import_Records is
# the first source migrated to this pattern — see ingest_common.py for the
# shared gate functions every ingest script should eventually adopt.
# ---------------------------------------------------------------------------

# raw_vfi: append-only, exactly-as-collected KR-language fields. Never
# hand-edited. Bootstrap-seeded from the existing VFI_Import_Records tab
# (scripts/bootstrap_raw_vfi.py) since original pre-ingest source files for
# past batches were not retained (Downloads/ is transit-only, per fleet rule).
RAW_VFI_HEADERS = [
    "date",
    "importer_ko",
    "product_name",
    "product_type_ko",
    "country_origin_ko",
    "country_export_ko",
    "expiry_date",
    "notes",
    "ingested_at",
]

# map_companies: the ONLY tab a human (Commander) edits. Replaces the retired
# manual VLOOKUP file. match_key is precomputed by
# ingest_common.normalise_company_key() at seed/write time so lookups never
# recompute it at read time.
MAP_COMPANIES_HEADERS = [
    "source_name_kr",
    "match_key",
    "canonical_name_en",
    "public_display_name",
    "country",
    "notes",
]

# needs_review: build/backfill scripts only. Any row whose company name has
# no map_companies match lands here instead of silently rendering "—" or
# raw Korean text forever.
NEEDS_REVIEW_HEADERS = [
    "source_tab",
    "row_ref",
    "field",
    "raw_value",
    "match_key",
    "reason",
    "flagged_at",
]


# weekly_brief (C-14 item 4, 2026-07-05): auto-drafted "this week at a
# glance" brief + human publication gate. See scripts/vkh_brief.py for the
# full design. Human-edited columns are marked below — the build script
# never overwrites them, same convention as manual_override above.
WEEKLY_BRIEF_HEADERS = [
    "week_ending_date",     # script-written; ISO date; one row per build week (unique key)
    "draft_text",           # script-written; only written once per week_ending_date, never overwritten
    "fact_check_status",    # script-written: "ok" | "review_needed"
    "fact_check_detail",    # script-written: human-readable list of any unmatched figures, blank if ok
    "approved",             # human-only — Commander sets TRUE to publish this week's brief
    "approved_at",          # auto-stamped once by the script, the first run after approved=TRUE with this cell blank
    "published_text",       # human-only — optional edited final text; blank means the site falls back to draft_text
    "notes",                # human-only — free text
]


def verify_header(worksheet, expected: list[str] = KVN_ARTICLES_HEADERS) -> None:
    """
    Read row 1 of worksheet and compare it to expected.

    Prints a loud WARNING (not a silent pass) if they differ — this is the
    A3 fix's "fail loudly" requirement. Does not raise or exit: a schema
    mismatch should surface in the build log, not crash a scheduled run
    that a non-technical Commander cannot debug (L-12 graceful degradation
    applies to the warning path, not to silence).
    """
    import sys  # ponytail: local import, avoids a module-level dependency for one call

    live_header = worksheet.row_values(1)
    if live_header != expected:
        print(
            f"WARNING: '{worksheet.title}' header row does not match schema.py "
            f"KVN_ARTICLES_HEADERS.\n"
            f"  live header:     {live_header}\n"
            f"  expected header: {expected}\n"
            "  Column-name lookups (get_all_records()) may silently return "
            "empty values. Update schema.py or the sheet header row to match.",
            file=sys.stderr,
        )
