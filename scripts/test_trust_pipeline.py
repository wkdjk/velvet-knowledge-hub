# Run as: PYTHONPATH=. python scripts/test_trust_pipeline.py
#
# test_trust_pipeline.py — regression test for the raw -> mapping -> master
# + needs_review gate (C-15, 2026-07-05, directive §4; extended C-16,
# 2026-07-05 to country_origin_en/country_export_en/product_type_en).
#
# No network, no Sheets — exercises normalise_company_key/load_company_mapping/
# resolve_company (scripts/ingest_common.py) and seed_map_companies.py's /
# seed_map_terms.py's majority-EN-pick logic with synthetic data replicating
# the exact live cases found in the 2026-07-05 audits (variant legal-entity
# markers on the same company; genuinely unmapped companies; MFDS rows with
# blank country/type _en fields).
#
# This repo has no test framework (see scripts/test_dedup_logic.py) — a
# single assert-based script matches the existing convention.

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.ingest_common import (  # noqa: E402
    load_company_mapping,
    normalise_company_key,
    resolve_company,
)
from scripts.seed_map_companies import build_seed_rows  # noqa: E402
from scripts.seed_map_terms import build_seed_rows as build_term_seed_rows  # noqa: E402


class _FakeWorksheet:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def get_all_records(self) -> list[dict]:
        return [dict(r) for r in self._rows]


def test_normalise_company_key_matches_legal_entity_variants() -> None:
    """
    Live variants confirmed 2026-07-05: 3 different legal-marker placements
    for the same company must all normalise to one key.
    """
    variants = ["(주)우성생약", "우성생약(주)", "주식회사 우성생약"]
    keys = {normalise_company_key(v) for v in variants}
    assert len(keys) == 1, f"expected all variants to share one key, got {keys}"
    assert keys == {"우성생약"}, f"unexpected key: {keys}"
    print("PASS: normalise_company_key collapses legal-entity marker variants")


def test_normalise_company_key_english_suffixes() -> None:
    """Directive §4.2's own example: LTD/CO./LIMITED style suffixes."""
    assert normalise_company_key("CK Import Export Ltd.") == normalise_company_key("CK IMPORT-EXPORT")
    print("PASS: normalise_company_key collapses English legal suffixes")


def test_normalise_company_key_empty_input() -> None:
    assert normalise_company_key("") == ""
    assert normalise_company_key(None) == ""
    print("PASS: normalise_company_key handles empty/None input")


def test_resolve_company_matched() -> None:
    ws = _FakeWorksheet([
        {"source_name_kr": "주식회사 이룡제약", "match_key": "이룡제약",
         "canonical_name_en": "Eryong Pharm.", "public_display_name": "Eryong Pharm.",
         "country": "KR", "notes": ""},
    ])
    mapping = load_company_mapping(ws)
    en, matched = resolve_company("주식회사 이룡제약", mapping)
    assert matched is True
    assert en == "Eryong Pharm."
    print("PASS: resolve_company matches a mapped company")


def test_resolve_company_unmatched_routes_to_review() -> None:
    """The exact 2026-07-05 live case: a company never seen in map_companies."""
    ws = _FakeWorksheet([
        {"source_name_kr": "주식회사 이룡제약", "match_key": "이룡제약",
         "canonical_name_en": "Eryong Pharm.", "public_display_name": "Eryong Pharm.",
         "country": "KR", "notes": ""},
    ])
    mapping = load_company_mapping(ws)
    en, matched = resolve_company("마더스초이스", mapping)
    assert matched is False
    assert en == ""
    print("PASS: resolve_company reports unmatched for a company with no mapping row")


