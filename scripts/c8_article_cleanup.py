"""C-8 one-off: delete pre-Apr-2026 articles, then deduplicate remaining rows by URL and title.

Usage:
  PYTHONPATH=. python scripts/c8_article_cleanup.py --dry-run   # preview counts only
  PYTHONPATH=. python scripts/c8_article_cleanup.py             # delete old + dedup
"""

import argparse
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.sheets_auth import connect_sheets, resolve_sheet_id

CUT_DATE = "2026-04-01"

_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "ref", "referer",
}
_KO_STOP = {'은', '는', '이', '가', '을', '를', '의', '에', '로', '도', '와', '과', '및', '한', '그'}


def _norm_url(url: str) -> str:
    p = urlparse(url)
    qs = {k: v for k, v in parse_qs(p.query).items() if k not in _TRACKING_PARAMS}
    return urlunparse((
        p.scheme.lower(), p.netloc.lower().replace("www.", ""),
        p.path.rstrip("/"), "", urlencode({k: v[0] for k, v in qs.items()}), "",
    ))


def _tokens(title: str) -> frozenset:
    toks = re.findall(r'[가-힣a-zA-Z0-9]{2,}', title)
    return frozenset(t for t in toks if t not in _KO_STOP)


def _batch_delete(ws, row_indices: list[int]) -> int:
    """Delete rows bottom-to-top to avoid index shifting."""
    if not row_indices:
        return 0
    to_del = sorted(row_indices)
    # build contiguous ranges
    ranges, s, e = [], to_del[0], to_del[0]
    for r in to_del[1:]:
        if r == e + 1:
            e = r
        else:
            ranges.append((s, e)); s = e = r
    ranges.append((s, e))
    deleted = 0
    for start, end in reversed(ranges):
        ws.delete_rows(start, end)
        deleted += end - start + 1
    return deleted


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    spreadsheet = connect_sheets(resolve_sheet_id())
    ws = spreadsheet.worksheet("KVN_Articles")
    rows = ws.get_all_records()

    # ── Pass 1: tag pre-cut rows ─────────────────────────────────────────
    old_rows = []
    keep_rows = []  # list of (sheet_row_idx, row_dict)
    for i, row in enumerate(rows, start=2):
        d = (row.get("published_date") or "")[:10]
        if d and d < CUT_DATE:
            old_rows.append(i)
        else:
            keep_rows.append((i, row))

    print(f"Pre-{CUT_DATE} (old, delete): {len(old_rows)}")
    print(f"On/after {CUT_DATE} (keep pool): {len(keep_rows)}")

    # ── Pass 2: URL dedup ────────────────────────────────────────────────
    seen_urls: set[str] = set()
    url_dups: list[int] = []
    url_keep: list[tuple[int, dict]] = []
    for idx, row in keep_rows:
        norm = _norm_url(row.get("url") or "")
        if norm and norm in seen_urls:
            url_dups.append(idx)
        else:
            seen_urls.add(norm)
            url_keep.append((idx, row))

    print(f"URL duplicates (delete): {len(url_dups)}")

    # ── Pass 3: same-day title dedup (≥50% token overlap) ───────────────
    by_date: dict[str, list[tuple[int, frozenset]]] = {}
    for idx, row in url_keep:
        d = (row.get("published_date") or "")[:10]
        toks = _tokens(row.get("title_ko") or "")
        by_date.setdefault(d, []).append((idx, toks))

    title_dups: list[int] = []
    title_keep: list[int] = []
    for d, entries in by_date.items():
        kept: list[tuple[int, frozenset]] = []
        for idx, toks in entries:
            dup = False
            for _, prior_toks in kept:
                if toks and prior_toks:
                    overlap = len(toks & prior_toks) / min(len(toks), len(prior_toks))
                    if overlap >= 0.5:
                        dup = True; break
            if dup:
                title_dups.append(idx)
            else:
                kept.append((idx, toks))
                title_keep.append(idx)

    print(f"Same-day title duplicates (delete): {len(title_dups)}")
    total_del = len(old_rows) + len(url_dups) + len(title_dups)
    total_keep = len(title_keep)
    print(f"Total to delete: {total_del} → rows remaining: {total_keep}")

    if args.dry_run:
        print("--dry-run: no changes made.")
        return

    all_del = sorted(set(old_rows + url_dups + title_dups))
    n = _batch_delete(ws, all_del)
    print(f"Done. {n} rows deleted, {total_keep} rows kept.")


if __name__ == "__main__":
    main()
