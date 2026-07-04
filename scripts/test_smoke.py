# Run as: PYTHONPATH=. python scripts/test_smoke.py
#
# test_smoke.py — VKH pipeline smoke test (C-12c, code review addendum C2).
#
# No test framework (matches test_dedup_logic.py's existing convention: this
# repo has no pytest config). A single assert-based script, run as a CI step
# before build (addendum C2), that would have caught the "—" regression and
# the float/string HS-code bug (L-9/L-15) before they shipped.
#
# Covers:
#   1. One sample file parse per trade source (QIA annual XLSX, KSTAT customs
#      CSV, NZ export CSV) against small trimmed real-format fixtures in
#      scripts/fixtures/ — catches parser/header-detection breakage (L-13).
#   2. Dedup key check (ingest_common.build_dedup_key) — including the L-15
#      float-vs-string hs_code regression.
#   3. Fixture-based checks of the derived-analysis maths in vkh_charts.py
#      (_compute_dried_eq_kg, _compute_rolling_12m, _compute_unit_price) —
#      these are exactly the functions Phase 2 triangulation (C-12f) reuses
#      for the DMT (÷0.33) conversion and unit-value analysis, so a fixture
#      regression here would also protect the new section.
#
# No network, no Sheets, no credentials required.

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.ingest_common import _normalise_hs_code, build_dedup_key  # noqa: E402
from scripts.ingest_kstat import parse_kstat_csv  # noqa: E402
from scripts.ingest_nz_export import parse_nz_export_csv  # noqa: E402
from scripts.ingest_qia import parse_qia_file  # noqa: E402
from scripts.vkh_charts import (  # noqa: E402
    _compute_dried_eq_kg,
    _compute_rolling_12m,
    _compute_unit_price,
    _kpta_estimate_context,
    _purpose_split,
)

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

_checks_run = 0
_checks_failed = 0


def _check(label: str, condition: bool) -> None:
    global _checks_run, _checks_failed
    _checks_run += 1
    status = "ok" if condition else "FAIL"
    print(f"  [{status}] {label}")
    if not condition:
        _checks_failed += 1


# ---------------------------------------------------------------------------
# 1. Per-source sample file parse
# ---------------------------------------------------------------------------

def test_parse_qia_annual_sample() -> None:
    print("test_parse_qia_annual_sample")
    rows = parse_qia_file(FIXTURES_DIR / "qia_annual_sample.xlsx")
    _check("rows parsed > 0", len(rows) > 0)
    _check("all rows have series=korea_quarantine",
           all(r["series"] == "korea_quarantine" for r in rows))
    _check("both units present (shipments + KG)",
           {r["unit"] for r in rows} == {"shipments", "KG"})
    _check("product_type is frozen or dried only",
           {r["product_type"] for r in rows} <= {"frozen", "dried"})
    _check("NZ present in sample (뉴질랜드)",
           any(r["country"] == "뉴질랜드" for r in rows))


def test_parse_kstat_customs_sample() -> None:
    print("test_parse_kstat_customs_sample")
    rows = parse_kstat_csv(FIXTURES_DIR / "kstat_customs_sample.csv")
    _check("rows parsed > 0", len(rows) > 0)
    _check("hs_code stored as dot-notation TEXT (L-9)",
           all(r["hs_code"] in ("0507.90", "0510.00") for r in rows))
    _check("both units present (KG + USD_thousands)",
           {r["unit"] for r in rows} == {"KG", "USD_thousands"})
    _check("summary row (총계) excluded",
           all(r["country"] != "" for r in rows))


def test_parse_nz_export_sample() -> None:
    print("test_parse_nz_export_sample")
    rows = parse_nz_export_csv(FIXTURES_DIR / "nz_export_sample.csv", "nz_export_sample.csv")
    _check("rows parsed > 0", len(rows) > 0)
    _check("series=nz_export on every row",
           all(r["series"] == "nz_export" for r in rows))
    _check("Korea destination present", any(r["country"] == "Korea" for r in rows))
    _check("China destination present", any(r["country"] == "China" for r in rows))


# ---------------------------------------------------------------------------
# 2. Dedup key check (L-9, L-10, L-15)
# ---------------------------------------------------------------------------

def test_dedup_key_width() -> None:
    print("test_dedup_key_width")
    row = {"date": "2024-01", "series": "kstat_api", "hs_code": "0507.90",
           "unit": "KG", "country": "뉴질랜드"}
    key = build_dedup_key(row)
    _check("key has 5 fields (L-10)", len(key) == 5)

    # Two rows differing only by unit must produce different keys.
    row_usd = dict(row, unit="USD_thousands")
    _check("unit distinguishes dedup key",
           build_dedup_key(row) != build_dedup_key(row_usd))

    # Two rows differing only by country must produce different keys.
    row_other_country = dict(row, country="중국")
    _check("country distinguishes dedup key",
           build_dedup_key(row) != build_dedup_key(row_other_country))


