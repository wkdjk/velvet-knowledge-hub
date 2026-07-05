# Run as: PYTHONPATH=. python scripts/append_readme_admin_tab_guide.py [--dry-run]
#
# append_readme_admin_tab_guide.py — one-time (idempotent) append of a
# per-tab reference row to README_Admin, one row for each of the 15 live
# VKH_Data tabs (Phase 1 sheet cleanup, CaptainQ dispatch 2026-07-06).
#
# The existing README_Admin rows are task how-tos ("Add trade data",
# "Update keywords", ...). These new rows are a tab-by-tab architecture
# index: what writes it, what reads it, which site section it feeds, and
# which column (if any) the Commander hand-edits. Complements, does not
# replace, the existing rows.
#
# Same 3-column schema as the existing tab (section, instruction, example) —
# no header change, no new tab. Appended below the existing rows via
# append_rows(), so the current row count in the live sheet does not need
# to be known in advance.
#
# Idempotent: reads the existing `section` column and skips any tab name
# already present, so re-running after a partial write (or after the
# Commander adds their own row) never creates a duplicate entry.
#
# Phase 1 is cosmetic only — no tab renames, no schema/data changes. Phase 2
# (renaming tabs to match site section names) is explicitly NOT authorised.
#
# Security: no credentials in this file. All secrets from environment only.

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.sheets_auth import connect_sheets, resolve_sheet_id  # noqa: E402

README_TAB = "README_Admin"

