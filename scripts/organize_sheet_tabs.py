# Run as: PYTHONPATH=. python scripts/organize_sheet_tabs.py [--dry-run]
#
# organize_sheet_tabs.py — one-time (idempotent) reorder + color-group +
# hide of the 15 live VKH_Data tabs (Phase 1 sheet cleanup, CaptainQ
# dispatch 2026-07-06). Purely cosmetic: only ever touches sheet
# *properties* (index, tabColorStyle, hidden) via one combined
# spreadsheet.batch_update() call — never a tab rename, a header, or a
# data cell (see README_Admin's "Row 1 is sacred" row).
#
# Phase 2 (renaming tabs to match the live site's section names) is a
# separate, NOT-yet-authorised piece of work. Do not add renames here.
#
# Hides only raw_vfi/needs_review (pure machine tabs — never hand-edited).
# map_companies/map_countries/map_types stay visible (Commander corrects
# these by hand to resolve needs_review flags) but get a visibly distinct
# tint within the Section 4 color group, per the Commander's "색상만 구분"
# request — see the handoff doc for the exact color rationale.
#
# One combined batch_update per run (L-4: no per-property API calls in a
# loop) — reuses gspread's already-installed convert_hex_to_colors_dict
# helper rather than hand-rolling hex math.
#
# Security: no credentials in this file. All secrets from environment only.

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gspread.utils import convert_hex_to_colors_dict  # noqa: E402

from scripts.sheets_auth import connect_sheets, resolve_sheet_id  # noqa: E402

# ---------------------------------------------------------------------------
# Color groups (hex) — see handoff doc for the full rationale per group.
# ---------------------------------------------------------------------------
GREY_ADMIN = "#B7B7B7"       # README_Admin, Source_Status
BLUE_TRADE = "#4A86E8"       # VTW_Trade_Monthly (Sections 1+2)
GREEN_NEWS = "#6AA84F"       # KVN_Articles, _keywords (Section 3)
AMBER_RECORDS = "#E69138"    # VFI_Import_Records, VFI_Price_Annual, review_view
GOLD_MAPPING = "#F1C232"     # map_companies/map_countries/map_types — same
                             # family as AMBER_RECORDS, lighter tint, visibly
                             # distinct: "editable mapping tab" vs "read-only
                             # records tab" at a glance.
DARKGREY_HIDDEN = "#666666"  # raw_vfi, needs_review — hidden, but colored
                             # consistently in case a tab is ever unhidden.
PURPLE_BRIEF = "#8E7CC3"     # weekly_brief
LIGHTGREY_LOG = "#D9D9D9"    # csv_request_log

# Desired final state: (tab name, index, tab color, hidden).
DESIRED = [
    ("README_Admin", 0, GREY_ADMIN, False),
    ("Source_Status", 1, GREY_ADMIN, False),
    ("VTW_Trade_Monthly", 2, BLUE_TRADE, False),
    ("KVN_Articles", 3, GREEN_NEWS, False),
    ("_keywords", 4, GREEN_NEWS, False),
    ("VFI_Import_Records", 5, AMBER_RECORDS, False),
    ("VFI_Price_Annual", 6, AMBER_RECORDS, False),
    ("map_companies", 7, GOLD_MAPPING, False),
    ("map_countries", 8, GOLD_MAPPING, False),
    ("map_types", 9, GOLD_MAPPING, False),
    ("raw_vfi", 10, DARKGREY_HIDDEN, True),
    ("needs_review", 11, DARKGREY_HIDDEN, True),
    ("review_view", 12, AMBER_RECORDS, False),
    ("weekly_brief", 13, PURPLE_BRIEF, False),
    ("csv_request_log", 14, LIGHTGREY_LOG, False),
]


def build_requests(spreadsheet, desired):
    """
    Diff desired state against the live sheet's current worksheet
    properties and return only the batch_update requests needed to close
    the gap — a tab already matching desired index/color/hidden produces
    no request at all (idempotency: a second run is a clean no-op).

    Returns (requests, plan) where plan is a list of human-readable lines
    for --dry-run / logging.
    """
    by_title = {ws.title: ws for ws in spreadsheet.worksheets()}
    requests = []
    plan = []

    for title, index, color_hex, hidden in desired:
        ws = by_title.get(title)
        if ws is None:
            print(f"WARNING: tab '{title}' not found in the live sheet — skipping.", file=sys.stderr)
            continue

        changes = {}
        if ws.index != index:
            changes["index"] = index
        if (ws.tab_color or "").upper() != color_hex.upper():
            changes["tabColorStyle"] = {"rgbColor": convert_hex_to_colors_dict(color_hex)}
        if ws.isSheetHidden != hidden:
            changes["hidden"] = hidden

        if not changes:
            plan.append(f"  {title}: already up to date, no-op")
            continue

        requests.append({
            "updateSheetProperties": {
                "properties": {"sheetId": ws.id, **changes},
                "fields": ",".join(changes.keys()),
            }
        })
        plan.append(f"  {title}: {changes}")

    return requests, plan


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reorder + color-group + hide VKH_Data tabs (Phase 1 cleanup)."
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    sheet_id = resolve_sheet_id()
    print(f"Connecting to sheet: {sheet_id}")
    spreadsheet = connect_sheets(sheet_id)
    print(f"  Connected to: '{spreadsheet.title}'")

    requests, plan = build_requests(spreadsheet, DESIRED)

    print(f"\nPlanned changes ({len(requests)} tab(s) need an update):")
    for line in plan:
        print(line)

    if args.dry_run:
        print("\n[DRY RUN] no writes made.")
        return

    if not requests:
        print("\nAll tabs already match the desired order/color/hidden state. Nothing to do.")
        return

    spreadsheet.batch_update({"requests": requests})
    print(f"\n{len(requests)} tab(s) updated in one batch_update call.")


if __name__ == "__main__":
    main()
