# Run as: PYTHONPATH=. python scripts/ingest_kstat.py [--recent N | --file PATH | --historical DIR | --nitemtrade N] [--dry-run]
#
# ingest_kstat.py — KSTAT Korea Customs data ingestion for Velvet Knowledge Hub.
#
# Supports four input modes (mutually exclusive):
#   --recent N        Fetch the last N months from the KSTAT getItemtradeList API
#                      (requires KSTAT_API_KEY).
#   --file PATH       Parse a single KSTAT CSV file (offline / historical).
#   --historical DIR  Parse all *.csv files in DIR (offline / historical bulk load).
#   --nitemtrade N    C-12d: fetch the last N months from the country-aware
#                      getNitemtradeList API for NZ/China/Hong Kong (requires
#                      KSTAT_API_KEY — same key, already registered per the
#                      C-12a spike). Writes to the same series (kstat_api),
#                      same tab, same dedup key — genuinely new rows only
#                      where a country/date/hs_code/unit combination is not
#                      already present (e.g. from a manual CSV historical load).
#
# Upserts new rows into the VTW_Trade_Monthly tab of VKH_Data Google Sheet.
#
# Usage:
#   PYTHONPATH=. python scripts/ingest_kstat.py
#   PYTHONPATH=. python scripts/ingest_kstat.py --recent 6
#   PYTHONPATH=. python scripts/ingest_kstat.py --file /path/to/CUSTOMS_2024.csv
#   PYTHONPATH=. python scripts/ingest_kstat.py --historical /path/to/customs_dir/
#   PYTHONPATH=. python scripts/ingest_kstat.py --file /path/to/CUSTOMS_2024.csv --dry-run
#   PYTHONPATH=. python scripts/ingest_kstat.py --nitemtrade 12
#   PYTHONPATH=. python scripts/ingest_kstat.py --nitemtrade 12 --dry-run
#
# CSV format (utf-8-sig BOM, comma-separated):
#   기간,국가,HS코드,품목명,수출 중량,수출 금액,수입 중량,수입 금액,무역수지
#
# Parsing rules:
#   - Skip rows where 국가 is empty or "총계" (summary rows).
#   - Skip rows where 수입 중량 == 0 AND 수입 금액 == 0.
#   - HS코드 mapped to dot notation TEXT ("0507.90") — L-9.
#   - Emits two rows per CSV data row: unit=KG and unit=USD_thousands.
#   - notes = "source=<basename>".
#   - Encoding: utf-8-sig (BOM-aware).
#
# L-1:  PYTHONPATH=. ensures repo root is importable.
# L-2:  .env must be at repo root (/Users/Qs/C/velvet-knowledge-hub/.env).
# L-3:  GOOGLE_SERVICE_ACCOUNT_JSON must be single-line JSON in .env.
# L-4:  get_all_records() called once; new rows written in one append_rows() call.
# L-9:  hs_code stored as TEXT dot notation ("0507.90") — not cast to int.
#       Dedup comparison uses string equality.
# L-10: Dedup key is (date, series, hs_code, country, unit) — five fields.
#       country is required because each country is a separate API row.
#       unit is required because each period/country/hs_code yields two rows (KG + USD_thousands).
# L-13: CSV column positions detected by header name — never by fixed index.
# GAP-5 fix (C-3e 2026-06-02): hs_code_10digit and product_type columns added.
#   hs_code_10digit stores the 10-digit KSTAT code as TEXT ("0507901110").
#   product_type is derived from hs10:
#     0507901110 → "frozen"  (deer velvet, immature = fresh/frozen)
#     0507901190 → "dried"   (deer velvet, other = dried)
#   The existing hs_code dot-notation column is preserved unchanged.
#
# C-12d (2026-07-04) — --nitemtrade mode:
#   IntelQ's phase2_data_access_research found the country breakdown already
#   in the live sheet came entirely from manually-downloaded historical CSVs
#   (tradedata.go.kr portal exports), never from the automated --recent API
#   path — getItemtradeList's live JSON response does not reliably carry a
#   usable per-country dimension. getNitemtradeList (note the leading N) is
#   the sibling operation that does, confirmed live in the C-12a spike:
#     - cntyCd (2-char code) is mandatory; NZ/CN/HK all resolve correctly.
#     - KSTAT_API_KEY is already registered for this operation — no new
#       credential needed (resultCode=00 confirmed).
#     - CN and HK return zero rows under both velvet HS codes across the
#       full 2020-2025 history tested — a genuine data finding (the direct
#       re-export channel is not visible under this HS-code series), not a
#       bug. Included anyway so a future month with real CN/HK activity is
#       captured automatically rather than silently assumed impossible.
#     - strtYymm/endYymm span is capped at 1 year per call (resultCode=99,
#       resultMsg="시작과 종료의 조회기간은 1년이내 기간만 가능합니다." if
#       exceeded) — backfill loops year-by-year via _year_chunks().
#     - Response is XML regardless of the type=json param (observed live in
#       the spike) — parsed directly with xml.etree.ElementTree, not
#       response.json().
#
# KSTAT_API_KEY: read from .env at repo root (API mode only).
# Commander action: copy KSTAT_API_KEY from /Users/Qs/C/velvet-trade-watch/.env
#
# API endpoints:
#   https://apis.data.go.kr/1220000/Itemtrade/getItemtradeList        (--recent)
#   http://apis.data.go.kr/1220000/nitemtrade/getNitemtradeList       (--nitemtrade, C-12d)
# Velvet HS codes: 0507901110 (deer velvet, immature), 0507901190 (deer velvet, other)
#
# Security: no credentials or secrets in this file. All secrets from .env only.

