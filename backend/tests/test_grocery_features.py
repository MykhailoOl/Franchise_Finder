from copy import deepcopy
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.core.data_contract import PREDICTORS, RADII_M, FeatureObservation
from app.core.grocery_features import (
    EXTRACTION_VERSION,
    FEATURE_IDS,
    SOURCE_TABLES,
    build_feature_row,
    build_source_context,
    summarize_feature_rows,
)

CALCULATED_AT = datetime(2026, 11, 10, 12, 30, tzinfo=UTC)


def readiness():
    sources = {"map_sources": [], "profile_sources": []}
    for identifier, (table, _) in SOURCE_TABLES.items():
        metadata = {
            "id": identifier,
            "url": "https://example.org/source",
            "observed_at": "2021-03-31" if identifier == "warsaw-population-2021" else None,
            "retrieved_at": "2026-10-23T18:41:13Z",
            "snapshot_at": "2026-10-23T18:45:16Z" if "osm" in identifier else None,
            "coverage_bounds": [20.82, 52.07, 21.32, 52.4],
            "raw_sha256": "a" * 64,
            "notes": ["Mapped points are not measured customer activity"],
        }
        if table == "map_sources":
            sources[table].append(
                {
                    "id": identifier,
                    "url": "https://example.org/buildings",
                    "feature_count": 20,
                    "imported_at": "2026-10-23T19:15:17Z",
                    "updated_at": None,
                    "bounds": [20.82, 52.07, 21.32, 52.4],
                }
            )
        else:
            sources[table].append(
                {
                    "id": identifier,
                    "feature_count": 100,
                    "metadata": metadata,
                    "imported_at": "2026-11-01T19:15:17Z",
                }
            )
    return {
        "source_metadata": sources,
        "source_snapshots": {
            table: {"sha256": "b" * 64, "row_count": 100} for _, table in SOURCE_TABLES.values()
        },
    }


def source_context():
    return build_source_context(readiness(), CALCULATED_AT)


def raw_row(**changes):
    values = {name: 2 for name in FEATURE_IDS if name != "mapped_storeys"}
    values.update(
        {
            "building_source_id": "municipal:1",
            "building_footprint_area_m2": 123.456789,
            "mapped_building_use": "h",
            "mapped_storeys_raw": "3",
            "residential_building_fraction_500m": 0.25,
            "mapped_building_use_coverage_fraction_500m": 1,
            "neighbor_building_count_500m": 20,
            "known_use_building_count_500m": 20,
            "residential_building_count_500m": 5,
            "activity_excluded_onsite_count_500m": 1,
            "activity_excluded_inactive_count_500m": 2,
            "activity_lifecycle_ambiguous_count_500m": 1,
            "source_available_by_dataset": {name: True for name in SOURCE_TABLES},
            "source_coverage_fraction_by_radius": {
                name: {str(radius): 1 for radius in RADII_M} for name in SOURCE_TABLES
            },
        }
    )
    for radius in RADII_M:
        values[f"published_cell_area_fraction_{radius}m"] = 1
        values[f"population_cells_{radius}m"] = {
            "observed": 10,
            "missing": 0,
            "suppressed": 0,
            "total": 10,
        }
    values.update(changes)
    return values


def build(raw=None, context=None):
    return build_feature_row(raw or raw_row(), context or source_context(), CALCULATED_AT)


def test_frozen_features_are_compact_contract_validated_and_unrounded():
    context = source_context()
    row = build(raw_row(resident_subtotal_500m=12.3456789), context)
    assert len(row["values"]) == 17
    assert set(row["values"]) == set(FEATURE_IDS)
    assert not any(
        name in row["values"] for name in ("grocery_count_500m", "profit", "label", "fold")
    )
    assert row["values"]["resident_subtotal_500m"] == 12.3456789
    assert "calculated_at" not in row
    for name, observation in row["observations"].items():
        assert "calculated_at" not in observation
        assert "sources" not in observation
        resolved = FeatureObservation.model_validate(
            {
                "feature_id": name,
                "value": row["values"][name],
                **{key: value for key, value in observation.items() if key != "source_refs"},
                "sources": [
                    context["sources"][ref]["evidence"] for ref in observation["source_refs"]
                ],
                "calculated_at": CALCULATED_AT,
            }
        )
        assert resolved.status == "available"


