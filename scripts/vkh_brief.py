# Import only — do not run directly. Called from scripts/news_data.py.
#
# vkh_brief.py — Velvet Knowledge Hub: "this week at a glance" auto-drafted
# brief + human publication gate (C-14 item 4, 2026-07-05; rewired to sqlite
# storage, D3 Phase D rebuild, 2026-07-11).
#
# Design (잠망경-approved 2026-07-04, Domain_Knowledge/vkh_brief_gate_premortem_2026-07-04.md;
# storage rewire per Domain_Knowledge_staging/VKH_D3_news_scaffolding_proposal_2026-07-11.md §5):
#   1. One Haiku call per build synthesises this week's KPI deltas, the
#      Section 2 triangulation headline, top news, and notable import
#      records into 4-6 plain-English sentences. UNCHANGED from C-14.
#   2. A cheap, non-LLM fact check extracts every %/kg/tonnes/articles/
#      declarations figure the draft cites and mechanically compares it
#      against the actual numbers computed this same build. UNCHANGED.
#   3. Human gate: the draft is written to raw_weekly_brief_drafts (sqlite,
#      script-authored) AND pushed to the existing weekly_brief Sheets tab
#      (Commander's edit/approve surface — reused as-is, not a new tab; see
#      §7 deviation note in the D3 implementation report). Nothing is shown
#      on the live site until the Commander sets approved=TRUE there.
#      sync_weekly_brief_approvals() pulls approved/published_text/notes
#      back into weekly_briefs (sqlite, canonical). No "publish automatically
#      after N days" fallback exists in this file — deliberately never
#      written (Pre-Mortem #4).
#   4. Once approved, the site shows the most recently *approved* week's
#      text (never an unapproved newer draft) with a staleness warning once
#      the approval is more than stale_after_days old.
#   5. weekly_brief.enabled in config.yaml is a hard kill switch.
#
# Security: no credentials in this file. All secrets from environment only.

import os
import re
import sqlite3
import sys
from datetime import date, datetime, timezone

import anthropic
import gspread

from scripts.classify_articles import validate_api_key

_HAIKU_MODEL = "claude-haiku-4-5-20251001"
_BRIEF_TAB = "weekly_brief"  # reused as the Commander's curation surface — see module docstring point 3

_DEFAULT_STALE_AFTER_DAYS = 14  # ~2 weekly build cycles (build_site.yml cron)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Fact check — mechanical, no LLM. UNCHANGED from C-14 (§5: "prompt/fact-check/
# approval-gate logic unchanged; storage retargeted").
# ---------------------------------------------------------------------------

_CITED_NUMBER_RE = re.compile(
    r"([\d][\d,]*\.?\d*)\s*(%|kg|kilograms?|tonnes?|dmt|articles?|declarations?|records?)",
    re.IGNORECASE,
)

_UNIT_MAP = {
    "%": "%",
    "kg": "kg", "kilogram": "kg", "kilograms": "kg",
    "tonne": "tonnes", "tonnes": "tonnes", "dmt": "tonnes",
    "article": "articles", "articles": "articles",
    "declaration": "declarations", "declarations": "declarations",
    "record": "records", "records": "records",
}


def _pct_in(s: str) -> list[float]:
    """Extract every number immediately followed by '%' in s."""
    return [float(m) for m in re.findall(r"([\d]+\.?\d*)\s*%", str(s or ""))]


def _num_only(s) -> float | None:
    """Parse a pure numeric KPI string (e.g. '153,674' or '8,810.5') to float."""
    cleaned = str(s or "").replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def _reference_numbers(kpi: dict, chart_data: dict) -> dict[str, list[float]]:
    """
    Build the set of "correct" figures the draft is allowed to cite, bucketed
    by unit, from the exact same kpi/chart_data dicts this build computed.
    """
    pct: list[float] = []
    kg: list[float] = []
    tonnes: list[float] = []
    articles: list[float] = []
    counts: list[float] = []

    pct += _pct_in(kpi.get("qia_yoy_label", ""))
    pct += _pct_in(kpi.get("nz_export_delta", ""))
    yoy_chip = (chart_data or {}).get("yoy_chip", {}) or {}
    pct += _pct_in(yoy_chip.get("label", ""))

    v = _num_only(kpi.get("qia_rolling12m_kg"))
    if v is not None:
        kg.append(v)
    v = _num_only(kpi.get("articles_90d"))
    if v is not None:
        articles.append(v)
    v = _num_only(kpi.get("food_imports_90d"))
    if v is not None:
        counts.append(v)
    v = _num_only(kpi.get("nz_export_latest"))
    if v is not None:
        tonnes.append(v)

    triangulation = (chart_data or {}).get("triangulation", {}) or {}
    purpose = triangulation.get("purpose_split", {}) or {}
    if purpose.get("available"):
        tonnes += [purpose["quarantine_total_dmt"], purpose["pharma_dmt"], purpose["food_dmt"]]
        pct += [purpose["pharma_pct"], purpose["food_pct"]]

    return {
        "%": pct, "kg": kg, "tonnes": tonnes,
        "articles": articles, "declarations": counts, "records": counts,
    }