import argparse
import csv
import logging
import os
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

import gspread
import requests
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# L-1: ensure repo root is on sys.path.
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.sheets_auth import _load_config, connect_sheets, resolve_sheet_id  # noqa: E402
from scripts.ingest_common import _normalise_hs_code, build_dedup_key, rows_to_append  # noqa: E402

logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger("ingest_kstat")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TARGET_TAB = "VTW_Trade_Monthly"
SERIES_VALUE = "kstat_api"

# KSTAT API endpoint and parameters.
_API_ENDPOINT = "https://apis.data.go.kr/1220000/Itemtrade/getItemtradeList"
_NUM_OF_ROWS = 500

# Velvet HS 10-digit codes — only soft velvet (immature and other).
# 0507901200 (녹각, hard dried antler) deliberately excluded.
_VELVET_HS_CODES = ["0507901110", "0507901190"]

# Human-readable labels for each 10-digit code (written to hs_label column).
_HS10_LABEL_MAP: dict[str, str] = {
    "0507901110": "Deer velvet (immature)",
    "0507901190": "Deer velvet (other)",
}

# VKH schema stores hs_code as TEXT dot notation — not the 10-digit API code.
_HS_CODE_DOT = "0507.90"

# GAP-5 fix: product_type derived from 10-digit HS code.
# 0507901110 = immature (= fresh/frozen 생녹용) → "frozen"
# 0507901190 = other (= dried 녹용)             → "dried"
_HS10_PRODUCT_TYPE: dict[str, str] = {
    "0507901110": "frozen",
    "0507901190": "dried",
}

# ---------------------------------------------------------------------------
# C-12d: getNitemtradeList (country-aware) constants
# ---------------------------------------------------------------------------

_NITEMTRADE_API_ENDPOINT = "http://apis.data.go.kr/1220000/nitemtrade/getNitemtradeList"
_NITEMTRADE_NUM_OF_ROWS = 100

# Country codes to loop per the phase2 data-access research (NZ = origin of
# interest; China/Hong Kong = re-export triangulation candidates). Both CN
# and HK returned zero rows under both velvet HS codes for 2020-2025 in the
# C-12a spike — kept in the loop regardless so a future month with real
# direct-import activity is captured automatically.
_NITEMTRADE_COUNTRIES: list[str] = ["NZ", "CN", "HK"]

# Korean names data.go.kr's statCdCntnKor1 field returns, kept here only for
# reference/logging — the ingested country value is taken directly from the
# API response, never hardcoded, so any country resolves correctly.
_NITEMTRADE_COUNTRY_LABELS: dict[str, str] = {
    "NZ": "뉴질랜드",
    "CN": "중국",
    "HK": "홍콩",
}


# ---------------------------------------------------------------------------
# KSTAT API helpers
# ---------------------------------------------------------------------------

def _current_ym() -> str:
    """Return the current year-month as YYYY-MM."""
    today = date.today()
    return f"{today.year:04d}-{today.month:02d}"


def _subtract_months(ym: str, n: int) -> str:
    """Return YYYY-MM string n months before ym (e.g. '2026-03', 2 → '2026-01')."""
    year, month = int(ym[:4]), int(ym[5:7])
    for _ in range(n):
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    return f"{year:04d}-{month:02d}"


