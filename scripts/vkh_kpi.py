# Import only — do not run directly. Entry point is scripts/build.py.
#
# vkh_kpi.py — Velvet Knowledge Hub: headline KPI computation.
#
# C-12b split (2026-07-04): extracted from build.py. Pure move — no logic
# changed. See build.py's docstring for the full pipeline order.
#
# Security: no credentials in this file. All secrets from environment only.

import sys
from datetime import datetime, timedelta

from scripts.vkh_charts import _compute_dried_eq_kg, _compute_rolling_12m
from scripts.vkh_data import _MONTH_ABBR, _backfill_product_type, _is_truthy, _today_kst


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
    # _compute_rolling_12m which are defined in vkh_charts.py.
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
