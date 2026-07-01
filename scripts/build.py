# Run as: PYTHONPATH=. python scripts/build.py
#
# build.py — Velvet Knowledge Hub core build pipeline
#
# Three steps:
#   1. Load   — read config.yaml, connect to Google Sheets once, read all
#               enabled tabs in a single pass (L-4: never inside a loop).
#   2. Transform — filter rows by series_value where applicable, compute KPIs.
#   3. Render — pass all data to Jinja2 template, write docs/index.html.
#
# Security: no credentials in this file. All secrets from environment only.

import csv
import json
import re
import os
import shutil
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

# M-3 fix (VKH audit 2026-07-01): use KST, not runner UTC, for calendar-date
# cutoffs — workflow_dispatch triggers between 00:00-09:00 KST are still the
# previous UTC day, which shifted 90-day KPI windows and the build date back
# by one day.
_KST = ZoneInfo("Asia/Seoul")


def _today_kst() -> date:
    return datetime.now(_KST).date()
from pathlib import Path

import gspread
import yaml
from jinja2 import Environment, FileSystemLoader

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

# Column order for the news_pulse CSV export.
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
# Step 5 — KPI computation
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


def compute_kpis(sections: dict) -> dict:
    """
    Compute KPI values from assembled section data.

    Returns kpi dict:
      nz_export_latest  — rolling 12-month sum of dried-equivalent KG, in tonnes
                          (A-1: NZ EXPORTS ROLLING 12-MONTH)
      nz_export_delta   — "▲ X%" or "▼ X%" vs prior month, or "—"
      articles_90d      — count of KVN_Articles rows within last 90 days (C-5e: was 30d)
      food_imports_90d  — count of VFI_Import_Records rows in last 90 days
                          (OI-1: extended from 30d to align with news pulse 90-day window)

    Any value that cannot be computed returns "—".
    """
    kpi: dict = {
        "nz_export_latest": "—",
        "nz_export_delta": "—",
        "articles_90d": "—",
        "food_imports_90d": "—",
        # P2-D: QIA rolling-12-month Korea imports (replaces NZ as KPI Box 1).
        "qia_rolling12m_kg": "—",
        "qia_rolling12m_date_start": "—",
        "qia_rolling12m_date_end": "—",
        "qia_yoy_label": "—",
    }

    # --- KPI 1: NZ export rolling 12-month in tonnes (dried-equivalent) ------
    # A-1: compute rolling 12-month sum across all NZ export rows, convert to
    # tonnes (÷1000, 1 decimal place). Uses _compute_dried_eq_kg and
    # _compute_rolling_12m which are defined in the chart data helpers below.
    trade_data = sections.get("trade_flows", {}).get("data", [])
    # GAP-5 back-fill: resolve empty product_type before dried-eq computation.
    nz_export_rows = _backfill_product_type(
        [r for r in trade_data if r.get("series") == "nz_export"]
    )

    if nz_export_rows:
        try:
            nz_dried_eq = _compute_dried_eq_kg(nz_export_rows, "KG")
            if nz_dried_eq:
                nz_rolling = _compute_rolling_12m(nz_dried_eq)
                sorted_dates = sorted(nz_rolling.keys(), reverse=True)
                latest_date = sorted_dates[0]
                latest_rolling_kg = nz_rolling[latest_date]
                latest_tonnes = round(latest_rolling_kg / 1000, 1)
                kpi["nz_export_latest"] = f"{latest_tonnes:,.1f}"

                # Delta: compare latest rolling total vs prior month rolling total.
                if len(sorted_dates) >= 2:
                    prior_date = sorted_dates[1]
                    prior_rolling_kg = nz_rolling[prior_date]
                    if prior_rolling_kg != 0:
                        delta_pct = (latest_rolling_kg - prior_rolling_kg) / prior_rolling_kg * 100
                        symbol = "▲" if delta_pct >= 0 else "▼"
                        kpi["nz_export_delta"] = f"{symbol} {abs(delta_pct):.1f}%"
        except (ValueError, TypeError, ZeroDivisionError) as exc:
            print(f"WARNING: nz_export_delta KPI computation failed — {exc}", file=sys.stderr)

    # --- KPI 1b: QIA Korea imports rolling 12-month (P2-D: for Box 1 redesign) ---
    # Compute rolling 12-month sum of QIA korea_quarantine KG rows.
    # Show date range as e.g. "Apr 2025 – Mar 2026" derived from data.
    # GAP-5 back-fill: resolve empty product_type before dried-eq computation.
    qia_rows_kpi = _backfill_product_type(
        [r for r in trade_data if r.get("series") == "korea_quarantine"]
    )
    if qia_rows_kpi:
        try:
            qia_dried_eq_kpi = _compute_dried_eq_kg(qia_rows_kpi, "KG")
            if qia_dried_eq_kpi:
                qia_rolling_kpi = _compute_rolling_12m(qia_dried_eq_kpi)
                sorted_qia_dates = sorted(qia_rolling_kpi.keys(), reverse=True)
                latest_qia_date = sorted_qia_dates[0]
                latest_qia_kg = qia_rolling_kpi[latest_qia_date]
                kpi["qia_rolling12m_kg"] = f"{latest_qia_kg:,.0f}"

                # Build date range: 12 months ending at latest_qia_date.
                end_year, end_month = int(latest_qia_date[:4]), int(latest_qia_date[5:7])
                start_month = end_month - 11
                start_year = end_year
                while start_month <= 0:
                    start_month += 12
                    start_year -= 1
                kpi["qia_rolling12m_date_start"] = f"{_MONTH_ABBR[start_month]} {start_year}"
                kpi["qia_rolling12m_date_end"] = f"{_MONTH_ABBR[end_month]} {end_year}"

                # YoY: compare latest rolling to same month prior year.
                prior_qia_date = f"{end_year - 1}-{end_month:02d}"
                prior_qia_rolling = qia_rolling_kpi.get(prior_qia_date)
                if prior_qia_rolling and prior_qia_rolling != 0:
                    delta_pct = (latest_qia_kg - prior_qia_rolling) / prior_qia_rolling * 100
                    symbol = "▲" if delta_pct >= 0 else "▼"
                    kpi["qia_yoy_label"] = (
                        f"{symbol} {abs(delta_pct):.1f}% vs "
                        f"{_MONTH_ABBR[end_month]} {end_year - 1}"
                    )
        except (ValueError, TypeError, ZeroDivisionError) as exc:
            print(f"WARNING: qia_rolling12m KPI computation failed — {exc}", file=sys.stderr)

    # --- KPI 2: Articles past 90 days (C-5e: changed from 30 to 90) -----------
    # C-5g fix: articles_90d must use the SAME predicate as the article list —
    # include_on_site truthy AND published_date >= cutoff. L-COUNT-LIST.
    news_data = sections.get("news_pulse", {}).get("data", [])
    if news_data:
        cutoff = _today_kst() - timedelta(days=90)
        count = 0
        for row in news_data:
            # C-5g: require include_on_site truthy (same as template filter).
            if not _is_truthy(row.get("include_on_site", "")):
                continue
            raw_date = row.get("published_date") or row.get("date", "")
            if not raw_date:
                continue
            try:
                # Support YYYY-MM-DD and YYYY-MM-DDTHH:MM:SS formats.
                article_date = datetime.strptime(str(raw_date)[:10], "%Y-%m-%d").date()
                if article_date >= cutoff:
                    count += 1
            except ValueError:
                continue
        kpi["articles_90d"] = str(count)

    # --- KPI 3: Food imports last 90 days (OI-1: window extended 30d → 90d) ----
    # Count VFI_Import_Records rows with notification date >= today - 90 days.
    # Aligned with the news pulse 90-day window so all sections share the same
    # time basis for DINZ readers.
    import_data = sections.get("import_intelligence", {}).get("data", [])
    if import_data:
        cutoff = _today_kst() - timedelta(days=90)
        count = 0
        for row in import_data:
            raw_date = str(row.get("date", "")).strip()
            if not raw_date:
                continue
            try:
                row_date = datetime.strptime(raw_date[:10], "%Y-%m-%d").date()
                if row_date >= cutoff:
                    count += 1
            except ValueError:
                continue
        kpi["food_imports_90d"] = str(count)

    return kpi


