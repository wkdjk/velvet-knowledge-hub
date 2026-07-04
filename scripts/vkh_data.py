# Import only — do not run directly. Entry point is scripts/build.py.
#
# vkh_data.py — Velvet Knowledge Hub: config, Sheets I/O, section assembly,
# CSV downloads.
#
# C-12b split (2026-07-04): extracted from build.py (was 1,848 lines) so new
# Phase 2 triangulation logic (C-12f) lands in a fresh module, not the
# largest/most fragile file in the repo. Pure move — no logic changed.
# Pipeline order and full module map are documented in build.py's docstring.
#
# This file owns: config.yaml loading, Sheets connection, single-pass tab
# read (L-4), section assembly, date/truthy/product-type normalisation
# helpers shared by KPI + chart computation, and the three CSV download
# writers.
#
# Security: no credentials in this file. All secrets from environment only.

import csv
import re
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import gspread

from scripts.sheets_auth import FULL_SCOPES, _load_config, connect_sheets as _sa_connect_sheets

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config.yaml"
TEMPLATE_DIR = REPO_ROOT / "templates"
OUTPUT_PATH = REPO_ROOT / "docs" / "index.html"
DOWNLOADS_DIR = REPO_ROOT / "docs" / "downloads"
TRADE_FLOWS_CSV = DOWNLOADS_DIR / "trade_flows.csv"
NEWS_PULSE_CSV = DOWNLOADS_DIR / "news_pulse.csv"

# M-3 fix (VKH audit 2026-07-01): use KST, not runner UTC, for calendar-date
# cutoffs — workflow_dispatch triggers between 00:00-09:00 KST are still the
# previous UTC day, which shifted 90-day KPI windows and the build date back
# by one day.
_KST = ZoneInfo("Asia/Seoul")


def _today_kst() -> date:
    return datetime.now(_KST).date()


# Column order for the trade_flows CSV export.
_TRADE_FLOWS_CSV_HEADERS = [
    "date", "series", "hs_code", "hs_label", "value", "unit", "country", "notes",
    "hs_code_10digit", "product_type",
]

# Column order for the import_intelligence CSV export (VFI_Import_Records only).
_IMPORT_INTELLIGENCE_CSV_HEADERS = [
    "date", "importer", "product_en", "product_name", "product_type_en",
    "country_origin_en", "importer_ko", "importer_en", "notes",
]

IMPORT_INTELLIGENCE_CSV = DOWNLOADS_DIR / "import_intelligence.csv"

# Public display-name lookup (Task 1, 2026-07-03): importer_en carries
# internal memo aliases inherited from the VFI predecessor project (e.g.
# "WooSung (Mr Bang)") that were never meant to be public — they name the
# importer's individual contact, visible to the importer themselves on a
# public GitHub Pages site. Keyed by the raw importer_en value (including
# both spelling variants seen in the live sheet); any value not listed here
# falls through unchanged via _display_name() below — this only strips known
# nicknames, it never hides a company with no entry.
_IMPORTER_DISPLAY_NAMES: dict[str, str] = {
    "WooSung (Mr Bang)": "WooSung",
    "WooSung (Mr. Bang)": "WooSung",
    "YongBo (Mr. Woljin Kim)": "YongBo",
    "Taeahn (Mr. Ahn)": "Taeahn",
    "MyungGa (Mr.Lee in Daegu)": "MyungGa",
    "SamBo (Mr. Kang in Busan)": "SamBo",
    "Su Food (Mr. Choi)": "Su Food",
    "Bio Dot (Han Dong Herb)": "Bio Dot",
    "KVIE (Alex, HanPure)": "KVIE",
}


def _display_name(raw: str) -> str:
    """Return the public display name for an importer_en value.

    Falls back to the raw value unchanged if it has no lookup entry —
    strips known nicknames only, never hides unmapped data.
    """
    return _IMPORTER_DISPLAY_NAMES.get(raw, raw)


# Column order for the news_pulse CSV export.
# C-6a: updated to match the *actual* live KVN_Articles tab column names
# (confirmed C-5h: 'title' col = article URL, 'url' col = Korean title text,
# 'source' col = source name). Previous assumed-schema names (title_ko,
# description, source_name, source_type, keyword_matched) are absent from the
# live tab and produced empty CSV columns. Replaced with live tab column names.
_NEWS_PULSE_CSV_HEADERS = [
    "article_id", "title", "url", "content_hash", "published_date",
    "source", "category", "english_title", "english_summary", "ai_processed_at", "include_on_site",
]

