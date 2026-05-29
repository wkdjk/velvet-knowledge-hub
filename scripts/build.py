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
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import gspread
import yaml
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from jinja2 import Environment, FileSystemLoader

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent
CONFIG_PATH = REPO_ROOT / "config.yaml"
TEMPLATE_DIR = REPO_ROOT / "templates"
OUTPUT_PATH = REPO_ROOT / "docs" / "index.html"
DOWNLOADS_DIR = REPO_ROOT / "docs" / "downloads"
TRADE_FLOWS_CSV = DOWNLOADS_DIR / "trade_flows.csv"
NEWS_PULSE_CSV = DOWNLOADS_DIR / "news_pulse.csv"

# Column order for the trade_flows CSV export.
_TRADE_FLOWS_CSV_HEADERS = [
    "date", "series", "hs_code", "hs_label", "value", "unit", "country", "notes",
]

# Column order for the news_pulse CSV export.
# Matches KVN_Articles tab schema (from classify_articles.py).
_NEWS_PULSE_CSV_HEADERS = [
    "content_hash", "url", "title_ko", "description", "published_date",
    "source_name", "source_type", "keyword_matched",
    "category", "english_summary", "ai_processed_at", "include_on_site",
]

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

# Maps config section identifiers to the list of source IDs they aggregate.
SECTION_SOURCE_MAP = {
    "trade_flows": ["nz_export", "korea_quarantine", "kstat_api"],
    "import_intelligence": ["vfi_import_records", "vfi_price_annual"],
    "market_presence": ["market_presence"],
    "news_pulse": ["kvn_articles"],
}


# ---------------------------------------------------------------------------
# Step 1 — Config loading
# ---------------------------------------------------------------------------

def load_config(path: Path = CONFIG_PATH) -> dict:
    """Read config.yaml from repo root and return the full config dict."""
    if not path.exists():
        print(f"ERROR: config.yaml not found at {path}", file=sys.stderr)
        sys.exit(1)
    with path.open("r", encoding="utf-8") as fh:
        try:
            return yaml.safe_load(fh)
        except yaml.YAMLError as exc:
            print(f"ERROR: Failed to parse config.yaml — {exc}", file=sys.stderr)
            sys.exit(1)


# ---------------------------------------------------------------------------
# Step 2 — Sheets connection
# ---------------------------------------------------------------------------

def connect_sheets(config: dict):
    """
    Load credentials from environment, connect to Google Sheets.

    Returns (gspread.Client, gspread.Spreadsheet) tuple.
    Calls sys.exit(1) on missing env vars or missing sheet_id.
    """
    # L-2: load_dotenv() searches from repo root (cwd when running from repo root)
    load_dotenv(REPO_ROOT / ".env")

    sheet_id = config.get("sheet_id", "").strip()
    if not sheet_id:
        print(
            "ERROR: sheet_id is empty in config.yaml. "
            "Run A-3 setup_sheets.py first and record the sheet ID.",
            file=sys.stderr,
        )
        sys.exit(1)

    sa_json_raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not sa_json_raw:
        print(
            "ERROR: GOOGLE_SERVICE_ACCOUNT_JSON environment variable is not set.\n"
            "  Local dev: add it to .env at the repo root (single-line JSON — L-3).\n"
            "  GitHub Actions: add it to repository Secrets.",
            file=sys.stderr,
        )
        sys.exit(1)

    # L-3: JSON must be on a single line in .env; json.loads() handles it correctly.
    try:
        sa_info = json.loads(sa_json_raw)
    except json.JSONDecodeError as exc:
        print(
            f"ERROR: GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON — {exc}\n"
            "  Tip: minify to one line with: "
            'python -c "import json,sys; print(json.dumps(json.load(sys.stdin), '
            'separators=(\',\',\':\')))" < key.json',
            file=sys.stderr,
        )
        sys.exit(1)

    creds = Credentials.from_service_account_info(sa_info, scopes=SCOPES)
    gc = gspread.authorize(creds)

    try:
        sheet = gc.open_by_key(sheet_id)
    except gspread.exceptions.APIError as exc:
        print(
            f"ERROR: Could not open sheet {sheet_id} — {exc}\n"
            "  Check the service account has Editor access to the sheet.",
            file=sys.stderr,
        )
        sys.exit(1)

    return gc, sheet


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
        last_updated = _extract_last_updated(combined_data)

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

        sections[section_id] = section_dict

    return sections


def _extract_last_updated(rows: list[dict]) -> str | None:
    """
    Return the most recent date string found in the 'date' or 'published_date'
    columns of the given row list. Returns None if no date column exists or rows
    is empty.
    """
    if not rows:
        return None

    date_candidates: list[str] = []
    for row in rows:
        for col in ("date", "published_date"):
            val = row.get(col)
            if val:
                date_candidates.append(str(val))

    if not date_candidates:
        return None

    # Return lexicographically greatest date string (works for YYYY-MM-DD format).
    return max(date_candidates)


# ---------------------------------------------------------------------------
# Step 5 — KPI computation
# ---------------------------------------------------------------------------

