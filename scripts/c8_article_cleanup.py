"""C-8 one-off: delete pre-Apr-2026 articles, extract Apr-Jun titles for manual translation.

Usage:
  PYTHONPATH=. python scripts/c8_article_cleanup.py --dry-run   # preview counts only
  PYTHONPATH=. python scripts/c8_article_cleanup.py             # delete + export titles
"""

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.sheets_auth import connect_sheets, resolve_sheet_id

CUT_DATE = "2026-04-01"
APR_START = "2026-04-01"
JUN_END   = "2026-06-30"
OUTPUT    = Path(__file__).resolve().parent.parent / "c8_titles_for_translation.txt"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    spreadsheet = connect_sheets(resolve_sheet_id())
    ws = spreadsheet.worksheet("KVN_Articles")
    rows = ws.get_all_records()  # list of dicts, header=row1

    pre_cut, mid, after = [], [], []
    for i, row in enumerate(rows, start=2):  # row 2 = first data row
        d = (row.get("published_date") or "")[:10]
        if d and d < CUT_DATE:
            pre_cut.append(i)
        elif d and APR_START <= d <= JUN_END:
            mid.append((i, row.get("url", ""), d))  # C-6a swap: url col = Korean title
        else:
            after.append(i)

    print(f"  Pre-{CUT_DATE} (delete): {len(pre_cut)} rows")
    print(f"  {APR_START}–{JUN_END} (export titles): {len(mid)} rows")
    print(f"  After {JUN_END} (keep): {len(after)} rows")

    if args.dry_run:
        print("  --dry-run: no changes made.")
        return

    # Export titles first (safe read)
    lines = [f"{row_num}\t{date}\t{title}" for row_num, title, date in mid]
    OUTPUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"  Titles exported → {OUTPUT}")

    # Delete pre-cut rows: find contiguous ranges, delete bottom-to-top.
    to_delete = sorted(pre_cut)
    ranges = []
    start = end = to_delete[0]
    for r in to_delete[1:]:
        if r == end + 1:
            end = r
        else:
            ranges.append((start, end))
            start = end = r
    ranges.append((start, end))

    deleted = 0
    for s, e in reversed(ranges):
        ws.delete_rows(s, e)
        deleted += e - s + 1
        print(f"  Deleted rows {s}–{e} ({deleted}/{len(pre_cut)})")

    print(f"  Done. {len(pre_cut)} rows deleted.")


if __name__ == "__main__":
    main()