# ---------------------------------------------------------------------------
# Step 6 — Render
# ---------------------------------------------------------------------------

def render(config: dict, sections: dict, kpi: dict, chart_data: dict, build_date: str) -> int:
    """
    Render the Jinja2 template with all collected data and write docs/index.html.
    Returns the number of bytes written.
    """
    import json as _json

    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=False)
    # tojson filter: serialise Python objects to JSON strings safe for <script> blocks.
    env.filters["tojson"] = lambda obj: _json.dumps(obj, ensure_ascii=False)
    template = env.get_template("index.html.j2")

    # meta object satisfies template's {{ meta.last_built_utc }} reference.
    meta = {"last_built_utc": build_date}

    # B-7: build import intelligence display context.
    ii = sections.get("import_intelligence", {})
    all_import_records = ii.get("import_records_rows", [])

    # Sort descending by date.
    # B-9: default display is 3 rows; "Show last 30 days" button expands via JS.
    # We pass ALL sorted records to the template and let JS control visibility.
    try:
        sorted_records = sorted(
            all_import_records,
            key=lambda r: str(r.get("date", "")),
            reverse=True,
        )
    except Exception:
        sorted_records = all_import_records

    # Determine 30-day cutoff for the JS expand function.
    cutoff_30d = (_today_kst() - timedelta(days=30)).isoformat()
    # C-5e: 90-day cutoff for news pulse article list filter and KPI count.
    cutoff_90d = (_today_kst() - timedelta(days=90)).isoformat()

    import_records_display = []
    for row in sorted_records:
        display_row = dict(row)
        # B-10: new column mapping.
        # Date: from 'date' field.
        # Origin: country_origin_en.
        # Export country: country_export_en.
        # Exporter: exporter_en (already English in source sheet — all 576 rows populated).
        # Importer: importer_en.
        # Product name: product_en or product_name fallback.
        # Type: product_type_en.
        display_row["_display_date"] = str(row.get("date", ""))
        display_row["_display_origin"] = row.get("country_origin_en") or "—"
        display_row["_display_export_country"] = row.get("country_export_en") or "—"
        display_row["_display_exporter"] = row.get("exporter_en") or "—"
        display_row["_display_importer"] = (
            row.get("importer_en") or row.get("importer") or row.get("importer_ko") or "—"
        )
        display_row["_display_product"] = (
            row.get("product_en") or row.get("product_name") or "—"
        )
        display_row["_display_type"] = row.get("product_type_en") or "—"
        # Flag rows within the last 90 days for JS expand control (P1-H: 30d → 90d).
        display_row["_in_last_90d"] = display_row["_display_date"] >= cutoff_90d
        import_records_display.append(display_row)

    import_records_total = len(all_import_records)
    import_records_has_data = ii.get("import_records_has_data", False)
    price_annual_has_data = ii.get("price_annual_has_data", False)

    # Pre-format price_krw subtitle value as comma-separated string so the
    # template does not need a format_number filter.
    b7_price_subtitle_raw = chart_data.get("b7_price_subtitle")
    if b7_price_subtitle_raw:
        b7_price_subtitle = {
            "price_krw": f"{b7_price_subtitle_raw['price_krw']:,}",
            "year": b7_price_subtitle_raw["year"],
        }
    else:
        b7_price_subtitle = None

    html = template.render(
        build_date=build_date,
        meta=meta,
        kpi=kpi,
        sections=sections,
        chart_data=chart_data,
        config=config,
        # P1-G: MFDS annual import-value series (fixes wrong-tab placeholder).
        mfds_annual_series=chart_data.get("mfds_annual_series", {"has_data": False, "chart_points": [], "table_rows": []}),
        # C-3e trade flows — new structured source objects.
        tf_nz_export=chart_data.get("nz_export", {}),
        tf_korea_qia=chart_data.get("korea_qia", {}),
        tf_korea_kstat=chart_data.get("korea_kstat", {}),
        tf_harvest_boundaries=chart_data.get("harvest_boundaries", []),
        tf_yoy_chip=chart_data.get("yoy_chip", {"label": "—", "direction": "neutral"}),
        tf_window=chart_data.get("window", {}),
        # C-3h destination breakdown charts.
        tf_destination_area=chart_data.get("tf_destination_area", {}),
        tf_destination_pie=chart_data.get("tf_destination_pie", {}),
        tf_qia_by_origin=chart_data.get("tf_qia_by_origin", []),
        # P2-H: QIA monthly origin view.
        tf_qia_monthly_by_origin=chart_data.get("tf_qia_monthly_by_origin", {"labels": [], "countries": [], "series": {}}),
        # C-4e import intelligence context.
        # import_records_display: ALL sorted rows (JS controls 3-row / 30-day view).
        import_records_display=import_records_display,
        import_records_total=import_records_total,
        import_records_has_data=import_records_has_data,
        price_annual_has_data=price_annual_has_data,
        price_annual_series=chart_data.get("b7_price_series", []),
        price_annual_subtitle=b7_price_subtitle,
        # cutoff_30d: ISO date string for JS row-visibility logic.
        cutoff_30d=cutoff_30d,
        # C-5e: cutoff_90d for news pulse article list 90-day filter.
        cutoff_90d=cutoff_90d,
        # C-8: raw KG rows for A1/A2 toggle charts.
        qia_raw_rows=chart_data.get("qia_raw_rows", []),
        nz_raw_rows=chart_data.get("nz_raw_rows", []),
        # C8-P2: Apps Script endpoint URL injected at build time.
        csv_endpoint_url=config.get("csv_endpoint_url", ""),
    )

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(html, encoding="utf-8")
    return len(html.encode("utf-8"))