def _numbers_match(cited: float, reference: list[float]) -> bool:
    for ref in reference:
        if abs(cited - ref) <= 0.5:
            return True
        if ref != 0 and abs(cited - ref) / abs(ref) <= 0.02:
            return True
    return False


def fact_check_draft(draft_text: str, kpi: dict, chart_data: dict) -> tuple[str, str]:
    """
    Mechanically compare every %/kg/tonnes/articles/declarations figure cited
    in draft_text against this build's actual KPI/chart_data values.

    Returns (status, detail): status is "ok" or "review_needed". Never raises
    — a regex/parse issue degrades to "review_needed" with a note (L-12).
    """
    try:
        reference = _reference_numbers(kpi, chart_data)
        mismatches: list[str] = []
        for raw_num, raw_unit in _CITED_NUMBER_RE.findall(draft_text or ""):
            unit = _UNIT_MAP.get(raw_unit.lower())
            if unit is None:
                continue
            cited = float(raw_num.replace(",", ""))
            if not _numbers_match(cited, reference.get(unit, [])):
                mismatches.append(f"{raw_num}{raw_unit}")
        if mismatches:
            return "review_needed", "not found among this build's figures: " + ", ".join(mismatches)
        return "ok", ""
    except Exception as exc:  # noqa: BLE001
        return "review_needed", f"fact-check error — {exc}"


# ---------------------------------------------------------------------------
# Prompt construction — UNCHANGED from C-14.
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "You write a short weekly brief for the Velvet Knowledge Hub, a dashboard "
    "that New Zealand deer velvet exporters use to track the Korean market. "
    "Write 4-6 plain-English sentences for a farmer, not an analyst — avoid "
    "jargon (dried-equivalent, rolling 12 months, HS code) unless a plain "
    "figure needs it. Sentence case only (capitalise the first word and "
    "proper nouns, never Title Case). State only the figures given to you — "
    "never invent, round unusually, or estimate a number that was not "
    "supplied. If a figure is marked unavailable, do not guess at it."
)


def _build_prompt(kpi: dict, chart_data: dict, sections: dict, week_ending_date: str) -> str:
    lines = [f"Week ending: {week_ending_date}", ""]

    lines.append("KPI deltas this week:")
    lines.append(f"- Korea imports, rolling 12 months: {kpi.get('qia_rolling12m_kg', '—')} kg "
                 f"({kpi.get('qia_rolling12m_date_start', '—')} - {kpi.get('qia_rolling12m_date_end', '—')}), "
                 f"year-on-year: {kpi.get('qia_yoy_label', '—')}")
    lines.append(f"- News articles, last 90 days: {kpi.get('articles_90d', '—')}")
    lines.append(f"- Food-channel import declarations, last 90 days: {kpi.get('food_imports_90d', '—')}")

    triangulation = (chart_data or {}).get("triangulation", {}) or {}
    purpose = triangulation.get("purpose_split", {}) or {}
    if purpose.get("available"):
        lines.append(
            "- Section 2 triangulation (quarantine total split by end use): "
            f"{purpose['quarantine_total_display']} DMT total, of which "
            f"{purpose['pharma_display']} DMT ({purpose['pharma_pct']}%) pharmaceutical channel and "
            f"{purpose['food_display']} DMT ({purpose['food_pct']}%) food channel."
        )
    else:
        lines.append("- Section 2 triangulation split: not available this week — do not mention a pharma/food split.")

    news_rows = (sections or {}).get("news_pulse", {}).get("data", []) or []
    top_news = sorted(
        (r for r in news_rows if str(r.get("published_date", ""))),
        key=lambda r: str(r.get("published_date", "")),
        reverse=True,
    )[:3]
    if top_news:
        lines.append("")
        lines.append("Top recent news items:")
        for r in top_news:
            headline = r.get("english_title") or r.get("english_summary") or "—"
            lines.append(f"- {headline}")

    import_rows = (sections or {}).get("import_intelligence", {}).get("import_records_rows", []) or []
    top_imports = sorted(
        (r for r in import_rows if str(r.get("date", ""))),
        key=lambda r: str(r.get("date", "")),
        reverse=True,
    )[:2]
    if top_imports:
        lines.append("")
        lines.append("Most recent import records of note:")
        for r in top_imports:
            product = r.get("product_en") or r.get("product_name") or "—"
            origin = r.get("country_origin_en") or "—"
            lines.append(f"- {r.get('date', '—')}: {product} from {origin}")

    lines.append("")
    lines.append("Write the brief now, using only the figures above.")
    return "\n".join(lines)