def _fetch_kstat_month(api_key: str, year: int, month: int) -> list[dict]:
    """
    Fetch KSTAT import data for a single month across both velvet HS codes.

    Returns a list of raw API record dicts. Empty list on any error.
    Skips rows where both weight and value are zero.
    """
    period_str = f"{year:04d}-{month:02d}"
    month_str = f"{month:02d}"
    results: list[dict] = []

    for hs10 in _VELVET_HS_CODES:
        params = {
            "serviceKey": api_key,
            "year": str(year),
            "month": month_str,
            "hs10": hs10,
            "tradeType": "I",          # I = import
            "numOfRows": str(_NUM_OF_ROWS),
            "pageNo": "1",
            "type": "json",
        }

        try:
            resp = requests.get(_API_ENDPOINT, params=params, timeout=30)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "KSTAT API error for hs=%s %04d-%02d: %s",
                hs10, year, month, exc,
            )
            continue

        # Parse items — the exact path varies; use the VTW fallback chain.
        try:
            items = payload.get(
                "items",
                payload.get("response", {}).get("body", {}).get("items", [])
            )
            if isinstance(items, dict):
                # Some data.go.kr APIs wrap single items in a dict with "item" key.
                items = items.get("item", [])
            if not isinstance(items, list):
                items = [items] if items else []
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "KSTAT response parse error for hs=%s %04d-%02d: %s",
                hs10, year, month, exc,
            )
            continue

        for item in items:
            if not isinstance(item, dict):
                continue

            try:
                country_name = str(item.get("cntyNm", "")).strip()
                imp_weight = int(float(str(item.get("imp_cur_mon_wgt", 0) or 0)))
                imp_value = int(float(str(item.get("imp_cur_mon_usd", 0) or 0)))
            except (ValueError, TypeError) as exc:
                logger.debug("Skipping malformed KSTAT item: %s — %s", item, exc)
                continue

            # Skip rows with no import activity (as per brief).
            if imp_weight == 0 and imp_value == 0:
                continue

            results.append({
                "period": period_str,
                "hs10": hs10,
                "country": country_name,
                "imp_weight_kg": imp_weight,
                "imp_value_usd_thousands": imp_value,
            })

    logger.info("KSTAT %04d-%02d: %d records fetched.", year, month, len(results))
    return results


def fetch_kstat_recent(api_key: str, months_back: int = 3) -> list[dict]:
    """
    Fetch KSTAT import data for the last N calendar months.

    Returns a combined list sorted by period → country → hs10.
    """
    current = _current_ym()
    results: list[dict] = []

    for i in range(months_back):
        ym = _subtract_months(current, i)
        year = int(ym[:4])
        month = int(ym[5:7])
        rows = _fetch_kstat_month(api_key, year, month)
        results.extend(rows)

    results.sort(key=lambda r: (r["period"], r["country"], r["hs10"]))
    return results


def api_records_to_sheet_rows(raw_records: list[dict]) -> list[dict]:
    """
    Convert raw KSTAT API records into VTW_Trade_Monthly schema rows.

    For each raw record, emits TWO rows:
      1. KG row (unit = "KG", value = imp_weight_kg)
      2. USD row (unit = "USD_thousands", value = imp_value_usd_thousands)

    L-9: hs_code stored as TEXT dot notation "0507.90" — not the 10-digit API code.
    GAP-5: hs_code_10digit stores the actual 10-digit code; product_type is
           derived from it (frozen / dried).
    """
    output: list[dict] = []

    for rec in raw_records:
        hs10 = rec["hs10"]
        hs_label = _HS10_LABEL_MAP.get(hs10, hs10)
        notes = f"hs10={hs10}"
        period = rec["period"]
        country = rec["country"]
        product_type = _HS10_PRODUCT_TYPE.get(hs10, "other")

        # KG row.
        output.append({
            "date":            period,
            "series":          SERIES_VALUE,
            "hs_code":         _HS_CODE_DOT,   # TEXT dot notation — L-9 note
            "hs_label":        hs_label,
            "value":           rec["imp_weight_kg"],
            "unit":            "KG",
            "country":         country,
            "notes":           notes,
            "hs_code_10digit": hs10,            # GAP-5: full 10-digit code
            "product_type":    product_type,    # GAP-5: frozen | dried | other
        })

        # USD_thousands row.
        output.append({
            "date":            period,
            "series":          SERIES_VALUE,
            "hs_code":         _HS_CODE_DOT,   # TEXT dot notation — L-9 note
            "hs_label":        hs_label,
            "value":           rec["imp_value_usd_thousands"],
            "unit":            "USD_thousands",
            "country":         country,
            "notes":           notes,
            "hs_code_10digit": hs10,            # GAP-5: full 10-digit code
            "product_type":    product_type,    # GAP-5: frozen | dried | other
        })

    return output


# ---------------------------------------------------------------------------
# C-12d: getNitemtradeList (country-aware) fetch + parse
# ---------------------------------------------------------------------------

def _year_chunks(months_back: int) -> list[tuple[str, str]]:
    """
    Split the last `months_back` calendar months into <=1-year (strtYymm,
    endYymm) chunks, one per calendar year touched by the window.

    getNitemtradeList rejects any single call spanning more than 1 year
    (resultCode=99 — confirmed in the C-12a spike). A full calendar year
    (YYYY01-YYYY12) is exactly 12 months, satisfying that limit.

    Returns [(strt_yymm, end_yymm), ...] ordered oldest year first.
    """
    current = _current_ym()
    start = _subtract_months(current, months_back - 1) if months_back > 0 else current
    start_year = int(start[:4])
    end_year = int(current[:4])

    chunks: list[tuple[str, str]] = []
    for year in range(start_year, end_year + 1):
        chunk_start = start if year == start_year else f"{year}-01"
        chunk_end = current if year == end_year else f"{year}-12"
        chunks.append((chunk_start.replace("-", ""), chunk_end.replace("-", "")))
    return chunks


