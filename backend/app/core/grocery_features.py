import math
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, date, datetime
from numbers import Real

from pydantic import AwareDatetime, TypeAdapter

from app.core.data_contract import (
    BUILDING_USE_CODES,
    DATA_CONTRACT_VERSION,
    PREDICTORS,
    RADII_M,
    FeatureObservation,
    SourceEvidence,
)
from app.core.experiment import content_hash, load_experiment

EXTRACTION_VERSION = "warsaw-grocery-features-v1"
FEATURE_IDS = load_experiment().primary_predictors
COMPLETE_COVERAGE_TOLERANCE = 1e-9
SOURCE_TABLES = {
    "warsaw-buildings": ("map_sources", "buildings"),
    "warsaw-population-2021": ("profile_sources", "profile_population"),
    "warsaw-osm-pois": ("profile_sources", "profile_pois"),
    "warsaw-osm-activity": ("profile_sources", "profile_activity"),
}
EXTRACTION_POLICY = {
    "version": EXTRACTION_VERSION,
    "contract_version": DATA_CONTRACT_VERSION,
    "predictors": list(FEATURE_IDS),
    "origin": "ST_PointOnSurface of the municipal footprint in EPSG:2180",
    "distance": "straight_line_metres",
    "radii_m": list(RADII_M),
    "complete_coverage_tolerance": COMPLETE_COVERAGE_TOLERANCE,
    "population": "unrounded_area_weighted_published_subtotal_no_extrapolation",
    "absent_population": "unknown_never_zero",
    "count_zero": "observed_empty_query_only_with_positive_declared_area_coverage",
    "partial_counts": "measured_subset_not_an_estimate_of_complete_catchment",
    "nearest_empty": "censored_only_with_complete_1000m_query_coverage",
    "nearest_partial": "recorded_distance_not_guaranteed_nearest_in_full_catchment",
    "mapped_storeys": "positive_integer_source_value_otherwise_missing",
    "residential_fraction": "residential_neighbor_count_over_known_use_neighbor_count",
    "residential_use_codes": ["m"],
    "known_use_codes": sorted(BUILDING_USE_CODES),
    "focal_activity": "exclude_points_covered_by_selected_building",
    "competition": "excluded_until_fold_aware_target_masking_is_versioned",
    "source_age": "UTC_calendar_days_since_census_observation_or_OSM_snapshot",
    "municipal_dates": "import_timestamp_is_not_observation_or_retrieval",
    "coverage": "geographic_query_coverage_does_not_establish_real_world_completeness",
    "quality_summary": "only_supplied_development_rows_no_labels_or_holdout_statistics",
}


def _aware_datetime(value: datetime | str) -> datetime:
    return TypeAdapter(AwareDatetime).validate_python(value).astimezone(UTC)


def _number(value: object, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be numeric or null")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{name} must be finite and nonnegative")
    return result


def _fraction(value: object, name: str) -> float | None:
    result = _number(value, name)
    if result is not None and result > 1:
        raise ValueError(f"{name} must be at most one")
    return result


def _complete(value: float | None) -> bool:
    return value is not None and value >= 1 - COMPLETE_COVERAGE_TOLERANCE


def _snapshot_digest(snapshot: dict) -> str | None:
    digest = snapshot.get("sha256")
    if digest is None:
        return None
    if not isinstance(digest, str) or len(digest) != 64:
        raise ValueError("Source snapshot requires a SHA256 digest")
    try:
        bytes.fromhex(digest)
    except ValueError as error:
        raise ValueError("Source snapshot requires a SHA256 digest") from error
    return digest