# Maps config section identifiers to the list of source IDs they aggregate.
SECTION_SOURCE_MAP = {
    "trade_flows": ["nz_export", "korea_quarantine", "kstat_api"],
    "import_intelligence": ["vfi_import_records", "vfi_price_annual"],
    "market_presence": ["market_presence"],
    "news_pulse": ["kvn_articles"],
}

# Month abbreviation list for KPI date labels (e.g. "Jan 2025").
_MONTH_ABBR = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


# ---------------------------------------------------------------------------
# Step 1 — Config loading
# ---------------------------------------------------------------------------

def load_config(path: Path = CONFIG_PATH) -> dict:
    """Read config.yaml from repo root and return the full config dict."""
    return _load_config(path)


# ---------------------------------------------------------------------------
# Step 2 — Sheets connection
# ---------------------------------------------------------------------------

def connect_sheets(config: dict):
    """
    Load credentials from environment, connect to Google Sheets.

    Returns (None, gspread.Spreadsheet) — gc is not used by callers (kept for
    backward compatibility with the _gc, sheet = connect_sheets(config) call site).
    Calls sys.exit(1) on missing env vars or missing sheet_id.
    """
    sheet_id = config.get("sheet_id", "").strip()
    if not sheet_id:
        print(
            "ERROR: sheet_id is empty in config.yaml. "
            "Run A-3 setup_sheets.py first and record the sheet ID.",
            file=sys.stderr,
        )
        sys.exit(1)
    sheet = _sa_connect_sheets(sheet_id, scopes=FULL_SCOPES)
    return None, sheet


# ---------------------------------------------------------------------------
# Step 3 — Data loading (single pass — L-4)
# ---------------------------------------------------------------------------

def load_all_tabs(sheet, config: dict) -> dict:
    """
    Read every enabled source tab from Sheets in a single pass.

    Returns tab_data dict keyed by tab name → list of row dicts.
    Each tab is read exactly once regardless of how many sources share it.
    L-4: get_all_records() is called once per tab, never inside a row loop.
    """
    # Collect unique tab names for enabled sources.
    tabs_to_load: dict[str, bool] = {}  # tab_name -> enabled
    for source in config.get("sources", []):
        tab = source.get("tab", "")
        enabled = source.get("enabled", False)
        if tab and tab not in tabs_to_load:
            tabs_to_load[tab] = enabled
        elif tab and enabled:
            # If another source already registered this tab as disabled,
            # upgrade to enabled (another source needs it).
            tabs_to_load[tab] = True

    # Also load admin/keyword tabs that are not in sources but exist in the sheet.
    # We discover them by looking at ALL tab names in the sheet once.
    try:
        all_worksheets = {ws.title: ws for ws in sheet.worksheets()}
    except gspread.exceptions.APIError as exc:
        print(f"  WARNING: Could not list worksheets — {exc}", file=sys.stderr)
        all_worksheets = {}

    # Add admin tabs that exist in the sheet but are not source tabs.
    known_source_tabs = set(tabs_to_load.keys())
    admin_tabs = {"_keywords", "README_Admin", "Source_Status"}
    for admin_tab in admin_tabs:
        if admin_tab in all_worksheets and admin_tab not in tabs_to_load:
            tabs_to_load[admin_tab] = True

    tab_data: dict[str, list] = {}

    for tab_name, enabled in tabs_to_load.items():
        if not enabled:
            tab_data[tab_name] = []
            continue

        if tab_name not in all_worksheets:
            # Tab not found — graceful degradation.
            print(
                f"  WARNING: tab '{tab_name}' not found in sheet — "
                "section will render placeholder card"
            )
            tab_data[tab_name] = []
            continue

        try:
            ws = all_worksheets[tab_name]
            # L-4: single bulk read — never call the API inside a row loop.
            rows = ws.get_all_records()
            tab_data[tab_name] = rows
        except gspread.exceptions.APIError as exc:
            print(
                f"  WARNING: APIError reading tab '{tab_name}' — {exc} "
                "— section will render placeholder card"
            )
            tab_data[tab_name] = []

    # Console: rows loaded per tab.
    print("  tabs loaded:")
    for tab_name, rows in sorted(tab_data.items()):
        print(f"    {tab_name:<30}: {len(rows)} rows")

    return tab_data