def _parse_nitemtrade_xml(raw_xml: str, cnty_cd: str, hs10: str) -> list[dict]:
    """
    Parse a getNitemtradeList XML response into raw record dicts.

    Response is XML regardless of the `type=json` request param (observed
    live in the C-12a spike) — parsed directly, never via response.json().
    resultCode != "00" is treated as a failed call (logged, empty result),
    not a silent empty page (per the spike's exact error signature check).

    Skips the "총계" (grand total) summary row — only "YYYY.MM" period rows
    are kept. Skips rows with zero weight and zero value (same rule as the
    getItemtradeList path).
    """
    try:
        root = ET.fromstring(raw_xml)
    except ET.ParseError as exc:
        logger.warning(
            "getNitemtradeList XML parse error for cntyCd=%s hs=%s: %s",
            cnty_cd, hs10, exc,
        )
        return []

    result_code_el = root.find(".//resultCode")
    result_code = result_code_el.text.strip() if result_code_el is not None and result_code_el.text else ""
    if result_code != "00":
        result_msg_el = root.find(".//resultMsg")
        result_msg = result_msg_el.text.strip() if result_msg_el is not None and result_msg_el.text else "(no resultMsg)"
        logger.warning(
            "getNitemtradeList non-OK resultCode=%s for cntyCd=%s hs=%s: %s",
            result_code, cnty_cd, hs10, result_msg,
        )
        return []

    results: list[dict] = []
    for item in root.findall(".//item"):
        period_raw = (item.findtext("year") or "").strip()
        if period_raw == "총계" or not period_raw:
            continue
        # "2024.01" -> "2024-01".
        if "." not in period_raw:
            continue
        period_str = period_raw.replace(".", "-")

        country_name = (item.findtext("statCdCntnKor1") or "").strip()

        try:
            imp_weight = int(float(item.findtext("impWgt") or "0"))
            # BUGFIX (found during C-12f build, 2026-07-04): impDlr is plain
            # USD, not thousands — getItemtradeList's imp_cur_mon_usd field
            # (used elsewhere in this file) already returns thousands, but
            # getNitemtradeList does not. Confirmed by cross-checking against
            # the manually-downloaded CSV for the same period (NZ 2024-01,
            # HS 0507901110): CSV value=2880 (USD_thousands, i.e. $2.88M) for
            # 20,336 kg; this API's raw impDlr for the same period is
            # 2,879,659 (plain USD) — dividing by 1000 gives 2879.7, matching
            # the CSV within rounding. Storing the raw value unconverted
            # inflated every nitemtrade-sourced USD_thousands row by 1000x.
            imp_value_usd_thousands = float(item.findtext("impDlr") or "0") / 1000.0
        except (ValueError, TypeError):
            logger.debug("Skipping malformed nitemtrade item for cntyCd=%s", cnty_cd)
            continue

        if imp_weight == 0 and imp_value_usd_thousands == 0:
            continue

        results.append({
            "period": period_str,
            "hs10": hs10,
            "country": country_name or _NITEMTRADE_COUNTRY_LABELS.get(cnty_cd, cnty_cd),
            "imp_weight_kg": imp_weight,
            "imp_value_usd_thousands": round(imp_value_usd_thousands, 3),
        })

    return results


def _fetch_nitemtrade_chunk(
    api_key: str, cnty_cd: str, hs10: str, strt_yymm: str, end_yymm: str,
) -> list[dict]:
    """Fetch one (country, hs_code, <=1yr window) chunk from getNitemtradeList."""
    params = {
        "serviceKey": api_key,
        "strtYymm": strt_yymm,
        "endYymm": end_yymm,
        "cntyCd": cnty_cd,
        "hsSgn": hs10,
        "type": "json",
        "numOfRows": str(_NITEMTRADE_NUM_OF_ROWS),
        "pageNo": "1",
    }
    try:
        resp = requests.get(_NITEMTRADE_API_ENDPOINT, params=params, timeout=30)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "getNitemtradeList request error for cntyCd=%s hs=%s %s-%s: %s",
            cnty_cd, hs10, strt_yymm, end_yymm, exc,
        )
        return []

    return _parse_nitemtrade_xml(resp.text, cnty_cd, hs10)


