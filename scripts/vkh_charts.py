# Import only — do not run directly. Entry point is scripts/build.py.
#
# vkh_charts.py — Velvet Knowledge Hub: chart-data preparation and derived
# analysis maths (dried-equivalent conversion, rolling 12-month windows,
# YoY %, blended unit price, destination/origin breakdowns).
#
# C-12b split (2026-07-04): extracted from build.py. Pure move — no logic
# changed. This is the designated landing file for Phase 2 triangulation
# analyses (C-12f: purpose split, direct-vs-indirect NZ supply, unit value)
# per the 잠망경 pre-mortem's C4 finding — new analytical logic lands in its
# own file, not build.py.
#
# Security: no credentials in this file. All secrets from environment only.

from collections import defaultdict

from scripts.vkh_data import _backfill_product_type, _normalise_date_str

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