# ---------------------------------------------------------------------------
# Chart data preparation
# ---------------------------------------------------------------------------

def _aggregate_series_by_date(rows: list[dict], unit_filter: str) -> dict[str, float]:
    """
    Aggregate rows for a single series by date, summing values across countries.

    Returns {date_str: total_value} sorted by date ascending.
    Only rows matching unit_filter are included.
    Non-numeric values are ignored (graceful degradation — L-12).
    """
    totals: dict[str, float] = {}
    for row in rows:
        if str(row.get("unit", "")) != unit_filter:
            continue
        date_str = str(row.get("date", ""))
        if not date_str:
            continue
        try:
            val = float(row.get("value", 0) or 0)
        except (ValueError, TypeError):
            continue
        totals[date_str] = totals.get(date_str, 0.0) + val

    return dict(sorted(totals.items()))


def _build_b7_price_series(price_rows: list[dict]) -> list[dict]:
    """
    Build Chart.js dataset dicts for the B-7 annual price chart.

    Groups VFI_Price_Annual rows by origin_country. Takes the top 3 countries
    by row count. All remaining countries are aggregated into 'Other origins'
    using the mean price_krw per year.

    Returns a list of Chart.js dataset dicts, each with:
      label, data ([{x: year_int, y: price_int}]), borderColor, backgroundColor,
      fill, tension, borderDash, pointStyle, pointRadius.
    """
    if not price_rows:
        return []

    # Group rows by origin_country.
    by_country: dict[str, list[dict]] = defaultdict(list)
    for row in price_rows:
        country = str(row.get("origin_country", "")).strip() or "Unknown"
        by_country[country].append(row)

    # Sort by row count descending; take top 3.
    sorted_countries = sorted(by_country.items(), key=lambda kv: len(kv[1]), reverse=True)
    top_3 = sorted_countries[:3]
    other_countries = sorted_countries[3:]

    # Line style definitions (design spec §2).
    styles = [
        {"borderDash": [],       "pointStyle": "circle",   "pointRadius": 3},
        {"borderDash": [6, 3],   "pointStyle": "triangle", "pointRadius": 4},
        {"borderDash": [2, 2],   "pointStyle": "rect",     "pointRadius": 3},
    ]
    other_style = {"borderDash": [10, 5], "pointStyle": "crossRot", "pointRadius": 3}

    datasets: list[dict] = []

    for i, (country_name, rows) in enumerate(top_3):
        # Build {year: price_krw} — take first (lowest rank) if multiple rows per year.
        year_price: dict[int, int] = {}
        for row in sorted(rows, key=lambda r: int(r.get("rank", 999))):
            yr = int(row.get("year", 0))
            if yr and yr not in year_price:
                year_price[yr] = int(row.get("price_krw", 0))
        data_points = [{"x": yr, "y": price} for yr, price in sorted(year_price.items())]
        style = styles[i]
        datasets.append({
            "label": country_name,
            "data": data_points,
            "borderColor": "#111111",
            "backgroundColor": "transparent",
            "fill": False,
            "tension": 0,
            "borderDash": style["borderDash"],
            "pointStyle": style["pointStyle"],
            "pointRadius": style["pointRadius"],
        })

    # Build "Other origins" aggregate (mean price_krw per year).
    if other_countries:
        other_by_year: dict[int, list[int]] = defaultdict(list)
        for _country, rows in other_countries:
            for row in rows:
                yr = int(row.get("year", 0))
                price = int(row.get("price_krw", 0))
                if yr:
                    other_by_year[yr].append(price)
        if other_by_year:
            other_points = [
                {"x": yr, "y": round(sum(prices) / len(prices))}
                for yr, prices in sorted(other_by_year.items())
            ]
            datasets.append({
                "label": "Other origins",
                "data": other_points,
                "borderColor": "#111111",
                "backgroundColor": "transparent",
                "fill": False,
                "tension": 0,
                "borderDash": other_style["borderDash"],
                "pointStyle": other_style["pointStyle"],
                "pointRadius": other_style["pointRadius"],
            })

    return datasets


def _build_b7_price_subtitle(price_rows: list[dict]) -> dict | None:
    """
    Find the row with minimum rank in the maximum year of VFI_Price_Annual.
    Returns {"price_krw": int, "year": int} or None if no data.
    price_krw is already coerced to int by assemble_sections.
    """
    if not price_rows:
        return None

    try:
        max_year = max(int(row.get("year", 0)) for row in price_rows if row.get("year"))
        year_rows = [r for r in price_rows if int(r.get("year", 0)) == max_year]
        # Minimum rank = highest-ranked entry.
        best_row = min(year_rows, key=lambda r: int(r.get("rank", 999)))
        return {
            "price_krw": int(best_row.get("price_krw", 0)),
            "year": max_year,
        }
    except (ValueError, TypeError):
        return None


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


def _compute_dried_eq_kg(rows: list[dict], unit_filter: str = "KG") -> dict[str, float]:
    """
    Aggregate rows by date, applying the 0.33 dried-equivalent conversion.

    For each row:
      - product_type == "frozen": value × 0.33
      - product_type == "dried":  value × 1.0
      - product_type == "other":  value × 1.0 (no conversion)
      - product_type empty:       back-filled via _backfill_product_type() before
                                  this function is called — should not occur at
                                  runtime after the GAP-5 fix.

    Returns {date_str: dried_eq_kg} sorted ascending.
    GAP-5 fix: _backfill_product_type() is called by prepare_chart_data() and
    compute_kpis() before any call to _compute_dried_eq_kg(), so empty
    product_type rows are resolved before reaching this function.
    """
    totals: dict[str, float] = {}
    for row in rows:
        if str(row.get("unit", "")) != unit_filter:
            continue
        date_str = str(row.get("date", ""))
        if not date_str:
            continue
        try:
            raw_val = float(row.get("value", 0) or 0)
        except (ValueError, TypeError):
            continue

        pt = str(row.get("product_type", "")).strip().lower()
        if pt == "frozen":
            converted = raw_val * 0.33
        else:
            # "dried", "other", or any remaining empty value → no conversion.
            converted = raw_val

        totals[date_str] = totals.get(date_str, 0.0) + converted

    return dict(sorted(totals.items()))