def fetch_nitemtrade_recent(api_key: str, months_back: int = 12) -> list[dict]:
    """
    Fetch getNitemtradeList records for the last N months, looping over
    _NITEMTRADE_COUNTRIES x _VELVET_HS_CODES x _year_chunks(months_back).

    Returns a combined list of raw records sorted by period -> country -> hs10.
    """
    results: list[dict] = []
    chunks = _year_chunks(months_back)

    for cnty_cd in _NITEMTRADE_COUNTRIES:
        for hs10 in _VELVET_HS_CODES:
            for strt_yymm, end_yymm in chunks:
                rows = _fetch_nitemtrade_chunk(api_key, cnty_cd, hs10, strt_yymm, end_yymm)
                results.extend(rows)
                # L-19-style intra-loop pacing: this API is a different
                # data.go.kr service quota to getItemtradeList's, but a
                # small pause avoids bursting many calls in immediate
                # succession (countries x hs_codes x year_chunks).
                time.sleep(0.2)

    results.sort(key=lambda r: (r["period"], r["country"], r["hs10"]))
    logger.info(
        "getNitemtradeList: %d records fetched across %d countries, %d hs codes, %d year chunk(s).",
        len(results), len(_NITEMTRADE_COUNTRIES), len(_VELVET_HS_CODES), len(chunks),
    )
    return results


def nitemtrade_records_to_sheet_rows(raw_records: list[dict]) -> list[dict]:
    """
    Convert raw getNitemtradeList records into VTW_Trade_Monthly schema rows.

    Same schema and series_value ("kstat_api") as api_records_to_sheet_rows()
    — this is the same logical source (KSTAT customs), extended with a real
    per-country dimension. Rows for a (date, country, hs_code, unit) already
    present from a manual CSV historical load are naturally skipped by the
    existing dedup key (L-10) in _write_rows_to_sheets(), not duplicated.
    """
    output: list[dict] = []

    for rec in raw_records:
        hs10 = rec["hs10"]
        hs_label = _HS10_LABEL_MAP.get(hs10, hs10)
        notes = f"hs10={hs10} | source=getNitemtradeList"
        period = rec["period"]
        country = rec["country"]
        product_type = _HS10_PRODUCT_TYPE.get(hs10, "other")

        output.append({
            "date":            period,
            "series":          SERIES_VALUE,
            "hs_code":         _HS_CODE_DOT,
            "hs_label":        hs_label,
            "value":           rec["imp_weight_kg"],
            "unit":            "KG",
            "country":         country,
            "notes":           notes,
            "hs_code_10digit": hs10,
            "product_type":    product_type,
        })

        output.append({
            "date":            period,
            "series":          SERIES_VALUE,
            "hs_code":         _HS_CODE_DOT,
            "hs_label":        hs_label,
            "value":           rec["imp_value_usd_thousands"],
            "unit":            "USD_thousands",
            "country":         country,
            "notes":           notes,
            "hs_code_10digit": hs10,
            "product_type":    product_type,
        })

    return output


# ---------------------------------------------------------------------------
# CSV file parsing helpers (--file / --historical modes)
# ---------------------------------------------------------------------------

# Column name constants for the KSTAT CSV format (Korean headers).
_CSV_COL_PERIOD = "기간"
_CSV_COL_COUNTRY = "국가"
_CSV_COL_HS = "HS코드"
_CSV_COL_LABEL = "품목명"
_CSV_COL_IMP_WEIGHT = "수입 중량"
_CSV_COL_IMP_VALUE = "수입 금액"

# Summary row markers — these rows carry totals, not per-country data.
_CSV_SKIP_COUNTRY_VALUES = {"", "총계"}


def _hs_code_to_dot(raw_hs: str) -> str:
    """
    Map a raw KSTAT HS code string to dot notation TEXT.

    The KSTAT CSV uses 10-digit codes (e.g. "0507901110").
    VKH schema stores the 6-digit WCO heading in dot notation ("0507.90").

    Mapping rules (L-9: store as TEXT dot notation):
      - Starts with "050790" → "0507.90"
      - Starts with "051000" → "0510.00"
      - Default: first 4 chars + "." + chars 5-6 (e.g. "0507901110" → "0507.90").
    """
    raw = raw_hs.strip().replace(".", "").replace(" ", "")
    if raw.startswith("050790"):
        return "0507.90"
    if raw.startswith("051000"):
        return "0510.00"
    # Generic fallback: first 4 digits + "." + next 2 digits.
    if len(raw) >= 6:
        return f"{raw[:4]}.{raw[4:6]}"
    return raw


