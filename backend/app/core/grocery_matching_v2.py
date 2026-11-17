import hashlib
import json
import re
from collections import defaultdict

from app.core.address_search import STREET_PREFIXES, normalize_house_number, normalize_street

MATCHING_VERSION = "grocery-address-matching-v2"
MAXIMUM_DISTANCE_M = 10.0
POLICY = {
    "version": MATCHING_VERSION,
    "maximum_distance_m": MAXIMUM_DISTANCE_M,
    "street_expansion": "unique_municipality_dictionary_suffix_or_initials_with_coordinates",
    "postal_disagreement": (
        "unique_resolved_street_exact_address_and_same_single_covering_footprint"
    ),
    "house_number": "exact_preserve_suffix_slash_and_range",
    "nearest_only_matching": False,
    "coordinate_conflicts": "unresolved",
    "unlinked_municipal_address": (
        "single_locator_cover_with_one_exact_house_unmatched_address_point_within_10m"
    ),
    "references": [
        "https://eli.gov.pl/eli/DU/2021/1368/ogl",
        "https://postgis.net/docs/ST_Covers.html",
    ],
}


def stable_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def street_key(value: str | None) -> str:
    words = normalize_street(value or "").split()
    while words and words[0] in STREET_PREFIXES:
        words.pop(0)
    return " ".join(words)


def compatible_expansion(short: str, full: str) -> bool:
    left, right = short.split(), full.split()
    if not left or len(left) > len(right) or len(left[-1]) < 4:
        return False
    if left[-1] != right[-1] or any(any(c.isdigit() for c in token) for token in left):
        return False
    return all(
        a == b or (len(a) == 1 and b.startswith(a))
        for a, b in zip(left, right[-len(left) :], strict=True)
    )


class StreetCatalog:
    def __init__(self, streets: list[dict]):
        self.by_city = defaultdict(lambda: defaultdict(set))
        for row in streets:
            key = street_key(row["street"])
            if key:
                self.by_city[normalize_street(row["city"])][key].add(row["street_normalized"])

    def resolve(self, street: str | None, city: str) -> dict:
        key = street_key(street)
        dictionary = self.by_city.get(normalize_street(city), {})
        candidates = []
        if key in dictionary:
            candidates = [key]
            method = "exact_normalized"
        elif key:
            candidates = sorted(name for name in dictionary if compatible_expansion(key, name))
            method = (
                "unique_name_expansion"
                if len(candidates) == 1
                else "ambiguous_name_expansion"
                if candidates
                else "unrecognized_street"
            )
        else:
            method = "missing_street"
        return {
            "input_key": key,
            "method": method if key else "missing_street",
            "canonical_keys": candidates,
            "stored_keys": sorted({item for name in candidates for item in dictionary[name]}),
            "municipality_scoped": True,
            "authoritative_alias": False,
        }


def normalized_input(outlet: dict) -> dict:
    postal = re.sub(r"\s+", "", outlet.get("postal_code") or "") or None
    return {
        "city": normalize_street(outlet["city"]),
        "street": street_key(outlet.get("street")),
        "house_number": normalize_house_number(outlet.get("house_number") or "") or None,
        "postal_code": postal,
        "postal_code_valid": postal is None or bool(re.fullmatch(r"\d{2}-\d{3}", postal)),
    }


