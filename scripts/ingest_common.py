# ingest_common.py — Shared dedup helpers for VKH ingest scripts.
#
# Extracted from ingest_nz_export.py and ingest_qia.py to eliminate duplication.
#
# Functions:
#   _normalise_hs_code(raw) -> str
#   build_dedup_key(row)    -> tuple
#   rows_to_append(new_rows, existing_keys, headers) -> tuple[list[list], int]
#
# Security: no credentials in this file.


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