def build_source_context(readiness_report: dict, calculated_at: datetime | str) -> dict:
    calculated = _aware_datetime(calculated_at)
    source_metadata = readiness_report["source_metadata"]
    snapshots = readiness_report["source_snapshots"]
    sources = {}
    for identifier, (metadata_table, data_table) in SOURCE_TABLES.items():
        matches = [
            row for row in source_metadata.get(metadata_table, []) if row["id"] == identifier
        ]
        if len(matches) > 1:
            raise ValueError(f"Duplicate source metadata: {identifier}")
        record = matches[0] if matches else None
        metadata = (
            (record.get("metadata") or {})
            if metadata_table == "profile_sources" and record
            else record or {}
        )
        snapshot = snapshots.get(data_table, {})
        digest = _snapshot_digest(snapshot)
        version_digest = content_hash({"source_metadata": record, "data_sha256": digest})
        version = f"{identifier}:{version_digest}"
        limitations = []
        dates = {
            name: metadata.get(name) for name in ("observed_at", "retrieved_at", "snapshot_at")
        }
        for name in ("observed_at", "retrieved_at"):
            if dates[name] is None:
                limitations.append(f"{name} is unknown; import time is not source evidence")
        if identifier.startswith("warsaw-osm-") and dates["snapshot_at"] is None:
            limitations.append("OSM snapshot time is unknown")
        url = metadata.get("url")
        evidence = None
        if record is not None and digest is not None and url:
            evidence = SourceEvidence.model_validate(
                {
                    "source_id": identifier,
                    "source_url": url,
                    "snapshot_version": version,
                    **dates,
                    "date_limitations": limitations,
                }
            )
        row_count = _number(snapshot.get("row_count"), f"{data_table}.row_count")
        feature_count = _number((record or {}).get("feature_count"), f"{identifier}.feature_count")
        available = (
            evidence is not None
            and row_count is not None
            and row_count > 0
            and (feature_count is None or feature_count > 0)
        )
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
        age = (calculated.date() - basis_date).days if isinstance(basis_date, date) else None
        if age is not None and age < 0:
            limitations.append("Source age is unknown because its basis date is in the future")
            age = None
        bounds = (
            metadata.get("bounds")
            if metadata_table == "map_sources"
            else metadata.get("coverage_bounds")
        )
        sources[identifier] = {
            "available": available,
            "name": metadata.get("name"),
            "attribution": metadata.get("attribution"),
            "license": metadata.get("license"),
            "evidence": evidence.model_dump(mode="json") if evidence else None,
            "snapshot_version": version,
            "content_sha256": version_digest,
            "data_sha256": digest,
            "source_content_hashes": {
                key: value for key, value in metadata.items() if key.endswith("sha256")
            },
            "source_age_days": age,
            "source_age_basis": age_basis,
            "source_age_basis_date": basis_date.isoformat() if basis_date else None,
            "coverage_bounds": bounds,
            "coverage_bounds_semantics": metadata.get("coverage_bounds_semantics")
            or (
                "mapped_feature_envelope_not_administrative_boundary"
                if bounds and metadata_table == "map_sources"
                else "declared_extract_query_bounds"
                if bounds
                else "not_declared"
            ),
            "source_notes": metadata.get("notes", []),
            "lifecycle_conflict_count": metadata.get("lifecycle_conflict_count"),
            "lifecycle_conflict_source_ids": metadata.get("lifecycle_conflict_source_ids", []),
            "limitations": limitations,
        }
    return {
        "version": EXTRACTION_VERSION,
        "as_of_date": calculated.date().isoformat(),
        "sources": sources,
    }


def _storeys(value: object) -> tuple[float | None, bool]:
    if value is None:
        return None, False
    if isinstance(value, bool) or not isinstance(value, (Real, str)):
        return None, True
    try:
        parsed = float(value)
    except ValueError:
        return None, True
    if not math.isfinite(parsed) or parsed <= 0 or not parsed.is_integer():
        return None, True
    return parsed, False


def _coverage(raw: dict, identifier: str, radius: int) -> float | None:
    values = raw["source_coverage_fraction_by_radius"].get(identifier, {})
    return _fraction(values.get(str(radius)), f"{identifier}.{radius}.coverage")


