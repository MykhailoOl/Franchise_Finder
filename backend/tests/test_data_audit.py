from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.cli.audit_data import json_value
from app.core.data_audit import source_date_issues, summarize_population_sample
from app.core.profile_data import ProfilePoi


def test_population_audit_keeps_unknown_separate_from_explicit_zero() -> None:
    summary = summarize_population_sample(
        [
            {"radius_m": 300, "population_subtotal": None, "published_cell_area_fraction": 0},
            {"radius_m": 300, "population_subtotal": 0, "published_cell_area_fraction": 0.5},
            {"radius_m": 300, "population_subtotal": 100, "published_cell_area_fraction": 1},
        ]
    )[0]
    assert summary["no_published_cells"] == 1
    assert summary["explicit_zero_subtotals"] == 1
    assert summary["published_cell_area_fraction_median"] == 0.5
    assert "not resident completeness" in summary["interpretation"]


def test_source_dates_do_not_replace_observation_with_import_time() -> None:
    assert source_date_issues({"imported_at": "2026-11-01T12:00:00Z"}) == [
        "Retrieval date is unknown; import time is not a substitute"
    ]
    assert (
        source_date_issues({"retrieved_at": "2026-11-01T12:00:00Z", "observed_at": "2021-03-31"})
        == []
    )
    assert source_date_issues(
        {"retrieved_at": "2026-11-01T12:00:00Z", "snapshot_at": "2026-11-01T13:00:00Z"}
    ) == ["snapshot_at is later than retrieval"]
    assert "observed_at is not an ISO date or timestamp" in source_date_issues(
        {"retrieved_at": "2026-11-01T12:00:00Z", "observed_at": "unknown"}
    )
    assert json_value(datetime(2026, 11, 1, tzinfo=UTC)) == "2026-11-01T00:00:00+00:00"


def test_activity_categories_cannot_be_smuggled_into_competition() -> None:
    record = {
        "source_id": "node/1",
        "kind": "activity",
        "categories": ["school", "school"],
        "source_url": "https://www.openstreetmap.org/node/1",
    }
    assert ProfilePoi.model_validate(record).categories == ["school"]
    with pytest.raises(ValidationError, match="does not belong"):
        ProfilePoi.model_validate({**record, "kind": "competitor"})
    with pytest.raises(ValidationError, match="does not belong"):
        ProfilePoi.model_validate({**record, "categories": ["grocery"]})
