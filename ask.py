# ask.py — Velvet Knowledge Hub local CLI companion (B-16)
#
# Reads from the VKH Google Sheet and renders a Plotly chart in the browser.
# This script is for local use only — not deployed, not part of the website build.
#
# Usage:
#   PYTHONPATH=. python ask.py "뉴질랜드 수출 트렌드를 보여줘"
#   PYTHONPATH=. python ask.py "2024 import records"
#   PYTHONPATH=. python ask.py "latest news"
#   PYTHONPATH=. python ask.py --help
#
# Auth (L-2, L-3):
#   GOOGLE_SERVICE_ACCOUNT_JSON must be set in .env at the repo root.
#   The value must be single-line minified JSON.
#
# Security: no credentials or secrets in this file. All secrets from .env only.

import argparse
import json
import os
import sys
from pathlib import Path

import gspread
import yaml
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

# ---------------------------------------------------------------------------
# Optional Plotly import — fail gracefully with install hint.
# ---------------------------------------------------------------------------
try:
    import plotly.graph_objects as go
    import plotly.express as px
    _PLOTLY_AVAILABLE = True
except ImportError:
    _PLOTLY_AVAILABLE = False

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = REPO_ROOT / "config.yaml"

# Sheets API only (L-5: no Drive API required).
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

# ---------------------------------------------------------------------------
# Keyword routing table
#
# Each entry maps a set of trigger keywords (case-insensitive) to a handler
# name. The first matching handler wins (order matters — more specific first).
# ---------------------------------------------------------------------------

_ROUTES: list[tuple[frozenset[str], str]] = [
    # Trade flow queries — VTW_Trade_Monthly
    (frozenset(["nz", "뉴질랜드", "export", "수출", "new zealand", "stats nz", "trade", "거래"]),
     "trade_flow"),
    # Import record queries — VFI_Import_Records
    (frozenset(["import", "수입", "mfds", "record", "기록", "food", "식품", "importer", "수입업체"]),
     "import_records"),
    # News queries — KVN_Articles
    (frozenset(["news", "뉴스", "article", "기사", "latest", "최신", "naver", "네이버", "press", "언론"]),
     "news_summary"),
]

# ---------------------------------------------------------------------------
# Example questions shown in --help and error output
# ---------------------------------------------------------------------------

EXAMPLE_QUESTIONS = [
    '뉴질랜드 수출 트렌드를 보여줘',
    'NZ export trend by month',
    '2024 import records',
    '수입 기록 보여줘',
    'latest news articles',
    '최신 뉴스',
]

# ---------------------------------------------------------------------------
# Sheets connection helpers (mirrors connect_sheets() in ingest_nz_export.py)
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    """Read config.yaml from repo root."""
    if not CONFIG_PATH.exists():
        return {}
    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except yaml.YAMLError:
        return {}


def _resolve_sheet_id(config: dict) -> str:
    """
    Resolve sheet ID from env var then config.yaml fallback.

    Priority: VKH_SHEET_ID env var → config.yaml sheet_id.
    """
    sheet_id = os.environ.get("VKH_SHEET_ID", "").strip()
    if sheet_id:
        return sheet_id
    sheet_id = config.get("sheet_id", "").strip()
    if sheet_id:
        return sheet_id
    print(
        "ERROR: Sheet ID not found.\n"
        "  Set VKH_SHEET_ID in .env, or ensure sheet_id is set in config.yaml.",
        file=sys.stderr,
    )
    sys.exit(1)


