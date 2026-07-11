# Run as: PYTHONPATH=. python scripts/classify_articles.py [--dry-run] [--limit N] [--async]
#
# classify_articles.py — AI relevance + classification of raw_news_articles
# rows for Velvet Knowledge Hub (D3, Phase D rebuild, sqlite storage —
# 2026-07-11; ported from the pre-rebuild Sheets version — see §4/§6b of
# Domain_Knowledge_staging/VKH_D3_news_scaffolding_proposal_2026-07-11.md).
#
# Reads raw_news_articles rows with no matching news_articles row (the
# "pending judgement" queue — same query shape as Library's pending-curation
# queue), calls Haiku 4.5 once per article, then writes relevant +
# classification in ONE INSERT statement (§4 atomicity rule — never
# INSERT-then-UPDATE). conn.commit() only at batch boundaries.
#
# No more column-swap workaround: raw_news_articles.title_ko/description are
# named for what they hold — the old KVN_Articles Sheet's C-5h swap only
# existed because the pre-rebuild collector wrote columns in the wrong order.
#
# Usage:
#   PYTHONPATH=. python scripts/classify_articles.py
#   PYTHONPATH=. python scripts/classify_articles.py --dry-run
#   PYTHONPATH=. python scripts/classify_articles.py --limit 10
#   PYTHONPATH=. python scripts/classify_articles.py --async
#
# L-1: PYTHONPATH=. ensures repo root is importable.
# L-2: .env must be at repo root.
# L-3: GOOGLE_SERVICE_ACCOUNT_JSON must be single-line JSON in .env (unused
#      by this script now — no Sheets connection needed for classification).
# L-11: ANTHROPIC_API_KEY validated — must start with "sk-ant-api03-".
#
# AI model: claude-haiku-4-5-20251001
# Sync rate limiting: 0.5s sleep between API calls.
# Async mode: asyncio.Semaphore(3) + a 40 RPM token bucket.
#
# Security: no credentials or secrets in this file. All secrets from .env only.

import argparse
import asyncio
import json
import logging
import re
import sqlite3
import sys
import time
from datetime import date as _date, datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

import anthropic

# ---------------------------------------------------------------------------
# L-1: ensure repo root is on sys.path.
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import vkh_sqlite  # noqa: E402
from scripts.news_schema import NEWS_DDL  # noqa: E402
from scripts.sheets_auth import _load_config  # noqa: E402

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

_HAIKU_MODEL = "claude-haiku-4-5-20251001"

_VALID_CATEGORIES = frozenset([
    "규제정책", "무역시장", "건강제품", "수입유통", "업계소식", "기타"
])

# Near-duplicate clustering — semantic (Haiku pairwise match), ported as-is
# from the pre-rebuild version. See news_schema.py's schema note: the old
# string-sentinel duplicate_of_article_id ("" / "none" / article_id) is
# replaced by NULL-based states (dedup_judged_at IS NULL = never judged;
# dedup_judged_at IS NOT NULL AND duplicate_of_raw_ref IS NULL = judged,
# canonical) — no sentinel string anywhere in this file any more.
_CLUSTER_DATE_WINDOW_DAYS = 3
_CLUSTER_TITLE_RATIO = 0.72          # fallback-only strict threshold.
_CLUSTER_LLM_LOOSE_RATIO = 0.3       # pre-filter floor.
_CLUSTER_LLM_MAX_CANDIDATES = 20     # ponytail: caps one prompt's candidate list; revisit if a 3-day window regularly exceeds this.

# Rate limit pause between individual Claude API calls.
_API_SLEEP_SECONDS = 0.5

# Async mode: max concurrent API requests + token bucket (RPM headroom below
# the 50 RPM Haiku account limit).
_ASYNC_CONCURRENCY = 3
_ASYNC_RPM_LIMIT = 40

# Write-batch size — conn.commit() only at these boundaries (§4 rule 2).
_WRITE_BATCH_SIZE = 200

# System prompt for Haiku classification — UNCHANGED from the pre-rebuild version.
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


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Anthropic API helpers — sync (UNCHANGED from the pre-rebuild version)
# ---------------------------------------------------------------------------