def _compute_rolling_12m(monthly: dict[str, float]) -> dict[str, float]:
    """
    Compute rolling 12-month sum for each month in monthly.

    For each date M, sum months M-11 through M (inclusive).
    Returns {date_str: rolling_12m_total} for all dates in monthly.
    Months with fewer than 12 prior months still return the available sum.
    """
    dates = sorted(monthly.keys())
    rolling: dict[str, float] = {}
    for i, d in enumerate(dates):
        window = dates[max(0, i - 11): i + 1]
        rolling[d] = sum(monthly[w] for w in window)
    return rolling


def _compute_yoy_pct(rolling: dict[str, float]) -> dict[str, float | None]:
    """
    Compute year-on-year % change for each month in rolling.

    For date YYYY-MM, compare rolling[YYYY-MM] vs rolling[(YYYY-1)-MM].
    Returns {date_str: pct_change or None} — None if prior year not available.
    """
    result: dict[str, float | None] = {}
    for d, val in rolling.items():
        year, month = d[:4], d[5:7]
        prior_year = str(int(year) - 1)
        prior_d = f"{prior_year}-{month}"
        prior_val = rolling.get(prior_d)
        if prior_val is not None and prior_val != 0:
            result[d] = round((val - prior_val) / prior_val * 100, 1)
        else:
            result[d] = None
    return result


def _compute_unit_price(
    rows: list[dict],
    value_unit: str,
    value_multiplier: float = 1.0,
) -> dict[str, float]:
    """
    Compute monthly blended unit price = sum(value) / sum(dried_eq_kg).

    value_unit: "NZD" for NZ, "USD_thousands" for KSTAT.
    value_multiplier: multiply raw value before dividing (e.g. 1000 for USD_thousands → USD).
    Omits months where dried_eq_kg sum is zero.
    Returns {date_str: price} sorted ascending.
    """
    kg_by_date: dict[str, float] = {}
    val_by_date: dict[str, float] = {}

    for row in rows:
        date_str = str(row.get("date", ""))
        if not date_str:
            continue
        unit = str(row.get("unit", ""))

        if unit == "KG":
            try:
                raw_kg = float(row.get("value", 0) or 0)
            except (ValueError, TypeError):
                continue
            pt = str(row.get("product_type", "")).strip().lower()
            converted = raw_kg * 0.33 if pt == "frozen" else raw_kg
            kg_by_date[date_str] = kg_by_date.get(date_str, 0.0) + converted

        elif unit == value_unit:
            try:
                raw_val = float(row.get("value", 0) or 0) * value_multiplier
            except (ValueError, TypeError):
                continue
            val_by_date[date_str] = val_by_date.get(date_str, 0.0) + raw_val

    prices: dict[str, float] = {}
    for d in sorted(set(kg_by_date.keys()) & set(val_by_date.keys())):
        kg = kg_by_date[d]
        val = val_by_date[d]
        if kg > 0:
            prices[d] = round(val / kg, 2)

    return prices


def _last_24_months_window(all_dates: list[str]) -> tuple[str, str]:
    """
    Return (start_date, end_date) strings for the last 24 months of data.

    end_date is the most recent date in all_dates.
    start_date is 23 months before end_date (inclusive range = 24 months).
    If fewer than 24 months of data exist, start from the earliest available.
    """
    if not all_dates:
        return ("", "")
    sorted_dates = sorted(all_dates)
    end_date = sorted_dates[-1]
    end_year, end_month = int(end_date[:4]), int(end_date[5:7])

    start_month = end_month - 23
    start_year = end_year
    while start_month <= 0:
        start_month += 12
        start_year -= 1

    start_date = f"{start_year:04d}-{start_month:02d}"
    return (start_date, end_date)


def _filter_to_window(data: dict[str, float], start: str, end: str) -> dict[str, float]:
    """Return only entries whose date key falls within [start, end] inclusive."""
    return {d: v for d, v in data.items() if start <= d <= end}


def _harvest_boundaries(start: str, end: str) -> list[str]:
    """
    Return list of YYYY-10 date strings (October months) within [start, end].
    These mark harvest-year season starts for Panel A chart annotations.
    """
    if not start or not end:
        return []
    boundaries = []
    year = int(start[:4])
    end_year = int(end[:4])
    while year <= end_year + 1:
        candidate = f"{year:04d}-10"
        if start <= candidate <= end:
            boundaries.append(candidate)
        year += 1
    return boundaries


def _compute_yoy_chip(rolling_by_date: dict[str, float]) -> dict:
    """
    Compute the section-level YoY KPI chip value.

    Finds the most recent complete Oct-Sep window and compares to the
    same window one year prior. Returns:
      {
        "pct": float or None,
        "direction": "up" | "down" | "neutral",
        "label": "▲ X%" | "▼ X%" | "—",
      }
    """
    if not rolling_by_date:
        return {"pct": None, "direction": "neutral", "label": "—"}

    # Find the most recent September (end of a complete harvest year).
    sep_dates = [d for d in rolling_by_date if d.endswith("-09")]
    if not sep_dates:
        # Fall back to the most recent month with data.
        most_recent = max(rolling_by_date.keys())
        prior_year = f"{int(most_recent[:4]) - 1}-{most_recent[5:7]}"
    else:
        most_recent = max(sep_dates)
        prior_year = f"{int(most_recent[:4]) - 1}-09"

    current_val = rolling_by_date.get(most_recent)
    prior_val = rolling_by_date.get(prior_year)

    if current_val is None or prior_val is None or prior_val == 0:
        return {"pct": None, "direction": "neutral", "label": "—"}

    pct = round((current_val - prior_val) / prior_val * 100, 1)
    if abs(pct) < 0.5:
        direction = "neutral"
        label = "—"
    elif pct > 0:
        direction = "up"
        label = f"▲ {pct}%"
    else:
        direction = "down"
        label = f"▼ {abs(pct)}%"

    return {"pct": pct, "direction": direction, "label": label}


# ---------------------------------------------------------------------------
# C-3h helper functions — destination breakdown charts (Panel C + Panel D)
# ---------------------------------------------------------------------------

# Countries to display individually in destination charts. All other countries
# are collapsed into "Other".
_DEST_COUNTRIES: list[str] = ["China", "Korea", "Hong Kong"]

# Colour palette for destination charts (Panel C: NZ by destination).
# Taiwan → "Other" (grey). These match the task brief exactly.
DEST_COLOURS: dict[str, str] = {
    "China":      "#A78230",  # DINZ gold
    "Korea":      "#1A5276",  # dark blue
    "Hong Kong":  "#7D6608",  # dark amber
    "Other":      "#9E9E9E",  # grey
}