def _observation(feature_id: str, raw: dict, context: dict, calculated: datetime) -> dict:
    definition = PREDICTORS[feature_id]
    source_id = definition.sources[0]
    source = context["sources"][source_id]
    available = source["available"] and raw["source_available_by_dataset"][source_id]
    evidence = [source["evidence"]] if source["evidence"] else []
    value = None
    status = "unavailable"
    reason = "Source data is unavailable"
    coverage = None
    if available:
        measured = raw.get(feature_id)
        status, reason = "available", None
        if feature_id == "mapped_storeys":
            value, invalid = _storeys(raw["mapped_storeys_raw"])
            if value is None:
                status = "missing"
                reason = (
                    "Raw storeys are not a positive integer"
                    if invalid
                    else "No mapped storey value was supplied"
                )
        elif feature_id == "mapped_building_use":
            if isinstance(measured, str) and measured in BUILDING_USE_CODES:
                value = measured
            else:
                status, reason = "missing", "No recognized municipal building-use code"
        elif feature_id == "building_footprint_area_m2":
            value = _number(measured, feature_id)
            if value is None:
                status, reason = "missing", "No footprint area was calculated"
        elif feature_id.startswith("resident_subtotal_"):
            coverage = _fraction(
                raw[f"published_cell_area_fraction_{definition.radius_m}m"], feature_id
            )
            query_coverage = _coverage(raw, source_id, definition.radius_m)
            if coverage is None or coverage <= 0:
                status = "outside_coverage" if query_coverage == 0 else "missing"
                reason = (
                    "No published population cells cover this catchment; absent cells are unknown"
                )
            else:
                value = _number(measured, feature_id)
                if value is None:
                    raise ValueError(
                        "Positive published-cell coverage requires a population subtotal"
                    )
                if not _complete(coverage):
                    status = "partial"
                    reason = (
                        "Published-cell subtotal only; population in uncovered cells is unknown"
                    )
        elif feature_id == "residential_building_fraction_500m":
            coverage = _fraction(raw["mapped_building_use_coverage_fraction_500m"], feature_id)
            query_coverage = _coverage(raw, source_id, 500)
            if query_coverage == 0:
                status, reason = "outside_coverage", "Catchment is outside declared source coverage"
            elif coverage is None or coverage == 0:
                status, reason = "missing", "No neighboring buildings have a known use code"
            else:
                value = _fraction(measured, feature_id)
                if value is None:
                    raise ValueError("Known-use neighbors require a residential fraction")
                if not _complete(coverage) or not _complete(query_coverage):
                    status, reason = (
                        "partial",
                        "Unknown building uses or geographic coverage remain",
                    )
        else:
            coverage = _coverage(raw, source_id, definition.radius_m)
            numeric = _number(measured, feature_id)
            if coverage is None or coverage == 0:
                status = "outside_coverage" if coverage == 0 else "missing"
                reason = "No declared geographic coverage for this catchment"
            elif feature_id.startswith("nearest_"):
                if numeric is None:
                    status = "censored" if _complete(coverage) else "outside_coverage"
                    reason = (
                        "No mapped point within the fully covered 1000m search radius"
                        if status == "censored"
                        else "Incomplete query coverage cannot establish absence within 1000m"
                    )
                elif numeric > definition.radius_m:
                    raise ValueError("Nearest point is outside the declared search radius")
                else:
                    value = numeric
                    if not _complete(coverage):
                        status = "partial"
                        reason = (
                            "Nearest recorded point in covered area; full catchment is incomplete"
                        )
            elif numeric is None:
                status, reason = "missing", "No count was calculated for this source"
            else:
                value = numeric
                if not _complete(coverage):
                    status = "partial"
                    reason = "Count covers only the observed part of the catchment"
    result = FeatureObservation.model_validate(
        {
            "feature_id": feature_id,
            "value": value,
            "unit": definition.unit,
            "status": status,
            "reason": reason,
            "coverage_fraction": coverage,
            "sources": evidence,
            "calculated_at": calculated,
        }
    )
    return {
        "value": result.value,
        "status": result.status,
        "unit": result.unit,
        "reason": result.reason,
        "coverage_fraction": result.coverage_fraction,
        "source_refs": [item.source_id for item in result.sources],
    }