def connect_sheets(sheet_id: str):
    """
    Connect to Google Sheets using service account credentials from .env.

    L-2: .env loaded from repo root.
    L-3: GOOGLE_SERVICE_ACCOUNT_JSON must be single-line JSON.
    Returns a gspread.Spreadsheet object.
    """
    load_dotenv(REPO_ROOT / ".env")

    sa_json_raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not sa_json_raw:
        print(
            "ERROR: GOOGLE_SERVICE_ACCOUNT_JSON environment variable is not set.\n"
            "  Add it to .env at the repo root (single-line JSON — see L-3 in lessons).\n"
            "  Minify: python -c \"import json,sys; "
            "print(json.dumps(json.load(sys.stdin), separators=(',',':')))\" < key.json",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        sa_info = json.loads(sa_json_raw)
    except json.JSONDecodeError as exc:
        print(
            f"ERROR: GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON — {exc}\n"
            "  Ensure the value is single-line minified JSON (L-3).",
            file=sys.stderr,
        )
        sys.exit(1)

    creds = Credentials.from_service_account_info(sa_info, scopes=SCOPES)
    gc = gspread.authorize(creds)

    try:
        return gc.open_by_key(sheet_id)
    except gspread.exceptions.APIError as exc:
        print(
            f"ERROR: Could not open sheet {sheet_id} — {exc}\n"
            "  Check the service account has Viewer access to the sheet.",
            file=sys.stderr,
        )
        sys.exit(1)


def _read_tab(spreadsheet, tab_name: str) -> list[dict]:
    """
    Read all records from a named tab. Returns [] if tab is missing or empty.

    L-4: get_all_records() called once — never in a loop.
    """
    try:
        ws = spreadsheet.worksheet(tab_name)
        return ws.get_all_records()
    except gspread.exceptions.WorksheetNotFound:
        print(f"WARNING: tab '{tab_name}' not found — skipping.", file=sys.stderr)
        return []

# ---------------------------------------------------------------------------
# Keyword router
# ---------------------------------------------------------------------------

def _route(question: str) -> str | None:
    """
    Match a natural-language question to a handler name.

    Returns the handler name string or None if no keywords match.
    """
    q_lower = question.lower()
    tokens = set(q_lower.split())

    for keywords, handler in _ROUTES:
        # Match if any keyword appears as a substring of the question
        # (covers multi-word phrases like "new zealand").
        for kw in keywords:
            if kw in q_lower:
                return handler

    return None

# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _handle_trade_flow(spreadsheet, config: dict) -> None:
    """
    Handler: trade_flow
    Tab: VTW_Trade_Monthly
    Chart: line chart of value over time, one line per series.
    Filters to unit=KG rows to show volume.
    """
    tab = "VTW_Trade_Monthly"
    print(f"Reading {tab}...")
    rows = _read_tab(spreadsheet, tab)

    if not rows:
        print("No trade flow data available.")
        return

    # Filter to KG (volume) rows only for a clean chart.
    kg_rows = [r for r in rows if str(r.get("unit", "")).upper() == "KG"]

    if not kg_rows:
        print("No KG-unit rows found in trade data.")
        return

    # Group by series → sorted list of (date, value).
    series_data: dict[str, dict[str, float]] = {}
    for r in kg_rows:
        series = str(r.get("series", "unknown"))
        date = str(r.get("date", ""))
        try:
            value = float(r.get("value", 0) or 0)
        except (ValueError, TypeError):
            value = 0.0
        if series not in series_data:
            series_data[series] = {}
        series_data[series][date] = series_data[series].get(date, 0.0) + value

    if not _PLOTLY_AVAILABLE:
        print("Plotly is not installed. Install with: pip install plotly")
        print(f"\nSeries found: {list(series_data.keys())}")
        print(f"Total KG rows: {len(kg_rows)}")
        return

    fig = go.Figure()
    for series_name, date_map in sorted(series_data.items()):
        dates = sorted(date_map.keys())
        values = [date_map[d] for d in dates]
        fig.add_trace(go.Scatter(
            x=dates,
            y=values,
            mode="lines+markers",
            name=series_name,
        ))

    fig.update_layout(
        title="NZ deer velvet trade — monthly volume (KG)",
        xaxis_title="Month",
        yaxis_title="Volume (KG)",
        legend_title="Series",
        hovermode="x unified",
        template="plotly_white",
    )

    total_series = len(series_data)
    total_rows = len(kg_rows)
    print(f"  series: {total_series} | rows plotted: {total_rows}")
    fig.show()


def _handle_import_records(spreadsheet, config: dict) -> None:
    """
    Handler: import_records
    Tab: VFI_Import_Records
    Chart: bar chart — top 20 importers by total quantity.
    """
    tab = "VFI_Import_Records"
    print(f"Reading {tab}...")
    rows = _read_tab(spreadsheet, tab)

    if not rows:
        print("No import records available.")
        return

    # Aggregate quantity by importer.
    importer_totals: dict[str, float] = {}
    for r in rows:
        importer = str(r.get("importer", r.get("importer_ko", "Unknown")) or "Unknown")
        try:
            qty = float(r.get("quantity_kg", 0) or r.get("quantity", 0) or 0)
        except (ValueError, TypeError):
            qty = 0.0
        importer_totals[importer] = importer_totals.get(importer, 0.0) + qty

    # Sort descending and take top 20.
    top_importers = sorted(importer_totals.items(), key=lambda x: x[1], reverse=True)[:20]

    if not _PLOTLY_AVAILABLE:
        print("Plotly is not installed. Install with: pip install plotly")
        print(f"\nTotal import records: {len(rows)}")
        print(f"Unique importers: {len(importer_totals)}")
        print("Top 5 importers by volume:")
        for name, qty in top_importers[:5]:
            print(f"  {name}: {qty:.1f} KG")
        return

    names = [t[0] for t in top_importers]
    values = [t[1] for t in top_importers]

    fig = px.bar(
        x=values,
        y=names,
        orientation="h",
        title="Top 20 importers — deer velvet (total KG)",
        labels={"x": "Total quantity (KG)", "y": "Importer"},
        template="plotly_white",
        color=values,
        color_continuous_scale="Blues",
    )
    fig.update_layout(
        yaxis={"autorange": "reversed"},
        coloraxis_showscale=False,
    )

    print(f"  total records: {len(rows)} | importers shown: {len(top_importers)}")
    fig.show()


def _handle_news_summary(spreadsheet, config: dict) -> None:
    """
    Handler: news_summary
    Tab: KVN_Articles
    Output: console table of the 20 most recent articles (no chart needed).
    Also renders a Plotly table if Plotly is available.
    """
    tab = "KVN_Articles"
    print(f"Reading {tab}...")
    rows = _read_tab(spreadsheet, tab)

    if not rows:
        print("No news articles available.")
        return

    # Sort by date descending, take top 20.
    def _sort_key(r):
        return str(r.get("published_date", r.get("date", "")) or "")

    recent = sorted(rows, key=_sort_key, reverse=True)[:20]

    # Always print a console summary (useful even without Plotly).
    print(f"\n  {len(rows)} total articles — showing 20 most recent:\n")
    header = f"{'Date':<12}  {'Category':<20}  {'Title'}"
    print(header)
    print("-" * min(len(header) + 40, 100))
    for r in recent:
        date = str(r.get("published_date", r.get("date", "—")) or "—")[:10]
        category = str(r.get("category", r.get("category_ko", "—")) or "—")[:20]
        title = str(r.get("title", r.get("title_ko", "—")) or "—")[:60]
        print(f"{date:<12}  {category:<20}  {title}")

    if not _PLOTLY_AVAILABLE:
        print("\nInstall Plotly for a browser table: pip install plotly")
        return

    # Render a Plotly table in the browser.
    dates = [str(r.get("published_date", r.get("date", "—")) or "—")[:10] for r in recent]
    categories = [str(r.get("category", r.get("category_ko", "—")) or "—") for r in recent]
    titles = [str(r.get("title", r.get("title_ko", "—")) or "—")[:80] for r in recent]
    include = [str(r.get("include_on_site", "—")) for r in recent]

    fig = go.Figure(data=[go.Table(
        header=dict(
            values=["Date", "Category", "Include on site", "Title"],
            fill_color="#1a3a5c",
            font=dict(color="white", size=13),
            align="left",
        ),
        cells=dict(
            values=[dates, categories, include, titles],
            fill_color=[["#f5f7fa" if i % 2 == 0 else "white" for i in range(len(recent))]],
            align="left",
            font=dict(size=12),
        ),
    )])

    fig.update_layout(
        title=f"KVN news articles — 20 most recent (of {len(rows)} total)",
        template="plotly_white",
        margin=dict(l=20, r=20, t=60, b=20),
    )

    fig.show()

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

_HANDLER_MAP = {
    "trade_flow":      _handle_trade_flow,
    "import_records":  _handle_import_records,
    "news_summary":    _handle_news_summary,
}


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="ask.py",
        description=(
            "Velvet Knowledge Hub — local CLI companion.\n"
            "Ask a question in English or Korean. "
            "Reads from Google Sheets and renders a Plotly chart in the browser.\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Example questions:\n"
            + "\n".join(f"  python ask.py \"{q}\"" for q in EXAMPLE_QUESTIONS)
            + "\n\nAuth: set GOOGLE_SERVICE_ACCOUNT_JSON in .env at the repo root."
        ),
    )
    parser.add_argument(
        "question",
        nargs="?",
        help="Natural language question (English or Korean).",
    )
    parser.add_argument(
        "--list-tabs",
        action="store_true",
        help="List all tabs in the Google Sheet and exit.",
    )

    args = parser.parse_args()

    # Load .env before any env access (L-2).
    load_dotenv(REPO_ROOT / ".env")

    # --list-tabs: connect and print tab names.
    if args.list_tabs:
        config = _load_config()
        sheet_id = _resolve_sheet_id(config)
        spreadsheet = connect_sheets(sheet_id)
        tabs = [ws.title for ws in spreadsheet.worksheets()]
        print(f"Tabs in sheet {sheet_id}:")
        for t in tabs:
            print(f"  {t}")
        return

    # Require a question.
    if not args.question:
        parser.print_help()
        sys.exit(0)

    question = args.question.strip()
    if not question:
        parser.print_help()
        sys.exit(0)

    # Route the question.
    handler_name = _route(question)

    if handler_name is None:
        print(f"Sorry, I could not understand: \"{question}\"")
        print()
        print("Try one of these questions:")
        for q in EXAMPLE_QUESTIONS:
            print(f"  python ask.py \"{q}\"")
        print()
        print("Keywords recognised:")
        print("  Trade flows  — trade, export, import, 수출, 뉴질랜드, nz, new zealand")
        print("  Import intel — import, 수입, mfds, record, food, importer")
        print("  News         — news, 뉴스, article, 기사, latest, naver, press")
        sys.exit(1)

    # Connect to Sheets.
    config = _load_config()
    sheet_id = _resolve_sheet_id(config)

    print(f"ask.py — Velvet Knowledge Hub")
    print(f"  question  : {question}")
    print(f"  handler   : {handler_name}")
    print(f"  sheet_id  : {sheet_id}")
    print()

    spreadsheet = connect_sheets(sheet_id)

    # Dispatch to handler.
    handler = _HANDLER_MAP[handler_name]
    handler(spreadsheet, config)


if __name__ == "__main__":
    main()