# Colour palette for QIA origin chart (Panel D).
QIA_COLOURS: dict[str, str] = {
    "New Zealand":       "#1A5276",
    "China":             "#A78230",
    "Russia":            "#888888",
    "Hong Kong (SAR)":   "#7D6608",
    "Kazakhstan":        "#E8A87C",
    "Australia":         "#2C3E50",
    "Other":             "#BDBDBD",
}

# Korean → English country name map for QIA rows.
_QIA_COUNTRY_MAP: dict[str, str] = {
    "뉴질랜드":   "New Zealand",
    "중국":       "China",
    "러시아":     "Russia",
    "홍콩":       "Hong Kong (SAR)",
    "카자흐스탄": "Kazakhstan",
    "호주":       "Australia",
}


def _normalise_dest_country(raw: str) -> str:
    """
    Collapse countries not in _DEST_COUNTRIES into 'Other'.

    Used for NZ export destination charts (Panel C).
    """
    return raw if raw in _DEST_COUNTRIES else "Other"


def _normalise_qia_country(raw: str) -> str:
    """
    Map Korean QIA country names to English display names.

    Unknown values are preserved as-is (kept rather than silently dropped).
    """
    return _QIA_COUNTRY_MAP.get(raw.strip(), raw.strip())


def _aggregate_nz_by_destination(nz_rows: list[dict]) -> dict[str, dict[str, float]]:
    """
    Aggregate NZ export NZD FOB value rows by destination country and date.

    Countries not in _DEST_COUNTRIES are collapsed into "Other".
    Returns {country: {date_str: nzd_value}} with all dates sorted ascending.

    C-3h Panel C1: stacked area chart data.
    """
    result: dict[str, dict[str, float]] = {c: {} for c in _DEST_COUNTRIES + ["Other"]}

    for row in nz_rows:
        if str(row.get("unit", "")) != "NZD":
            continue
        # R-4: normalise date so "2022-2" → "2022-02" before aggregation,
        # preventing unpadded x-axis labels in the stacked area chart.
        date_str = _normalise_date_str(str(row.get("date", "")))
        if not date_str:
            continue
        try:
            val = float(row.get("value", 0) or 0)
        except (ValueError, TypeError):
            continue

        raw_country = str(row.get("country", ""))
        country = _normalise_dest_country(raw_country)
        result[country][date_str] = result[country].get(date_str, 0.0) + val

    # Sort each country's date dict ascending.
    return {c: dict(sorted(v.items())) for c, v in result.items()}


def _latest_full_year_kg_by_destination(nz_rows: list[dict]) -> dict:
    """
    Find the most recent calendar year where at least 10 months of KG data exist,
    then sum KG by destination for that year.

    Countries not in _DEST_COUNTRIES are collapsed to "Other".
    Returns {"year": YYYY, "China": kg, "Korea": kg, "Hong Kong": kg, "Other": kg}.

    C-3h Panel C2: pie chart data.
    """
    kg_rows = [r for r in nz_rows if str(r.get("unit", "")) == "KG"]

    # Gather all months present per year (across all countries combined).
    months_per_year: dict[str, set] = defaultdict(set)
    for row in kg_rows:
        date_str = str(row.get("date", ""))
        if date_str and len(date_str) >= 7:
            months_per_year[date_str[:4]].add(date_str[5:7])

    # Latest full year: most recent year with >=10 months.
    full_years = sorted(
        [yr for yr, months in months_per_year.items() if len(months) >= 10],
        reverse=True,
    )
    if not full_years:
        return {"year": None}

    latest_year = full_years[0]

    # Sum KG by destination for that year.
    kg_by_dest: dict[str, float] = {c: 0.0 for c in _DEST_COUNTRIES + ["Other"]}
    for row in kg_rows:
        date_str = str(row.get("date", ""))
        if not date_str or date_str[:4] != latest_year:
            continue
        try:
            val = float(row.get("value", 0) or 0)
        except (ValueError, TypeError):
            continue
        raw_country = str(row.get("country", ""))
        country = _normalise_dest_country(raw_country)
        kg_by_dest[country] = kg_by_dest.get(country, 0.0) + val

    kg_by_dest["year"] = int(latest_year)
    return kg_by_dest


def _qia_monthly_by_country(qia_rows: list[dict]) -> dict:
    """
    P2-H: Aggregate QIA quarantine rows by month and country for a monthly
    time-series view.

    Returns {
      "labels": [YYYY-MM, ...],             # sorted, all unique months
      "countries": [country_en, ...],       # sorted by total kg desc
      "series": {country_en: [kg, ...]},    # parallel to labels
    }

    Country names normalised via _normalise_qia_country(). Countries outside
    QIA_COLOURS mapped to "Other".
    Returns {"labels": [], "countries": [], "series": {}} if no data.
    """
    kg_rows = [r for r in qia_rows if str(r.get("unit", "")) == "KG"]
    if not kg_rows:
        return {"labels": [], "countries": [], "series": {}}

    # Aggregate by (month_str, country_en).
    by_month_country: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for row in kg_rows:
        date_str = str(row.get("date", ""))
        if not date_str or len(date_str) < 7:
            continue
        month_str = date_str[:7]  # YYYY-MM
        raw_country = str(row.get("country", ""))
        country_en = _normalise_qia_country(raw_country)
        if country_en not in QIA_COLOURS:
            country_en = "Other"
        try:
            val = float(row.get("value", 0) or 0)
        except (ValueError, TypeError):
            continue
        by_month_country[month_str][country_en] += val

    if not by_month_country:
        return {"labels": [], "countries": [], "series": {}}

    labels = sorted(by_month_country.keys())

    # Determine country order by total kg descending.
    totals: dict[str, float] = defaultdict(float)
    for month_data in by_month_country.values():
        for c, kg in month_data.items():
            totals[c] += kg
    countries = sorted(totals.keys(), key=lambda c: totals[c], reverse=True)

    series: dict[str, list[float]] = {}
    for c in countries:
        series[c] = [round(by_month_country[m].get(c, 0.0), 2) for m in labels]

    return {"labels": labels, "countries": countries, "series": series}