def build_feature_row(raw: dict, source_context: dict, calculated_at: datetime | str) -> dict:
    calculated = _aware_datetime(calculated_at)
    if source_context["version"] != EXTRACTION_VERSION:
        raise ValueError("Feature source context version does not match extraction")
    if source_context["as_of_date"] != calculated.date().isoformat():
        raise ValueError("Source age date does not match extraction date")
    identifier = raw.get("building_source_id")
    if not isinstance(identifier, str) or not identifier.strip():
        raise ValueError("Feature rows require a stable building source identity")
    required = (set(FEATURE_IDS) - {"mapped_storeys"}) | {"mapped_storeys_raw"}
    if required - raw.keys():
        raise ValueError(f"Raw feature columns are missing: {sorted(required - raw.keys())}")
    for source_id in SOURCE_TABLES:
        if type(raw["source_available_by_dataset"].get(source_id)) is not bool:
            raise ValueError("Source availability must be explicit for every predictor dataset")
    observations = {
        name: _observation(name, raw, source_context, calculated) for name in FEATURE_IDS
    }
    values = {name: item.pop("value") for name, item in observations.items()}
    known_fraction = _fraction(
        raw["mapped_building_use_coverage_fraction_500m"],
        "mapped_building_use_coverage_fraction_500m",
    )
    storeys, invalid_storeys = _storeys(raw["mapped_storeys_raw"])
    coverage = {
        source_id: {str(radius): _coverage(raw, source_id, radius) for radius in RADII_M}
        for source_id in SOURCE_TABLES
    }
    quality = {
        **{
            f"published_cell_area_fraction_{radius}m": _fraction(
                raw[f"published_cell_area_fraction_{radius}m"], f"population.{radius}.coverage"
            )
            for radius in RADII_M
        },
        "mapped_building_use_coverage_fraction_500m": known_fraction,
        "unknown_neighbor_building_use_fraction_500m": 1 - known_fraction
        if known_fraction is not None
        else None,
        "source_coverage_fraction_by_radius": coverage,
        "source_available_by_dataset": {
            source_id: raw["source_available_by_dataset"][source_id] and source["available"]
            for source_id, source in source_context["sources"].items()
        },
        "source_age_days_by_dataset": {
            source_id: source["source_age_days"]
            for source_id, source in source_context["sources"].items()
        },
        "source_age_basis_by_dataset": {
            source_id: source["source_age_basis"]
            for source_id, source in source_context["sources"].items()
        },
        "missingness_by_predictor": {name: value is None for name, value in values.items()},
        "nearest_distance_censored_by_category": {
            name.removeprefix("nearest_").removesuffix("_distance_m"): item["status"] == "censored"
            for name, item in observations.items()
            if name.startswith("nearest_")
        },
        "zero_or_missing_raw_storeys": storeys is None,
        "invalid_raw_storeys": invalid_storeys,
        "unknown_mapped_building_use": values["mapped_building_use"] is None,
        "source_coverage_method": raw.get("source_coverage_method"),
    }
    for field in (
        "source_origin_inside_bounds_by_dataset",
        "source_building_within_bounds_by_dataset",
    ):
        flags = {name: (raw.get(field) or {}).get(name) for name in SOURCE_TABLES}
        if any(value is not None and type(value) is not bool for value in flags.values()):
            raise ValueError("Source boundary membership must be boolean or unknown")
        quality[field] = flags
    for key in (
        "activity_excluded_onsite_count_500m",
        "activity_excluded_inactive_count_500m",
        "activity_lifecycle_ambiguous_count_500m",
        "neighbor_building_count_500m",
        "known_use_building_count_500m",
        "residential_building_count_500m",
    ):
        quality[key] = _number(raw.get(key), key)
    for radius in RADII_M:
        key = f"population_cells_{radius}m"
        quality[key] = {
            name: _number(value, f"{key}.{name}") for name, value in (raw.get(key) or {}).items()
        }
    return {
        "building_source_id": identifier,
        "extraction_version": EXTRACTION_VERSION,
        "values": values,
        "observations": observations,
        "quality": quality,
    }