def test_municipal_import_time_never_becomes_observation_or_retrieval():
    context = source_context()
    source = context["sources"]["warsaw-buildings"]
    assert source["evidence"]["observed_at"] is None
    assert source["evidence"]["retrieved_at"] is None
    assert source["evidence"]["snapshot_at"] is None
    assert source["evidence"]["date_limitations"]
    assert source["source_age_days"] is None
    assert source["source_age_basis"] is None
    assert source["coverage_bounds"] == [20.82, 52.07, 21.32, 52.4]
    assert (
        source["coverage_bounds_semantics"] == "mapped_feature_envelope_not_administrative_boundary"
    )


def test_source_age_uses_census_observation_and_osm_snapshot_calendar_dates():
    context = source_context()
    census = context["sources"]["warsaw-population-2021"]
    osm = context["sources"]["warsaw-osm-pois"]
    assert census["source_age_basis"] == "observed_at"
    assert census["source_age_days"] == (CALCULATED_AT.date() - datetime(2021, 3, 31).date()).days
    assert osm["source_age_basis"] == "snapshot_at"
    assert osm["source_age_days"] == 18
    report = readiness()
    next(
        row
        for row in report["source_metadata"]["profile_sources"]
        if row["id"] == "warsaw-osm-pois"
    )["metadata"]["snapshot_at"] = None
    assert (
        build_source_context(report, CALCULATED_AT)["sources"]["warsaw-osm-pois"]["source_age_days"]
        is None
    )


def test_source_snapshot_version_changes_with_metadata_or_data_content():
    original = source_context()["sources"]["warsaw-population-2021"]["snapshot_version"]
    changed = readiness()
    changed["source_snapshots"]["profile_population"]["sha256"] = "c" * 64
    assert (
        build_source_context(changed, CALCULATED_AT)["sources"]["warsaw-population-2021"][
            "snapshot_version"
        ]
        != original
    )
    changed = readiness()
    next(
        row
        for row in changed["source_metadata"]["profile_sources"]
        if row["id"] == "warsaw-population-2021"
    )["metadata"]["observed_at"] = "2021-04-01"
    assert (
        build_source_context(changed, CALCULATED_AT)["sources"]["warsaw-population-2021"][
            "snapshot_version"
        ]
        != original
    )


def test_source_context_needs_an_aware_calculation_time():
    with pytest.raises(ValidationError):
        build_source_context(readiness(), "2026-11-10T12:30:00")


def test_missing_source_records_or_empty_datasets_never_become_zero():
    for mode in ("record", "rows", "fingerprint"):
        report = readiness()
        if mode == "record":
            report["source_metadata"]["profile_sources"] = []
        elif mode == "rows":
            report["source_snapshots"]["profile_pois"]["row_count"] = 0
        else:
            report["source_snapshots"].pop("profile_pois")
        result = build(raw_row(bus_stop_count_500m=0), build_source_context(report, CALCULATED_AT))
        assert result["values"]["bus_stop_count_500m"] is None
        assert result["observations"]["bus_stop_count_500m"]["status"] == "unavailable"


@pytest.mark.parametrize("coverage", [0, None])
def test_absent_population_cells_are_missing_even_if_raw_sum_is_zero(coverage):
    result = build(raw_row(resident_subtotal_500m=0, published_cell_area_fraction_500m=coverage))
    assert result["values"]["resident_subtotal_500m"] is None
    assert result["observations"]["resident_subtotal_500m"]["status"] == "missing"


@pytest.mark.parametrize("subtotal", [0, 2.345678901])
def test_partial_population_is_published_subtotal_without_extrapolation(subtotal):
    result = build(raw_row(resident_subtotal_500m=subtotal, published_cell_area_fraction_500m=0.4))
    assert result["values"]["resident_subtotal_500m"] == subtotal
    assert result["observations"]["resident_subtotal_500m"]["status"] == "partial"
    assert result["quality"]["published_cell_area_fraction_500m"] == 0.4


