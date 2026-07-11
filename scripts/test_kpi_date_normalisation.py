# Run as: PYTHONPATH=. python scripts/test_kpi_date_normalisation.py
#
# test_kpi_date_normalisation.py — regression test for the trade KPI
# date-sort bug (VKH_trade_kpi_reconciliation_2026-07-04.md).
#
# No network, no Sheets — exercises load_all_tabs()'s date normalisation
# and compute_kpis()'s rolling-12m/YoY logic with synthetic data replicating
# the exact failure mode: unpadded "YYYY-M" stray rows (from a historical
# ingest run) coexisting with correctly zero-padded "YYYY-MM" rows for the
# same series. Without the fix, an unpadded month sorts lexicographically
# after "YYYY-12" and gets mistaken for "the latest month", corrupting the
# rolling-12m window end-date and the YoY %.
#
# This repo has no test framework (see scripts/test_news_pipeline.py) — a
# single assert-based script matches the existing convention.
#
# Ported 2026-07-04 (post C-12b module split): build.load_all_tabs /
# assemble_sections now live in scripts/vkh_data.py; compute_kpis now lives
# in scripts/vkh_kpi.py. Logic and assertions unchanged from the original
# 3ddfceb version.

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import vkh_data, vkh_kpi  # noqa: E402


class _FakeWorksheet:
    def __init__(self, title: str, rows: list[dict]) -> None:
        self.title = title
        self._rows = rows

    def get_all_records(self) -> list[dict]:
        # gspread returns a fresh list of dicts on every call.
        return [dict(r) for r in self._rows]


class _FakeSheet:
    def __init__(self, worksheets: list[_FakeWorksheet]) -> None:
        self.title = "fake-sheet"
        self._worksheets = worksheets

    def worksheets(self):
        return self._worksheets


def test_load_all_tabs_normalises_unpadded_dates() -> None:
    """
    A row with date="2026-3" (from a historical unpadded ingest run) must be
    normalised to "2026-03" by load_all_tabs(), matching what a correctly
    zero-padded row for the same month looks like.
    """
    rows = [
        {"date": "2026-3", "series": "korea_quarantine", "unit": "KG", "value": 100,
         "country": "China", "notes": "", "product_type": ""},
        {"date": "2026-04", "series": "korea_quarantine", "unit": "KG", "value": 200,
         "country": "China", "notes": "", "product_type": ""},
    ]
    ws = _FakeWorksheet("VTW_Trade_Monthly", rows)
    sheet = _FakeSheet([ws])
    config = {"sources": [{
        "id": "korea_quarantine", "tab": "VTW_Trade_Monthly", "enabled": True,
        "series_value": "korea_quarantine",
    }]}

    tab_data = vkh_data.load_all_tabs(sheet, config)
    dates = [r["date"] for r in tab_data["VTW_Trade_Monthly"]]
    assert dates == ["2026-03", "2026-04"], f"expected zero-padded dates, got {dates}"
    print("PASS: load_all_tabs normalises unpadded 'date' fields")


def test_compute_kpis_picks_true_latest_month() -> None:
    """
    Regression for the live bug: an unpadded stray row for an EARLIER month
    (2026-01, mis-written as "2026-1") must not be picked as "the latest
    month" ahead of a correctly-padded LATER month (2026-04). Confirms the
    fix end-to-end through compute_kpis(), not just load_all_tabs().
    """
    rows = []
    # 12 months of "prior year" baseline, correctly padded.
    for m in range(1, 13):
        rows.append({
            "date": f"2025-{m:02d}", "series": "korea_quarantine", "unit": "KG",
            "value": 1000.0, "country": "China", "notes": "", "product_type": "dried",
        })
    # Current year: Jan–Apr all present. Jan and Mar are ALSO duplicated under
    # an unpadded date string (the exact live failure mode) — this must not
    # cause April to be excluded from the rolling window.
    for m in range(1, 5):
        rows.append({
            "date": f"2026-{m:02d}", "series": "korea_quarantine", "unit": "KG",
            "value": 1000.0, "country": "China", "notes": "", "product_type": "dried",
        })
    rows.append({"date": "2026-1", "series": "korea_quarantine", "unit": "KG",
                  "value": 50.0, "country": "China", "notes": "", "product_type": "dried"})
    rows.append({"date": "2026-3", "series": "korea_quarantine", "unit": "KG",
                  "value": 50.0, "country": "China", "notes": "", "product_type": "dried"})

    ws = _FakeWorksheet("VTW_Trade_Monthly", rows)
    sheet = _FakeSheet([ws])
    config = {"sources": [{
        "id": "korea_quarantine", "tab": "VTW_Trade_Monthly", "enabled": True,
        "series_value": "korea_quarantine",
    }]}

    tab_data = vkh_data.load_all_tabs(sheet, config)
    sections = vkh_data.assemble_sections(config, tab_data)
    kpi = vkh_kpi.compute_kpis(sections)

    assert kpi["qia_rolling12m_date_end"] == "Apr 2026", (
        f"expected window to end at true latest month (Apr 2026), got "
        f"{kpi['qia_rolling12m_date_end']} — the unpadded-date sort bug has regressed"
    )
    print("PASS: compute_kpis() ends the rolling window at the true latest month")


if __name__ == "__main__":
    test_load_all_tabs_normalises_unpadded_dates()
    test_compute_kpis_picks_true_latest_month()
    print("\nAll KPI date-normalisation regression tests passed.")