def parse_kstat_csv(filepath: Path) -> list[dict]:
    """
    Parse a single KSTAT CSV file into VTW_Trade_Monthly schema rows.

    Each data row emits TWO output rows:
      1. unit = "KG",            value = 수입 중량
      2. unit = "USD_thousands", value = 수입 금액

    Skip rules:
      - 국가 is empty or "총계" (summary rows).
      - Both 수입 중량 == 0 AND 수입 금액 == 0.

    Encoding: utf-8-sig (BOM-aware — KSTAT CSVs start with BOM U+FEFF).
    L-13: column positions detected by header name, never by fixed index.
    L-9:  hs_code stored as TEXT dot notation.
    """
    logger.info("Parsing KSTAT CSV: %s", filepath)
    source_note = f"source={filepath.name}"
    output: list[dict] = []

    with filepath.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)

        # Validate required columns are present (L-13: dynamic header check).
        if reader.fieldnames is None:
            raise ValueError(f"CSV has no header row: {filepath}")

        # Strip whitespace from field names to normalise.
        fieldnames = [f.strip() if f else f for f in reader.fieldnames]
        required = {_CSV_COL_PERIOD, _CSV_COL_COUNTRY, _CSV_COL_HS, _CSV_COL_LABEL,
                    _CSV_COL_IMP_WEIGHT, _CSV_COL_IMP_VALUE}
        missing = required - set(fieldnames)
        if missing:
            raise ValueError(
                f"CSV missing required columns {missing} in {filepath.name}.\n"
                f"  Found columns: {fieldnames}"
            )

        for row in reader:
            # Strip keys to match normalised fieldnames.
            row = {k.strip() if k else k: v for k, v in row.items()}

            country = row.get(_CSV_COL_COUNTRY, "").strip().strip('"')
            # Skip summary rows.
            if country in _CSV_SKIP_COUNTRY_VALUES:
                continue

            period = row.get(_CSV_COL_PERIOD, "").strip().strip('"')
            raw_hs = row.get(_CSV_COL_HS, "").strip().strip('"')
            hs_label = row.get(_CSV_COL_LABEL, "").strip().strip('"')

            # Parse weight and value — strip commas from formatted numbers.
            try:
                imp_weight = int(float(row.get(_CSV_COL_IMP_WEIGHT, "0").strip().replace(",", "") or "0"))
                imp_value = int(float(row.get(_CSV_COL_IMP_VALUE, "0").strip().replace(",", "") or "0"))
            except (ValueError, TypeError):
                logger.debug("Skipping malformed CSV row: %s", row)
                continue

            # Skip rows with no import activity.
            if imp_weight == 0 and imp_value == 0:
                continue

            hs_dot = _hs_code_to_dot(raw_hs)
            # GAP-5: store 10-digit code and derive product_type from it.
            # raw_hs from CSV is the 10-digit code; normalise to remove dots/spaces.
            hs_10digit = raw_hs.strip().replace(".", "").replace(" ", "")
            product_type = _HS10_PRODUCT_TYPE.get(hs_10digit, "other")

            # KG row.
            output.append({
                "date":            period,
                "series":          SERIES_VALUE,
                "hs_code":         hs_dot,
                "hs_label":        hs_label,
                "value":           imp_weight,
                "unit":            "KG",
                "country":         country,
                "notes":           source_note,
                "hs_code_10digit": hs_10digit,   # GAP-5: full 10-digit code
                "product_type":    product_type, # GAP-5: frozen | dried | other
            })

            # USD_thousands row.
            output.append({
                "date":            period,
                "series":          SERIES_VALUE,
                "hs_code":         hs_dot,
                "hs_label":        hs_label,
                "value":           imp_value,
                "unit":            "USD_thousands",
                "country":         country,
                "notes":           source_note,
                "hs_code_10digit": hs_10digit,   # GAP-5: full 10-digit code
                "product_type":    product_type, # GAP-5: frozen | dried | other
            })

    logger.info("KSTAT CSV parse complete: %d schema rows from %s", len(output), filepath.name)
    return output


def parse_kstat_historical(directory: Path) -> list[dict]:
    """
    Parse all *.csv files in directory and return combined schema rows.

    Files are sorted by name (ascending) to process chronologically.
    L-13: relies on parse_kstat_csv for per-file column detection.
    """
    csv_files = sorted(directory.glob("*.csv"))
    if not csv_files:
        raise ValueError(f"No *.csv files found in directory: {directory}")

    all_rows: list[dict] = []
    for csv_path in csv_files:
        try:
            rows = parse_kstat_csv(csv_path)
            all_rows.extend(rows)
            print(f"  parsed {csv_path.name}: {len(rows)} schema rows")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Skipping %s — %s", csv_path.name, exc)

    return all_rows


# ---------------------------------------------------------------------------
# Dedup helpers
# ---------------------------------------------------------------------------
#
# _normalise_hs_code, build_dedup_key, rows_to_append imported from
# scripts.ingest_common (see H-2 fix, VKH audit 2026-07-01) — kstat's own key
# field order (date, series, hs_code, country, unit) differs from
# ingest_common's (date, series, hs_code, unit, country), but both are 5-tuples
# of the same five values so set-membership dedup is unaffected.


