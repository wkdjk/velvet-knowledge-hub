# Run as: PYTHONPATH=. python scripts/classify_articles.py [--dry-run] [--limit N] [--async]
#
# classify_articles.py — AI classification of KVN_Articles rows for Velvet Knowledge Hub.
#
# Reads KVN_Articles rows with an empty ai_processed_at column, calls Haiku 4.5
# to classify each article, then updates category, english_summary, ai_processed_at,
# and include_on_site in-place via batch_update.
#
# Usage:
#   PYTHONPATH=. python scripts/classify_articles.py
#   PYTHONPATH=. python scripts/classify_articles.py --dry-run
#   PYTHONPATH=. python scripts/classify_articles.py --limit 10
#   PYTHONPATH=. python scripts/classify_articles.py --async
#
# L-1: PYTHONPATH=. ensures repo root is importable.
# L-2: .env must be at repo root (/Users/Qs/C/velvet-knowledge-hub/.env).
# L-3: GOOGLE_SERVICE_ACCOUNT_JSON must be single-line JSON in .env.
# L-4: get_all_records() called once; batch_update called once per batch of rows.
# L-11: ANTHROPIC_API_KEY validated — must start with "sk-ant-api03-".
#
# AI model: claude-haiku-4-5-20251001
# Sync rate limiting: 0.5s sleep between API calls.
# Async mode: asyncio.Semaphore(20) caps concurrency at 20 simultaneous requests.
#
# In-place update: uses gspread worksheet.batch_update() with A1 cell ranges.
# Header is row 1 → first data row is row 2.
#
# Security: no credentials or secrets in this file. All secrets from .env only.

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

import anthropic
import gspread
from dotenv import load_dotenv
from gspread.utils import rowcol_to_a1

# ---------------------------------------------------------------------------
# L-1: ensure repo root is on sys.path.
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.sheets_auth import _load_config, connect_sheets, resolve_sheet_id  # noqa: E402
from scripts.schema import KVN_ARTICLES_HEADERS, verify_header  # noqa: E402

logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger("classify_articles")

# Force unbuffered stdout so progress prints appear in real time when piped.
sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TARGET_TAB = "KVN_Articles"

# Haiku model ID as specified in the brief.
_HAIKU_MODEL = "claude-haiku-4-5-20251001"

# KVN_Articles column indices (0-based) — must match schema.py KVN_ARTICLES_HEADERS,
# the single source of truth for this tab (A3 fix, 2026-07-03).
# Verified 2026-06-05 by reading ws.row_values(1) directly from the sheet;
# verify_header() re-checks this at every run startup and warns on drift.
#
# Header row (0-based):
#   article_id(0) | title(1) | url(2) | content_hash(3) | published_date(4) |
#   source(5) | category(6) | english_summary(7) | ai_processed_at(8) |
#   include_on_site(9) | crawled_at(10) | english_title(11) |
#   duplicate_of_article_id(12) | dedup_judged_at(13) | manual_override(14)
#
# Note: input columns (title_ko, description) are read by header NAME, not
# index, at each call site — see _classify_article_async() and the sync loop
# in main(), which both read row.get("url") / row.get("content_hash").
# A3 cleanup: removed unused _COL_TITLE_KO / _COL_DESCRIPTION index constants
# — neither was referenced anywhere else in this file.
# Columns written by the classifier (0-based, A=0):
_COL_CATEGORY = 6        # 'category' (G)
_COL_ENGLISH_SUMMARY = 7  # 'english_summary' (H)
_COL_AI_PROCESSED_AT = 8  # 'ai_processed_at' (I)
_COL_INCLUDE_ON_SITE = 9  # 'include_on_site' (J)
# crawled_at occupies col K (index 10) — english_title goes in col L (index 11).
# C-8 P0b: english_title is a new column appended to the right of the existing schema.
# Prerequisite: run setup_sheets_add_english_title.py (or manually add the header
# 'english_title' to cell L1 of KVN_Articles) before running the classifier.
_COL_ENGLISH_TITLE = 11   # 'english_title' (L) — added C-8 P0b

# C-13 Task 1 (2026-07-04): semantic dedup cache + manual-protection columns.
# Prerequisite: run scripts/add_dedup_columns_header.py once against the live
# sheet before running the semantic clustering pass (mirrors C-8 P0b's
# add_english_title_header.py migration pattern).
_COL_DUPLICATE_OF = 12         # 'duplicate_of_article_id' (M)
_COL_DEDUP_JUDGED_AT = 13      # 'dedup_judged_at' (N)
_COL_MANUAL_OVERRIDE = 14      # 'manual_override' (O) — written only by a human, never by this script.

# Valid category values — classifier must return one of these.
_VALID_CATEGORIES = frozenset([
    "규제정책", "무역시장", "건강제품", "수입유통", "업계소식", "기타"
])

# Near-duplicate clustering (Task 2, 2026-07-03; superseded by semantic
# matching Task 1, 2026-07-04): the same press release covered by multiple
# outlets gets a different URL and content_hash per collect_naver.py's
# exact-hash dedup — content_hash dedup cannot catch this by design (see
# classifier_guidance.md §7: cluster_id was intentionally dropped from KVN's
# design, but that assumed exact-hash dedup would catch same-story
# republication, which it does not for multi-outlet coverage).
#
# Task 1 root-cause fix (2026-07-04): pure difflib.SequenceMatcher ratio
# (Task 2) only caught near-verbatim headlines (ratio >= 0.72) — confirmed
# live it misses the same press release written up with structurally
# different headlines by different outlets (measured ratios 0.46-0.68 for
# the Joa Pharma / 몽진환마인 cluster). Haiku 4.5 (the model already wired
# into this file for classification) now makes the match decision;
# SequenceMatcher stays only as (a) a loose pre-filter to cap how many
# candidate headlines go into one LLM prompt, and (b) the fallback match
# rule if an individual LLM call errors — see run_semantic_clustering_pass().
_CLUSTER_DATE_WINDOW_DAYS = 3
_CLUSTER_TITLE_RATIO = 0.72          # fallback-only strict threshold (was the sole rule pre-Task-1).
_CLUSTER_LLM_LOOSE_RATIO = 0.3       # pre-filter floor — well below the 0.46 lowest confirmed real duplicate, so it only trims obviously-unrelated headlines, never the ones Task 1 exists to catch.
_CLUSTER_LLM_MAX_CANDIDATES = 20     # ponytail: caps one prompt's candidate list; revisit if a 3-day window regularly exceeds this (today's live max is 10).
_DEDUP_SENTINEL_NONE = "none"        # duplicate_of_article_id value meaning "judged, not a duplicate of anything".