# ---------------------------------------------------------------------------
# Step 4 — Section data assembly
# ---------------------------------------------------------------------------

def assemble_sections(config: dict, tab_data: dict) -> dict:
    """
    Build a section dict for each dashboard section.

    Each section dict:
      {
        "enabled": bool,
        "data": [list of row dicts],
        "last_updated": "YYYY-MM-DD" or None,
        "has_data": bool,
      }

    For sources with series_value: filter tab rows to matching series only.
    For market_presence (disabled): empty placeholder dict.
    """
    sources_by_id = {s["id"]: s for s in config.get("sources", [])}

    sections: dict[str, dict] = {}

    for section_id, source_ids in SECTION_SOURCE_MAP.items():
        # Gather all sources for this section.
        section_sources = [sources_by_id[sid] for sid in source_ids if sid in sources_by_id]

        # Section is enabled if at least one of its sources is enabled.
        section_enabled = any(s.get("enabled", False) for s in section_sources)

        if not section_enabled:
            sections[section_id] = {
                "enabled": False,
                "data": [],
                "last_updated": None,
                "has_data": False,
            }
            continue

        combined_data: list[dict] = []

        for source in section_sources:
            if not source.get("enabled", False):
                continue

            tab = source.get("tab", "")
            rows = tab_data.get(tab, [])

            series_value = source.get("series_value")
            if series_value:
                # Filter to rows belonging to this series only.
                rows = [r for r in rows if r.get("series") == series_value]

            combined_data.extend(rows)

        # Derive last_updated from the most recent date-like value in combined_data.
        # BR-1: _extract_last_updated already calls _normalise_date_str internally,
        # but we apply it again here as a belt-and-suspenders guarantee so that the
        # template always receives a zero-padded YYYY-MM string (never "YYYY-M").
        last_updated = _extract_last_updated(combined_data)
        if last_updated is not None:
            last_updated = _normalise_date_str(last_updated)

        section_dict: dict = {
            "enabled": True,
            "data": combined_data,
            "last_updated": last_updated,
            "has_data": len(combined_data) > 0,
        }

        # M-2: For trade_flows, split kstat_api rows by hs_code so the
        # JS HS-code toggle can switch datasets without re-reading data.
        #
        # L-9 note: Sheets returns numeric cells as floats (leading zero stripped).
        # "0507.90" is stored as 507.9 in the sheet → str() gives "507.9".
        # Normalise by stripping leading zeros after removing the dot, then
        # comparing the numeric prefix ("507" for 0507.xx, "510" for 0510.xx).
        if section_id == "trade_flows":
            kstat_rows = [r for r in combined_data if r.get("series") == "kstat_api"]

            def _hs_prefix(row: dict) -> str:
                """Return the numeric prefix of the hs_code field (e.g. '507', '510')."""
                raw = str(row.get("hs_code", "")).replace(".", "").lstrip("0")
                return raw[:3] if len(raw) >= 3 else raw

            section_dict["kstat_0507"] = [
                r for r in kstat_rows if _hs_prefix(r) == "507"
            ]
            section_dict["kstat_0510"] = [
                r for r in kstat_rows if _hs_prefix(r) == "510"
            ]

        # B-7: For import_intelligence, split into import_records and
        # price_annual subsets. The two tabs share the section but have
        # completely different schemas — they must not be mixed.
        #
        # tab names: VFI_Import_Records and VFI_Price_Annual.
        # Detection: VFI_Price_Annual rows carry a 'rank' column;
        #   VFI_Import_Records rows carry a 'date' column but no 'rank'.
        # We read the raw tabs separately from tab_data for a clean split.
        if section_id == "import_intelligence":
            import_records_rows = tab_data.get("VFI_Import_Records", [])
            price_annual_rows_raw = tab_data.get("VFI_Price_Annual", [])

            # Coerce price_krw from comma-string to int (B-7 design spec §7).
            # Guard: Sheets may return numeric cells as int already.
            price_annual_rows: list[dict] = []
            for raw_row in price_annual_rows_raw:
                row = dict(raw_row)
                v = row.get("price_krw", "")
                if isinstance(v, str):
                    try:
                        row["price_krw"] = int(v.replace(",", ""))
                    except (ValueError, TypeError):
                        row["price_krw"] = 0
                elif isinstance(v, float):
                    row["price_krw"] = int(v)
                # else already int — leave as-is
                price_annual_rows.append(row)

            # Keep `data` pointing at import_records so compute_kpis() still works.
            section_dict["data"] = import_records_rows
            section_dict["has_data"] = len(import_records_rows) > 0
            section_dict["import_records_rows"] = import_records_rows
            section_dict["price_annual_rows"] = price_annual_rows
            section_dict["import_records_has_data"] = len(import_records_rows) > 0
            section_dict["price_annual_has_data"] = len(price_annual_rows) > 0

        sections[section_id] = section_dict

    return sections


