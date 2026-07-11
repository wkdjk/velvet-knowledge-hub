# Run as: PYTHONPATH=. python scripts/build.py
#
# build.py — Velvet Knowledge Hub core build pipeline (thin orchestrator).
#
# Six steps, split across four modules (C-12b, 2026-07-04 — this file was
# 1,848 lines and had outgrown its "no Python edits for a new source" design
# promise per addendum C1; split so Phase 2 triangulation logic (C-12f) lands
# in its own file rather than the largest/most fragile file in the repo):
#
#   1. Load      — scripts/vkh_data.py: read config.yaml, connect to Google
#                  Sheets once, read all enabled tabs in a single pass (L-4).
#   2. Assemble  — scripts/vkh_data.py: build per-section row dicts.
#   3. KPIs      — scripts/vkh_kpi.py: headline KPI computation.
#   4. Charts    — scripts/vkh_charts.py: chart-data prep + derived analysis
#                  maths (dried-eq conversion, rolling windows, YoY, unit
#                  price, destination/origin breakdowns). New Phase 2
#                  triangulation analyses land here, not in this file.
#   5. CSV       — scripts/vkh_data.py: write the three downloadable CSVs.
#   6. Render    — scripts/vkh_render.py: pass all data to Jinja2, write
#                  docs/index.html.
#
# This file only orchestrates the above in order and copies static assets —
# it holds no data-loading, KPI, chart, or render logic itself.
#
# Security: no credentials in this file. All secrets from environment only.

import shutil

from scripts.vkh_data import (
    REPO_ROOT,
    SECTION_SOURCE_MAP,
    _today_kst,
    assemble_sections,
    connect_sheets,
    load_all_tabs,
    load_config,
    _write_import_intelligence_csv,
    _write_news_pulse_csv,
    _write_trade_flows_csv,
)
from scripts.vkh_kpi import compute_kpis
from scripts.vkh_charts import prepare_chart_data
from scripts.vkh_render import render
from scripts.vkh_brief import (
    emit_weekly_brief_notice,
    generate_weekly_brief_draft,
    get_weekly_brief_context,
    push_pending_drafts_to_sheet,
    rehydrate_drafts_from_sheet,
    sync_weekly_brief_approvals,
)
from scripts.library_data import assemble_library_section
from scripts.news_schema import NEWS_DDL
from scripts import vkh_sqlite


def run_weekly_brief_step(config: dict, sheet, conn, kpi: dict, chart_data: dict, sections: dict, build_date: str):
    """
    Step 5f as its own function so it has a real caller to test from (the
    class of bug that escaped review twice per SurveyorQ's D3 re-merge audit,
    2026-07-11, §2: "the new module's own tests pass but nothing tests the
    real caller in build.py"). main() just wires this up with the live
    Sheet/sqlite connection; tests pass fakes for both.

    conn must already be migrated (NEWS_DDL) by the caller — this function
    only runs the weekly-brief read/write sequence, not schema setup.

    Order matters (SurveyorQ audit §6 item 2): vkh.sqlite is an ephemeral
    build cache (D1 decision, 2026-07-10) — raw_weekly_brief_drafts starts
    empty every build, so it must be rehydrated from the durable
    weekly_brief Sheets tab BEFORE the approvals sync can find anything to
    promote, and BEFORE this week's draft is generated. weekly_brief.enabled
    in config.yaml is a hard kill switch (pre-mortem item 5) — every
    vkh_brief call below is a no-op if off.

    Returns (brief_notice, weekly_brief).
    """
    rehydrate_drafts_from_sheet(conn, sheet)
    sync_weekly_brief_approvals(conn, sheet)
    brief_notice = generate_weekly_brief_draft(config, conn, kpi, chart_data, sections, build_date)
    emit_weekly_brief_notice(brief_notice)
    push_pending_drafts_to_sheet(conn, sheet)
    weekly_brief = get_weekly_brief_context(config, conn)
    return brief_notice, weekly_brief


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

    # --- Step 4b: Library section (D1) — sqlite-backed, not a Sheets tab, so
    # it does not go through assemble_sections()'s generic tab_data loader.
    sections["library"] = assemble_library_section(config)

    # --- Step 5: Compute KPIs -------------------------------------------------
    kpi = compute_kpis(sections)

    # --- Step 5b: Prepare chart datasets (pre-aggregated for Jinja2) ----------
    # Pass tab_data so mfds_annual series can be read from the unfiltered tab.
    # Pass config so the C-12e KPTA manual constant can be read (config.yaml,
    # not Sheets — see prepare_chart_data()'s docstring).
    chart_data = prepare_chart_data(sections, tab_data=tab_data, config=config)

    # --- Step 5c: Write trade_flows CSV download ------------------------------
    _write_trade_flows_csv(sections)

    # --- Step 5d: Write import_intelligence CSV download ----------------------
    _write_import_intelligence_csv(sections)

    # --- Step 5e: Write news_pulse CSV download -------------------------------
    _write_news_pulse_csv(sections)

    # --- Step 5f: Weekly brief — rehydrate + sync + draft + push + read
    # (C-14 item 4; rewired to sqlite D3 Phase D 2026-07-11; call-site wiring
    # fix, SurveyorQ T3 audit B-1/B-2/B-4, 2026-07-11). See
    # run_weekly_brief_step()'s docstring for why this is its own function
    # and the ordering rationale.
    brief_conn = vkh_sqlite.connect()
    vkh_sqlite.migrate(brief_conn, NEWS_DDL)
    brief_notice, weekly_brief = run_weekly_brief_step(config, sheet, brief_conn, kpi, chart_data, sections, build_date)
    brief_conn.close()

    # --- Step 6: Render -------------------------------------------------------
    bytes_written = render(config, sections, kpi, chart_data, build_date, weekly_brief=weekly_brief)

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
    for section_id in [*SECTION_SOURCE_MAP, "library"]:
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
    if brief_notice.get("new_draft"):
        print(f"  weekly brief: new draft written for {brief_notice['week_ending_date']} "
              f"(fact-check: {brief_notice['fact_check_status']})")
    print(f"  weekly brief published: enabled={weekly_brief.get('enabled')} "
          f"available={weekly_brief.get('available', '—')}")
    print(f"  output: docs/index.html ({bytes_written} bytes)")
    print(f"  build complete: {build_date}")


if __name__ == "__main__":
    main()