def test_seed_map_companies_picks_majority_en() -> None:
    """
    A company appearing with 3 EN spellings must seed with the most common
    one, not the first or last — replicating the live "WooSung" /
    "WooSung (Mr Bang)" / "WooSung (Mr. Bang)" case.
    """
    records = [
        {"importer_ko": "(주)우성생약", "importer_en": "WooSung"},
        {"importer_ko": "(주)우성생약", "importer_en": "WooSung"},
        {"importer_ko": "우성생약(주)", "importer_en": "WooSung (Mr Bang)"},
    ]
    seed_rows = build_seed_rows(records)
    assert len(seed_rows) == 1, f"expected one merged key, got {len(seed_rows)}"
    row = seed_rows[0]
    # [source_name_kr, match_key, canonical_name_en, public_display_name, country, notes]
    assert row[1] == "우성생약"
    assert row[2] == "WooSung", f"expected majority EN 'WooSung', got {row[2]}"
    print("PASS: seed_map_companies picks the majority EN spelling per company key")


def test_seed_map_companies_skips_never_mapped_company() -> None:
    """A company with zero non-empty EN anywhere must not appear in the seed
    (it surfaces via needs_review/backfill instead, not a fabricated guess)."""
    records = [{"importer_ko": "아로하가든", "importer_en": ""}]
    seed_rows = build_seed_rows(records)
    assert seed_rows == [], f"expected no seed row for a never-mapped company, got {seed_rows}"
    print("PASS: seed_map_companies skips companies with no known EN name anywhere")


def test_resolve_company_reused_for_country_mapping() -> None:
    """
    C-16: map_countries/map_types are shaped like map_companies (match_key /
    source_name_kr / canonical_name_en) so load_company_mapping/resolve_company
    work unchanged against them — no new resolution functions were written.
    """
    ws = _FakeWorksheet([
        {"source_name_kr": "뉴질랜드", "match_key": "뉴질랜드",
         "canonical_name_en": "New Zealand", "notes": ""},
    ])
    mapping = load_company_mapping(ws)
    en, matched = resolve_company("뉴질랜드", mapping)
    assert matched is True
    assert en == "New Zealand"
    en, matched = resolve_company("호주", mapping)
    assert matched is False
    assert en == ""
    print("PASS: resolve_company reused unchanged for country-name mapping")


def test_seed_map_terms_merges_origin_and_export_columns() -> None:
    """
    map_countries is seeded from BOTH country_origin_ko/en and
    country_export_ko/en pairs — the same country appearing in either column
    across different rows must merge into one seed row.
    """
    records = [
        {"country_origin_ko": "뉴질랜드", "country_origin_en": "New Zealand",
         "country_export_ko": "", "country_export_en": ""},
        {"country_origin_ko": "", "country_origin_en": "",
         "country_export_ko": "뉴질랜드", "country_export_en": "New Zealand"},
    ]
    field_pairs = [
        ("country_origin_ko", "country_origin_en"),
        ("country_export_ko", "country_export_en"),
    ]
    seed_rows = build_term_seed_rows(records, field_pairs)
    assert len(seed_rows) == 1, f"expected one merged key, got {seed_rows}"
    assert seed_rows[0][1] == "뉴질랜드"
    assert seed_rows[0][2] == "New Zealand"
    print("PASS: seed_map_terms merges country_origin and country_export columns")


def test_seed_map_terms_skips_never_mapped_term() -> None:
    """A term with zero non-empty EN anywhere must not appear in the seed."""
    records = [{"product_type_ko": "냉동", "product_type_en": ""}]
    seed_rows = build_term_seed_rows(records, [("product_type_ko", "product_type_en")])
    assert seed_rows == [], f"expected no seed row, got {seed_rows}"
    print("PASS: seed_map_terms skips terms with no known EN name anywhere")


if __name__ == "__main__":
    test_normalise_company_key_matches_legal_entity_variants()
    test_normalise_company_key_english_suffixes()
    test_normalise_company_key_empty_input()
    test_resolve_company_matched()
    test_resolve_company_unmatched_routes_to_review()
    test_seed_map_companies_picks_majority_en()
    test_seed_map_companies_skips_never_mapped_company()
    test_resolve_company_reused_for_country_mapping()
    test_seed_map_terms_merges_origin_and_export_columns()
    test_seed_map_terms_skips_never_mapped_term()
    print("\nAll trust-pipeline regression tests passed.")
