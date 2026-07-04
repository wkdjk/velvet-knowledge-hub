# ingest_common.py — Shared dedup helpers for VKH ingest scripts.
#
# Extracted from ingest_nz_export.py and ingest_qia.py to eliminate duplication.
#
# Functions:
#   _normalise_hs_code(raw) -> str
#   build_dedup_key(row)    -> tuple
#   rows_to_append(new_rows, existing_keys, headers) -> tuple[list[list], int]
#
# Trust-pipeline gate (C-15, 2026-07-05, directive §4 addendum — "no ingest
# path may write directly to master"): every current and future ingest
# script resolves company names through this shared gate rather than each
# maintaining its own KR->EN lookup.
#   normalise_company_key(name) -> str
#   load_company_mapping(worksheet) -> dict[str, dict]
#   resolve_company(source_name, mapping) -> tuple[str, bool]
#
# Security: no credentials in this file.

import re

# Legal-entity markers stripped before matching (directive §4.2): Korean
# forms first (주식회사/(주)/㈜ all mean "Co., Ltd."), English forms mirror
# the spec's own LTD/CO./LIMITED example. Order doesn't matter — each is
# removed independently.
_CORP_MARKERS = [
    "주식회사", "(주)", "㈜", "유한회사", "(유)", "(사)",
    "LTD.", "LTD", "CO.", "CO", "LIMITED", "INC.", "INC",
]


def normalise_company_key(name: str) -> str:
    """
    Return a normalised matching key for a company name.

    Uppercases, strips whitespace/punctuation and common legal-entity
    markers so minor source variants resolve to one key — e.g.
    "주식회사 이룡제약" and "이룡제약(주)" both -> "이룡제약".
    Directive §4.2. Empty input returns "".
    """
    if not name:
        return ""
    key = name.strip().upper()
    for marker in _CORP_MARKERS:
        key = key.replace(marker.upper(), "")
    key = re.sub(r"[^\w가-힣]", "", key)
    return key


def load_company_mapping(worksheet) -> dict:
    """
    Read the map_companies tab once and return {match_key: row_dict}.

    L-4: caller calls this once and caches the result — never inside a
    per-row loop. Falls back to computing match_key from source_name_kr if
    a seed row has an empty match_key cell (defensive — seed script always
    fills it, but a hand-added row might not).
    """
    rows = worksheet.get_all_records()
    mapping: dict = {}
    for row in rows:
        key = row.get("match_key", "") or normalise_company_key(
            row.get("source_name_kr", "")
        )
        if key:
            mapping[key] = row
    return mapping


def resolve_company(source_name: str, mapping: dict) -> tuple:
    """
    Resolve a raw company name to its canonical EN name via map_companies.

    Returns (canonical_name_en_or_empty, matched). matched=False means the
    caller routes this row's company field to needs_review (the exceptions
    gate, directive §4.2) instead of writing a silent blank/"—".
    """
    key = normalise_company_key(source_name)
    if not key:
        return "", False
    match = mapping.get(key)
    if match is None:
        return "", False
    return match.get("canonical_name_en", ""), True


def _normalise_hs_code(raw) -> str:
    """
    Normalise an hs_code value to a canonical dot-notation string.

    Google Sheets returns numeric cells as float (e.g. 507.9) even when the
    stored value is the string "0507.90". Converting via str() produces "507.9",
    which does not match the parser output "0507.90" — causing silent dedup
    failure (L-9, L-15). This function maps both representations to "0507.90".

    Mapping: 507.9 → "0507.90", 510.0 → "0510.00", "0507.90" → "0507.90".
    Unknown values are returned as-is (str).
    """
    _FLOAT_TO_DOT: dict[float, str] = {
        507.9: "0507.90",
        510.0: "0510.00",
    }
    if isinstance(raw, float):
        return _FLOAT_TO_DOT.get(raw, str(raw))
    if isinstance(raw, int):
        return _FLOAT_TO_DOT.get(float(raw), str(raw))
    return str(raw)


def build_dedup_key(row: dict) -> tuple:
    """
    Return the dedup key tuple for a VTW_Trade_Monthly row.

    Key is (date, series, hs_code, unit, country) — five fields (L-10).
    'unit' distinguishes KG and NZD/shipment rows that share date/series/hs_code.
    'country' distinguishes per-destination rows under the same hs_code.
    L-9: hs_code is normalised via _normalise_hs_code() to handle the
    float/string mismatch between Sheets output and parser output.
    """
    return (
        str(row.get("date", "")),
        str(row.get("series", "")),
        _normalise_hs_code(row.get("hs_code", "")),
        str(row.get("unit", "")),
        str(row.get("country", "")),
    )


def rows_to_append(
    new_rows: list[dict],
    existing_keys: set,
    headers: list[str],
) -> tuple[list[list], int]:
    """
    Filter new_rows to those not already in existing_keys.

    Returns (list_of_lists_for_gspread, skipped_count).
    Each row is converted to an ordered list matching headers.
    """
    to_write: list[list] = []
    skipped = 0

    for row in new_rows:
        key = build_dedup_key(row)
        if key in existing_keys:
            skipped += 1
            continue
        to_write.append([row.get(h, "") for h in headers])

    return to_write, skipped