# Rate limit pause between individual Claude API calls (brief specifies 0.5s).
_API_SLEEP_SECONDS = 0.5

# Async mode: max concurrent API requests.
# Set conservatively — the account limit is 50 RPM on Haiku.
# With Semaphore(3) and a token bucket at 40 tokens/min we stay well under quota.
_ASYNC_CONCURRENCY = 3

# Token bucket: max requests per minute for async mode.
# Set to 40 to leave 10 RPM headroom below the 50 RPM account limit.
_ASYNC_RPM_LIMIT = 40

# System prompt for Haiku classification.
# A2b fix (2026-07-03): enriched with explicit include/exclude criteria and
# worked examples from Domain_Knowledge/classifier_guidance.md §2 and §6.
# Previously a one-line relevance instruction let celebrity-reminiscence and
# clinic-treatment articles (녹용 mentioned only in passing) score as
# include_on_site=true. CaptainQ-approved per classifier_guidance.md §8.
_SYSTEM_PROMPT = (
    "You are a Korean deer velvet industry news classifier. "
    "Given a Korean news article title and description, return JSON with four fields:\n"
    "- category: one of [규제정책, 무역시장, 건강제품, 수입유통, 업계소식, 기타]\n"
    "- english_title: a short English headline for this article (12 words max, title case, no full stop)\n"
    "- english_summary: 1-2 sentence English summary of the article\n"
    "- include_on_site: true if this article is relevant to the NZ deer velvet "
    "trade/import/market in Korea; false if it is off-topic\n"
    "\n"
    "Relevance means Korean deer velvet (녹용) import, trade, price, market share, "
    "regulation, or MFDS action is the PRIMARY subject — not a passing mention.\n"
    "\n"
    "Set include_on_site=true for:\n"
    "- Deer velvet import/price/trade volume/market share/regulation/MFDS action as the central topic.\n"
    "- Foreign-origin velvet (Russia, China, Kazakhstan) — affects NZ's competitive position.\n"
    "- Animal-welfare/동물권 critique of deer velvet farming — regulatory and reputational risk.\n"
    "- Product launches, health research, or traditional medicine where deer velvet is the primary subject.\n"
    "\n"
    "Set include_on_site=false for:\n"
    "- 녹용 used only as a metaphor for energy/vitality (K-pop, celebrity reminiscence, political speech).\n"
    "- Clinic/hospital treatment articles where 녹용 is one ingredient mentioned in passing, not the subject.\n"
    "- 사슴뿔 (antlers) in décor, wildlife, museum, or café-interior contexts — 사슴뿔 is NOT 녹용, treat as a "
    "distinct exclusion signal even though the words look related.\n"
    "- 녹용 listed as one ingredient among many in a general 보양식 (health food) list, with no trade/import angle.\n"
    "- If the headline mentions 녹용 but the body reveals it is a gimmick (product name, event name, unrelated "
    "context) — score on the body's actual subject, not the headline.\n"
    "\n"
    "Return ONLY valid JSON. No explanation."
)


# ---------------------------------------------------------------------------
# Anthropic API helpers — sync
# ---------------------------------------------------------------------------

def validate_api_key(api_key: str) -> None:
    """
    L-11: Validate ANTHROPIC_API_KEY prefix.

    A valid key starts with 'sk-ant-api03-'. Exits with code 1 if invalid.
    """
    if not api_key.startswith("sk-ant-api03-"):
        print(
            "ERROR: ANTHROPIC_API_KEY does not start with 'sk-ant-api03-'.\n"
            "  This indicates a corrupted paste or wrong key.\n"
            "  Check: echo $ANTHROPIC_API_KEY | cut -c1-14  (must print 'sk-ant-api03-')\n"
            "  Reissue at: https://console.anthropic.com/settings/keys",
            file=sys.stderr,
        )
        sys.exit(1)


def _parse_classification_response(raw_text: str, title_ko: str) -> dict:
    """
    Parse and normalise a raw JSON string from the Haiku classifier.

    Shared between sync and async paths. Returns a classification dict with
    keys: category, english_summary, include_on_site, _error.
    """
    # Strip markdown code fences if present (```json ... ``` or ``` ... ```).
    if raw_text.startswith("```"):
        raw_text = raw_text.strip("`").strip()
        if raw_text.lower().startswith("json"):
            raw_text = raw_text[4:].strip()

    result = json.loads(raw_text)

    # Normalise category — ensure it is one of the valid values.
    category = str(result.get("category", "기타")).strip()
    if category not in _VALID_CATEGORIES:
        logger.warning(
            "Invalid category '%s' returned for title='%s' — defaulting to '기타'",
            category, title_ko[:60],
        )
        category = "기타"

    english_title = str(result.get("english_title", "")).strip()
    english_summary = str(result.get("english_summary", "")).strip()

    # include_on_site — accept bool or string.
    raw_include = result.get("include_on_site", False)
    if isinstance(raw_include, bool):
        include_on_site = raw_include
    else:
        include_on_site = str(raw_include).strip().lower() in ("true", "yes", "1")

    return {
        "category": category,
        "english_title": english_title,
        "english_summary": english_summary,
        "include_on_site": include_on_site,
        "_error": False,
    }