def _qia_annual_by_country(qia_rows: list[dict], n_years: int = 99) -> list[dict]:
    """
    Find the n most recent calendar years where at least 10 months of KG data
    exist in the QIA rows, then return annual KG totals by (English) country name.

    Returns [{"year": YYYY, "countries": {country_en: total_kg}}, ...] sorted
    ascending by year.

    Country names are normalised via _normalise_qia_country() (Korean → English).
    Countries not in QIA_COLOURS keys are placed under "Other".

    C-3h Panel D: stacked bar chart data.
    C-6g G4-B: default n_years changed to 99 — include all available complete years.
    """
    kg_rows = [r for r in qia_rows if str(r.get("unit", "")) == "KG"]

    if not kg_rows:
        return []

    # Gather months present per year.
    months_per_year: dict[str, set] = defaultdict(set)
    for row in kg_rows:
        date_str = str(row.get("date", ""))
        if date_str and len(date_str) >= 7:
            months_per_year[date_str[:4]].add(date_str[5:7])

    # Select n most recent years with >=10 months.
    full_years = sorted(
        [yr for yr, months in months_per_year.items() if len(months) >= 10],
        reverse=True,
    )[:n_years]
    full_years = sorted(full_years)  # ascending for chart

    if not full_years:
        return []

    result = []
    for yr in full_years:
        kg_by_country: dict[str, float] = {}
        for row in kg_rows:
            date_str = str(row.get("date", ""))
            if not date_str or date_str[:4] != yr:
                continue
            try:
                val = float(row.get("value", 0) or 0)
            except (ValueError, TypeError):
                continue
            raw_country = str(row.get("country", ""))
            country_en = _normalise_qia_country(raw_country)
            # Collapse unknown countries to "Other".
            if country_en not in QIA_COLOURS:
                country_en = "Other"
            kg_by_country[country_en] = kg_by_country.get(country_en, 0.0) + val

        result.append({"year": int(yr), "countries": kg_by_country})

    return result


def _build_mfds_annual_series(all_trade_rows: list[dict]) -> list[dict]:
    """
    P1-G: Build the MFDS annual import-value chart dataset from VTW_Trade_Monthly.

    Filters rows where series == 'mfds_annual' and unit == 'USD_thousands'.
    Returns list of {x: year_str, y: value_usd_thousands} sorted ascending,
    ready for Chart.js use.

    Also returns an accompanying compact table list [{year, value_usd_thousands}].

    The function returns a dict:
      {
        "chart_points": [{x, y}, ...],    # Chart.js xy pairs
        "table_rows":   [{year, usd_k}],  # compact table rows
        "has_data": bool,
      }
    """
    mfds_rows = [
        r for r in all_trade_rows
        if str(r.get("series", "")).strip() == "mfds_annual"
        and str(r.get("unit", "")).strip() == "USD_thousands"
    ]

    if not mfds_rows:
        return {"chart_points": [], "table_rows": [], "has_data": False}

    # Aggregate by year (4-digit string).
    # Dates stored as 'YYYY-MM' (e.g. '2004-01') — extract first 4 characters
    # so x-axis labels show clean '2004', '2005', ... not '2004-01' etc.
    by_year: dict[str, float] = {}
    for row in mfds_rows:
        raw_date = str(row.get("date", "")).strip()
        if not raw_date or len(raw_date) < 4:
            continue
        year_str = raw_date[:4]  # '2004-01' → '2004'
        try:
            val = float(row.get("value", 0) or 0)
        except (ValueError, TypeError):
            continue
        by_year[year_str] = by_year.get(year_str, 0.0) + val

    sorted_years = sorted(by_year.keys())
    chart_points = [{"x": yr, "y": round(by_year[yr], 1)} for yr in sorted_years]
    table_rows = [{"year": yr, "usd_k": round(by_year[yr], 1)} for yr in sorted_years]

    return {
        "chart_points": chart_points,
        "table_rows": table_rows,
        "has_data": len(chart_points) > 0,
    }


