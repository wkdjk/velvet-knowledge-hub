"""C-8 one-off: write Commander-translated English titles to KVN_Articles column L.

Usage:
  PYTHONPATH=. python scripts/c8_write_english_titles.py --dry-run
  PYTHONPATH=. python scripts/c8_write_english_titles.py
"""

import argparse
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.sheets_auth import connect_sheets, resolve_sheet_id

TRANS_FILE = Path("/Users/Qs/Downloads/c8_titles_for_translation.txt")
CHUNK_SIZE = 500


def has_korean(text: str) -> bool:
    return bool(re.search(r"[가-힣ㄱ-ㆎ]", text))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    lines = TRANS_FILE.read_text(encoding="utf-8-sig").strip().split("\n")

    updates: list[dict] = []
    skipped = 0
    for line in lines:
        line = line.rstrip("\r\n")
        m = re.match(r"(\d+)\s+(\d{4}-\d{2}-\d{2})\s+(.+)", line)
        if not m:
            continue
        try:
            row_num = int(m.group(1))
        except ValueError:
            continue
        title = m.group(3).strip()
        if not title or has_korean(title):
            skipped += 1
            continue
        updates.append({"range": f"L{row_num}", "values": [[title]]})

    print(f"  English titles to write : {len(updates)}")
    print(f"  Korean/empty rows skipped: {skipped}")

    if args.dry_run:
        print("  --dry-run: no changes made.")
        if updates:
            print(f"  Sample: row {updates[0]['range']} → {updates[0]['values'][0][0][:60]}")
        return

    spreadsheet = connect_sheets(resolve_sheet_id())
    ws = spreadsheet.worksheet("KVN_Articles")

    written = 0
    for i in range(0, len(updates), CHUNK_SIZE):
        chunk = updates[i : i + CHUNK_SIZE]
        ws.batch_update(chunk, value_input_option="RAW")
        written += len(chunk)
        print(f"  Written {written}/{len(updates)}")

    print("  Done.")


if __name__ == "__main__":
    main()