def classify_article(
    client: anthropic.Anthropic,
    title_ko: str,
    description: str,
) -> dict:
    """
    Call Haiku 4.5 to classify a single article (sync path).

    Returns a dict with keys: category, english_summary, include_on_site.
    On any failure (API error, JSON parse error), returns safe defaults and
    logs a warning. The caller must still write ai_processed_at so the row
    is not reprocessed.
    """
    user_msg = f"Title: {title_ko}\nDescription: {description}"

    try:
        message = client.messages.create(
            model=_HAIKU_MODEL,
            max_tokens=256,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw_text = message.content[0].text.strip()
        return _parse_classification_response(raw_text, title_ko)

    except json.JSONDecodeError as exc:
        logger.warning(
            "JSON parse error for title='%s': %s", title_ko[:60], exc
        )
        return {"category": "기타", "english_title": "", "english_summary": "", "include_on_site": False, "_error": True}
    except anthropic.APIError as exc:
        logger.warning(
            "Anthropic API error for title='%s': %s", title_ko[:60], exc
        )
        return {"category": "기타", "english_title": "", "english_summary": "", "include_on_site": False, "_error": True}
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Unexpected error for title='%s': %s", title_ko[:60], exc
        )
        return {"category": "기타", "english_title": "", "english_summary": "", "include_on_site": False, "_error": True}


# ---------------------------------------------------------------------------
# Anthropic API helpers — async
# ---------------------------------------------------------------------------

async def _classify_article_async(
    client: anthropic.AsyncAnthropic,
    semaphore: asyncio.Semaphore,
    sheet_row_num: int,
    row: dict,
    counter: list,
    counter_lock: asyncio.Lock,
    total: int,
) -> tuple[int, dict]:
    """
    Classify a single article asynchronously, bounded by semaphore.

    Returns (sheet_row_num, classification_result).
    Increments shared counter and prints progress every 100 rows.
    On any failure, returns safe defaults and sets ai_processed_at so the
    row is not reprocessed on the next run.
    """
    # C-5h fix: live KVN_Articles sheet has data written to wrong columns.
    # 'url' column holds the Korean article title; 'content_hash' holds description.
    title_ko = str(row.get("url") or row.get("title_ko", "")).strip()
    description = str(row.get("content_hash", "")).strip()
    user_msg = f"Title: {title_ko}\nDescription: {description}"

    async with semaphore:
        try:
            message = await client.messages.create(
                model=_HAIKU_MODEL,
                max_tokens=256,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_msg}],
            )
            raw_text = message.content[0].text.strip()
            result = _parse_classification_response(raw_text, title_ko)

        except json.JSONDecodeError as exc:
            logger.warning("JSON parse error for title='%s': %s", title_ko[:60], exc)
            result = {"category": "기타", "english_summary": "", "include_on_site": False, "_error": True}
        except anthropic.APIError as exc:
            logger.warning("Anthropic API error for title='%s': %s", title_ko[:60], exc)
            result = {"category": "기타", "english_summary": "", "include_on_site": False, "_error": True}
        except Exception as exc:  # noqa: BLE001
            logger.warning("Unexpected error for title='%s': %s", title_ko[:60], exc)
            result = {"category": "기타", "english_summary": "", "include_on_site": False, "_error": True}

    # Update shared progress counter (thread-safe via asyncio.Lock).
    async with counter_lock:
        counter[0] += 1
        done = counter[0]
        if done % 100 == 0 or done == total:
            print(f"  classified {done}/{total} rows...")

    return sheet_row_num, result


