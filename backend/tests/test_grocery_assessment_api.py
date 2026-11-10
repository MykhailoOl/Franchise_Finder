from copy import deepcopy
from unittest.mock import MagicMock

import psycopg
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError

from app.core.config import Settings
from app.db.buildings import MapDataUnavailableError
from app.main import create_app


@pytest.fixture
def assessment_api(monkeypatch):
    engine = MagicMock(spec=Engine)
    monkeypatch.setattr("app.main.create_database_engine", lambda settings: engine)
    payload = {
        "building_id": 12,
        "building_source_id": "municipal-building-12",
        "category": "grocery",
        "versions": {"features": "warsaw-grocery-features-v1"},
        "evidence": [
            {
                "feature_id": "resident_subtotal_500m",
                "value": None,
                "status": "missing",
                "unit": "people",
            }
        ],
        "missing_details": [{"id": "usable_unit_area_m2", "status": "unknown"}],
        "_runtime": {"internal": "not public"},
        "score": 0.95,
        "shap": [{"contribution": 1.0}],
        "predictions": [{"score": 0.95}],
        "ranking": {"status": "available", "score": 0.95, "rank": 1},
    }
    state = {
        "status": "validation_withheld",
        "version": "warsaw-grocery-catboost-v1",
        "evaluated_at": "2026-11-09T12:00:00Z",
        "source_status": "metadata_match_content_unverified",
        "reason": "Recommendations are not available yet.",
    }
    get_assessment = MagicMock(side_effect=lambda *args: deepcopy(payload))
    monkeypatch.setattr("app.api.routes.map.grocery_assessments.get_assessment", get_assessment)
    with TestClient(create_app(Settings(_env_file=None))) as client:
        client.app.state.grocery_model_status = MagicMock()
        client.app.state.grocery_model_status.describe.side_effect = lambda runtime: deepcopy(state)
        yield client, get_assessment, state


def test_anonymous_assessment_preserves_unknown_values_and_withholds_all_model_outputs(
    assessment_api,
):
    client, get_assessment, _ = assessment_api
    response = client.get("/api/v1/map/buildings/12/grocery-assessment")
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "evidence_only"
    assert result["ranking"]["status"] == "withheld"
    assert result["ranking"]["score"] is result["ranking"]["rank"] is None
    assert result["evidence"][0]["value"] is None
    assert result["evidence"][0]["status"] == "missing"
    assert result["missing_details"][0]["status"] == "unknown"
    assert result["versions"]["model"] == "warsaw-grocery-catboost-v1"
    assert not {"_runtime", "score", "shap", "predictions"}.intersection(result)
    assert "set-cookie" not in response.headers
    assert response.headers["cache-control"] == "public, max-age=0, must-revalidate"
    assert get_assessment.call_args.args[1] == 12


@pytest.mark.parametrize("identifier", ["0", "-1", "9007199254740992", "invalid"])
def test_invalid_assessment_building_id_does_not_query_database(assessment_api, identifier):
    client, get_assessment, _ = assessment_api
    response = client.get(f"/api/v1/map/buildings/{identifier}/grocery-assessment")
    assert response.status_code == 422
    get_assessment.assert_not_called()


def test_unknown_building_returns_404(assessment_api):
    client, get_assessment, _ = assessment_api
    get_assessment.side_effect = None
    get_assessment.return_value = None
    response = client.get("/api/v1/map/buildings/12/grocery-assessment")
    assert response.status_code == 404
    assert response.json() == {"detail": "Building not found"}


@pytest.mark.parametrize(
    "error",
    [
        OperationalError("", {}, Exception("password=secret")),
        psycopg.OperationalError("postgresql://secret@localhost/private"),
        ValueError("private artifact path C:/secret/model.joblib"),
    ],
)
def test_assessment_failures_are_sanitized(assessment_api, error):
    client, get_assessment, _ = assessment_api
    get_assessment.side_effect = error
    response = client.get("/api/v1/map/buildings/12/grocery-assessment")
    assert response.status_code == 503
    assert response.json() == {"detail": "The grocery assessment is unavailable"}


def test_missing_buildings_returns_a_user_facing_error(assessment_api):
    client, get_assessment, _ = assessment_api
    get_assessment.side_effect = MapDataUnavailableError
    response = client.get("/api/v1/map/buildings/12/grocery-assessment")
    assert response.status_code == 503
    assert response.json() == {"detail": "Building information is unavailable"}


def test_etag_changes_with_model_state_even_when_evidence_is_cached(assessment_api):
    client, _, state = assessment_api
    path = "/api/v1/map/buildings/12/grocery-assessment"
    original = client.get(path)
    etag = original.headers["etag"]
    assert client.get(path, headers={"If-None-Match": etag}).status_code == 304
    state.update(
        {
            "status": "stale",
            "source_status": "changed",
            "reason": "Source data has changed; recommendations remain unavailable.",
        }
    )
    changed = client.get(path, headers={"If-None-Match": etag})
    assert changed.status_code == 200
    assert changed.headers["etag"] != etag
    assert changed.json()["model"]["status"] == "stale"
    assert changed.json()["ranking"]["score"] is None