def prepare_chart_data(sections: dict, tab_data: dict | None = None) -> dict:
    """
    Build pre-aggregated chart datasets for the trade_flows section and
    the B-7 import intelligence price chart.

    C-3e additions:
      - dried_eq_kg per source (0.33 conversion applied per product_type)
      - rolling_12m_dried_eq_kg per source
      - yoy_pct per source (rolling YoY % change)
      - unit_price_nzd_per_dried_eq_kg (NZ monthly blended)
      - unit_price_usd_per_dried_eq_kg (KSTAT monthly blended)
      - harvest_boundaries: list of "YYYY-10" dates in the 24-month window
      - yoy_chip: section-level KPI chip data (primary source = NZ exports)
      - source objects: nz_export, korea_qia, korea_kstat (each with monthly/rolling arrays)
      - window: {"start": "YYYY-MM", "end": "YYYY-MM"} for the 24-month display range

    C-3h additions:
      - tf_destination_area: {country: [{x, y}, ...]} — NZD FOB by destination, all dates
      - tf_destination_pie: {year, China, Korea, Hong Kong, Other} — KG by dest, latest year
      - tf_qia_by_origin: [{year, countries: {country_en: kg}}, ...] — QIA annual by origin

    """
    tf = sections.get("trade_flows", {})
    all_data = tf.get("data", [])

    nz_rows_raw = [r for r in all_data if r.get("series") == "nz_export"]
    qia_rows_raw = [r for r in all_data if r.get("series") == "korea_quarantine"]
    kstat_all_rows_raw = [r for r in all_data if r.get("series") == "kstat_api"]

    # GAP-5 back-fill: resolve empty product_type for rows ingested before C-3e/C-6.
    # This is a read-time patch — the Sheets layer is not modified.
    nz_rows = _backfill_product_type(nz_rows_raw)
    qia_rows = _backfill_product_type(qia_rows_raw)
    kstat_all_rows = _backfill_product_type(kstat_all_rows_raw)

    # Legacy split kept for backward compat.
    kstat_0507_rows = tf.get("kstat_0507", [])
    kstat_0510_rows = tf.get("kstat_0510", [])

    def to_xy(totals: dict[str, float]) -> list[dict]:
        return [{"x": k, "y": round(v, 2)} for k, v in totals.items()]

    def align_to_window(data: dict, start: str, end: str) -> list[dict]:
        """Return xy pairs for all months in window, filling missing months with 0."""
        result = []
        if not start or not end:
            return to_xy(data)
        year, month = int(start[:4]), int(start[5:7])
        end_year, end_month = int(end[:4]), int(end[5:7])
        while (year, month) <= (end_year, end_month):
            d = f"{year:04d}-{month:02d}"
            result.append({"x": d, "y": round(data.get(d, 0.0), 2)})
            month += 1
            if month > 12:
                month = 1
                year += 1
        return result

    # ── Raw KG aggregations (legacy + new) ────────────────────────────────────
    nz_kg_raw = _aggregate_series_by_date(nz_rows, "KG")
    qia_kg_raw = _aggregate_series_by_date(qia_rows, "KG")
    kstat_kg_raw = _aggregate_series_by_date(kstat_all_rows, "KG")

    # ── Dried-equivalent KG ───────────────────────────────────────────────────
    nz_dried_eq = _compute_dried_eq_kg(nz_rows, "KG")
    qia_dried_eq = _compute_dried_eq_kg(qia_rows, "KG")
    kstat_dried_eq = _compute_dried_eq_kg(kstat_all_rows, "KG")

    # ── Rolling 12-month dried-eq KG ─────────────────────────────────────────
    nz_rolling = _compute_rolling_12m(nz_dried_eq)
    qia_rolling = _compute_rolling_12m(qia_dried_eq)
    kstat_rolling = _compute_rolling_12m(kstat_dried_eq)

    # ── YoY % per rolling series ──────────────────────────────────────────────
    nz_yoy = _compute_yoy_pct(nz_rolling)
    kstat_yoy = _compute_yoy_pct(kstat_rolling)

    # ── Unit prices ───────────────────────────────────────────────────────────
    nz_unit_price = _compute_unit_price(nz_rows, "NZD", value_multiplier=1.0)
    kstat_unit_price = _compute_unit_price(kstat_all_rows, "USD_thousands", value_multiplier=1000.0)

    # ── 24-month display window ───────────────────────────────────────────────
    all_dates = (
        list(nz_kg_raw.keys())
        + list(qia_kg_raw.keys())
        + list(kstat_kg_raw.keys())
    )
    win_start, win_end = _last_24_months_window(all_dates)

    # ── Harvest boundaries ────────────────────────────────────────────────────
    harvest_boundaries = _harvest_boundaries(win_start, win_end)

    # ── YoY KPI chip (primary source = NZ exports) ───────────────────────────
    yoy_chip = _compute_yoy_chip(nz_rolling)

    # ── Source objects (windowed to 24 months) ────────────────────────────────
    nz_source = {
        "monthly_kg":            align_to_window(nz_kg_raw,   win_start, win_end),
        "monthly_dried_eq_kg":   align_to_window(nz_dried_eq, win_start, win_end),
        "rolling_12m_dried_eq_kg": align_to_window(nz_rolling, win_start, win_end),
        "unit_price":            align_to_window(nz_unit_price, win_start, win_end),
        "yoy_pct":               {d: nz_yoy.get(d) for d in nz_rolling if win_start <= d <= win_end},
        "labels":                [p["x"] for p in align_to_window(nz_kg_raw, win_start, win_end)],
    }

    qia_source = {
        "monthly_kg":            align_to_window(qia_kg_raw,   win_start, win_end),
        "monthly_dried_eq_kg":   align_to_window(qia_dried_eq, win_start, win_end),
        "rolling_12m_dried_eq_kg": align_to_window(qia_rolling, win_start, win_end),
        "yoy_pct":               {},
        "labels":                [p["x"] for p in align_to_window(qia_kg_raw, win_start, win_end)],
    }

    kstat_source = {
        "monthly_kg":            align_to_window(kstat_kg_raw,   win_start, win_end),
        "monthly_dried_eq_kg":   align_to_window(kstat_dried_eq, win_start, win_end),
        "rolling_12m_dried_eq_kg": align_to_window(kstat_rolling, win_start, win_end),
        "unit_price":            align_to_window(kstat_unit_price, win_start, win_end),
        "yoy_pct":               {d: kstat_yoy.get(d) for d in kstat_rolling if win_start <= d <= win_end},
        "labels":                [p["x"] for p in align_to_window(kstat_kg_raw, win_start, win_end)],
    }

    # ── C-3h: destination breakdown charts ───────────────────────────────────
    # Panel C: NZ exports by destination (stacked area + pie).
    # Panel D: QIA Korea quarantine imports by origin (annual grouped bar).

    # Panel C1 — stacked area: FOB NZD by destination, all available dates.
    dest_by_country = _aggregate_nz_by_destination(nz_rows)
    tf_destination_area: dict[str, list] = {}
    for country, by_date in dest_by_country.items():
        tf_destination_area[country] = [
            {"x": d, "y": round(v, 0)} for d, v in by_date.items()
        ]

    # Panel C2 — pie: KG by destination, latest full year.
    tf_destination_pie = _latest_full_year_kg_by_destination(nz_rows)

    # Panel D — stacked bar: QIA annual KG by origin, all available complete years.
    # C-6g G4-B: n_years=2 → all available years (eliminates stair-step appearance).
    tf_qia_by_origin = _qia_annual_by_country(qia_rows, n_years=99)

    # Panel D2 (P2-H) — monthly time-series: QIA KG by origin, all available months.
    tf_qia_monthly_by_origin = _qia_monthly_by_country(qia_rows)

    # ── B-7 price chart data ──────────────────────────────────────────────────
    ii = sections.get("import_intelligence", {})
    price_rows = ii.get("price_annual_rows", [])
    b7_price_series = _build_b7_price_series(price_rows)
    b7_price_subtitle = _build_b7_price_subtitle(price_rows)

    # ── P1-G: MFDS annual import-value series from VTW_Trade_Monthly ──────────
    # Series name = 'mfds_annual'; unit = 'USD_thousands'; date = 4-digit year.
    # Exists in VTW_Trade_Monthly (24 rows, 2004–2024) — NOT in VFI_Price_Annual.
    # Must read unfiltered tab rows: the assemble_sections step filters by series_value
    # for each source block, so mfds_annual rows are not present in sections["trade_flows"].
    vtw_monthly_all = (tab_data or {}).get("VTW_Trade_Monthly", []) if tab_data else all_data
    mfds_annual_series = _build_mfds_annual_series(vtw_monthly_all)

    # ── C-8 A1/A2 toggle raw rows ─────────────────────────────────────────────
    # QIA rows: each dict has date (YYYY-MM), country (Korean text, e.g. 뉴질랜드),
    # value (float, kg), unit (KG), product_type (dried/frozen), series.
    # NZ rows: date (YYYY-MM), country (English destination), value (float, kg),
    # unit (KG), product_type (dried/frozen), series.
    # product_type back-filled above via _backfill_product_type().
    # country already normalised to English for NZ rows via _normalise_qia_country
    # for QIA (Korean text — JS does the lookup via QIA_COUNTRY_MAP).
    # Sending only KG rows to keep payload small.
    qia_raw_for_toggle = [
        {"date": str(r.get("date", ""))[:7],
         "country": str(r.get("country", "")).strip(),
         "value": float(r.get("value", 0) or 0),
         "product_type": str(r.get("product_type", "")).strip()}
        for r in qia_rows
        if str(r.get("unit", "")) == "KG" and str(r.get("date", ""))
    ]
    nz_raw_for_toggle = [
        {"date": str(r.get("date", ""))[:7],
         "country": str(r.get("country", "")).strip(),
         "value": float(r.get("value", 0) or 0),
         "product_type": str(r.get("product_type", "")).strip()}
        for r in nz_rows
        if str(r.get("unit", "")) == "KG" and str(r.get("date", ""))
    ]

    return {
        # ── C-3e: new structured source objects ──────────────────────────────
        "nz_export":          nz_source,
        "korea_qia":          qia_source,
        "korea_kstat":        kstat_source,
        "harvest_boundaries": harvest_boundaries,
        "yoy_chip":           yoy_chip,
        "window":             {"start": win_start, "end": win_end},

        # ── B-7 price chart ───────────────────────────────────────────────────
        "b7_price_series":   b7_price_series,
        "b7_price_subtitle": b7_price_subtitle,

        # ── C-3h: destination breakdown charts ───────────────────────────────
        "tf_destination_area": tf_destination_area,   # Panel C1: stacked area
        "tf_destination_pie":  tf_destination_pie,    # Panel C2: pie (latest year KG)
        "tf_qia_by_origin":    tf_qia_by_origin,      # Panel D: QIA annual by origin

        # ── P1-G: MFDS annual import-value series (from VTW_Trade_Monthly) ──
        "mfds_annual_series": mfds_annual_series,

        # ── P2-H: QIA monthly origin view ────────────────────────────────────
        "tf_qia_monthly_by_origin": tf_qia_monthly_by_origin,

        # ── C-8: raw rows for A1/A2 toggle charts ────────────────────────────
        "qia_raw_rows": qia_raw_for_toggle,
        "nz_raw_rows":  nz_raw_for_toggle,
    }


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
        rows_out = []
        for row in all_records:
            out = dict(row)
            out["importer"] = row.get("importer") or row.get("importer_ko") or ""
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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    build_date = _today_kst().isoformat()

    print("build.py — VKH build pipeline")

    # --- Step 1: Load config --------------------------------------------------
    config = load_config()
    sources = config.get("sources", [])
    enabled_sources = [s for s in sources if s.get("enabled", False)]
    print(f"  config: {len(sources)} sources, {len(enabled_sources)} enabled")

    # --- Step 2: Connect to Sheets --------------------------------------------
    _gc, sheet = connect_sheets(config)
    sheet_id = config.get("sheet_id", "")
    print(f"  sheet: {sheet.title} ({sheet_id})")

    # --- Step 3: Load all tabs (single pass — L-4) ----------------------------
    tab_data = load_all_tabs(sheet, config)

    # --- Step 4: Assemble section dicts ---------------------------------------
    sections = assemble_sections(config, tab_data)

    # --- Step 5: Compute KPIs -------------------------------------------------
    kpi = compute_kpis(sections)

    # --- Step 5b: Prepare chart datasets (pre-aggregated for Jinja2) ----------
    # Pass tab_data so mfds_annual series can be read from the unfiltered tab.
    chart_data = prepare_chart_data(sections, tab_data=tab_data)

    # --- Step 5c: Write trade_flows CSV download ------------------------------
    _write_trade_flows_csv(sections)

    # --- Step 5d: Write import_intelligence CSV download ----------------------
    _write_import_intelligence_csv(sections)

    # --- Step 5e: Write news_pulse CSV download -------------------------------
    _write_news_pulse_csv(sections)

    # --- Step 6: Render -------------------------------------------------------
    bytes_written = render(config, sections, kpi, chart_data, build_date)

    # --- Step 7: Copy static assets to docs/assets/ (GitHub Pages serves from docs/) ---
    src_assets = REPO_ROOT / "assets"
    dst_assets = REPO_ROOT / "docs" / "assets"
    if src_assets.is_dir():
        if dst_assets.exists():
            shutil.rmtree(dst_assets)
        shutil.copytree(src_assets, dst_assets)
        print(f"  assets: copied {len(list(dst_assets.iterdir()))} files to docs/assets/")

    # --- Console output -------------------------------------------------------
    print("  sections rendered:")
    for section_id in SECTION_SOURCE_MAP:
        sec = sections.get(section_id, {})
        enabled = sec.get("enabled", False)
        rows = len(sec.get("data", []))
        if not enabled:
            print(f"    {section_id:<30}: disabled — placeholder")
        elif rows == 0:
            print(f"    {section_id:<30}: enabled, 0 rows — placeholder")
        else:
            print(f"    {section_id:<30}: enabled, {rows} rows")

    nz = kpi.get("nz_export_latest", "—")
    qia_rolling = kpi.get("qia_rolling12m_kg", "—")
    art = kpi.get("articles_90d", "—")
    food_90d = kpi.get("food_imports_90d", "—")
    print(f"  kpis: nz_export_rolling12m_tonnes={nz} | qia_rolling12m_kg={qia_rolling} | articles_90d={art} | food_imports_90d={food_90d}")
    tf = sections.get("trade_flows", {})
    kstat_0507 = len(tf.get("kstat_0507", []))
    kstat_0510 = len(tf.get("kstat_0510", []))
    print(f"  trade_flows kstat split: 0507.90={kstat_0507} rows | 0510.00={kstat_0510} rows")
    # C-3e: report new data structure stats.
    cd = chart_data
    win = cd.get("window", {})
    chip = cd.get("yoy_chip", {})
    nz_pts = len(cd.get("nz_export", {}).get("monthly_dried_eq_kg", []))
    kstat_pts = len(cd.get("korea_kstat", {}).get("monthly_dried_eq_kg", []))
    qia_pts = len(cd.get("korea_qia", {}).get("monthly_dried_eq_kg", []))
    hb = len(cd.get("harvest_boundaries", []))
    print(f"  window: {win.get('start', '?')} → {win.get('end', '?')}")
    print(f"  harvest_boundaries: {hb} October months marked")
    print(f"  yoy_chip: {chip.get('label', '—')}")
    print(f"  source pts (dried-eq KG): NZ={nz_pts} QIA={qia_pts} KSTAT={kstat_pts}")
    # C-3h: destination chart stats.
    dest_area = cd.get("tf_destination_area", {})
    dest_pie = cd.get("tf_destination_pie", {})
    qia_origin = cd.get("tf_qia_by_origin", [])
    dest_countries = [c for c in dest_area if dest_area[c]]
    print(f"  C-3h dest area countries: {dest_countries}")
    print(f"  C-3h dest pie year: {dest_pie.get('year', '—')}")
    print(f"  C-3h QIA origin years: {[r['year'] for r in qia_origin]}")
    print(f"  output: docs/index.html ({bytes_written} bytes)")
    print(f"  build complete: {build_date}")


if __name__ == "__main__":
    main()
