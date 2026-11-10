from copy import deepcopy
from datetime import UTC, date, datetime

from pydantic import ValidationError

from app.core.brand_requirements import REQUIREMENTS_VERSION, get_brand_requirements
from app.core.data_contract import (
    DATA_CONTRACT_VERSION,
    MISSING_PREMISES,
    PREDICTORS,
    SourceEvidence,
)
from app.core.experiment import content_hash
from app.core.grocery_features import EXTRACTION_VERSION, FEATURE_IDS, SOURCE_TABLES

ASSESSMENT_VERSION = "warsaw-grocery-assessment-v1"
WITHHELD_REASON = (
    "A reliable grocery recommendation is not available yet. "
    "The current models have not passed independent validation."
)
HIGHLIGHTS = (
    "school_count_500m",
    "university_count_500m",
    "office_count_500m",
    "retail_destination_count_500m",
)
SOURCE_NAMES = {
    "warsaw-buildings": "Warsaw municipal building footprints",
    "warsaw-population-2021": "Statistics Poland 2021 population grid",
    "warsaw-osm-pois": "OpenStreetMap facilities and access points",
    "warsaw-osm-activity": "OpenStreetMap activity places",
}
LABELS = {
    "building_footprint_area_m2": "Building footprint area",
    "mapped_building_use": "Mapped building use",
    "mapped_storeys": "Mapped above-ground storeys",
    "resident_subtotal_300m": "Residents in published cells within 300 m",
    "resident_subtotal_500m": "Residents in published cells within 500 m",
    "resident_subtotal_1000m": "Residents in published cells within 1 km",
    "nearest_bus_stop_distance_m": "Nearest mapped bus stop",
    "nearest_tram_stop_distance_m": "Nearest mapped tram stop",
    "nearest_rail_station_distance_m": "Nearest mapped rail station",
    "nearest_metro_entrance_distance_m": "Nearest mapped metro entrance",
    "bus_stop_count_500m": "Mapped bus stops within 500 m",
    "parking_count_500m": "Mapped parking features within 500 m",
    "school_count_500m": "Schools",
    "university_count_500m": "Universities",
    "office_count_500m": "Offices",
    "retail_destination_count_500m": "Retail destinations",
    "residential_building_fraction_500m": "Residential share of nearby classified buildings",
}
MISSING_LABELS = {
    "usable_unit_area_m2": "Usable commercial unit area",
    "rent_and_service_charges": "Rent and service charges",
    "vacancy_and_lease_availability": "Vacancy and lease availability",
    "frontage_and_customer_entrance": "Frontage and customer entrance",
    "step_free_access": "Step-free access",
    "utilities_and_ventilation": "Utilities and ventilation",
    "permitted_unit_use": "Permitted use of the commercial unit",
}


def build_live_source_context(snapshot: dict, calculated_at: datetime) -> dict:
    if calculated_at.tzinfo is None:
        raise ValueError("Assessment timestamps must include a timezone")
    calculated_at = calculated_at.astimezone(UTC)
    sources = {}
    for identifier, (metadata_table, data_table) in SOURCE_TABLES.items():
        records = snapshot["source_metadata"][metadata_table]
        record = next((row for row in records if row["id"] == identifier), None)
        metadata = (
            (record.get("metadata") or {})
            if record and metadata_table == ("profile_sources")
            else record or {}
        )
        digest = content_hash(record)
        version = f"{identifier}:metadata:{digest}"
        limitations = [
            "Source metadata and import revision are checked; live table content is not rehashed"
        ]
        dates = {
            name: metadata.get(name) for name in ("observed_at", "retrieved_at", "snapshot_at")
        }
        for name in ("observed_at", "retrieved_at"):
            if dates[name] is None:
                limitations.append(f"{name} is unknown; import time is not source evidence")
        if identifier.startswith("warsaw-osm-") and dates["snapshot_at"] is None:
            limitations.append("OSM snapshot time is unknown")
        evidence = None
        if metadata.get("url"):
            try:
                evidence = SourceEvidence.model_validate(
                    {
                        "source_id": identifier,
                        "source_url": metadata["url"],
                        "snapshot_version": version,
                        **dates,
                        "date_limitations": limitations,
                    }
                )
            except ValidationError:
                limitations.append("Source URL or dates are invalid; this source is unavailable")
        age_basis = (
            "observed_at"
            if identifier == "warsaw-population-2021"
            else "snapshot_at"
            if identifier.startswith("warsaw-osm-")
            else None
        )
        basis_date = getattr(evidence, age_basis, None) if evidence and age_basis else None
        if isinstance(basis_date, datetime):
            basis_date = basis_date.astimezone(UTC).date()
        age = (calculated_at.date() - basis_date).days if isinstance(basis_date, date) else None
        if age is not None and age < 0:
            age = None
            limitations.append("Source age is unknown because its basis date is in the future")
        available = bool(
            evidence
            and snapshot["tables"].get(data_table)
            and record
            and record.get("feature_count", 0) > 0
        )
        sources[identifier] = {
            "available": available,
            "name": metadata.get("name") or SOURCE_NAMES[identifier],
            "attribution": metadata.get("attribution"),
            "license": metadata.get("license"),
            "evidence": evidence.model_dump(mode="json") if evidence else None,
            "snapshot_version": version,
            "metadata_sha256": digest,
            "data_sha256": None,
            "fingerprint_scope": "source_metadata_and_import_revision",
            "source_age_days": age,
            "source_age_basis": age_basis,
            "source_age_basis_date": basis_date.isoformat() if basis_date else None,
            "coverage_bounds": metadata.get("bounds")
            if metadata_table == "map_sources"
            else metadata.get("coverage_bounds"),
            "limitations": limitations,
        }
    return {
        "version": EXTRACTION_VERSION,
        "as_of_date": calculated_at.date().isoformat(),
        "sources": sources,
    }