# One row per tab, in the same order as the CaptainQ tab inventory. Order
# here is just append order — display order is organize_sheet_tabs.py's job.
NEW_ROWS: list[list[str]] = [
    [
        "VTW_Trade_Monthly",
        "Written by ingest_nz_export.py/ingest_qia.py/ingest_kstat.py/"
        "ingest_mfds_annual.py, read by vkh_data.py/vkh_charts.py. Feeds "
        "Section 1 (Basic trade statistics), Section 2 (Trade "
        "triangulation), and the A3 import-price chart. Human-editable: "
        "paste new rows below existing data (see 'Add trade data' above) — "
        "no other column is hand-edited.",
        "The series column tells sources apart in one tab: nz_export, qia, "
        "kstat, mfds_annual.",
    ],
    [
        "csv_request_log",
        "Written by the CSV-by-email Apps Script (vkh_csv_email.gs), not "
        "read by any Python script or rendered on the site. Supports the "
        "CSV-by-email request feature. Never hand-edited — it is a log, "
        "not a data source.",
        "Each row records one request: requester email, timestamp, and "
        "which tab's CSV was sent.",
    ],
    [
        "VFI_Import_Records",
        "Written by ingest_vfi_records.py, read by vkh_render.py. Feeds "
        "Section 4 (Import records). See 'Update import records' above for "
        "the manual-paste workflow — importer_en/product_en are patched by "
        "the trust-pipeline backfill scripts afterwards, never typed in by "
        "hand.",
        "Dedup key is (date, importer, product_name) — pasting the same "
        "MFDS quarter twice does not create duplicate rows.",
    ],
    [
        "VFI_Price_Annual",
        "Written by ingest_vfi_price.py, read by vkh_charts.py. Feeds the "
        "Section 4 annual price chart. Human-editable: paste MFDS annual "
        "ranking rows below existing data — see 'Annual price data' above.",
        "Dedup key is (year, rank, product_name); re-pasting last year's "
        "rankings is safe.",
    ],
    [
        "KVN_Articles",
        "Written by collect_naver.py/classify_articles.py, read by "
        "vkh_render.py. Feeds Section 3 (News pulse). Only "
        "manual_override is hand-edited.",
        "Set manual_override=TRUE on an article the dedup pass wrongly "
        "flagged as a duplicate, to keep it on the site.",
    ],
    [
        "_keywords",
        "Fully human-edited — feeds collect_naver.py's collection filter, "
        "not a display section itself. See 'Update keywords' above for "
        "the row format.",
        "term=사슴농장, type=allow, language=ko.",
    ],
    [
        README_TAB,
        "This tab — the admin guide itself. Fully human-edited (and "
        "TechQ-appended when a new tab is added). Not read by any script; "
        "reference only.",
        "You are reading it.",
    ],
    [
        "Source_Status",
        "Fully human-edited freshness tracker. Not read by any script — "
        "reference only, so the Commander can see at a glance which "
        "source was last updated when. See 'Source_Status freshness' "
        "above.",
        "The nz_export row's last_updated cell shows the date of the most "
        "recent CSV paste.",
    ],
    [
        "raw_vfi",
        "Append-only machine audit trail written by ingest_vfi_records.py, "
        "backing Section 4's trust pipeline. Never hand-edited — hidden by "
        "default; if you unhide it, still don't type into it.",
        "One row per raw MFDS import record exactly as collected, before "
        "any KR->EN mapping is applied.",
    ],
    [
        "map_companies",
        "Seeded by seed_map_companies.py, then corrected by the Commander; "
        "read by ingest_vfi_records.py/backfill_company_mapping.py. Feeds "
        "Section 4's trust pipeline (importer name KR->EN resolution). The "
        "tab you edit to resolve a company-name entry flagged in "
        "needs_review.",
        "needs_review flags 마더스초이스 as unmapped — add a row here with "
        "canonical_name_en = Mothers Choice to resolve it.",
    ],
    [
        "needs_review",
        "Exceptions queue written by ingest_vfi_records.py and the "
        "backfill scripts, reviewed (not hand-edited) by the Commander. "
        "Feeds Section 4's trust pipeline. Fix the matching map_* tab "
        "instead of editing rows here.",
        "A row here with field=importer_en means: add the missing name to "
        "map_companies, not to this tab.",
    ],
    [
        "review_view",
        "Generated by generate_review_view.py so the Commander can review "
        "the trust pipeline's output at a glance. Feeds Section 4's "
        "review workflow. Don't hand-edit — it is regenerated, not "
        "maintained.",
        "Shows date/importer/product/country side-by-side in KO and EN so "
        "a mismatch is easy to spot.",
    ],
    [
        "weekly_brief",
        "Auto-drafted by vkh_brief.py, read by vkh_render.py. Feeds the "
        "weekly brief shown near the top of the site. Only the approved "
        "column is hand-edited — set TRUE to publish that week's draft.",
        "Set approved=TRUE on the current week_ending_date row once "
        "draft_text has been read and looks right.",
    ],
    [
        "map_countries",
        "Seeded by seed_map_terms.py, then corrected by the Commander; "
        "read by ingest_vfi_records.py/backfill_term_mapping.py. Feeds "
        "Section 4's trust pipeline (country name KR->EN resolution). "
        "Same role as map_companies, for countries.",
        "needs_review flags 베트남산 as unmapped — add a row here with "
        "canonical_name_en = Vietnam.",
    ],
    [
        "map_types",
        "Seeded by seed_map_terms.py, then corrected by the Commander; "
        "read by ingest_vfi_records.py/backfill_term_mapping.py. Feeds "
        "Section 4's trust pipeline (product-type name KR->EN resolution). "
        "Same role as map_companies, for product types.",
        "needs_review flags 편록 as unmapped — add a row here with "
        "canonical_name_en = Sliced velvet.",
    ],
]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Append one per-tab reference row to README_Admin for each live VKH_Data tab."
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    sheet_id = resolve_sheet_id()
    print(f"Connecting to sheet: {sheet_id}")
    spreadsheet = connect_sheets(sheet_id)
    print(f"  Connected to: '{spreadsheet.title}'")

    ws = spreadsheet.worksheet(README_TAB)
    header = ws.row_values(1)
    if header != ["section", "instruction", "example"]:
        print(
            f"WARNING: '{README_TAB}' header row does not match the expected "
            f"3-column schema.\n  live header: {header}",
            file=sys.stderr,
        )

    existing_sections = {row.get("section", "").strip() for row in ws.get_all_records()}
    print(f"  {README_TAB}: {len(existing_sections)} existing row(s)")

    rows_to_add = [row for row in NEW_ROWS if row[0] not in existing_sections]
    skipped = len(NEW_ROWS) - len(rows_to_add)
    print(f"  {len(rows_to_add)} new row(s) to add, {skipped} already present (skipped)")

    if args.dry_run:
        print("[DRY RUN] no writes made.")
        for row in rows_to_add:
            print(f"  + {row[0]}")
        return

    if not rows_to_add:
        print("Nothing to do — all tab reference rows already present.")
        return

    ws.append_rows(rows_to_add, value_input_option="USER_ENTERED")
    print(f"  {README_TAB}: {len(rows_to_add)} row(s) appended.")


if __name__ == "__main__":
    main()
