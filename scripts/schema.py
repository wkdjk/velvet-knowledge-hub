# schema.py — single source of truth for the KVN_Articles tab header row.
#
# A3 fix (2026-07-03): collect_naver.py, classify_articles.py, and
# setup_sheets.py each hardcoded their own copy of this header list and had
# drifted out of sync (different column count, order, and names). This
# module is now the only place the header is defined; the other three
# scripts import it and verify the live sheet matches before writing.
#
# Security: no credentials in this file.

# Live KVN_Articles header, row 1 (12 columns). Matches the sheet as
# hand-verified 2026-06-05 (C-5g/C-5h) plus english_title added C-8 P0b.
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