def grocery_requirements() -> list[dict]:
    result = []
    for brand_id, name in (("zabka", "Żabka"), ("lewiatan", "Lewiatan")):
        brand = get_brand_requirements(brand_id)
        brand.update({"brand_name": name, "status": "unknown"})
        urls = {source["id"]: source["url"] for source in brand["sources"]}
        for format_record in brand["formats"]:
            for requirement in format_record["requirements"]:
                requirement.update(
                    {
                        "assessment_status": "unknown",
                        "assessment_reason": (
                            "These public criteria require confirmation with the chain."
                            if requirement["strength"] in {"unknown", "approval"}
                            else "The public map does not verify this unit-level requirement."
                        ),
                        "source_url": urls[requirement["source_id"]],
                    }
                )
        result.append(brand)
    return result


def build_assessment(
    feature_row: dict,
    source_context: dict,
    building_id: int,
    origin: dict,
    source_revision: str,
    calculated_at: datetime,
) -> dict:
    if feature_row["extraction_version"] != EXTRACTION_VERSION:
        raise ValueError("Assessment features must use the shared extraction version")
    if set(feature_row["values"]) != set(FEATURE_IDS) or set(feature_row["observations"]) != set(
        FEATURE_IDS
    ):
        raise ValueError("Assessment requires the complete shared environmental feature contract")
    evidence = []
    for identifier in FEATURE_IDS:
        definition = PREDICTORS[identifier]
        evidence.append(
            {
                "id": identifier,
                "label": LABELS[identifier],
                "value": feature_row["values"][identifier],
                **deepcopy(feature_row["observations"][identifier]),
                "radius_m": definition.radius_m,
                "method": definition.method,
                "limitation": definition.limitation,
            }
        )
    sources = []
    for identifier, source in source_context["sources"].items():
        item = source["evidence"] or {}
        sources.append(
            {
                "id": identifier,
                "name": source["name"],
                "attribution": source["attribution"],
                "url": item.get("source_url"),
                **{key: item.get(key) for key in ("observed_at", "retrieved_at", "snapshot_at")},
                "limitations": source["limitations"].copy(),
                "available": source["available"],
                "snapshot_version": source["snapshot_version"],
                "source_age_days": source["source_age_days"],
                "source_age_basis": source["source_age_basis"],
            }
        )
    return {
        "building_id": building_id,
        "building_source_id": feature_row["building_source_id"],
        "category": "grocery",
        "status": "evidence_only",
        "reason": WITHHELD_REASON,
        "computed_at": calculated_at.astimezone(UTC).isoformat(),
        "origin": deepcopy(origin),
        "available_radii_m": [300, 500, 1000],
        "evidence": evidence,
        "evidence_highlights": list(HIGHLIGHTS),
        "evidence_note": (
            "These are mapped nearby places, not measured visitors or customer traffic. "
            "Distances are straight-line from inside the building, not walking routes."
        ),
        "quality": deepcopy(feature_row["quality"]),
        "sources": sources,
        "requirements": grocery_requirements(),
        "missing_details": [
            {"id": name, "label": MISSING_LABELS[name], "status": "unknown"}
            for name in MISSING_PREMISES
        ],
        "versions": {
            "assessment": ASSESSMENT_VERSION,
            "features": EXTRACTION_VERSION,
            "data_contract": DATA_CONTRACT_VERSION,
            "requirements": REQUIREMENTS_VERSION,
            "source_revision": source_revision,
        },
        "ranking": {"status": "withheld", "score": None, "rank": None, "reason": WITHHELD_REASON},
    }