def decide_match(evidence: dict) -> dict:
    outlet = evidence["outlet"]
    normalized = evidence["normalized_input"]
    street = evidence["street_resolution"]
    addresses = sorted(evidence["address_candidates"], key=lambda row: row["source_id"])
    points = sorted(evidence["point_buildings"], key=lambda row: row["source_id"])
    postal = normalized["postal_code"]
    compatible = [row for row in addresses if not postal or row["postal_code"] == postal]
    postal_status = (
        "not_supplied"
        if not postal
        else "consistent"
        if compatible
        else "disagreement"
        if addresses
        else "unverified"
    )
    candidates = compatible or addresses
    linked = {row["building_source_id"] for row in candidates if row["building_source_id"]}
    all_linked = bool(candidates) and all(row["building_source_id"] for row in candidates)
    unique = next(iter(linked)) if len(linked) == 1 and all_linked else None
    point = points[0]["source_id"] if len(points) == 1 else None
    expanded = street["method"] == "unique_name_expansion"
    target = None
    status = "unmatched"
    method = None
    distance = None
    address_point_distance = None
    corroborating_address_ids = []
    reason = "insufficient_unique_building_evidence"
    if evidence["outside_extent"]:
        status, reason = "conflict", "locator_point_outside_dataset_extent"
    elif not normalized["postal_code_valid"]:
        status, reason = "conflict", "invalid_explicit_postal_code"
    elif street["method"] == "ambiguous_name_expansion":
        status, reason = "ambiguous", "street_expansion_not_unique_in_municipality"
    elif len(points) > 1:
        status, reason = "ambiguous", "locator_point_covers_multiple_buildings"
    elif postal_status == "disagreement":
        if unique and point == unique and len(candidates) == 1:
            target, method, distance = unique, "postal_disagreement_address_and_point", 0.0
            reason = "exact_address_and_single_covering_building_agree_despite_postal_disagreement"
        else:
            status, reason = "conflict", "explicit_postal_code_disagrees_with_municipal_address"
    elif not outlet["has_coordinates"]:
        if expanded:
            reason = "street_expansion_requires_coordinate_corroboration"
        elif unique:
            target, method = unique, "exact_address"
            reason = "exact_municipal_address_identifies_one_building"
        elif len(candidates) > 1:
            status, reason = "ambiguous", "address_identifies_multiple_or_unlinked_buildings"
    elif point:
        if point in linked:
            target = point
            method = "expanded_address_and_point" if expanded else "address_and_point"
            distance, reason = 0.0, "municipal_address_and_single_covering_building_agree"
        elif linked:
            status, reason = "conflict", "locator_point_and_municipal_address_do_not_agree"
        elif candidates:
            candidate = candidates[0]
            address_distance = candidate.get("locator_covering_distance_m")
            if (
                len(candidates) == 1
                and candidate.get("address_match_status") == "unmatched"
                and address_distance is not None
                and 0 <= address_distance <= MAXIMUM_DISTANCE_M
            ):
                target, method, distance = point, "point_inside_building_near_unlinked_address", 0.0
                address_point_distance = address_distance
                corroborating_address_ids = [candidate["source_id"]]
                reason = "single_cover_and_unlinked_municipal_address_point_within_10m"
            elif any(row.get("address_match_status") == "ambiguous" for row in candidates):
                status, reason = "ambiguous", "municipal_address_has_multiple_covering_buildings"
            else:
                reason = "municipal_address_does_not_corroborate_locator_footprint"
        else:
            target, method, distance = point, "point_inside_building", 0.0
            reason = "single_covering_building_without_resolved_municipal_address"
    elif unique:
        distances = [row["building_distance_m"] for row in candidates]
        if all(value is not None and 0 <= value <= MAXIMUM_DISTANCE_M for value in distances):
            target = unique
            method = "expanded_address_near_point" if expanded else "exact_address_near_point"
            distance, reason = (
                max(distances),
                "address_building_within_10m_and_no_alternative_cover",
            )
        else:
            status, reason = "conflict", "locator_point_exceeds_10m_from_address_building"
    elif len(candidates) > 1:
        status, reason = "ambiguous", "address_identifies_multiple_or_unlinked_buildings"
    elif candidates:
        reason = "municipal_address_not_uniquely_linked_to_footprint"
    if target:
        status = "matched"
    selected_addresses = [row for row in candidates if row["building_source_id"] == target]
    address_ids = [row["source_id"] for row in selected_addresses] if target else []
    all_buildings = {
        row["building_source_id"]: row["building_id"]
        for row in addresses
        if row["building_source_id"]
    }
    all_buildings.update({row["source_id"]: row["id"] for row in points})
    decision = {
        "matching_version": MATCHING_VERSION,
        "policy_sha256": stable_hash(POLICY),
        "evidence_sha256": stable_hash(evidence),
        "brand_id": outlet["brand_id"],
        "source_id": outlet["source_id"],
        "previous_match_status": outlet["previous_match_status"],
        "previous_building_source_id": outlet["previous_building_source_id"],
        "match_status": status,
        "building_source_id": target,
        "address_source_id": address_ids[0] if address_ids else None,
        "address_source_ids": address_ids,
        "corroborating_address_source_ids": corroborating_address_ids or address_ids,
        "match_method": method,
        "match_distance_m": distance,
        "address_point_distance_m": address_point_distance,
        "reason": reason,
        "candidate_building_ids": sorted(set(all_buildings.values())),
        "candidate_building_source_ids": sorted(all_buildings),
        "normalized_input": normalized,
        "street_resolution": street,
        "postal_status": postal_status,
        "source_references": evidence["source_references"],
        "association_is_inferred": True,
    }
    decision["decision_sha256"] = stable_hash(decision)
    return decision
