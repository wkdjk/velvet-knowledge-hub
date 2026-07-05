# Import only — do not run directly. Entry point is scripts/build.py.
#
# vkh_render.py — Velvet Knowledge Hub: Jinja2 template render.
#
# C-12b split (2026-07-04): extracted from build.py. Pure move — no logic
# changed. See build.py's docstring for the full pipeline order.
#
# Security: no credentials in this file. All secrets from environment only.

import json as _json
from datetime import timedelta

from jinja2 import Environment, FileSystemLoader

from scripts.vkh_data import OUTPUT_PATH, TEMPLATE_DIR, _display_name, _today_kst

# News pulse category badges — closed 6-value enum fixed by
# classify_articles.py's _VALID_CATEGORIES (the classifier prompt only ever
# returns one of these Korean strings). Site-facing text must be English
# (CLAUDE.md language split); translate at render time only — raw Korean
# stays in the sheet/data layer untouched.
_CATEGORY_EN = {
    "규제정책": "Regulatory policy",
    "무역시장": "Trade market",
    "건강제품": "Health products",
    "수입유통": "Import & distribution",
    "업계소식": "Industry news",
    "기타": "Other",
}


def render(
    config: dict,
    sections: dict,
    kpi: dict,
    chart_data: dict,
    build_date: str,
    weekly_brief: dict | None = None,
) -> int:
    """
    Render the Jinja2 template with all collected data and write docs/index.html.
    Returns the number of bytes written.
    """
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
        display_row["_display_importer"] = _display_name(
            row.get("importer_en") or row.get("importer") or row.get("importer_ko") or "—"
        )
        display_row["_display_product"] = (
            row.get("product_en") or row.get("product_name") or "—"
        )
        display_row["_display_type"] = row.get("product_type_en") or "—"
        # Flag rows within the last 90 days for JS expand control (P1-H: 30d → 90d).
        display_row["_in_last_90d"] = display_row["_display_date"] >= cutoff_90d
        import_records_display.append(display_row)

    # News pulse: translate the Korean category enum to English for display,
    # falling back to the raw value if somehow not in the map (defensive —
    # should never trigger since classify_articles.py already validates
    # against _VALID_CATEGORIES before writing to the sheet).
    news_pulse = sections.get("news_pulse", {})
    news_pulse_display = dict(news_pulse)
    news_pulse_display["data"] = [
        {**row, "_display_category": _CATEGORY_EN.get(row.get("category"), row.get("category"))}
        for row in news_pulse.get("data", [])
    ]
    sections = {**sections, "news_pulse": news_pulse_display}

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
        # C-14 item 4: weekly brief + human publication gate.
        weekly_brief=weekly_brief or {"enabled": False},
    )

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(html, encoding="utf-8")
    return len(html.encode("utf-8"))
