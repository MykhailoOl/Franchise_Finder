import pytest
from pydantic import ValidationError

from app.core.data_contract import FeatureObservation, SourceEvidence, data_contract


def evidence():
    return {
        "source_id": "warsaw-population-2021",
        "source_url": "https://geo.stat.gov.pl/atom-web/atom/GRID",
        "snapshot_version": "census-2021-extract-2026-10-23",
        "observed_at": "2021-03-31",
        "retrieved_at": "2026-10-23T18:41:13Z",
        "snapshot_at": None,
    }


def observation(**changes):
    return FeatureObservation.model_validate(
        {
            "feature_id": "resident_subtotal_500m",
            "value": 0,
            "unit": "people",
            "status": "available",
            "coverage_fraction": 1,
            "sources": [evidence()],
            "calculated_at": "2026-11-01T18:00:00Z",
            **changes,
        }
    )


def test_zero_requires_an_observation_and_missing_values_remain_null():
    assert observation().value == 0
    assert observation(value=None, status="missing", reason="No published cells").value is None
    with pytest.raises(ValidationError):
        observation(value=0, status="missing", reason="No published cells")
    with pytest.raises(ValidationError):
        observation(value=None)


@pytest.mark.parametrize("value", [-1, float("inf"), float("nan"), "unknown", True])
def test_invalid_numeric_values_are_rejected(value):
    with pytest.raises(ValidationError):
        observation(value=value)


def test_partial_subtotal_requires_explanation_and_provenance():
    partial = observation(
        status="partial", reason="Absent cells are unknown", coverage_fraction=0.4
    )
    assert partial.coverage_fraction == 0.4
    with pytest.raises(ValidationError):
        observation(status="partial")
    with pytest.raises(ValidationError):
        observation(sources=[])
    with pytest.raises(ValidationError):
        observation(unit="m2")


def test_unknown_source_dates_are_not_retrieval_dates():
    with pytest.raises(ValidationError):
        SourceEvidence.model_validate({**evidence(), "observed_at": None})
    source = SourceEvidence.model_validate(
        {**evidence(), "observed_at": None, "date_limitations": ["No observation date published"]}
    )
    assert source.observed_at is None


@pytest.mark.parametrize("coverage", [None, 0, 0.4])
def test_missing_census_coverage_cannot_be_a_complete_zero(coverage):
    with pytest.raises(ValidationError):
        observation(value=0, coverage_fraction=coverage)


@pytest.mark.parametrize("coverage", [None, 0])
def test_partial_census_values_still_require_observed_cells(coverage):
    with pytest.raises(ValidationError):
        observation(status="partial", reason="Incomplete coverage", coverage_fraction=coverage)


@pytest.mark.parametrize(
    "feature_id,unit,value,source_id",
    [
        ("mapped_storeys", "storeys", 0, "warsaw-buildings"),
        ("mapped_storeys", "storeys", 1.5, "warsaw-buildings"),
        ("building_footprint_area_m2", "m2", 0, "warsaw-buildings"),
        ("grocery_count_500m", "count", 0.5, "warsaw-osm-pois"),
        ("mapped_building_use", "category", "commercially_permitted", "warsaw-buildings"),
    ],
)
def test_source_field_domains_are_preserved(feature_id, unit, value, source_id):
    with pytest.raises(ValidationError):
        observation(
            feature_id=feature_id,
            unit=unit,
            value=value,
            sources=[{**evidence(), "source_id": source_id}],
        )


def test_unrelated_sources_cannot_provide_predictor_evidence():
    with pytest.raises(ValidationError):
        observation(sources=[{**evidence(), "source_id": "unrelated"}])
    known = observation(
        feature_id="grocery_count_500m",
        unit="count",
        value=5,
        sources=[{**evidence(), "source_id": "brand-zabka"}],
        status="partial",
        reason="Only one official brand source is available",
    )
    assert known.value == 5


def test_contract_is_only_data_definitions_and_excludes_business_outcomes():
    contract = data_contract()
    assert contract["status"] == "definitions_only_not_computed"
    assert 15 <= len(contract["predictors"]) <= 25
    assert len({item["id"] for item in contract["predictors"]}) == len(contract["predictors"])
    assert "profit" in contract["excluded_targets"]
    assert "usable_unit_area_m2" in contract["missing_premises"]
    competition = [item for item in contract["predictors"] if item["id"] == "grocery_count_500m"]
    assert competition[0]["exclude_selected_building"]