def load_existing_keys(worksheet) -> tuple[set, int]:
    """
    Read all rows from the worksheet once and return a set of dedup keys.

    L-4: get_all_records() is called exactly once — never inside a loop.
    """
    existing_rows = worksheet.get_all_records()
    return {build_dedup_key(r) for r in existing_rows}, len(existing_rows)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Ingest KSTAT Korea Customs data into the VTW_Trade_Monthly tab "
            "of VKH_Data Google Sheet. Four input modes: --recent (API), "
            "--file (single CSV), --historical (directory of CSVs), "
            "--nitemtrade (country-aware API, C-12d)."
        )
    )

    # Input mode arguments — mutually exclusive.
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--recent",
        type=int,
        default=None,
        metavar="N",
        help="Fetch the last N calendar months from the KSTAT API (requires KSTAT_API_KEY).",
    )
    mode_group.add_argument(
        "--file",
        metavar="PATH",
        help="Parse a single KSTAT CSV file (offline / historical mode).",
    )
    mode_group.add_argument(
        "--historical",
        metavar="DIR",
        help="Parse all *.csv files in DIR (offline / bulk historical load).",
    )
    mode_group.add_argument(
        "--nitemtrade",
        type=int,
        default=None,
        metavar="N",
        help=(
            "C-12d: fetch the last N calendar months from the country-aware "
            "getNitemtradeList API for NZ/China/Hong Kong (requires KSTAT_API_KEY)."
        ),
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse only — do not write to Google Sheets.",
    )
    args = parser.parse_args()

    # Default to --recent 3 if no mode supplied.
    if args.file is None and args.historical is None and args.recent is None and args.nitemtrade is None:
        args.recent = 3

    # L-2: load .env from repo root.
    load_dotenv(REPO_ROOT / ".env")

    # ----- Mode: --file -------------------------------------------------------
    if args.file is not None:
        csv_path = Path(args.file).resolve()
        print("ingest_kstat.py — VKH KSTAT CSV file mode")
        print(f"  file: {csv_path.name}")
        print(f"  dry-run: {args.dry_run}")

        if not csv_path.exists():
            print(f"ERROR: CSV file not found: {csv_path}", file=sys.stderr)
            sys.exit(1)

        try:
            sheet_rows = parse_kstat_csv(csv_path)
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR: CSV parse failed — {exc}", file=sys.stderr)
            sys.exit(1)

        rows_parsed = len(sheet_rows)
        print(f"  schema rows parsed: {rows_parsed}")

        if args.dry_run:
            print()
            print("[DRY RUN] Parse complete — no Sheets write.")
            print("  Sample rows (first 3):")
            for row in sheet_rows[:3]:
                print(f"    {row}")
            print(f"rows_parsed: {rows_parsed} | new_rows: 0 | skipped_duplicates: 0")
            sys.exit(0)

        _write_rows_to_sheets(sheet_rows, rows_parsed)
        return

    # ----- Mode: --historical ------------------------------------------------
    if args.historical is not None:
        hist_dir = Path(args.historical).resolve()
        print("ingest_kstat.py — VKH KSTAT historical CSV mode")
        print(f"  directory: {hist_dir}")
        print(f"  dry-run: {args.dry_run}")

        if not hist_dir.is_dir():
            print(f"ERROR: directory not found: {hist_dir}", file=sys.stderr)
            sys.exit(1)

        try:
            sheet_rows = parse_kstat_historical(hist_dir)
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR: historical parse failed — {exc}", file=sys.stderr)
            sys.exit(1)

        rows_parsed = len(sheet_rows)
        print(f"  total schema rows parsed: {rows_parsed}")

        if args.dry_run:
            print()
            print("[DRY RUN] Parse complete — no Sheets write.")
            print("  Sample rows (first 3):")
            for row in sheet_rows[:3]:
                print(f"    {row}")
            print(f"rows_parsed: {rows_parsed} | new_rows: 0 | skipped_duplicates: 0")
            sys.exit(0)

        _write_rows_to_sheets(sheet_rows, rows_parsed)
        return

    # ----- Mode: --nitemtrade (country-aware API, C-12d) ----------------------
    if args.nitemtrade is not None:
        months_back = args.nitemtrade
        print("ingest_kstat.py — VKH getNitemtradeList (country-aware) ingestion")
        print(f"  months_back: {months_back}")
        print(f"  countries: {_NITEMTRADE_COUNTRIES}")
        print(f"  dry-run: {args.dry_run}")

        api_key = os.environ.get("KSTAT_API_KEY", "").strip()
        if not api_key:
            print(
                "INFO: KSTAT_API_KEY is not set — skipping getNitemtradeList fetch.\n"
                "  To enable: add KSTAT_API_KEY to .env at the repo root."
            )
            print("rows_fetched: 0 | new_rows: 0 | skipped_duplicates: 0")
            sys.exit(0)

        raw_records = fetch_nitemtrade_recent(api_key, months_back=months_back)
        print(f"  raw API records: {len(raw_records)}")

        sheet_rows = nitemtrade_records_to_sheet_rows(raw_records)
        rows_fetched = len(sheet_rows)
        print(f"  schema rows generated: {rows_fetched}")

        if args.dry_run:
            print()
            print("[DRY RUN] Fetch complete — no Sheets write.")
            print("  Sample rows (first 3):")
            for row in sheet_rows[:3]:
                print(f"    {row}")
            print(f"rows_fetched: {rows_fetched} | new_rows: 0 | skipped_duplicates: 0")
            sys.exit(0)

        if rows_fetched == 0:
            print("  No API records returned — nothing to write.")
            print("rows_fetched: 0 | new_rows: 0 | skipped_duplicates: 0")
            sys.exit(0)

        _write_rows_to_sheets(sheet_rows, rows_fetched)
        return

    # ----- Mode: --recent (API) -----------------------------------------------
    months_back = args.recent
    print("ingest_kstat.py — VKH KSTAT API ingestion")
    print(f"  months_back: {months_back}")
    print(f"  dry-run: {args.dry_run}")

    # Graceful skip if KSTAT_API_KEY is not set.
    api_key = os.environ.get("KSTAT_API_KEY", "").strip()
    if not api_key:
        print(
            "INFO: KSTAT_API_KEY is not set — skipping KSTAT API fetch.\n"
            "  To enable: add KSTAT_API_KEY to .env at the repo root.\n"
            "  Commander action: copy KSTAT_API_KEY from "
            "/Users/Qs/C/velvet-trade-watch/.env"
        )
        print("rows_fetched: 0 | new_rows: 0 | skipped_duplicates: 0")
        sys.exit(0)

    print(f"  fetching last {months_back} month(s) from KSTAT API...")
    raw_records = fetch_kstat_recent(api_key, months_back=months_back)
    print(f"  raw API records: {len(raw_records)}")

    sheet_rows = api_records_to_sheet_rows(raw_records)
    rows_fetched = len(sheet_rows)
    print(f"  schema rows generated: {rows_fetched}")

    if args.dry_run:
        print()
        print("[DRY RUN] Fetch complete — no Sheets write.")
        print("  Sample rows (first 3):")
        for row in sheet_rows[:3]:
            print(f"    {row}")
        print(f"rows_fetched: {rows_fetched} | new_rows: 0 | skipped_duplicates: 0")
        sys.exit(0)

    if rows_fetched == 0:
        print("  No API records returned — nothing to write.")
        print("rows_fetched: 0 | new_rows: 0 | skipped_duplicates: 0")
        sys.exit(0)

    _write_rows_to_sheets(sheet_rows, rows_fetched)