def compute_kpis(sections: dict) -> dict:
    """
    Compute KPI values from assembled section data.

    Returns kpi dict:
      nz_export_latest  — latest value from nz_export data rows
      nz_export_delta   — "▲ X%" or "▼ X%" vs prior month, or "—"
      articles_30d      — count of KVN_Articles rows within last 30 days
      mfds_latest_date  — max date from VFI_Import_Records rows

    Any value that cannot be computed returns "—".
    """
    kpi: dict = {
        "nz_export_latest": "—",
        "nz_export_delta": "—",
        "articles_30d": "—",
        "mfds_latest_date": "—",
    }

    # --- KPI 1 & 2: NZ export (trade_flows section, series=nz_export) -------
    trade_data = sections.get("trade_flows", {}).get("data", [])
    nz_export_rows = [r for r in trade_data if r.get("series") == "nz_export"]

    if nz_export_rows:
        # Sort by date descending; date column assumed YYYY-MM format.
        try:
            sorted_rows = sorted(
                nz_export_rows,
                key=lambda r: str(r.get("date", "")),
                reverse=True,
            )
            latest_row = sorted_rows[0]
            latest_value = latest_row.get("value", "")
            if latest_value != "" and latest_value is not None:
                kpi["nz_export_latest"] = str(latest_value)

            if len(sorted_rows) >= 2:
                prior_row = sorted_rows[1]
                prior_value = prior_row.get("value")
                if (
                    prior_value is not None
                    and prior_value != ""
                    and float(prior_value) != 0
                ):
                    delta_pct = (
                        (float(latest_value) - float(prior_value)) / float(prior_value)
                    ) * 100
                    symbol = "▲" if delta_pct >= 0 else "▼"
                    kpi["nz_export_delta"] = f"{symbol} {abs(delta_pct):.1f}%"
        except (ValueError, TypeError, ZeroDivisionError):
            # Cannot compute delta — leave as "—".
            pass

    # --- KPI 3: Articles past 30 days ----------------------------------------
    news_data = sections.get("news_pulse", {}).get("data", [])
    if news_data:
        cutoff = date.today() - timedelta(days=30)
        count = 0
        for row in news_data:
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
        kpi["articles_30d"] = str(count)

    # --- KPI 4: Latest MFDS import date ---------------------------------------
    import_data = sections.get("import_intelligence", {}).get("data", [])
    if import_data:
        date_values = [
            str(r.get("date", ""))
            for r in import_data
            if r.get("date") and str(r.get("date", "")).strip()
        ]
        if date_values:
            kpi["mfds_latest_date"] = max(date_values)

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

    html = template.render(
        build_date=build_date,
        meta=meta,
        kpi=kpi,
        sections=sections,
        chart_data=chart_data,
        config=config,
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


def prepare_chart_data(sections: dict) -> dict:
    """
    Build pre-aggregated chart datasets for the trade_flows section.

    Returns a dict with two chart panels:
      chart_kg:    NZ export KG, Korea quarantine KG, kstat KG (by HS toggle)
      chart_value: NZ export NZD, kstat USD_thousands (by HS toggle)

    Each dataset is a list of {x: "YYYY-MM", y: float} objects for Chart.js.
    kstat datasets are split by hs_code: kstat_0507 and kstat_0510.

    All data is pre-aggregated (summed across countries) by date.
    """
    tf = sections.get("trade_flows", {})
    all_data = tf.get("data", [])

    nz_rows = [r for r in all_data if r.get("series") == "nz_export"]
    qia_rows = [r for r in all_data if r.get("series") == "korea_quarantine"]
    kstat_0507_rows = tf.get("kstat_0507", [])
    kstat_0510_rows = tf.get("kstat_0510", [])

    def to_xy(totals: dict[str, float]) -> list[dict]:
        return [{"x": k, "y": round(v, 2)} for k, v in totals.items()]

    return {
        # KG panel — common Y-axis, three series.
        "nz_export_kg": to_xy(_aggregate_series_by_date(nz_rows, "KG")),
        "qia_kg": to_xy(_aggregate_series_by_date(qia_rows, "KG")),
        "kstat_0507_kg": to_xy(_aggregate_series_by_date(kstat_0507_rows, "KG")),
        "kstat_0510_kg": to_xy(_aggregate_series_by_date(kstat_0510_rows, "KG")),
        # Value panel — NZD and USD_thousands on separate Y-axes.
        "nz_export_nzd": to_xy(_aggregate_series_by_date(nz_rows, "NZD")),
        "kstat_0507_usd": to_xy(_aggregate_series_by_date(kstat_0507_rows, "USD_thousands")),
        "kstat_0510_usd": to_xy(_aggregate_series_by_date(kstat_0510_rows, "USD_thousands")),
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
    """
    trade_data = sections.get("trade_flows", {}).get("data", [])
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

    with TRADE_FLOWS_CSV.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=_TRADE_FLOWS_CSV_HEADERS,
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(trade_data)

    print(f"  trade_flows.csv: {len(trade_data)} rows → {TRADE_FLOWS_CSV}")


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

    # Filter to include_on_site truthy — Sheets writes "TRUE" / "FALSE" strings
    # (see classify_articles.py line: include_val = "TRUE" if ... else "FALSE").
    publishable = [
        row for row in all_news
        if str(row.get("include_on_site", "")).strip().upper() in ("TRUE", "1", "YES")
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
    build_date = date.today().isoformat()

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
    chart_data = prepare_chart_data(sections)

    # --- Step 5c: Write trade_flows CSV download ------------------------------
    _write_trade_flows_csv(sections)

    # --- Step 5d: Write news_pulse CSV download -------------------------------
    _write_news_pulse_csv(sections)

    # --- Step 6: Render -------------------------------------------------------
    bytes_written = render(config, sections, kpi, chart_data, build_date)

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
    art = kpi.get("articles_30d", "—")
    mfds = kpi.get("mfds_latest_date", "—")
    print(f"  kpis: nz_export={nz} | articles_30d={art} | mfds_latest={mfds}")
    tf = sections.get("trade_flows", {})
    kstat_0507 = len(tf.get("kstat_0507", []))
    kstat_0510 = len(tf.get("kstat_0510", []))
    print(f"  trade_flows kstat split: 0507.90={kstat_0507} rows | 0510.00={kstat_0510} rows")
    print(f"  output: docs/index.html ({bytes_written} bytes)")
    print(f"  build complete: {build_date}")


if __name__ == "__main__":
    main()
