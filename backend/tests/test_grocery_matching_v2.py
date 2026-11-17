from copy import deepcopy

import pytest

from app.core.grocery_matching_v2 import (
    MATCHING_VERSION,
    StreetCatalog,
    decide_match,
    normalized_input,
    stable_hash,
    street_key,
)


def catalog(*names: str) -> StreetCatalog:
    return StreetCatalog(
        [{"city": "Radom", "street": name, "street_normalized": name.casefold()} for name in names]
    )


def evidence() -> dict:
    outlet = {
        "brand_id": "zabka",
        "source_id": "one",
        "city": "Radom",
        "street": "Traugutta",
        "house_number": "11",
        "postal_code": None,
        "has_coordinates": True,
        "previous_match_status": "unmatched",
        "previous_building_source_id": None,
    }
    return {
        "outlet": outlet,
        "normalized_input": normalized_input(outlet),
        "street_resolution": catalog("Romualda Traugutta").resolve("Traugutta", "Radom"),
        "outside_extent": False,
        "point_buildings": [],
        "address_candidates": [
            {
                "source_id": "address-one",
                "postal_code": "26-600",
                "building_source_id": "building-one",
                "building_id": 1,
                "building_distance_m": 1.5,
            }
        ],
        "source_references": {"municipal": [], "locator": {"url": "https://example.test"}},
    }


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("pl. plac Dworcowy", "dworcowy"),
        ("ul. Aleja Jana Pawła II", "jana pawla ii"),
        ("UL. ŻÓŁKIEWSKIEGO", "zolkiewskiego"),
        ("11 Listopada", "11 listopada"),
        (None, ""),
    ],
)
def test_street_format_normalization(raw, expected):
    assert street_key(raw) == expected


@pytest.mark.parametrize("number", ["104/122", "63A/8", "10-12", "10A", "10", "0"])
def test_house_numbers_are_not_split_or_approximated(number):
    assert normalized_input({"city": "Radom", "house_number": number})["house_number"] == (
        number.casefold()
    )


def test_unique_expansion_uses_city_dictionary_and_preserves_initials():
    names = catalog("Romualda Traugutta", "Józefa Grzecznarowskiego")
    assert names.resolve("R. Traugutta", "Radom")["method"] == "unique_name_expansion"
    assert names.resolve("J. Traugutta", "Radom")["method"] == "unrecognized_street"
    assert names.resolve("Traugutta", "Warszawa")["method"] == "unrecognized_street"
    assert names.resolve("J. Grzecznarowskiego", "Radom")["canonical_keys"] == [
        "jozefa grzecznarowskiego"
    ]


def test_street_ambiguity_is_not_resolved_using_house_or_locator():
    names = catalog("Stanisława Konarskiego", "Szymona Konarskiego")
    item = evidence()
    item["street_resolution"] = names.resolve("Konarskiego", "Radom")
    item["point_buildings"] = [{"source_id": "building-one", "id": 1}]
    result = decide_match(item)
    assert result["match_status"] == "ambiguous"
    assert result["building_source_id"] is None


def test_exact_short_street_takes_precedence_over_another_longer_street():
    result = catalog("Traugutta", "Romualda Traugutta").resolve("Traugutta", "Radom")
    assert result["method"] == "exact_normalized"
    assert result["canonical_keys"] == ["traugutta"]


@pytest.mark.parametrize("value", ["3 Maja", "Paderewskiego 14/5 i", "Focha 14 lok 1", "FOHCA"])
def test_numbers_and_typographic_guesses_do_not_expand(value):
    names = catalog("Konstytucji 3 Maja", "Ignacego Paderewskiego", "Ferdynanda Focha")
    assert names.resolve(value, "Radom")["method"] == "unrecognized_street"


@pytest.mark.parametrize("distance", [0.0, 1.5, 10.0])
def test_name_expansion_recovers_only_with_existing_ten_metre_rule(distance):
    item = evidence()
    item["address_candidates"][0]["building_distance_m"] = distance
    result = decide_match(item)
    assert result["match_status"] == "matched"
    assert result["match_method"] == "expanded_address_near_point"
    assert result["match_distance_m"] == distance


@pytest.mark.parametrize("distance", [10.00001, 77.9, None])
def test_name_expansion_does_not_widen_radius(distance):
    item = evidence()
    item["address_candidates"][0]["building_distance_m"] = distance
    assert decide_match(item)["match_status"] == "conflict"


def test_nearby_alternative_covering_building_is_not_overridden():
    item = evidence()
    item["point_buildings"] = [{"source_id": "other", "id": 2}]
    result = decide_match(item)
    assert result["match_status"] == "conflict"
    assert result["candidate_building_source_ids"] == ["building-one", "other"]


def test_expansion_without_coordinates_requires_more_evidence():
    item = evidence()
    item["outlet"]["has_coordinates"] = False
    result = decide_match(item)
    assert result["match_status"] == "unmatched"
    assert result["reason"] == "street_expansion_requires_coordinate_corroboration"


def test_multiple_address_points_on_same_building_are_not_ambiguous():
    item = evidence()
    item["outlet"]["has_coordinates"] = False
    item["street_resolution"]["method"] = "exact_normalized"
    item["address_candidates"].append({**item["address_candidates"][0], "source_id": "address-two"})
    result = decide_match(item)
    assert result["match_status"] == "matched"
    assert result["address_source_ids"] == ["address-one", "address-two"]