def test_dedup_hs_code_float_regression() -> None:
    print("test_dedup_hs_code_float_regression (L-15)")
    # Sheets returns "0507.90" back as float 507.9 on read (L-15 regression).
    row_from_parser = {"date": "2024-01", "series": "kstat_api", "hs_code": "0507.90",
                        "unit": "KG", "country": "뉴질랜드"}
    row_from_sheets = {"date": "2024-01", "series": "kstat_api", "hs_code": 507.9,
                        "unit": "KG", "country": "뉴질랜드"}
    _check("float 507.9 and string '0507.90' normalise to the same dedup key",
           build_dedup_key(row_from_parser) == build_dedup_key(row_from_sheets))
    _check("_normalise_hs_code(507.9) == '0507.90'",
           _normalise_hs_code(507.9) == "0507.90")


# ---------------------------------------------------------------------------
# 3. Fixture-based checks of derived-analysis maths (vkh_charts.py)
# ---------------------------------------------------------------------------

def test_compute_dried_eq_kg() -> None:
    print("test_compute_dried_eq_kg (0.33 DMT conversion — Appendix B reuses this)")
    rows = [
        {"date": "2024-01", "unit": "KG", "value": "1000", "product_type": "frozen"},
        {"date": "2024-01", "unit": "KG", "value": "500", "product_type": "dried"},
        {"date": "2024-01", "unit": "USD_thousands", "value": "999", "product_type": "dried"},
    ]
    result = _compute_dried_eq_kg(rows, "KG")
    # 1000 * 0.33 (frozen) + 500 * 1.0 (dried) = 830.0. USD row must be ignored.
    _check("frozen converted at 0.33, dried unconverted, non-KG unit ignored",
           result == {"2024-01": 830.0})


def test_compute_rolling_12m() -> None:
    print("test_compute_rolling_12m")
    monthly = {f"2024-{m:02d}": 100.0 for m in range(1, 13)}
    rolling = _compute_rolling_12m(monthly)
    _check("month 12 rolling sum = 12 x 100", rolling["2024-12"] == 1200.0)
    _check("month 1 rolling sum = 100 (only 1 month available)", rolling["2024-01"] == 100.0)


def test_kpta_estimate_context() -> None:
    print("test_kpta_estimate_context (C-12e — manual constant + staleness flag)")
    from datetime import date, timedelta

    today_iso = date.today().isoformat()
    fresh = _kpta_estimate_context({
        "kpta_pharma_estimate": {
            "pharma_total_dmt": 181.8,
            "pharma_nz_origin_dmt": 127.6,
            "as_of_date": today_iso,
            "source": "test fixture",
        }
    })
    _check("fresh estimate: available=True", fresh["available"] is True)
    _check("fresh estimate: is_stale=False", fresh["is_stale"] is False)
    _check("fresh estimate: age_days == 0", fresh["age_days"] == 0)

    old_date = (date.today() - timedelta(days=400)).isoformat()
    stale = _kpta_estimate_context({
        "kpta_pharma_estimate": {
            "pharma_total_dmt": 181.8,
            "pharma_nz_origin_dmt": 127.6,
            "as_of_date": old_date,
            "source": "test fixture",
        }
    })
    _check("400-day-old estimate: is_stale=True", stale["is_stale"] is True)

    missing = _kpta_estimate_context({})
    _check("missing block: available=False (graceful degradation)",
           missing == {"available": False})

    malformed = _kpta_estimate_context({
        "kpta_pharma_estimate": {"as_of_date": "not-a-date"}
    })
    _check("malformed as_of_date: available=False, no exception raised",
           malformed == {"available": False})


def test_purpose_split_sanity_guard() -> None:
    print("test_purpose_split_sanity_guard (C-12f — pharma estimate vs live quarantine total)")
    kpta_ok = {"available": True, "pharma_total_dmt": 30.0}
    kpta_too_big = {"available": True, "pharma_total_dmt": 181.8}

    normal = _purpose_split({"2026-03": 61200.0}, kpta_ok)
    _check("plausible split: available=True", normal["available"] is True)
    _check("plausible split: food_dmt > 0", normal["food_dmt"] > 0)

    mismatched = _purpose_split({"2026-03": 61200.0}, kpta_too_big)
    _check("pharma estimate exceeding live total: available=False, not a negative number",
           mismatched["available"] is False and "reason" in mismatched)

    missing_kpta = _purpose_split({"2026-03": 61200.0}, {"available": False})
    _check("no KPTA constant: available=False", missing_kpta == {"available": False})


def test_compute_unit_price() -> None:
    print("test_compute_unit_price (customs value / dried-eq kg — Appendix B derived analysis 3)")
    rows = [
        {"date": "2024-01", "unit": "KG", "value": "1000", "product_type": "dried"},
        {"date": "2024-01", "unit": "USD_thousands", "value": "50"},
    ]
    prices = _compute_unit_price(rows, "USD_thousands", value_multiplier=1000.0)
    # value = 50 * 1000 = 50,000 USD ; kg = 1000 -> price = 50.0 USD/kg.
    _check("blended unit price = value / dried_eq_kg", prices == {"2024-01": 50.0})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("test_smoke.py — VKH pipeline smoke test\n")

    test_parse_qia_annual_sample()
    test_parse_kstat_customs_sample()
    test_parse_nz_export_sample()
    test_dedup_key_width()
    test_dedup_hs_code_float_regression()
    test_compute_dried_eq_kg()
    test_compute_rolling_12m()
    test_compute_unit_price()
    test_kpta_estimate_context()
    test_purpose_split_sanity_guard()

    print(f"\n{_checks_run} checks run, {_checks_failed} failed.")
    if _checks_failed:
        sys.exit(1)
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