def _normalise_date_str(raw: str) -> str:
    """
    Normalise a date string to ISO format so lexicographic comparison is reliable.

    Handles:
      "YYYY-M"   → "YYYY-0M"   (KSTAT CSV months without zero-padding)
      "YYYY-MM"  → "YYYY-MM"   (already correct)
      "YYYY-MM-DD" → "YYYY-MM-DD" (already correct)

    Anything else is returned unchanged.
    F-05 fix: KSTAT source files store months as "2026-3" rather than "2026-03",
    causing the Sheets-derived last_updated value to render as "2026-3" on site.
    """
    # Match "YYYY-M" (4-digit year, dash, 1-digit month) — pad month to 2 digits.
    m = re.fullmatch(r"(\d{4})-(\d)$", raw.strip())
    if m:
        return f"{m.group(1)}-0{m.group(2)}"
    return raw.strip()


def _extract_last_updated(rows: list[dict]) -> str | None:
    """
    Return the most recent date string found in the 'date' or 'published_date'
    columns of the given row list. Returns None if no date column exists or rows
    is empty.

    Dates are normalised to ISO format before comparison so that KSTAT months
    stored as "YYYY-M" (e.g. "2026-3") sort correctly against "YYYY-MM" strings.
    F-05: always returns a zero-padded string such as "2026-03", never "2026-3".
    """
    if not rows:
        return None

    date_candidates: list[str] = []
    for row in rows:
        for col in ("date", "published_date"):
            val = row.get(col)
            if val:
                date_candidates.append(_normalise_date_str(str(val)))

    if not date_candidates:
        return None

    # Return lexicographically greatest date string (works for YYYY-MM-DD format).
    return max(date_candidates)


# ---------------------------------------------------------------------------
# Shared row-level helpers (used by both vkh_kpi.py and vkh_charts.py)
# ---------------------------------------------------------------------------

def _is_truthy(val) -> bool:
    """
    Return True if val represents a truthy include_on_site flag.

    Handles all forms Google Sheets / gspread may return:
      - Python bool True
      - String "TRUE", "true", "True", "1", "YES", "yes"
      - Integer 1

    C-5g fix: L-9 / L-INCLUDE-FLAG-VALIDATION — Sheets type-coercion can
    return booleans, integers, or strings depending on cell format.
    A robust normalisation here means the comparison never silently fails.
    """
    if isinstance(val, bool):
        return val
    if isinstance(val, int):
        return val == 1
    if isinstance(val, str):
        return val.strip().upper() in ("TRUE", "1", "YES")
    return False


def _infer_qia_product_type(notes: str) -> str:
    """
    Infer product_type for a korea_quarantine row from its notes field.

    Annual XLSX rows carry "| product: 녹용" or "| product: 생녹용" in notes.
    Monthly HTML-XLS rows carry only the source filename — default to "dried".

    GAP-5 back-fill: applied at build time for rows ingested before the C-3e
    schema fix, which lacked product_type. No re-ingest required.

    Mapping:
      notes contains "생녹용" → "frozen"  (fresh/living velvet; 0.33 applies)
      notes contains "녹용"   → "dried"   (dried velvet; no conversion)
      otherwise               → "dried"   (safe default for monthly aggregate rows)
    """
    if "생녹용" in notes:
        return "frozen"
    if "녹용" in notes:
        return "dried"
    return "dried"


def _backfill_product_type(rows: list[dict]) -> list[dict]:
    """
    Return a copy of rows with product_type back-filled for any row that lacks it.

    For korea_quarantine rows: infer from the notes field via _infer_qia_product_type().
    For all other series: fall back to "other" (no conversion; safe degradation).

    GAP-5 fix (2026-06-06): eliminates build.py warnings for existing rows
    ingested before ingest_qia.py was updated to emit product_type.
    Original row dicts are not mutated — a shallow copy is returned.
    """
    result: list[dict] = []
    for row in rows:
        pt = str(row.get("product_type", "")).strip()
        if pt:
            result.append(row)
        else:
            patched = dict(row)
            series = str(row.get("series", ""))
            if series == "korea_quarantine":
                patched["product_type"] = _infer_qia_product_type(
                    str(row.get("notes", ""))
                )
            else:
                patched["product_type"] = "other"
            result.append(patched)
    return result


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

