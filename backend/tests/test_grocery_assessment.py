from copy import deepcopy
from datetime import UTC, datetime

import pytest
from test_grocery_features import raw_row, readiness

from app.core.brand_requirements import REQUIREMENTS_BY_BRAND
from app.core.grocery_assessment import (
    HIGHLIGHTS,
    build_assessment,
    build_live_source_context,
    grocery_requirements,
)
from app.core.grocery_features import SOURCE_TABLES, build_feature_row

NOW = datetime(2026, 11, 10, 14, tzinfo=UTC)


def snapshot():
    return {
        "source_metadata": readiness()["source_metadata"],
        "tables": {table: True for pair in SOURCE_TABLES.values() for table in pair},
        "profile_revision": 17,
        "source_revision": "current-metadata-revision",
    }


def assessment(raw=None, sources=None):
    context = build_live_source_context(sources or snapshot(), NOW)
    row = build_feature_row(raw or raw_row(), context, NOW)
    return build_assessment(
        row,
        context,
        123,
        {"coordinates": [21.01, 52.23], "method": "building_point_on_surface"},
        "current-metadata-revision",
        NOW,
    )


def test_live_context_preserves_dates_and_disclaims_full_content_verification():
    context = build_live_source_context(snapshot(), NOW)
    municipal = context["sources"]["warsaw-buildings"]
    assert municipal["available"] is True
    assert municipal["data_sha256"] is None
    assert municipal["fingerprint_scope"] == "source_metadata_and_import_revision"
    assert municipal["evidence"]["observed_at"] is None
    assert municipal["evidence"]["retrieved_at"] is None
    assert municipal["source_age_days"] is None
    assert any("import time" in item for item in municipal["limitations"])
    population = context["sources"]["warsaw-population-2021"]
    assert population["evidence"]["observed_at"] == "2021-03-31"
    assert population["source_age_basis"] == "observed_at"
    assert population["source_age_days"] > 2000
    assert context["sources"]["warsaw-osm-activity"]["source_age_basis"] == "snapshot_at"


def test_missing_or_invalid_source_metadata_does_not_invent_evidence_or_zero():
    sources = snapshot()
    sources["tables"]["profile_activity"] = False
    population = sources["source_metadata"]["profile_sources"][0]
    population["metadata"]["observed_at"] = "invalid-date"
    result = assessment(sources=sources)
    evidence = {item["id"]: item for item in result["evidence"]}
    assert evidence["school_count_500m"]["status"] == "unavailable"
    assert evidence["school_count_500m"]["value"] is None
    assert evidence["resident_subtotal_500m"]["status"] == "unavailable"
    assert evidence["resident_subtotal_500m"]["value"] is None
    assert evidence["building_footprint_area_m2"]["value"] == 123.456789


def test_assessment_preserves_partial_censored_missing_and_observed_zero():
    raw = raw_row(
        resident_subtotal_300m=12.3456789,
        published_cell_area_fraction_300m=0.4,
        resident_subtotal_500m=None,
        published_cell_area_fraction_500m=0,
        nearest_tram_stop_distance_m=None,
        school_count_500m=0,
        mapped_storeys_raw=0,
    )
    result = assessment(raw)
    evidence = {item["id"]: item for item in result["evidence"]}
    assert evidence["resident_subtotal_300m"]["value"] == 12.3456789
    assert evidence["resident_subtotal_300m"]["status"] == "partial"
    assert evidence["resident_subtotal_300m"]["coverage_fraction"] == 0.4
    assert evidence["resident_subtotal_500m"]["value"] is None
    assert evidence["resident_subtotal_500m"]["status"] == "missing"
    assert evidence["nearest_tram_stop_distance_m"]["value"] is None
    assert evidence["nearest_tram_stop_distance_m"]["status"] == "censored"
    assert evidence["school_count_500m"]["value"] == 0
    assert evidence["school_count_500m"]["status"] == "available"
    assert evidence["mapped_storeys"]["value"] is None
    assert result["evidence_highlights"] == list(HIGHLIGHTS)
    assert result["available_radii_m"] == [300, 500, 1000]


def test_requirements_remain_unknown_regardless_of_footprint_and_return_isolated_data():
    original = deepcopy(REQUIREMENTS_BY_BRAND)
    result = assessment(raw_row(building_footprint_area_m2=1000000))
    assert result["status"] == "evidence_only"
    assert result["ranking"]["status"] == "withheld"
    assert result["ranking"]["score"] is None
    assert result["ranking"]["rank"] is None
    assert {item["brand_id"] for item in result["requirements"]} == {"zabka", "lewiatan"}
    for brand in result["requirements"]:
        assert brand["status"] == "unknown"
        for format_record in brand["formats"]:
            for item in format_record["requirements"]:
                assert item["assessment_status"] == "unknown"
                assert item["public_building_evaluable"] is False
                assert item["source_url"].startswith("https://")
                item["label"] = "Caller mutation"
    assert all(item["status"] == "unknown" for item in result["missing_details"])
    assert REQUIREMENTS_BY_BRAND == original
    assert grocery_requirements()[0]["formats"][0]["requirements"][0]["label"] != "Caller mutation"
    assert not any(name in result for name in ("shap", "predictions", "probability", "model_score"))


def test_source_version_changes_on_metadata_refresh_and_future_age_is_unknown():
    sources = snapshot()
    old = build_live_source_context(sources, NOW)
    record = sources["source_metadata"]["profile_sources"][-1]
    record["metadata"]["snapshot_at"] = "2026-11-12T00:00:00Z"
    new = build_live_source_context(sources, NOW)
    source_id = "warsaw-osm-activity"
    assert (
        old["sources"][source_id]["snapshot_version"]
        != new["sources"][source_id]["snapshot_version"]
    )
    assert new["sources"][source_id]["source_age_days"] is None
    assert any("future" in note for note in new["sources"][source_id]["limitations"])
    with pytest.raises(ValueError, match="timezone"):
        build_live_source_context(sources, datetime(2026, 11, 10))