def _call_haiku(prompt: str) -> str | None:
    """Call Haiku once. Returns the draft text, or None on any failure (L-12)."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("WARNING: ANTHROPIC_API_KEY not set — skipping weekly brief draft.", file=sys.stderr)
        return None
    try:
        validate_api_key(api_key)
    except SystemExit:
        return None

    try:
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model=_HAIKU_MODEL,
            max_tokens=400,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text.strip()
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: weekly brief Haiku call failed — {exc}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# sqlite write path — raw_weekly_brief_drafts (script-authored)
# ---------------------------------------------------------------------------

def generate_weekly_brief_draft(
    config: dict,
    conn: sqlite3.Connection,
    kpi: dict,
    chart_data: dict,
    sections: dict,
    week_ending_date: str,
) -> dict:
    """
    Generate this week's draft brief, if one does not already exist for
    week_ending_date (UNIQUE(week_ending_date) makes this idempotent across
    multiple builds in the same week — e.g. push-triggered rebuilds).

    Returns a notice dict: {"new_draft": bool, "fact_check_status": str,
    "week_ending_date": str} for emit_weekly_brief_notice().
    """
    weekly_cfg = (config or {}).get("weekly_brief", {}) or {}
    if not weekly_cfg.get("enabled", False):
        return {"new_draft": False}

    existing = conn.execute(
        "SELECT id FROM raw_weekly_brief_drafts WHERE week_ending_date = ?", (week_ending_date,)
    ).fetchone()
    if existing is not None:
        return {"new_draft": False}

    prompt = _build_prompt(kpi, chart_data, sections, week_ending_date)
    draft_text = _call_haiku(prompt)
    if not draft_text:
        return {"new_draft": False}

    fact_check_status, fact_check_detail = fact_check_draft(draft_text, kpi, chart_data)

    conn.execute(
        "INSERT INTO raw_weekly_brief_drafts "
        "(week_ending_date, draft_text, fact_check_status, fact_check_detail, generated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (week_ending_date, draft_text, fact_check_status, fact_check_detail, _utc_now_iso()),
    )
    conn.commit()

    return {
        "new_draft": True,
        "fact_check_status": fact_check_status,
        "fact_check_detail": fact_check_detail,
        "week_ending_date": week_ending_date,
        "draft_text": draft_text,
    }


def emit_weekly_brief_notice(result: dict) -> None:
    """
    Write a GitHub Actions step output when a new draft was generated (same
    pattern as classify_articles.emit_succession_notice). No-op if
    GITHUB_OUTPUT isn't set (local run) or no new draft was written.
    """
    if not result.get("new_draft"):
        return

    github_output = os.environ.get("GITHUB_OUTPUT")
    if not github_output:
        return

    with open(github_output, "a", encoding="utf-8") as f:
        f.write("brief_ready=1\n")
        f.write(f"brief_week={result.get('week_ending_date', '')}\n")
        f.write(f"brief_fact_check_status={result.get('fact_check_status', '')}\n")


# ---------------------------------------------------------------------------
# Sheets curation surface — the existing weekly_brief tab, reused (§5
# deviation, see D3 implementation report). Push: new drafts appear for the
# Commander to review. Pull: approved/published_text/notes sync back into
# weekly_briefs (sqlite, canonical) — this is the ONLY write path to
# weekly_briefs, mirroring D1's sync_curation_tab() pattern.
# ---------------------------------------------------------------------------

def _get_brief_worksheet(sheet) -> "gspread.Worksheet | None":
    """Return the weekly_brief worksheet, or None if it does not exist yet."""
    try:
        return sheet.worksheet(_BRIEF_TAB)
    except gspread.exceptions.WorksheetNotFound:
        print(f"WARNING: '{_BRIEF_TAB}' tab not found — run scripts/setup_weekly_brief_tab.py. "
              "Weekly brief section will be unavailable.", file=sys.stderr)
        return None


def _is_truthy(val) -> bool:
    if isinstance(val, bool):
        return val
    return str(val).strip().upper() in ("TRUE", "1", "YES")


def rehydrate_drafts_from_sheet(conn: sqlite3.Connection, sheet) -> int:
    """
    Repopulate raw_weekly_brief_drafts from the weekly_brief Sheets tab.

    vkh.sqlite is an ephemeral build cache (D1 decision, 2026-07-10) — it
    starts empty every build, so without this step sync_weekly_brief_approvals()
    would find no matching sqlite draft for any already-approved week and
    the site would lose its approved brief on every single build (SurveyorQ
    B-4, D3 re-merge audit 2026-07-11). The weekly_brief tab is the durable,
    cheap-to-replay source — same posture as D1's Drive re-poll pattern.
    Call this BEFORE sync_weekly_brief_approvals() and BEFORE generating the
    current week's draft (build.py step 5f order).

    INSERT OR IGNORE keyed on UNIQUE(week_ending_date): safe to call more
    than once — an already-present week is a no-op, not an error.

    Returns the number of weeks rehydrated.
    """
    ws = _get_brief_worksheet(sheet)
    if ws is None:
        return 0

    rows = ws.get_all_records()
    rehydrated = 0
    for row in rows:
        week = str(row.get("week_ending_date", "")).strip()
        draft_text = str(row.get("draft_text", "")).strip()
        if not week or not draft_text:
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO raw_weekly_brief_drafts "
            "(week_ending_date, draft_text, fact_check_status, fact_check_detail, generated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                week,
                draft_text,
                str(row.get("fact_check_status", "")).strip() or "ok",
                str(row.get("fact_check_detail", "")).strip() or None,
                _utc_now_iso(),
            ),
        )
        if cur.rowcount:
            rehydrated += 1
    conn.commit()
    return rehydrated


def push_pending_drafts_to_sheet(conn: sqlite3.Connection, sheet) -> int:
    """
    Append any raw_weekly_brief_drafts row not yet present in the
    weekly_brief Sheets tab, so the Commander can see and approve it.
    Returns the number of rows pushed.
    """
    ws = _get_brief_worksheet(sheet)
    if ws is None:
        return 0

    rows = ws.get_all_records()
    existing_weeks = {str(r.get("week_ending_date", "")).strip() for r in rows}

    drafts = conn.execute(
        "SELECT week_ending_date, draft_text, fact_check_status, fact_check_detail "
        "FROM raw_weekly_brief_drafts ORDER BY week_ending_date"
    ).fetchall()

    new_rows = [
        [d["week_ending_date"], d["draft_text"], d["fact_check_status"], d["fact_check_detail"] or "", "", "", "", ""]
        for d in drafts
        if d["week_ending_date"] not in existing_weeks
    ]
    if new_rows:
        ws.append_rows(new_rows, value_input_option="USER_ENTERED")
    return len(new_rows)


def sync_weekly_brief_approvals(conn: sqlite3.Connection, sheet) -> dict:
    """
    Pull approved/published_text/notes from the weekly_brief Sheets tab into
    weekly_briefs (sqlite, canonical). Upserts keyed on draft_ref (UNIQUE),
    same promote-or-update posture as ingest_library.promote_or_update_one().

    Also auto-stamps approved_at in the Sheet the first time it sees
    approved=TRUE with approved_at still blank (ported from C-14 unchanged).

    Returns a summary dict: promoted / updated / skipped_no_draft.
    """
    ws = _get_brief_worksheet(sheet)
    if ws is None:
        return {"promoted": 0, "updated": 0, "skipped_no_draft": 0}

    rows = ws.get_all_records()
    header = ws.row_values(1)

    stamp_updates = []
    if "approved_at" in header and "approved" in header:
        approved_col = header.index("approved") + 1
        approved_at_col = header.index("approved_at") + 1
        for i, row in enumerate(rows):
            if _is_truthy(row.get("approved")) and not str(row.get("approved_at", "")).strip():
                sheet_row_num = i + 2
                week = str(row.get("week_ending_date", "")).strip()
                stamp_updates.append({
                    "range": gspread.utils.rowcol_to_a1(sheet_row_num, approved_at_col),
                    "values": [[week]],
                })
                row["approved_at"] = week
        if stamp_updates:
            ws.batch_update(stamp_updates, value_input_option="USER_ENTERED")

    promoted = 0
    updated = 0
    skipped_no_draft = 0

    for row in rows:
        week = str(row.get("week_ending_date", "")).strip()
        if not week:
            continue
        draft = conn.execute(
            "SELECT id FROM raw_weekly_brief_drafts WHERE week_ending_date = ?", (week,)
        ).fetchone()
        if draft is None:
            skipped_no_draft += 1
            continue
        draft_id = draft[0]

        approved = 1 if _is_truthy(row.get("approved")) else 0
        approved_at = str(row.get("approved_at", "")).strip() or None
        published_text = str(row.get("published_text", "")).strip() or None
        notes = str(row.get("notes", "")).strip() or None

        existing = conn.execute(
            "SELECT id FROM weekly_briefs WHERE draft_ref = ?", (draft_id,)
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO weekly_briefs (draft_ref, approved, approved_at, published_text, notes) "
                "VALUES (?, ?, ?, ?, ?)",
                (draft_id, approved, approved_at, published_text, notes),
            )
            promoted += 1
        else:
            conn.execute(
                "UPDATE weekly_briefs SET approved = ?, approved_at = ?, published_text = ?, notes = ? "
                "WHERE draft_ref = ?",
                (approved, approved_at, published_text, notes, draft_id),
            )
            updated += 1

    conn.commit()
    return {"promoted": promoted, "updated": updated, "skipped_no_draft": skipped_no_draft}


# ---------------------------------------------------------------------------
# Read path — News section's own read path (news_data.py) calls this
# directly, per §5: the weekly brief is no longer a standalone build-level
# section.
# ---------------------------------------------------------------------------

def get_weekly_brief_context(config: dict, conn: sqlite3.Connection) -> dict:
    """
    Return the most recently *approved* week's context for template
    rendering, from weekly_briefs JOIN raw_weekly_brief_drafts.

    Returns:
      {"enabled": False} — weekly_brief.enabled is false in config.yaml.
      {"enabled": True, "available": False} — feature on, but no approved
                            brief exists yet.
      {"enabled": True, "available": True, "text": str, "week_ending_date":
       str, "approved_at": str, "fact_check_status": str, "age_days": int,
       "is_stale": bool}
    """
    weekly_cfg = (config or {}).get("weekly_brief", {}) or {}
    if not weekly_cfg.get("enabled", False):
        return {"enabled": False}

    try:
        row = conn.execute(
            "SELECT d.week_ending_date, d.draft_text, d.fact_check_status, "
            "w.approved_at, w.published_text "
            "FROM weekly_briefs w JOIN raw_weekly_brief_drafts d ON d.id = w.draft_ref "
            "WHERE w.approved = 1 "
            "ORDER BY d.week_ending_date DESC LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return {"enabled": True, "available": False}

    if row is None:
        return {"enabled": True, "available": False}

    week_ending_date, draft_text, fact_check_status, approved_at, published_text = row
    text = str(published_text or "").strip() or str(draft_text or "").strip() or "—"
    approved_at_raw = str(approved_at or "").strip() or str(week_ending_date or "").strip()

    stale_after_days = int(weekly_cfg.get("stale_after_days", _DEFAULT_STALE_AFTER_DAYS))
    age_days = None
    is_stale = False
    try:
        from scripts.vkh_data import _today_kst
        approved_date = date.fromisoformat(approved_at_raw)
        age_days = (_today_kst() - approved_date).days
        is_stale = age_days > stale_after_days
    except ValueError:
        pass

    return {
        "enabled": True,
        "available": True,
        "text": text,
        "week_ending_date": str(week_ending_date),
        "approved_at": approved_at_raw,
        "fact_check_status": str(fact_check_status or ""),
        "age_days": age_days,
        "is_stale": is_stale,
    }