def validate_api_key(api_key: str) -> None:
    """L-11: Validate ANTHROPIC_API_KEY prefix. Exits with code 1 if invalid."""
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
    Returns a classification dict with keys: category, english_title,
    english_summary, include_on_site, _error.
    """
    if raw_text.startswith("```"):
        raw_text = raw_text.strip("`").strip()
        if raw_text.lower().startswith("json"):
            raw_text = raw_text[4:].strip()

    result = json.loads(raw_text)

    category = str(result.get("category", "기타")).strip()
    if category not in _VALID_CATEGORIES:
        logger.warning(
            "Invalid category '%s' returned for title='%s' — defaulting to '기타'",
            category, title_ko[:60],
        )
        category = "기타"

    english_title = str(result.get("english_title", "")).strip()
    english_summary = str(result.get("english_summary", "")).strip()

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
    On any failure, returns safe defaults and logs a warning.
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
        logger.warning("JSON parse error for title='%s': %s", title_ko[:60], exc)
        return {"category": "기타", "english_title": "", "english_summary": "", "include_on_site": False, "_error": True}
    except anthropic.APIError as exc:
        logger.warning("Anthropic API error for title='%s': %s", title_ko[:60], exc)
        return {"category": "기타", "english_title": "", "english_summary": "", "include_on_site": False, "_error": True}
    except Exception as exc:  # noqa: BLE001
        logger.warning("Unexpected error for title='%s': %s", title_ko[:60], exc)
        return {"category": "기타", "english_title": "", "english_summary": "", "include_on_site": False, "_error": True}


# ---------------------------------------------------------------------------
# Anthropic API helpers — async (UNCHANGED shape; source columns no longer swapped)
# ---------------------------------------------------------------------------

async def _classify_article_async(
    client: anthropic.AsyncAnthropic,
    semaphore: asyncio.Semaphore,
    raw_id: int,
    title_ko: str,
    description: str,
    counter: list,
    counter_lock: asyncio.Lock,
    total: int,
) -> tuple[int, dict]:
    """Classify a single article asynchronously, bounded by semaphore. Returns (raw_id, result)."""
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
            result = {"category": "기타", "english_title": "", "english_summary": "", "include_on_site": False, "_error": True}
        except anthropic.APIError as exc:
            logger.warning("Anthropic API error for title='%s': %s", title_ko[:60], exc)
            result = {"category": "기타", "english_title": "", "english_summary": "", "include_on_site": False, "_error": True}
        except Exception as exc:  # noqa: BLE001
            logger.warning("Unexpected error for title='%s': %s", title_ko[:60], exc)
            result = {"category": "기타", "english_title": "", "english_summary": "", "include_on_site": False, "_error": True}

    async with counter_lock:
        counter[0] += 1
        done = counter[0]
        if done % 100 == 0 or done == total:
            print(f"  classified {done}/{total} rows...")

    return raw_id, result


class _TokenBucket:
    """Simple async token bucket for rate limiting. Thread-safe via asyncio.Lock."""

    def __init__(self, rate_per_minute: int) -> None:
        self._rate = rate_per_minute / 60.0
        self._tokens = float(rate_per_minute)
        self._max_tokens = float(rate_per_minute)
        self._last_refill = asyncio.get_event_loop().time()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = asyncio.get_event_loop().time()
                elapsed = now - self._last_refill
                self._tokens = min(self._max_tokens, self._tokens + elapsed * self._rate)
                self._last_refill = now

                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return

            wait_seconds = (1.0 - self._tokens) / self._rate
            await asyncio.sleep(max(0.05, wait_seconds))


async def _classify_all_async(
    api_key: str,
    unprocessed: list[dict],
) -> list[tuple[int, dict]]:
    """
    Classify all unprocessed raw rows concurrently using AsyncAnthropic.
    unprocessed: list of raw_news_articles dicts (must have id/title_ko/description).
    Returns list of (raw_id, classification_result) in input order.
    """
    client = anthropic.AsyncAnthropic(api_key=api_key)
    semaphore = asyncio.Semaphore(_ASYNC_CONCURRENCY)
    bucket = _TokenBucket(_ASYNC_RPM_LIMIT)
    counter = [0]
    counter_lock = asyncio.Lock()
    total = len(unprocessed)

    async def _rate_limited_task(raw_row: dict) -> tuple[int, dict]:
        await bucket.acquire()
        return await _classify_article_async(
            client=client,
            semaphore=semaphore,
            raw_id=raw_row["id"],
            title_ko=str(raw_row.get("title_ko") or ""),
            description=str(raw_row.get("description") or ""),
            counter=counter,
            counter_lock=counter_lock,
            total=total,
        )

    tasks = [_rate_limited_task(row) for row in unprocessed]
    results = await asyncio.gather(*tasks)
    await client.close()
    return list(results)


# ---------------------------------------------------------------------------
# sqlite read/write paths — relevance + classification
# ---------------------------------------------------------------------------

def list_pending_relevance(conn: sqlite3.Connection) -> list[dict]:
    """
    Raw articles with no matching news_articles row — the "pending
    judgement" queue (§4). Same query shape as Library's pending-curation
    queue: SELECT * FROM raw WHERE id NOT IN (SELECT ref FROM canonical).
    """
    cur = conn.execute(
        "SELECT * FROM raw_news_articles "
        "WHERE id NOT IN (SELECT raw_ref FROM news_articles) "
        "ORDER BY id"
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def list_stuck_classification(conn: sqlite3.Connection) -> list[dict]:
    """
    news_articles rows with relevant=1 AND classified_at IS NULL — the
    "pending, retry" state (§4 correction 1). Should never arise from this
    script's own write path (one INSERT covers both column groups
    atomically — see write_relevance_and_classification()), but is wired in
    as a real, callable self-heal path per the dispatch brief, not just
    documented: a manual sqlite edit, or a future write path, could still
    produce it.
    """
    cur = conn.execute(
        "SELECT n.id AS news_id, n.raw_ref, r.title_ko, r.description "
        "FROM news_articles n JOIN raw_news_articles r ON r.id = n.raw_ref "
        "WHERE n.relevant = 1 AND n.classified_at IS NULL"
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def write_relevance_and_classification(
    conn: sqlite3.Connection,
    raw_id: int,
    result: dict,
    now_ts: str,
) -> None:
    """
    ONE INSERT statement covering both column groups — relevant +
    relevance_judged_at, and category/english_title/english_summary/
    classified_at (NULL unless relevant) — never an INSERT-then-UPDATE pair
    (§4 atomicity rule 1). Caller commits at batch boundaries, not here.
    """
    if result["include_on_site"]:
        conn.execute(
            "INSERT INTO news_articles "
            "(raw_ref, relevant, relevance_judged_at, category, english_title, english_summary, classified_at) "
            "VALUES (?, 1, ?, ?, ?, ?, ?)",
            (raw_id, now_ts, result["category"], result["english_title"], result["english_summary"], now_ts),
        )
    else:
        conn.execute(
            "INSERT INTO news_articles (raw_ref, relevant, relevance_judged_at) VALUES (?, 0, ?)",
            (raw_id, now_ts),
        )


def retry_stuck_classification(conn: sqlite3.Connection, news_id: int, result: dict, now_ts: str) -> None:
    """
    UPDATE path for a row already stuck at relevant=1/classified_at IS NULL.
    Not a violation of the "never INSERT-then-UPDATE" rule — that rule
    governs a single article's FIRST write (write_relevance_and_classification
    above); this repairs an already-broken row from a prior run/edit, which
    an INSERT cannot do (UNIQUE(raw_ref) would reject it).
    """
    conn.execute(
        "UPDATE news_articles SET category = ?, english_title = ?, english_summary = ?, classified_at = ? "
        "WHERE id = ?",
        (result["category"], result["english_title"], result["english_summary"], now_ts, news_id),
    )


# ---------------------------------------------------------------------------
# Near-duplicate clustering — semantic (ported as-is, NULL-based states)
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
    try:
        return _date.fromisoformat(raw[:10])
    except (ValueError, TypeError):
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
    or None if no match. Raises on API/parse error — caller decides fallback.
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
    """difflib fallback used only when an individual _match_duplicate_with_llm call errors."""
    for i, candidate in enumerate(candidate_titles):
        if SequenceMatcher(None, new_title, candidate).ratio() >= _CLUSTER_TITLE_RATIO:
            return i
    return None


def run_semantic_clustering_pass(
    conn: sqlite3.Connection,
    client: anthropic.Anthropic,
    dry_run: bool,
    force: bool = False,
) -> dict:
    """
    Incrementally judge every unjudged (dedup_judged_at IS NULL, or all rows
    if force) relevant=1 row against already-settled rows in its
    _CLUSTER_DATE_WINDOW_DAYS window, using Haiku to decide same-story
    matches. Ported from the pre-rebuild difflib+Haiku version; NULL-based
    judged/not-judged states replace the old string sentinel (news_schema.py
    schema note).

    Same-batch handling: rows are processed in ascending published_date
    order (raw_ref as tie-break, matching the old article_id tie-break) so a
    row settled earlier in THIS pass can become the canonical match for a
    row processed later in the same pass, before either has a
    dedup_judged_at value committed.

    manual_override protection: if a pending row has manual_override=1, a
    duplicate verdict is still cached (duplicate_of_raw_ref gets set — a
    human can see the LLM's opinion) but is never "applied" in the sense of
    hiding the row — news_data.py's display predicate already keeps a
    manual_override=1 row visible regardless of duplicate_of_raw_ref, so no
    separate "leave relevant=TRUE" step is needed here (unlike the old
    include_on_site-based predicate). A protected row does not join the
    settled pool either — only a genuinely confirmed-canonical row is
    offered as a match target for later rows.

    Never touches category/english_title/english_summary/classified_at.
    Never deletes rows. Returns {"suppressed": int, "judged": int,
    "llm_calls": int, "llm_errors": int}.
    """
    cur = conn.execute(
        "SELECT n.id AS news_id, n.raw_ref, n.duplicate_of_raw_ref, n.dedup_judged_at, n.manual_override, "
        "r.published_date, r.title_ko "
        "FROM news_articles n JOIN raw_news_articles r ON r.id = n.raw_ref "
        "WHERE n.relevant = 1"
    )
    cols = [d[0] for d in cur.description]
    all_relevant = [dict(zip(cols, row)) for row in cur.fetchall()]

    parsed = []
    for row in all_relevant:
        d = _parse_iso_date(str(row.get("published_date") or ""))
        title = str(row.get("title_ko") or "").strip()
        if d is None or not title:
            continue  # can't window- or title-compare an unparseable row — leave untouched
        parsed.append({"news_id": row["news_id"], "raw_ref": row["raw_ref"], "row": row, "date": d, "title": title})
    parsed.sort(key=lambda item: (item["date"], item["raw_ref"]))

    settled = [
        item for item in parsed
        if not force
        and item["row"]["dedup_judged_at"] is not None
        and item["row"]["duplicate_of_raw_ref"] is None
    ]
    pending = [
        item for item in parsed
        if force or item["row"]["dedup_judged_at"] is None
    ]

    if not pending:
        print("  semantic clustering: no unjudged rows — nothing to do")
        return {"suppressed": 0, "judged": 0, "llm_calls": 0, "llm_errors": 0}

    print(f"  semantic clustering: {len(pending)} unjudged row(s) to check "
          f"against {len(settled)} already-settled row(s){' (--force-cluster)' if force else ''}")

    now_ts = _utc_now_iso()
    suppressed = 0
    llm_calls = 0
    llm_errors = 0

    for item in pending:
        window_candidates = [
            s for s in settled
            if s["news_id"] != item["news_id"] and _within_cluster_window(item["date"], s["date"])
        ]

        duplicate_of = None
        if window_candidates:
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

            time.sleep(_API_SLEEP_SECONDS)

        is_protected = bool(item["row"]["manual_override"])

        if duplicate_of is not None and not is_protected:
            conn.execute(
                "UPDATE news_articles SET duplicate_of_raw_ref = ?, dedup_judged_at = ? WHERE id = ?",
                (duplicate_of["raw_ref"], now_ts, item["news_id"]),
            )
            suppressed += 1
            # A suppressed duplicate never joins `settled` — it must not
            # become someone else's "canonical" reference.
        elif duplicate_of is not None and is_protected:
            conn.execute(
                "UPDATE news_articles SET duplicate_of_raw_ref = ?, dedup_judged_at = ? WHERE id = ?",
                (duplicate_of["raw_ref"], now_ts, item["news_id"]),
            )
            print(f"  semantic clustering: news_id={item['news_id']} judged a duplicate of "
                  f"raw_ref={duplicate_of['raw_ref']} but manual_override=1 — display predicate keeps it visible")
        else:
            conn.execute(
                "UPDATE news_articles SET dedup_judged_at = ? WHERE id = ?",
                (now_ts, item["news_id"]),
            )
            settled.append(item)  # available to later pending rows in this same pass.

    print(f"  semantic clustering: {llm_calls} LLM call(s) ({llm_errors} fell back to difflib), "
          f"{suppressed} row(s) suppressed as duplicates")

    if dry_run:
        conn.rollback()
        return {"suppressed": suppressed, "judged": len(pending), "llm_calls": llm_calls, "llm_errors": llm_errors}

    conn.commit()  # batch boundary — one commit for the whole pass
    return {"suppressed": suppressed, "judged": len(pending), "llm_calls": llm_calls, "llm_errors": llm_errors}


def run_canonical_succession(conn: sqlite3.Connection, dry_run: bool) -> list[dict]:
    """
    If a human hides a cluster's canonical article (hidden_by_commander=1 —
    the only remaining human suppression signal in the new schema; relevant
    is Haiku-only per §2, so this replaces the old "human flips
    include_on_site=FALSE directly in the Sheet" trigger), promote that
    cluster's earliest-published still-suppressed mate back to canonical, so
    a real story doesn't silently vanish because of one manual hide
    elsewhere in the cluster.

    manual_override=1 mates are excluded from succession candidacy — the
    display predicate already keeps them visible regardless of
    duplicate_of_raw_ref, so they need no promotion (same rule (a) as the
    pre-rebuild version, restated for the new predicate).

    UNIQUE(raw_ref) on news_articles means one raw article has at most one
    canonical row — the old "up to 26 physical rows share one article_id"
    duplicate-insert-debt class of bug (pre-rebuild classify_articles.py
    docstring) is schema-enforced impossible here; that defensive
    multi-row-per-id lookup is dropped, not ported.

    Returns a list of promotion-event dicts (empty if none).
    """
    cur = conn.execute(
        "SELECT n.id AS news_id, n.raw_ref, n.duplicate_of_raw_ref, n.manual_override, n.hidden_by_commander, "
        "r.title_ko, r.published_date "
        "FROM news_articles n JOIN raw_news_articles r ON r.id = n.raw_ref "
        "WHERE n.relevant = 1"
    )
    cols = [d[0] for d in cur.description]
    all_rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    by_raw_ref = {row["raw_ref"]: row for row in all_rows}

    mates_by_canonical_raw_ref: dict[int, list[dict]] = {}
    for row in all_rows:
        dup_of = row["duplicate_of_raw_ref"]
        if dup_of is not None:
            mates_by_canonical_raw_ref.setdefault(dup_of, []).append(row)

    promotions: list[dict] = []
    now_ts = _utc_now_iso()

    for canonical_raw_ref, mates in mates_by_canonical_raw_ref.items():
        canonical = by_raw_ref.get(canonical_raw_ref)
        if canonical is None:
            continue  # dangling pointer — canonical row missing; leave alone

        if not canonical["hidden_by_commander"]:
            continue  # canonical still visible — nothing to do

        eligible = [m for m in mates if not m["manual_override"]]
        if not eligible:
            continue  # no eligible mate, or all remaining are protected

        eligible.sort(key=lambda m: (str(m["published_date"] or ""), m["raw_ref"]))
        promote = eligible[0]

        print(f"  succession: canonical raw_ref={canonical_raw_ref} (news_id={canonical['news_id']}) "
              f"is hidden_by_commander — promoting raw_ref={promote['raw_ref']} (news_id={promote['news_id']})")
        promotions.append({
            "old_canonical_raw_ref": canonical_raw_ref,
            "old_canonical_title": canonical["title_ko"],
            "old_canonical_news_id": canonical["news_id"],
            "new_canonical_raw_ref": promote["raw_ref"],
            "new_canonical_title": promote["title_ko"],
            "new_canonical_news_id": promote["news_id"],
        })

        if not dry_run:
            conn.execute(
                "UPDATE news_articles SET duplicate_of_raw_ref = NULL, dedup_judged_at = ? WHERE id = ?",
                (now_ts, promote["news_id"]),
            )
            # Repoint every other ELIGIBLE mate AND the old canonical itself
            # to the newly-promoted canonical, so no row is left pointing at
            # a no-longer-canonical raw_ref. A manual_override=1 mate is
            # left untouched — its pointer never mattered for its own
            # visibility (the display predicate already ignores
            # duplicate_of_raw_ref when manual_override=1), and repointing
            # it would misrepresent that a human never separately reviewed
            # its relationship to the new canonical.
            for m in eligible:
                if m["news_id"] != promote["news_id"]:
                    conn.execute(
                        "UPDATE news_articles SET duplicate_of_raw_ref = ? WHERE id = ?",
                        (promote["raw_ref"], m["news_id"]),
                    )
            conn.execute(
                "UPDATE news_articles SET duplicate_of_raw_ref = ? WHERE id = ?",
                (promote["raw_ref"], canonical["news_id"]),
            )

    if not dry_run:
        conn.commit()

    if promotions:
        print(f"  succession: promoted {len(promotions)} row(s)" + (" [dry-run]" if dry_run else ""))
    else:
        print("  succession: no hidden_by_commander canonicals found")

    return promotions


def emit_succession_notice(promotions: list[dict]) -> None:
    """
    Write a GitHub Actions step output when a succession event occurred, so
    a workflow step can turn it into an email. No-op if GITHUB_OUTPUT isn't
    set (local run).
    """
    import os
    github_output = os.environ.get("GITHUB_OUTPUT")
    if not github_output:
        return

    with open(github_output, "a", encoding="utf-8") as f:
        f.write(f"succession_count={len(promotions)}\n")
        if promotions:
            f.write("succession_notice<<SUCCESSION_EOF\n")
            for p in promotions:
                f.write(
                    f"- raw_ref={p['old_canonical_raw_ref']} ({p['old_canonical_title'][:80]}) was hidden by "
                    f"the Commander. Promoted raw_ref={p['new_canonical_raw_ref']} "
                    f"({p['new_canonical_title'][:80]}) to take its place.\n"
                    f"  If your intent was to hide the whole story (not just that one article), "
                    f"also set manual_override=1 and hidden_by_commander=1 on the promoted row via "
                    f"the articles_curation Sheets tab.\n"
                )
            f.write("SUCCESSION_EOF\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    import os
    from dotenv import load_dotenv

    parser = argparse.ArgumentParser(
        description=(
            "Judge relevance and classify unjudged raw_news_articles rows "
            "using Haiku 4.5, writing news_articles (sqlite) in-place."
        )
    )
    parser.add_argument("--dry-run", action="store_true",
                         help="Classify but do not write to sqlite; print first 5 results.")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                         help="Only process the first N unprocessed rows (for testing).")
    parser.add_argument("--async", dest="async_mode", action="store_true",
                         help="Use AsyncAnthropic with Semaphore(3) + token bucket (40 RPM).")
    parser.add_argument("--force", action="store_true",
                         help="Re-classify stuck (relevant=1, classified_at IS NULL) rows even without --limit filtering them out.")
    parser.add_argument("--force-cluster", dest="force_cluster", action="store_true",
                         help="Ignore the dedup_judged_at cache and re-run semantic clustering on every relevant=1 row.")
    args = parser.parse_args()

    print("classify_articles.py — VKH article AI classification (sqlite)")

    # --- L-44 guard: news source disabled in config.yaml — skip before any
    # Anthropic client is constructed or API call is made. Same sources_by_id
    # lookup convention as scripts/news_data.py's assemble_news_section().
    config = _load_config()
    sources_by_id = {s["id"]: s for s in config.get("sources", [])}
    if not sources_by_id.get("news_articles", {}).get("enabled", False):
        print("  news source disabled in config.yaml — skipping classification")
        sys.exit(0)

    print(f"  model: {_HAIKU_MODEL}")
    print(f"  mode: {'async (Semaphore 3, 40 RPM bucket)' if args.async_mode else 'sync (0.5s sleep)'}")
    print(f"  dry-run: {args.dry_run}")
    print(f"  force-cluster: {args.force_cluster}")
    if args.limit:
        print(f"  limit: {args.limit}")

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")

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

    conn = vkh_sqlite.connect()
    vkh_sqlite.migrate(conn, NEWS_DDL)

    pending = list_pending_relevance(conn)
    stuck = list_stuck_classification(conn)
    print(f"  pending relevance judgement: {len(pending)}")
    print(f"  stuck (relevant=1, classified_at IS NULL) — self-heal retry: {len(stuck)}")

    if args.limit is not None:
        pending = pending[: args.limit]
        print(f"  applying --limit: processing {len(pending)} rows")

    ai_client = anthropic.Anthropic(api_key=anthropic_key)

    # --- Self-heal stuck rows first (always sync — low volume, see docstring) ---
    stuck_errors = 0
    if stuck and not args.dry_run:
        now_ts = _utc_now_iso()
        for row in stuck:
            result = classify_article(ai_client, str(row.get("title_ko") or ""), str(row.get("description") or ""))
            if result.get("_error"):
                stuck_errors += 1
            retry_stuck_classification(conn, row["news_id"], result, now_ts)
        conn.commit()
        print(f"  self-healed {len(stuck)} stuck row(s) ({stuck_errors} errors)")

    if not pending:
        print("  No pending relevance judgements — nothing to classify.")
        cluster_stats = run_semantic_clustering_pass(conn, ai_client, dry_run=args.dry_run, force=args.force_cluster)
        promotions = run_canonical_succession(conn, dry_run=args.dry_run)
        emit_succession_notice(promotions)
        conn.close()
        print(f"articles_processed: 0 | written_to_sqlite: 0 | "
              f"duplicates_suppressed: {cluster_stats['suppressed']} | "
              f"canonical_promotions: {len(promotions)} | errors: 0")
        sys.exit(0)

    now_ts = _utc_now_iso()
    errors = 0
    written = 0

    # --- ASYNC PATH -----------------------------------------------------------
    if args.async_mode:
        print(f"  launching async classification ({_ASYNC_CONCURRENCY} concurrent)...")
        t_start = time.monotonic()
        results: list[tuple[int, dict]] = asyncio.run(_classify_all_async(anthropic_key, pending))
        elapsed = time.monotonic() - t_start
        errors = sum(1 for _, cls in results if cls.get("_error"))
        print(f"  async classification done in {elapsed:.1f}s — {len(results)} rows, {errors} errors")

        if args.dry_run:
            print("\n[DRY RUN] Classification complete — no sqlite write.")
            for raw_id, cls in results[:5]:
                print(f"    raw_id {raw_id}: category={cls['category']} | "
                      f"include_on_site={cls['include_on_site']} | summary={cls['english_summary'][:80]!r}")
            conn.close()
            print(f"articles_processed: {len(results)} | written_to_sqlite: 0 | errors: {errors}")
            sys.exit(0)

        for i, (raw_id, cls) in enumerate(results):
            write_relevance_and_classification(conn, raw_id, cls, now_ts)
            written += 1
            if (i + 1) % _WRITE_BATCH_SIZE == 0 or (i + 1) == len(results):
                conn.commit()  # batch boundary (§4 rule 2)

    # --- SYNC PATH (default) --------------------------------------------------
    else:
        sync_results: list[tuple[int, dict]] = []
        for i, row in enumerate(pending):
            title_ko = str(row.get("title_ko") or "")
            description = str(row.get("description") or "")
            classification = classify_article(ai_client, title_ko, description)
            if classification.get("_error"):
                errors += 1
            sync_results.append((row["id"], classification))

            if (i + 1) % 10 == 0 or (i + 1) == len(pending):
                print(f"  classified {i + 1}/{len(pending)} rows...")
            if i < len(pending) - 1:
                time.sleep(_API_SLEEP_SECONDS)

        if args.dry_run:
            print("\n[DRY RUN] Classification complete — no sqlite write.")
            for raw_id, cls in sync_results[:5]:
                print(f"    raw_id {raw_id}: category={cls['category']} | "
                      f"include_on_site={cls['include_on_site']} | summary={cls['english_summary'][:80]!r}")
            conn.close()
            print(f"articles_processed: {len(sync_results)} | written_to_sqlite: 0 | errors: {errors}")
            sys.exit(0)

        for i, (raw_id, cls) in enumerate(sync_results):
            write_relevance_and_classification(conn, raw_id, cls, now_ts)
            written += 1
            if (i + 1) % _WRITE_BATCH_SIZE == 0 or (i + 1) == len(sync_results):
                conn.commit()  # batch boundary (§4 rule 2)

    # --- Clustering + succession (post-write, sees newly-judged rows) --------
    cluster_stats = run_semantic_clustering_pass(conn, ai_client, dry_run=False, force=args.force_cluster)
    promotions = run_canonical_succession(conn, dry_run=False)
    emit_succession_notice(promotions)
    conn.close()

    print()
    print(f"  DONE: {written} row(s) written to news_articles.")
    print(
        f"articles_processed: {written} | "
        f"written_to_sqlite: {written} | "
        f"duplicates_suppressed: {cluster_stats['suppressed']} | "
        f"canonical_promotions: {len(promotions)} | "
        f"errors: {errors}"
    )


if __name__ == "__main__":
    main()