def _write_rows_to_sheets(sheet_rows: list[dict], rows_count: int) -> None:
    """
    Connect to Google Sheets, dedup, and write new rows.

    Shared by all three input modes. L-4: one read call, one write call.
    """
    sheet_id = resolve_sheet_id()
    print(f"  sheet_id: {sheet_id}")

    spreadsheet = connect_sheets(sheet_id)
    print(f"  sheet title: {spreadsheet.title}")

    try:
        ws = spreadsheet.worksheet(TARGET_TAB)
    except gspread.exceptions.WorksheetNotFound:
        print(
            f"ERROR: tab '{TARGET_TAB}' not found in sheet {sheet_id}.\n"
            "  Run scripts/setup_sheets.py first to create all Phase 1 tabs.",
            file=sys.stderr,
        )
        sys.exit(1)

    # L-4: one read call to load existing rows.
    existing_keys, existing_count = load_existing_keys(ws)
    print(f"  existing rows in tab: {existing_count}")

    headers = ws.row_values(1)
    if not headers:
        print(
            f"ERROR: tab '{TARGET_TAB}' has no header row. "
            "Re-run setup_sheets.py to restore it.",
            file=sys.stderr,
        )
        sys.exit(1)

    new_rows_lists, rows_skipped = rows_to_append(sheet_rows, existing_keys, headers)
    rows_new = len(new_rows_lists)

    if rows_new == 0:
        print("  Nothing to write — all rows already present.")
        print(
            f"rows_count: {rows_count} | new_rows: 0 | "
            f"skipped_duplicates: {rows_skipped}"
        )
        sys.exit(0)

    # L-4: one bulk append_rows call — never in a loop.
    ws.append_rows(new_rows_lists, value_input_option="USER_ENTERED")

    print()
    print(f"  DONE: {rows_new} rows written to '{TARGET_TAB}'.")
    print(
        f"rows_count: {rows_count} | new_rows: {rows_new} | "
        f"skipped_duplicates: {rows_skipped}"
    )


if __name__ == "__main__":
    main()
