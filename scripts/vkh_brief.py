# Import only — do not run directly. Called from scripts/build.py.
#
# vkh_brief.py — Velvet Knowledge Hub: "this week at a glance" auto-drafted
# brief + human publication gate (C-14 item 4, 2026-07-05).
#
# Design (잠망경-approved 2026-07-04, Domain_Knowledge/vkh_brief_gate_premortem_2026-07-04.md):
#   1. One Haiku call per build synthesises this week's KPI deltas, the
#      Section 2 triangulation headline, top news, and notable import
#      records into 4-6 plain-English sentences.
#   2. A cheap, non-LLM fact check extracts every %/kg/tonnes/articles/
#      declarations figure the draft cites and mechanically compares it
#      against the actual numbers computed this same build. A mismatch
#      flags the draft "review_needed" — it never blocks the draft from
#      being written, only warns the Commander before they approve it.
#   3. Human gate: the draft lands in the weekly_brief Sheets tab. Nothing
#      is shown on the live site until the Commander sets approved=TRUE.
#      No "publish automatically after N days" fallback exists in this file
#      — that code path was deliberately never written (pre-mortem
#      Pre-Mortem #4). Add it only after a fresh 잠망경 if ever requested.
#   4. Once approved, the site shows the most recently *approved* week's
#      text (never an unapproved newer draft) with a staleness warning once
#      the approval is more than stale_after_days old — same pattern as
#      _kpta_estimate_context() in vkh_charts.py.
#   5. weekly_brief.enabled in config.yaml is a hard kill switch: false means
#      no draft is generated and no section is rendered — not an empty box.
#
# Security: no credentials in this file. All secrets from environment only.

import os
import re
import sys
from datetime import date

import anthropic
import gspread

from scripts.classify_articles import validate_api_key

_HAIKU_MODEL = "claude-haiku-4-5-20251001"
_BRIEF_TAB = "weekly_brief"

# weekly_brief tab columns — see scripts/schema.py WEEKLY_BRIEF_HEADERS for
# the single source of truth. Referenced here by name (get_all_records()),
# not by index, per L-COLUMN-ALIAS.
_DEFAULT_STALE_AFTER_DAYS = 14  # ~2 weekly build cycles (build_site.yml cron)

# ---------------------------------------------------------------------------
# Fact check — mechanical, no LLM. Extracts every number immediately
# followed by a unit the brief is allowed to cite, and compares it against
# the actual KPI/chart_data values from this same build.
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

    Returns (status, detail): status is "ok" or "review_needed". detail is a
    human-readable list of unmatched figures (empty string if ok). Never
    raises — a regex/parse issue degrades to "review_needed" with a note,
    not a crash (L-12).
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
# Prompt construction
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
# Sheets I/O
# ---------------------------------------------------------------------------

def _get_brief_worksheet(sheet) -> "gspread.Worksheet | None":
    """Return the weekly_brief worksheet, or None if it does not exist yet
    (run scripts/setup_weekly_brief_tab.py first) — graceful degradation,
    never crashes the build (L-12)."""
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


def generate_weekly_brief_draft(
    config: dict,
    sheet,
    kpi: dict,
    chart_data: dict,
    sections: dict,
    week_ending_date: str,
) -> dict:
    """
    Generate and write this week's draft brief, if one does not already
    exist for week_ending_date. Also auto-stamps approved_at the first time
    it sees an approved=TRUE row with approved_at still blank.

    Returns a notice dict: {"new_draft": bool, "fact_check_status": str,
    "week_ending_date": str} for emit_weekly_brief_notice().
    """
    weekly_cfg = (config or {}).get("weekly_brief", {}) or {}
    if not weekly_cfg.get("enabled", False):
        return {"new_draft": False}

    ws = _get_brief_worksheet(sheet)
    if ws is None:
        return {"new_draft": False}

    rows = ws.get_all_records()

    # --- Auto-stamp approved_at once, for any row a human just approved ---
    header = ws.row_values(1)
    stamp_updates = []
    if "approved_at" in header and "approved" in header:
        approved_col = header.index("approved") + 1
        approved_at_col = header.index("approved_at") + 1
        for i, row in enumerate(rows):
            if _is_truthy(row.get("approved")) and not str(row.get("approved_at", "")).strip():
                sheet_row_num = i + 2  # header is row 1
                stamp_updates.append({
                    "range": gspread.utils.rowcol_to_a1(sheet_row_num, approved_at_col),
                    "values": [[week_ending_date]],
                })
                row["approved_at"] = week_ending_date  # keep in-memory rows fresh for this same render
        if stamp_updates:
            ws.batch_update(stamp_updates, value_input_option="USER_ENTERED")

    # --- Skip generation if this week already has a row (idempotent across
    # multiple builds in the same week, e.g. push-triggered rebuilds) ---
    if any(str(r.get("week_ending_date", "")).strip() == week_ending_date for r in rows):
        return {"new_draft": False}

    prompt = _build_prompt(kpi, chart_data, sections, week_ending_date)
    draft_text = _call_haiku(prompt)
    if not draft_text:
        return {"new_draft": False}

    fact_check_status, fact_check_detail = fact_check_draft(draft_text, kpi, chart_data)

    ws.append_row(
        [week_ending_date, draft_text, fact_check_status, fact_check_detail, "", "", "", ""],
        value_input_option="USER_ENTERED",
    )

    return {
        "new_draft": True,
        "fact_check_status": fact_check_status,
        "fact_check_detail": fact_check_detail,
        "week_ending_date": week_ending_date,
        "draft_text": draft_text,
    }


def emit_weekly_brief_notice(result: dict) -> None:
    """
    Write a GitHub Actions step output when a new draft was generated, so a
    workflow step can turn it into an email (same pattern as
    classify_articles.emit_succession_notice — C-13). A no-op if
    GITHUB_OUTPUT is not set (local run) or no new draft was written.
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


def get_weekly_brief_context(config: dict, sheet) -> dict:
    """
    Read the weekly_brief tab and return the most recently *approved* week's
    context for template rendering.

    Returns:
      {"enabled": False} — weekly_brief.enabled is false in config.yaml, or
                            the tab does not exist yet. Template renders
                            nothing (opt-out is a hard switch, not a hidden
                            option — pre-mortem item 5).
      {"enabled": True, "available": False} — feature on, but no approved
                            brief exists yet (e.g. first run before the
                            Commander has approved anything).
      {"enabled": True, "available": True, "text": str, "week_ending_date":
       str, "approved_at": str, "fact_check_status": str, "age_days": int,
       "is_stale": bool}
    """
    weekly_cfg = (config or {}).get("weekly_brief", {}) or {}
    if not weekly_cfg.get("enabled", False):
        return {"enabled": False}

    ws = _get_brief_worksheet(sheet)
    if ws is None:
        return {"enabled": False}

    rows = ws.get_all_records()
    approved_rows = [r for r in rows if _is_truthy(r.get("approved"))]
    if not approved_rows:
        return {"enabled": True, "available": False}

    latest = max(approved_rows, key=lambda r: str(r.get("week_ending_date", "")))

    text = str(latest.get("published_text", "")).strip() or str(latest.get("draft_text", "")).strip() or "—"
    approved_at_raw = str(latest.get("approved_at", "")).strip() or str(latest.get("week_ending_date", "")).strip()

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
        "week_ending_date": str(latest.get("week_ending_date", "")),
        "approved_at": approved_at_raw,
        "fact_check_status": str(latest.get("fact_check_status", "")),
        "age_days": age_days,
        "is_stale": is_stale,
    }