def _distribution(values: list[float]) -> dict:
    ordered = sorted(values)
    if not ordered:
        return {
            "count": 0,
            "min": None,
            "p25": None,
            "median": None,
            "p75": None,
            "max": None,
            "mean": None,
        }

    def quantile(fraction: float) -> float:
        position = (len(ordered) - 1) * fraction
        lower, upper = math.floor(position), math.ceil(position)
        return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)

    return {
        "count": len(ordered),
        "min": ordered[0],
        "p25": quantile(0.25),
        "median": quantile(0.5),
        "p75": quantile(0.75),
        "max": ordered[-1],
        "mean": math.fsum(ordered) / len(ordered),
    }


def summarize_feature_rows(rows: Iterable[dict]) -> dict:
    identifiers = set()
    statuses = {name: Counter() for name in FEATURE_IDS}
    numeric = {name: [] for name in FEATURE_IDS if PREDICTORS[name].unit != "category"}
    categories = Counter()
    coverage = {name: [] for name in FEATURE_IDS}
    source_missing = Counter({name: 0 for name in SOURCE_TABLES})
    quality_flags = Counter()
    for row in rows:
        identifier = row["building_source_id"]
        if identifier in identifiers:
            raise ValueError("Quality summaries require unique building rows")
        identifiers.add(identifier)
        if row.get("extraction_version") != EXTRACTION_VERSION:
            raise ValueError("Quality summary cannot mix feature extraction versions")
        if set(row["values"]) != set(FEATURE_IDS) or set(row["observations"]) != set(FEATURE_IDS):
            raise ValueError("Quality summary requires the frozen 17 predictors")
        for name, value in row["values"].items():
            observation = row["observations"][name]
            statuses[name][observation["status"]] += 1
            if value is not None:
                if name in numeric:
                    numeric[name].append(_number(value, name))
                else:
                    if value not in BUILDING_USE_CODES:
                        raise ValueError("Quality summary found an unknown building-use category")
                    categories[value] += 1
            fraction = _fraction(observation["coverage_fraction"], name)
            if fraction is not None:
                coverage[name].append(fraction)
        for name, available in row["quality"]["source_available_by_dataset"].items():
            if not available:
                source_missing[name] += 1
        for flag in (
            "zero_or_missing_raw_storeys",
            "invalid_raw_storeys",
            "unknown_mapped_building_use",
        ):
            quality_flags[flag] += int(row["quality"][flag])
    count = len(identifiers)
    return {
        "scope": "supplied_development_rows_only",
        "row_count": count,
        "final_holdout_statistics_computed": False,
        "predictors": {
            name: {
                "status_counts": dict(sorted(statuses[name].items())),
                "missing_count": count
                - sum(statuses[name][status] for status in ("available", "partial")),
                "missing_fraction": (
                    count - sum(statuses[name][status] for status in ("available", "partial"))
                )
                / count
                if count
                else None,
                "coverage_fraction": _distribution(coverage[name]),
                "observed_values": _distribution(numeric[name])
                if name in numeric
                else {
                    "count": sum(categories.values()),
                    "categories": dict(sorted(categories.items())),
                },
            }
            for name in FEATURE_IDS
        },
        "source_unavailable_row_counts": dict(sorted(source_missing.items())),
        "quality_flag_counts": dict(sorted(quality_flags.items())),
    }