def test_positive_population_coverage_requires_actual_subtotal():
    with pytest.raises(ValueError, match="subtotal"):
        build(raw_row(resident_subtotal_500m=None))


@pytest.mark.parametrize(
    "coverage,status",
    [(1, "available"), (0.5, "partial"), (0, "outside_coverage"), (None, "missing")],
)
def test_count_zero_distinguishes_query_coverage(coverage, status):
    raw = raw_row(bus_stop_count_500m=0)
    raw["source_coverage_fraction_by_radius"]["warsaw-osm-pois"]["500"] = coverage
    result = build(raw)
    assert result["observations"]["bus_stop_count_500m"]["status"] == status
    assert result["values"]["bus_stop_count_500m"] == (0 if coverage else None)


@pytest.mark.parametrize(
    "coverage,status",
    [(1, "censored"), (0.5, "outside_coverage"), (0, "outside_coverage"), (None, "missing")],
)
def test_absent_nearest_point_is_never_a_numeric_search_limit(coverage, status):
    raw = raw_row(nearest_bus_stop_distance_m=None)
    raw["source_coverage_fraction_by_radius"]["warsaw-osm-pois"]["1000"] = coverage
    result = build(raw)
    assert result["values"]["nearest_bus_stop_distance_m"] is None
    assert result["observations"]["nearest_bus_stop_distance_m"]["status"] == status
    assert result["quality"]["nearest_distance_censored_by_category"]["bus_stop"] == (
        status == "censored"
    )


def test_observed_nearest_distance_at_search_boundary_and_partial_coverage():
    raw = raw_row(nearest_bus_stop_distance_m=1000)
    assert build(raw)["values"]["nearest_bus_stop_distance_m"] == 1000
    raw["source_coverage_fraction_by_radius"]["warsaw-osm-pois"]["1000"] = 0.5
    assert build(raw)["observations"]["nearest_bus_stop_distance_m"]["status"] == "partial"
    raw["nearest_bus_stop_distance_m"] = 1000.01
    with pytest.raises(ValueError, match="search radius"):
        build(raw)


@pytest.mark.parametrize("raw_value", [None, 0, "0", -1, 1.5, "invalid", True, float("nan")])
def test_nonpositive_invalid_or_absent_storeys_remain_missing(raw_value):
    result = build(raw_row(mapped_storeys_raw=raw_value))
    assert result["values"]["mapped_storeys"] is None
    assert result["quality"]["zero_or_missing_raw_storeys"]
    assert result["observations"]["mapped_storeys"]["status"] == "missing"


@pytest.mark.parametrize("raw_value", [2, 2.0, "2", "2.0"])
def test_positive_integer_storeys_are_preserved(raw_value):
    assert build(raw_row(mapped_storeys_raw=raw_value))["values"]["mapped_storeys"] == 2


@pytest.mark.parametrize("raw_value", [None, "unknown", "commercial", "M", {}, 0])
def test_only_known_municipal_use_codes_are_values(raw_value):
    result = build(raw_row(mapped_building_use=raw_value))
    assert result["values"]["mapped_building_use"] is None
    assert result["quality"]["unknown_mapped_building_use"]


def test_residential_fraction_excludes_unknown_uses_and_marks_incompleteness():
    result = build(
        raw_row(
            residential_building_fraction_500m=0.25, mapped_building_use_coverage_fraction_500m=0.8
        )
    )
    assert result["values"]["residential_building_fraction_500m"] == 0.25
    assert result["observations"]["residential_building_fraction_500m"]["status"] == "partial"
    assert result["quality"]["unknown_neighbor_building_use_fraction_500m"] == pytest.approx(0.2)
    result = build(
        raw_row(
            residential_building_fraction_500m=None, mapped_building_use_coverage_fraction_500m=0
        )
    )
    assert result["values"]["residential_building_fraction_500m"] is None