class _TokenBucket:
    """
    Simple async token bucket for rate limiting.

    Replenishes at rate tokens/second. Each acquire() waits until a token
    is available. Thread-safe via asyncio.Lock.
    """

    def __init__(self, rate_per_minute: int) -> None:
        self._rate = rate_per_minute / 60.0  # tokens per second
        self._tokens = float(rate_per_minute)
        self._max_tokens = float(rate_per_minute)
        self._last_refill = asyncio.get_event_loop().time()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Block until a token is available, then consume one token."""
        while True:
            async with self._lock:
                now = asyncio.get_event_loop().time()
                elapsed = now - self._last_refill
                self._tokens = min(
                    self._max_tokens,
                    self._tokens + elapsed * self._rate,
                )
                self._last_refill = now

                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return  # token acquired

            # No token available — wait a small amount then retry.
            wait_seconds = (1.0 - self._tokens) / self._rate
            await asyncio.sleep(max(0.05, wait_seconds))


async def _classify_all_async(
    api_key: str,
    unprocessed: list[tuple[int, dict]],
) -> list[tuple[int, dict]]:
    """
    Classify all unprocessed rows concurrently using AsyncAnthropic.

    Rate-limiting strategy:
    - asyncio.Semaphore(_ASYNC_CONCURRENCY) caps simultaneous open connections.
    - _TokenBucket(_ASYNC_RPM_LIMIT) enforces requests-per-minute to stay
      within the account's 50 RPM Haiku limit.

    Results are collected in original order via asyncio.gather (index-preserving).
    Returns list of (sheet_row_num, classification_result) in input order.
    """
    client = anthropic.AsyncAnthropic(api_key=api_key)
    semaphore = asyncio.Semaphore(_ASYNC_CONCURRENCY)
    bucket = _TokenBucket(_ASYNC_RPM_LIMIT)
    counter = [0]  # mutable container for shared counter
    counter_lock = asyncio.Lock()
    total = len(unprocessed)

    async def _rate_limited_task(sheet_row_num: int, row: dict) -> tuple[int, dict]:
        # Acquire rate-limit token before acquiring semaphore (avoids holding
        # the semaphore slot while waiting for a rate-limit token).
        await bucket.acquire()
        return await _classify_article_async(
            client=client,
            semaphore=semaphore,
            sheet_row_num=sheet_row_num,
            row=row,
            counter=counter,
            counter_lock=counter_lock,
            total=total,
        )

    tasks = [
        _rate_limited_task(sheet_row_num, row)
        for sheet_row_num, row in unprocessed
    ]

    # gather preserves order — index i in results corresponds to tasks[i].
    results = await asyncio.gather(*tasks)
    await client.close()
    return list(results)


# ---------------------------------------------------------------------------
# Near-duplicate clustering — semantic (Task 1, 2026-07-04; supersedes the
# difflib-only Task 2, 2026-07-03 implementation)
# ---------------------------------------------------------------------------

_DEDUP_MATCH_SYSTEM_PROMPT = (
    "You compare ONE Korean news headline against a numbered list of other "
    "Korean headlines published within a few days of it, to decide whether "
    "any of them cover the SAME underlying news event (e.g. the same press "
    "release, product launch, or regulatory action reported by a different "
    "outlet with different wording). Headlines about a similar topic but a "
    "different event are NOT a match.\n"
    "\n"
    "Input: the new headline, then a numbered list of candidate headlines.\n"
    "Output: ONLY valid JSON — {\"match\": N} where N is the candidate number "
    "(an integer, 1-based) that describes the same event, or "
    "{\"match\": null} if none of them do. No explanation."
)


def _parse_iso_date(raw: str):
    """Parse a YYYY-MM-DD(-prefixed) string to a date object, or None."""
    from datetime import date as _date  # ponytail: local import, single call site
    try:
        return _date.fromisoformat(raw[:10])
    except ValueError:
        return None


def _within_cluster_window(date_a, date_b) -> bool:
    """True if two dates are within _CLUSTER_DATE_WINDOW_DAYS of each other, either direction."""
    return abs((date_a - date_b).days) <= _CLUSTER_DATE_WINDOW_DAYS


def _match_duplicate_with_llm(
    client: anthropic.Anthropic,
    new_title: str,
    candidate_titles: list[str],
) -> int | None:
    """
    Ask Haiku whether `new_title` describes the same news event as any of
    `candidate_titles`. Returns the 0-based index of the matching candidate,
    or None if no match. Raises on API/parse error — caller decides the
    fallback (see run_semantic_clustering_pass).
    """
    numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(candidate_titles))
    user_msg = f"New headline: {new_title}\n\nCandidates:\n{numbered}"

    message = client.messages.create(
        model=_HAIKU_MODEL,
        max_tokens=100,
        system=_DEDUP_MATCH_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    raw_text = message.content[0].text.strip()

    # Haiku sometimes wraps the JSON in a fence and/or adds explanatory
    # prose after it despite "No explanation" — confirmed live (2026-07-04):
    # ```json\n{"match": null}\n```\n\nThe new headline discusses... Simple
    # prefix/suffix stripping isn't reliable once there's trailing prose, so
    # pull out the first flat {...} object directly instead of trusting the
    # response to be ONLY that object.
    json_match = re.search(r"\{[^{}]*\}", raw_text)
    if not json_match:
        raise json.JSONDecodeError("no JSON object found in LLM response", raw_text, 0)
    result = json.loads(json_match.group(0))
    match = result.get("match")
    if match is None:
        return None
    idx = int(match) - 1
    if idx < 0 or idx >= len(candidate_titles):
        logger.warning("LLM returned out-of-range match index %r for %d candidates — treating as no match.",
                        match, len(candidate_titles))
        return None
    return idx


def _fallback_ratio_match(new_title: str, candidate_titles: list[str]) -> int | None:
    """
    difflib fallback used only when an individual _match_duplicate_with_llm
    call errors (fail loud via logger.warning, don't crash the run — same
    posture as the rest of this file). Reapplies Task 2's original strict
    threshold (_CLUSTER_TITLE_RATIO) so a transient API failure degrades to
    "at least catch verbatim duplicates", not "catch nothing this run".
    """
    for i, candidate in enumerate(candidate_titles):
        if SequenceMatcher(None, new_title, candidate).ratio() >= _CLUSTER_TITLE_RATIO:
            return i
    return None


def run_semantic_clustering_pass(
    ws,
    client: anthropic.Anthropic,
    dry_run: bool,
    force: bool = False,
) -> dict:
    """
    Incrementally judge every unjudged (dedup_judged_at empty, or all rows if
    force) include_on_site=TRUE row against already-settled rows in its
    _CLUSTER_DATE_WINDOW_DAYS window, using Haiku to decide same-story
    matches (see module header for the difflib-only Task 2 ceiling this
    replaces).

    Incremental strategy (Task 1, chosen over full-recompute — see
    Domain_Knowledge/C13_news_pulse_dedup_pagination_fix_2026-07-04.md Part
    B §5): only rows with no cached verdict trigger an LLM call. A row
    already judged 'not a duplicate' (duplicate_of_article_id == "none")
    joins the "settled" candidate pool other rows get compared against;
    a row judged a duplicate is suppressed and never becomes a candidate
    itself.

    Task 1a same-batch handling: rows are processed in ascending
    published_date order within this single pass, and each row judged
    'not a duplicate' is appended to the settled pool immediately — so a
    row classified earlier in the SAME run can become the canonical match
    for a row processed later in the same run, before either one has a
    dedup_judged_at value written to the sheet. This is what catches
    same-day multi-outlet coverage collected in one scrape.

    manual_override protection: if a pending row has manual_override=TRUE,
    a duplicate verdict is still cached (duplicate_of_article_id gets set —
    a human can see the LLM's opinion) but never applied — include_on_site
    is left TRUE. A protected row does not join the settled pool either
    (its duplicate_of_article_id is not "none"), so it is never offered as
    a match target for later rows in the same pass — only a genuinely
    confirmed-canonical row is. manual_override is never written by this
    function, only read.

    Never touches category, english_summary, ai_processed_at, or
    english_title. Never deletes rows. Returns a stats dict:
    {"suppressed": int, "judged": int, "llm_calls": int, "llm_errors": int}
    (all zero if there was nothing to judge).
    """
    all_rows = ws.get_all_records()
    included: list[tuple[int, dict]] = [
        (idx + 2, row)
        for idx, row in enumerate(all_rows)
        if str(row.get("include_on_site", "")).strip().upper() == "TRUE"
    ]

    parsed = []
    for row_num, row in included:
        date = _parse_iso_date(str(row.get("published_date", "")))
        title = str(row.get("url", "")).strip()
        if date is None or not title:
            continue  # can't window- or title-compare an unparseable row — leave untouched
        parsed.append({"row_num": row_num, "row": row, "date": date, "title": title})
    parsed.sort(key=lambda item: item["date"])  # deterministic order — required for Task 1a

    settled = [
        item for item in parsed
        if not force
        and str(item["row"].get("dedup_judged_at", "")).strip()
        and str(item["row"].get("duplicate_of_article_id", "")).strip() == _DEDUP_SENTINEL_NONE
    ]
    pending = [
        item for item in parsed
        if force or not str(item["row"].get("dedup_judged_at", "")).strip()
    ]

    if not pending:
        print("  semantic clustering: no unjudged rows — nothing to do")
        return {"suppressed": 0, "judged": 0, "llm_calls": 0, "llm_errors": 0}

    print(f"  semantic clustering: {len(pending)} unjudged row(s) to check "
          f"against {len(settled)} already-settled row(s){' (--force-cluster)' if force else ''}")

    now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cell_updates: list[dict] = []
    suppressed = 0
    llm_calls = 0
    llm_errors = 0

    for item in pending:
        window_candidates = [
            s for s in settled
            if s["row_num"] != item["row_num"] and _within_cluster_window(item["date"], s["date"])
        ]

        duplicate_of = None
        if window_candidates:
            # Cheap pre-filter (rung 2 of the ladder — SequenceMatcher is
            # already imported and used elsewhere in this file): bounds the
            # LLM prompt size, does NOT make the match decision — the loose
            # ratio is well below the confirmed real-duplicate floor (0.46).
            loose = [
                c for c in window_candidates
                if SequenceMatcher(None, item["title"], c["title"]).ratio() >= _CLUSTER_LLM_LOOSE_RATIO
            ]
            candidates = (loose or window_candidates)[:_CLUSTER_LLM_MAX_CANDIDATES]

            llm_calls += 1
            try:
                match_idx = _match_duplicate_with_llm(client, item["title"], [c["title"] for c in candidates])
            except Exception as exc:  # noqa: BLE001 — fail loud, don't crash a scheduled run
                llm_errors += 1
                logger.warning(
                    "Semantic dedup LLM call failed for '%s': %s — falling back to strict "
                    "difflib ratio (%.2f) for this row.",
                    item["title"][:60], exc, _CLUSTER_TITLE_RATIO,
                )
                match_idx = _fallback_ratio_match(item["title"], [c["title"] for c in candidates])
            duplicate_of = candidates[match_idx] if match_idx is not None else None

            time.sleep(_API_SLEEP_SECONDS)  # reuse the existing inter-call rate-limit pause

        is_protected = str(item["row"].get("manual_override", "")).strip().upper() == "TRUE"

        if duplicate_of is not None and not is_protected:
            canonical_article_id = str(duplicate_of["row"].get("article_id", ""))
            cell_updates.append({"range": rowcol_to_a1(item["row_num"], _COL_INCLUDE_ON_SITE + 1), "values": [["FALSE"]]})
            cell_updates.append({"range": rowcol_to_a1(item["row_num"], _COL_DUPLICATE_OF + 1), "values": [[canonical_article_id]]})
            cell_updates.append({"range": rowcol_to_a1(item["row_num"], _COL_DEDUP_JUDGED_AT + 1), "values": [[now_ts]]})
            suppressed += 1
            # A suppressed duplicate never joins `settled` — it must not
            # become someone else's "canonical" reference.
        elif duplicate_of is not None and is_protected:
            # manual_override=TRUE: the LLM's verdict is still cached (per
            # Part B §6 of the design doc — a human reviewing the sheet can
            # see the LLM's opinion) but never applied — include_on_site
            # stays TRUE. Does not join `settled` (duplicate_of_article_id
            # != "none"): this row is protected, not confirmed-canonical,
            # so it isn't offered as a match target for later rows either.
            canonical_article_id = str(duplicate_of["row"].get("article_id", ""))
            cell_updates.append({"range": rowcol_to_a1(item["row_num"], _COL_DUPLICATE_OF + 1), "values": [[canonical_article_id]]})
            cell_updates.append({"range": rowcol_to_a1(item["row_num"], _COL_DEDUP_JUDGED_AT + 1), "values": [[now_ts]]})
            print(f"  semantic clustering: row {item['row_num']} judged a duplicate of "
                  f"article_id={canonical_article_id} but manual_override=TRUE — not suppressed")
        else:
            cell_updates.append({"range": rowcol_to_a1(item["row_num"], _COL_DUPLICATE_OF + 1), "values": [[_DEDUP_SENTINEL_NONE]]})
            cell_updates.append({"range": rowcol_to_a1(item["row_num"], _COL_DEDUP_JUDGED_AT + 1), "values": [[now_ts]]})
            settled.append(item)  # Task 1a: available to later pending rows in this same pass.

    print(f"  semantic clustering: {llm_calls} LLM call(s) ({llm_errors} fell back to difflib), "
          f"{suppressed} row(s) suppressed as duplicates")

    if dry_run:
        print(f"  [dry-run] would write {len(cell_updates)} cell update(s) — no Sheets write")
        return {"suppressed": suppressed, "judged": len(pending), "llm_calls": llm_calls, "llm_errors": llm_errors}

    chunk_size = 200 * 3  # 3 cells per judged row (mirrors _write_results_to_sheets' chunking approach)
    for i in range(0, len(cell_updates), chunk_size):
        chunk = cell_updates[i: i + chunk_size]
        ws.batch_update(chunk, value_input_option="USER_ENTERED")
        if i + chunk_size < len(cell_updates):
            time.sleep(1.1)

    return {"suppressed": suppressed, "judged": len(pending), "llm_calls": llm_calls, "llm_errors": llm_errors}


def run_canonical_succession(ws, dry_run: bool) -> int:
    """
    Task 1b (2026-07-04): if a human manually flips a cluster's canonical
    row (duplicate_of_article_id == "none") to include_on_site=FALSE
    directly in the Sheet — bypassing this script entirely — promote that
    cluster's earliest-published still-suppressed mate back to
    include_on_site=TRUE, so a real story doesn't silently vanish from the
    site because of one manual edit elsewhere in the cluster (Commander
    directive: succession over cluster death — see design rationale in
    Domain_Knowledge/C13_news_pulse_dedup_pagination_fix_2026-07-04.md Part C).

    Only acts on rows this system's dedup columns actually describe (a
    non-empty duplicate_of_article_id pointing FROM a suppressed mate TO a
    canonical row). Never touches rows with no cluster relationship, and
    never revives a row a human suppressed on its own merits — only a
    canonical whose suppression orphaned at least one still-suppressed mate.

    Does not consult manual_override — that column protects a row from
    this script's OWN suppression decisions; it is unrelated to a human
    switching a canonical off by hand.

    Returns the number of rows promoted (0 on dry_run — reports only).
    """
    all_rows = ws.get_all_records()
    by_article_id = {
        str(row.get("article_id", "")): (idx + 2, row)
        for idx, row in enumerate(all_rows)
    }

    mates_by_canonical: dict[str, list[tuple[int, dict]]] = {}
    for idx, row in enumerate(all_rows):
        dup_of = str(row.get("duplicate_of_article_id", "")).strip()
        if dup_of and dup_of != _DEDUP_SENTINEL_NONE:
            mates_by_canonical.setdefault(dup_of, []).append((idx + 2, row))

    cell_updates: list[dict] = []
    promotions = 0
    now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for canonical_id, mates in mates_by_canonical.items():
        canonical_entry = by_article_id.get(canonical_id)
        if canonical_entry is None:
            continue  # dangling pointer — canonical row missing; leave alone
        canonical_row_num, canonical_row = canonical_entry
        if str(canonical_row.get("include_on_site", "")).strip().upper() != "FALSE":
            continue  # canonical still visible — nothing to do

        suppressed_mates = [
            (row_num, row) for row_num, row in mates
            if str(row.get("include_on_site", "")).strip().upper() == "FALSE"
        ]
        if not suppressed_mates:
            continue  # succession already happened, or no mates left to promote

        suppressed_mates.sort(key=lambda item: str(item[1].get("published_date", "")))
        promote_row_num, promote_row = suppressed_mates[0]
        new_canonical_id = str(promote_row.get("article_id", ""))

        print(f"  succession: canonical article_id={canonical_id} (row {canonical_row_num}) "
              f"was manually suppressed — promoting article_id={new_canonical_id} (row {promote_row_num})")
        promotions += 1

        if not dry_run:
            cell_updates.append({"range": rowcol_to_a1(promote_row_num, _COL_INCLUDE_ON_SITE + 1), "values": [["TRUE"]]})
            cell_updates.append({"range": rowcol_to_a1(promote_row_num, _COL_DUPLICATE_OF + 1), "values": [[_DEDUP_SENTINEL_NONE]]})
            cell_updates.append({"range": rowcol_to_a1(promote_row_num, _COL_DEDUP_JUDGED_AT + 1), "values": [[now_ts]]})
            # Repoint any remaining suppressed mates to the newly-promoted canonical.
            for row_num, _row in suppressed_mates[1:]:
                cell_updates.append({"range": rowcol_to_a1(row_num, _COL_DUPLICATE_OF + 1), "values": [[new_canonical_id]]})

    if cell_updates:
        ws.batch_update(cell_updates, value_input_option="USER_ENTERED")

    if promotions:
        print(f"  succession: promoted {promotions} row(s)" + (" [dry-run]" if dry_run else ""))
    else:
        print("  succession: no manually-suppressed canonicals found")

    return promotions


# ---------------------------------------------------------------------------
# Shared write-back helper
# ---------------------------------------------------------------------------

def _write_results_to_sheets(
    ws,
    results: list[tuple[int, dict]],
    processed_at_ts: str,
) -> int:
    """
    Write classification results back to Google Sheets via chunked batch_update.

    Returns the number of rows written.
    Splits into chunks of 200 rows (800 cell dicts each) with 1.1s inter-chunk
    sleep to stay within Sheets write quota (300 write requests/min per project).
    """
    _WRITE_CHUNK_ROWS = 200
    cells_per_row = 5  # category + english_title + english_summary + ai_processed_at + include_on_site

    cell_updates: list[dict] = []

    for sheet_row_num, cls in results:
        include_val = "TRUE" if cls["include_on_site"] else "FALSE"

        cell_updates.append({
            "range": rowcol_to_a1(sheet_row_num, _COL_CATEGORY + 1),
            "values": [[cls["category"]]],
        })
        cell_updates.append({
            "range": rowcol_to_a1(sheet_row_num, _COL_ENGLISH_SUMMARY + 1),
            "values": [[cls["english_summary"]]],
        })
        cell_updates.append({
            "range": rowcol_to_a1(sheet_row_num, _COL_AI_PROCESSED_AT + 1),
            "values": [[processed_at_ts]],
        })
        cell_updates.append({
            "range": rowcol_to_a1(sheet_row_num, _COL_INCLUDE_ON_SITE + 1),
            "values": [[include_val]],
        })
        cell_updates.append({
            "range": rowcol_to_a1(sheet_row_num, _COL_ENGLISH_TITLE + 1),
            "values": [[cls.get("english_title", "")]],
        })

    chunk_size = _WRITE_CHUNK_ROWS * cells_per_row
    total_chunks = (len(cell_updates) + chunk_size - 1) // chunk_size

    for chunk_idx in range(total_chunks):
        chunk = cell_updates[chunk_idx * chunk_size: (chunk_idx + 1) * chunk_size]
        ws.batch_update(chunk, value_input_option="USER_ENTERED")
        print(f"  wrote chunk {chunk_idx + 1}/{total_chunks} ({len(chunk) // cells_per_row} rows)...")
        if chunk_idx < total_chunks - 1:
            time.sleep(1.1)

    return len(results)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Classify unprocessed KVN_Articles rows using Haiku 4.5 and "
            "write results back to Google Sheets in-place."
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Classify but do not write back to Sheets; print first 5 results.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Only process the first N unprocessed rows (for testing).",
    )
    parser.add_argument(
        "--async",
        dest="async_mode",
        action="store_true",
        help=(
            "Use AsyncAnthropic with Semaphore(3) + token bucket (40 RPM) "
            "for rate-limited concurrent classification. "
            "Sync path (default) uses 0.5s sleep between calls."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Re-classify ALL rows, including those already processed. "
            "Use after a failed run where rows were written with error defaults."
        ),
    )
    parser.add_argument(
        "--force-cluster",
        dest="force_cluster",
        action="store_true",
        help=(
            "Ignore the dedup_judged_at cache and re-run semantic clustering "
            "on every include_on_site=TRUE row, not just unjudged ones. "
            "Mirrors --force but scoped to Task 1 dedup, not classification."
        ),
    )
    args = parser.parse_args()

    print("classify_articles.py — VKH article AI classification")
    print(f"  model: {_HAIKU_MODEL}")
    print(f"  mode: {'async (Semaphore 3, 40 RPM bucket)' if args.async_mode else 'sync (0.5s sleep)'}")
    print(f"  dry-run: {args.dry_run}")
    print(f"  force: {args.force}")
    print(f"  force-cluster: {args.force_cluster}")
    if args.limit:
        print(f"  limit: {args.limit}")

    # L-2: load .env from repo root.
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")

    # L-11: validate ANTHROPIC_API_KEY prefix before any API call.
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not anthropic_key:
        print(
            "ERROR: ANTHROPIC_API_KEY is not set.\n"
            "  Local dev: add to .env at the repo root.\n"
            "  GitHub Actions: add to repository Secrets.",
            file=sys.stderr,
        )
        sys.exit(1)
    validate_api_key(anthropic_key)

    # --- Connect to Sheets ----------------------------------------------------
    sheet_id = resolve_sheet_id()
    print(f"  sheet_id: {sheet_id}")

    spreadsheet = connect_sheets(sheet_id)
    print(f"  sheet title: {spreadsheet.title}")

    # Locate KVN_Articles tab (L-12: graceful skip if missing).
    try:
        ws = spreadsheet.worksheet(TARGET_TAB)
    except gspread.exceptions.WorksheetNotFound:
        print(
            f"ERROR: tab '{TARGET_TAB}' not found in sheet {sheet_id}.\n"
            "  Run scripts/setup_sheets.py first, then collect_naver.py to populate it.",
            file=sys.stderr,
        )
        sys.exit(1)

    # A3 fix: fail loudly (not silently) if the live header has drifted
    # from schema.py's KVN_ARTICLES_HEADERS.
    verify_header(ws)

    # --- Read all rows once (L-4) ---------------------------------------------
    # get_all_records() returns list of dicts keyed by header row.
    all_rows = ws.get_all_records()
    total_rows = len(all_rows)
    print(f"  total KVN_Articles rows: {total_rows}")

    if total_rows == 0:
        print("  Tab is empty — nothing to classify.")
        print("articles_processed: 0 | written_to_sheets: 0 | errors: 0")
        sys.exit(0)

    # --- Filter to unprocessed rows (ai_processed_at is empty) ----------------
    # Row number in Sheets: header = row 1, first data row = row 2.
    # Index in all_rows list: 0-based. Sheet row = list_index + 2.
    #
    # C-5g fix: live sheet column is 'ai_processed_at' (col I). Due to the
    # prior column mismatch, this column currently holds category values
    # (e.g. "기타") for all rows. After the C-5g re-classification run,
    # it will hold ISO timestamps. A row is treated as unprocessed when
    # ai_processed_at is empty OR is a category value (not a timestamp).
    # Detection: a valid timestamp starts with a 4-digit year, e.g. "2026-".
    def _is_unprocessed(row: dict) -> bool:
        val = str(row.get("ai_processed_at", "")).strip()
        if not val:
            return True
        # If it looks like a timestamp (YYYY-...) it was written by this script.
        import re as _re
        return not bool(_re.match(r"^\d{4}-", val))

    unprocessed: list[tuple[int, dict]] = []
    for idx, row in enumerate(all_rows):
        if args.force or _is_unprocessed(row):
            sheet_row_number = idx + 2  # header is row 1
            unprocessed.append((sheet_row_number, row))

    label = "all rows (--force)" if args.force else "unprocessed rows (empty ai_processed_at)"
    print(f"  {label}: {len(unprocessed)}")

    # C-13 Task 1 (2026-07-04): one sync client for semantic clustering,
    # shared by every call site below regardless of --async classification
    # mode — clustering call volume is small (incremental), sync is enough.
    dedup_client = anthropic.Anthropic(api_key=anthropic_key)

    if not unprocessed:
        print("  All rows already classified — nothing to do.")
        # Still run clustering — duplicate coverage can arrive in a separate
        # collect_naver.py run after the story was already classified.
        cluster_stats = run_semantic_clustering_pass(ws, dedup_client, dry_run=args.dry_run, force=args.force_cluster)
        promotions = run_canonical_succession(ws, dry_run=args.dry_run)
        print(f"articles_processed: 0 | written_to_sheets: 0 | "
              f"duplicates_suppressed: {cluster_stats['suppressed']} | "
              f"canonical_promotions: {promotions} | errors: 0")
        sys.exit(0)

    # Apply --limit if set.
    if args.limit is not None:
        unprocessed = unprocessed[: args.limit]
        print(f"  applying --limit: processing {len(unprocessed)} rows")

    processed_at_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # --- ASYNC PATH -----------------------------------------------------------
    if args.async_mode:
        print(f"  launching async classification ({_ASYNC_CONCURRENCY} concurrent)...")
        t_start = time.monotonic()

        results: list[tuple[int, dict]] = asyncio.run(
            _classify_all_async(anthropic_key, unprocessed)
        )

        elapsed = time.monotonic() - t_start
        errors = sum(1 for _, cls in results if cls.get("_error"))
        articles_processed = len(results)

        print(f"  async classification done in {elapsed:.1f}s — {articles_processed} rows, {errors} errors")

        if args.dry_run:
            print()
            print("[DRY RUN] Classification complete — no Sheets write.")
            print("  First 5 results:")
            for sheet_row_num, cls in results[:5]:
                print(
                    f"    row {sheet_row_num}: category={cls['category']} | "
                    f"include_on_site={cls['include_on_site']} | "
                    f"summary={cls['english_summary'][:80]!r}"
                )
            print(
                f"articles_processed: {articles_processed} | "
                f"written_to_sheets: 0 | "
                f"errors: {errors}"
            )
            sys.exit(0)

        written_to_sheets = _write_results_to_sheets(ws, results, processed_at_ts)

        # Cluster near-duplicate stories across outlets after fresh
        # classifications land, so newly written include_on_site rows are
        # included in the clustering pass (Task 1a: same-run new rows can
        # match each other, see run_semantic_clustering_pass docstring).
        cluster_stats = run_semantic_clustering_pass(ws, dedup_client, dry_run=False, force=args.force_cluster)
        promotions = run_canonical_succession(ws, dry_run=False)

        print()
        print(f"  DONE: {written_to_sheets} rows updated in '{TARGET_TAB}'.")
        print(
            f"articles_processed: {articles_processed} | "
            f"written_to_sheets: {written_to_sheets} | "
            f"duplicates_suppressed: {cluster_stats['suppressed']} | "
            f"canonical_promotions: {promotions} | "
            f"errors: {errors}"
        )
        return

    # --- SYNC PATH (default) --------------------------------------------------
    # Unchanged from original implementation. --dry-run and --limit work here too.

    ai_client = anthropic.Anthropic(api_key=anthropic_key)
    sync_results: list[tuple[int, dict]] = []
    errors = 0

    for i, (sheet_row_num, row) in enumerate(unprocessed):
        # C-5h fix: live KVN_Articles sheet has data written to wrong columns by
        # the original collector. The header names do not match the stored values:
        #   'title' column (col B) → holds the article URL (http://...)
        #   'url' column (col C)   → holds the Korean article title text
        #   'content_hash' (col D) → holds the article description/content snippet
        # Verified 2026-06-05 by direct row inspection (all 5,586 rows confirmed).
        # Read 'url' for the Korean title and 'content_hash' for description.
        title_ko = str(row.get("url") or row.get("title_ko", "")).strip()
        description = str(row.get("content_hash", "")).strip()

        classification = classify_article(ai_client, title_ko, description)

        if classification.get("_error"):
            errors += 1

        sync_results.append((sheet_row_num, classification))

        # Print progress every 10 rows.
        if (i + 1) % 10 == 0 or (i + 1) == len(unprocessed):
            print(f"  classified {i + 1}/{len(unprocessed)} rows...")

        # Rate limiting: sleep between calls (not after the last one).
        if i < len(unprocessed) - 1:
            time.sleep(_API_SLEEP_SECONDS)

    articles_processed = len(sync_results)

    if args.dry_run:
        print()
        print("[DRY RUN] Classification complete — no Sheets write.")
        print("  First 5 results:")
        for sheet_row_num, cls in sync_results[:5]:
            print(
                f"    row {sheet_row_num}: category={cls['category']} | "
                f"include_on_site={cls['include_on_site']} | "
                f"summary={cls['english_summary'][:80]!r}"
            )
        print(
            f"articles_processed: {articles_processed} | "
            f"written_to_sheets: 0 | "
            f"errors: {errors}"
        )
        sys.exit(0)

    written_to_sheets = _write_results_to_sheets(ws, sync_results, processed_at_ts)

    # Cluster near-duplicate stories across outlets (Task 1a: same-run new
    # rows can match each other, see run_semantic_clustering_pass docstring).
    cluster_stats = run_semantic_clustering_pass(ws, dedup_client, dry_run=False, force=args.force_cluster)
    promotions = run_canonical_succession(ws, dry_run=False)

    print()
    print(f"  DONE: {written_to_sheets} rows updated in '{TARGET_TAB}'.")
    print(
        f"articles_processed: {articles_processed} | "
        f"written_to_sheets: {written_to_sheets} | "
        f"duplicates_suppressed: {cluster_stats['suppressed']} | "
        f"canonical_promotions: {promotions} | "
        f"errors: {errors}"
    )


if __name__ == "__main__":
    main()