def test_unlinked_municipal_address_does_not_borrow_nearest_footprint():
    item = evidence()
    item["address_candidates"][0].update(building_source_id=None, building_id=None)
    result = decide_match(item)
    assert result["match_status"] == "unmatched"
    assert result["reason"] == "municipal_address_not_uniquely_linked_to_footprint"


@pytest.mark.parametrize("distance", [0.003, 4.28, 10.0])
def test_single_locator_cover_can_be_corroborated_by_one_unlinked_address_point(distance):
    item = evidence()
    item["point_buildings"] = [{"source_id": "building-one", "id": 1}]
    item["address_candidates"][0].update(
        building_source_id=None,
        building_id=None,
        address_match_status="unmatched",
        locator_covering_distance_m=distance,
    )
    result = decide_match(item)
    assert result["match_status"] == "matched"
    assert result["match_method"] == "point_inside_building_near_unlinked_address"
    assert result["address_source_id"] is None
    assert result["corroborating_address_source_ids"] == ["address-one"]
    assert result["match_distance_m"] == 0
    assert result["address_point_distance_m"] == distance


@pytest.mark.parametrize("distance", [10.01, 14.018, None])
def test_unlinked_address_without_close_corroboration_is_unknown_not_conflicting(distance):
    item = evidence()
    item["point_buildings"] = [{"source_id": "building-one", "id": 1}]
    item["address_candidates"][0].update(
        building_source_id=None,
        building_id=None,
        address_match_status="unmatched",
        locator_covering_distance_m=distance,
    )
    result = decide_match(item)
    assert result["match_status"] == "unmatched"
    assert result["reason"] == "municipal_address_does_not_corroborate_locator_footprint"


def test_ambiguous_municipal_point_is_not_treated_as_unlinked_boundary_offset():
    item = evidence()
    item["point_buildings"] = [{"source_id": "building-one", "id": 1}]
    item["address_candidates"][0].update(
        building_source_id=None,
        building_id=None,
        address_match_status="ambiguous",
        locator_covering_distance_m=1,
    )
    assert decide_match(item)["match_status"] == "ambiguous"


def test_duplicate_address_buildings_require_coordinate_disambiguation():
    item = evidence()
    item["address_candidates"].append(
        {**item["address_candidates"][0], "source_id": "address-two", "building_source_id": "two"}
    )
    assert decide_match(item)["match_status"] == "ambiguous"
    item["point_buildings"] = [{"source_id": "building-one", "id": 1}]
    assert decide_match(item)["match_status"] == "matched"


def test_postal_disagreement_needs_exact_point_corroboration_and_remains_visible():
    item = evidence()
    item["normalized_input"]["postal_code"] = "26-601"
    item["street_resolution"]["method"] = "exact_normalized"
    assert decide_match(item)["match_status"] == "conflict"
    item["point_buildings"] = [{"source_id": "building-one", "id": 1}]
    result = decide_match(item)
    assert result["match_status"] == "matched"
    assert result["postal_status"] == "disagreement"
    assert result["match_method"] == "postal_disagreement_address_and_point"
    item["point_buildings"] = [{"source_id": "other", "id": 2}]
    assert decide_match(item)["match_status"] == "conflict"


def test_postal_disagreement_unique_street_expansion_keeps_same_covering_building():
    item = evidence()
    item["normalized_input"]["postal_code"] = "26-601"
    item["point_buildings"] = [{"source_id": "building-one", "id": 1}]
    result = decide_match(item)
    assert result["match_status"] == "matched"
    assert result["postal_status"] == "disagreement"
    assert result["street_resolution"]["method"] == "unique_name_expansion"


def test_postal_filter_does_not_hide_a_competing_matching_address():
    item = evidence()
    item["normalized_input"]["postal_code"] = "26-601"
    item["address_candidates"].append(
        {
            **item["address_candidates"][0],
            "source_id": "address-two",
            "postal_code": "26-601",
            "building_source_id": "other",
            "building_id": 2,
        }
    )
    item["point_buildings"] = [{"source_id": "building-one", "id": 1}]
    assert decide_match(item)["match_status"] == "conflict"


def test_overlapping_footprints_never_choose_first():
    item = evidence()
    item["point_buildings"] = [
        {"source_id": "building-one", "id": 1},
        {"source_id": "two", "id": 2},
    ]
    assert decide_match(item)["match_status"] == "ambiguous"


def test_point_only_existing_association_is_retained_without_contradicting_address():
    item = evidence()
    item["address_candidates"] = []
    item["point_buildings"] = [{"source_id": "building-one", "id": 1}]
    result = decide_match(item)
    assert result["match_status"] == "matched"
    assert result["match_method"] == "point_inside_building"


def test_outside_extent_never_matches_even_with_agreeing_address():
    item = evidence()
    item["outside_extent"] = True
    assert decide_match(item)["match_status"] == "conflict"


def test_versioned_hashes_bind_decision_evidence_and_provenance():
    item = evidence()
    result = decide_match(item)
    assert result["matching_version"] == MATCHING_VERSION
    assert result["evidence_sha256"] == stable_hash(item)
    assert result["decision_sha256"] == stable_hash(
        {key: value for key, value in result.items() if key != "decision_sha256"}
    )
    changed = deepcopy(item)
    changed["source_references"]["locator"]["url"] = "https://example.test/changed"
    assert decide_match(changed)["evidence_sha256"] != result["evidence_sha256"]
    assert item == evidence()