@pytest.mark.parametrize(
    "feature,value",
    [
        ("building_footprint_area_m2", 0),
        ("building_footprint_area_m2", float("inf")),
        ("school_count_500m", float("nan")),
        ("parking_count_500m", -1),
        ("bus_stop_count_500m", 0.5),
        ("bus_stop_count_500m", True),
        ("resident_subtotal_500m", "2"),
        ("residential_building_fraction_500m", 1.1),
    ],
)
def test_invalid_numeric_predictors_fail_validation(feature, value):
    with pytest.raises(ValueError):
        build(raw_row(**{feature: value}))


def test_raw_source_unavailability_overrides_stale_numbers():
    raw = raw_row(school_count_500m=0)
    raw["source_available_by_dataset"]["warsaw-osm-activity"] = False
    result = build(raw)
    assert result["values"]["school_count_500m"] is None
    assert result["observations"]["school_count_500m"]["status"] == "unavailable"


def test_lifecycle_and_boundary_diagnostics_survive_conversion():
    raw = raw_row(source_coverage_method="declared_source_bbox")
    raw["source_origin_inside_bounds_by_dataset"] = {"warsaw-osm-activity": False}
    raw["source_building_within_bounds_by_dataset"] = {"warsaw-buildings": True}
    result = build(raw)
    assert result["quality"]["activity_lifecycle_ambiguous_count_500m"] == 1
    assert result["quality"]["source_coverage_method"] == "declared_source_bbox"
    assert (
        result["quality"]["source_origin_inside_bounds_by_dataset"]["warsaw-osm-activity"] is False
    )
    assert (
        result["quality"]["source_origin_inside_bounds_by_dataset"]["warsaw-population-2021"]
        is None
    )
    assert result["quality"]["source_building_within_bounds_by_dataset"]["warsaw-buildings"] is True


def test_missing_raw_columns_and_mixed_extraction_dates_are_rejected():
    raw = raw_row()
    raw.pop("school_count_500m")
    with pytest.raises(ValueError, match="columns are missing"):
        build(raw)
    context = source_context()
    context["as_of_date"] = "2026-11-09"
    with pytest.raises(ValueError, match="age date"):
        build(context=context)


def test_feature_rows_are_deterministic_within_one_extraction_date():
    context = source_context()
    morning = build_feature_row(raw_row(), context, "2026-11-10T08:00:00Z")
    evening = build_feature_row(raw_row(), context, "2026-11-10T20:00:00Z")
    assert morning == evening
    assert morning["extraction_version"] == EXTRACTION_VERSION


def test_quality_summary_has_only_supplied_rows_and_missingness_distributions():
    first = build()
    second = build(
        raw_row(
            building_source_id="municipal:2", mapped_storeys_raw=0, building_footprint_area_m2=200
        )
    )
    result = summarize_feature_rows(iter([first, second]))
    assert result["row_count"] == 2
    assert result["scope"] == "supplied_development_rows_only"
    assert result["final_holdout_statistics_computed"] is False
    assert result["predictors"]["mapped_storeys"]["missing_fraction"] == 0.5
    assert result["predictors"]["mapped_storeys"]["status_counts"] == {
        "available": 1,
        "missing": 1,
    }
    assert result["predictors"]["building_footprint_area_m2"]["observed_values"]["max"] == 200
    assert result["quality_flag_counts"]["invalid_raw_storeys"] == 1
    assert set(result["predictors"]) == set(PREDICTORS) - {
        "grocery_count_500m",
        "quick_service_count_500m",
        "pizza_count_500m",
        "beauty_count_500m",
    }
    with pytest.raises(ValueError, match="unique"):
        summarize_feature_rows([first, first])
    changed = deepcopy(second)
    changed["extraction_version"] = "different"
    with pytest.raises(ValueError, match="versions"):
        summarize_feature_rows([first, changed])


def test_empty_summary_does_not_invent_distribution_or_missing_fraction():
    result = summarize_feature_rows([])
    assert result["row_count"] == 0
    for feature in result["predictors"].values():
        assert feature["missing_fraction"] is None
        assert feature["coverage_fraction"]["mean"] is None