def _write_trade_flows_csv(sections: dict) -> None:
    """
    Write all VTW_Trade_Monthly rows (all series, all hs_codes) to
    docs/downloads/trade_flows.csv.

    Creates docs/downloads/ if it does not exist.
    No-ops silently if trade_flows section has no data.

    F-05 / C-6a: normalise date strings to ISO YYYY-MM format before writing.
    Source sheets store some months without zero-padding (e.g. "2026-3").
    The downloadable CSV must be fully ISO-compliant for external readers.
    """
    trade_data = sections.get("trade_flows", {}).get("data", [])
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

    # Normalise dates before writing — copy each row to avoid mutating in-memory data.
    rows_out = []
    for row in trade_data:
        out = dict(row)
        if out.get("date"):
            out["date"] = _normalise_date_str(str(out["date"]))
        rows_out.append(out)

    with TRADE_FLOWS_CSV.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=_TRADE_FLOWS_CSV_HEADERS,
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(rows_out)

    print(f"  trade_flows.csv: {len(rows_out)} rows → {TRADE_FLOWS_CSV}")


def _write_import_intelligence_csv(sections: dict) -> None:
    """
    Write all VFI_Import_Records rows to docs/downloads/import_intelligence.csv.

    Writes ALL records (not just the display slice of 30). Applies the same
    importer/product_en fallback logic used in the template display rows so
    the CSV matches what the user sees on screen.

    Creates docs/downloads/ if it does not exist.
    No-ops silently if import_intelligence section has no import records.
    """
    ii = sections.get("import_intelligence", {})
    all_records = ii.get("import_records_rows", [])
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

    with IMPORT_INTELLIGENCE_CSV.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=_IMPORT_INTELLIGENCE_CSV_HEADERS,
            extrasaction="ignore",
        )
        writer.writeheader()
        # Apply display fallbacks before writing so CSV matches dashboard.
        # Task 1: strip internal nicknames from importer/importer_en — same
        # lookup as the template's _display_importer, applied here too since
        # the CSV is a separate export path with its own raw-value columns.
        rows_out = []
        for row in all_records:
            out = dict(row)
            out["importer"] = _display_name(row.get("importer") or row.get("importer_ko") or "")
            out["importer_en"] = _display_name(row.get("importer_en") or "")
            out["product_en"] = row.get("product_en") or row.get("product_name") or ""
            rows_out.append(out)
        writer.writerows(rows_out)

    print(f"  import_intelligence.csv: {len(all_records)} rows → {IMPORT_INTELLIGENCE_CSV}")


def _write_news_pulse_csv(sections: dict) -> None:
    """
    Write KVN_Articles rows where include_on_site is truthy to
    docs/downloads/news_pulse.csv.

    Creates docs/downloads/ if it does not exist (it should already exist
    after _write_trade_flows_csv runs, but we guard here for resilience).
    Writes a header-only file when no publishable articles exist — graceful
    degradation (L-12): the CSV download link still works and returns a
    valid empty CSV rather than a 404.
    """
    all_news = sections.get("news_pulse", {}).get("data", [])

    # Filter 1: include_on_site truthy — use _is_truthy() to match build.py KPI
    # and template predicate (L-COUNT-LIST: all three must use the same test).
    # C-6a: _is_truthy() handles bool/int/str forms from Sheets coercion.
    # Filter 2 (OI-2): exclude rows where all three content fields are empty.
    # ~70 legacy articles (2005–2023) have include_on_site=TRUE but no content.
    # These predate the Naver/Google News pipeline and pollute the CSV download.
    # Dashboard rendering is unaffected — they fall outside the 90-day window.
    def _has_content(row: dict) -> bool:
        return any(
            str(row.get(col, "")).strip()
            for col in ("title", "url", "english_summary")
        )

    publishable = [
        row for row in all_news
        if _is_truthy(row.get("include_on_site", "")) and _has_content(row)
    ]

    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

    with NEWS_PULSE_CSV.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=_NEWS_PULSE_CSV_HEADERS,
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(publishable)

    print(f"  news_pulse.csv : {len(publishable)} rows (of {len(all_news)} total) → {NEWS_PULSE_CSV}")
