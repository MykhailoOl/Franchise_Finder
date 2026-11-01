from copy import deepcopy
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError

from app.core.config import Settings
from app.core.franchise_snapshot import FranchiseSnapshot
from app.main import create_app


@pytest.fixture
def snapshot() -> dict:
    return {
        "schema_version": 1,
        "categories": [{"id": "grocery", "name": "Grocery"}],
        "brands": [
            {
                "id": "example",
                "name": "Example",
                "category_id": "grocery",
                "website": "https://example.test",
                "source_url": "https://example.test/stores",
                "retrieved_at": "2026-10-23T12:00:00Z",
                "coverage_status": "complete",
                "coverage_note": "Published Warsaw locator snapshot",
            }
        ],
        "outlets": [
            {
                "source_id": "store-1",
                "brand_id": "example",
                "name": "Example Warsaw",
                "address": {
                    "raw": "Example 10, Warszawa",
                    "street": "Example",
                    "house_number": "10",
                    "city": "Warszawa",
                },
                "coordinates": [21.01, 52.23],
                "source_url": "https://example.test/store-1",
                "retrieved_at": "2026-10-23T12:00:00Z",
                "operating_model": "unknown",
            }
        ],
    }


def test_source_identity_and_missing_coordinates_are_preserved(snapshot: dict) -> None:
    snapshot["outlets"][0]["coordinates"] = None
    result = FranchiseSnapshot.model_validate(snapshot)
    assert result.outlets[0].source_id == "store-1"
    assert result.outlets[0].coordinates is None
    assert result.outlets[0].operating_model == "unknown"


def test_duplicate_identity_rejects_entire_snapshot(snapshot: dict) -> None:
    snapshot["outlets"].append(deepcopy(snapshot["outlets"][0]))
    with pytest.raises(ValidationError, match="source IDs must be unique"):
        FranchiseSnapshot.model_validate(snapshot)


def test_claimed_franchise_requires_outlet_evidence(snapshot: dict) -> None:
    snapshot["outlets"][0]["operating_model"] = "franchise"
    with pytest.raises(ValidationError, match="outlet-specific evidence"):
        FranchiseSnapshot.model_validate(snapshot)


@pytest.mark.parametrize("coordinates", [[float("nan"), 52], [21, float("inf")], [190, 52]])
def test_invalid_coordinates_reject_snapshot(snapshot: dict, coordinates: list[float]) -> None:
    snapshot["outlets"][0]["coordinates"] = coordinates
    with pytest.raises(ValidationError, match="finite longitude"):
        FranchiseSnapshot.model_validate(snapshot)


def test_outside_locality_and_missing_brand_are_rejected(snapshot: dict) -> None:
    snapshot["outlets"][0]["address"]["city"] = "Piaseczno"
    with pytest.raises(ValidationError, match="explicitly identified Warsaw"):
        FranchiseSnapshot.model_validate(snapshot)
    snapshot["outlets"][0]["address"]["city"] = "Warsaw"
    snapshot["outlets"][0]["brand_id"] = "missing"
    with pytest.raises(ValidationError, match="reference a brand"):
        FranchiseSnapshot.model_validate(snapshot)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    engine = MagicMock(spec=Engine)
    monkeypatch.setattr("app.main.create_database_engine", lambda settings: engine)
    with TestClient(create_app(Settings(_env_file=None))) as test_client:
        yield test_client


@pytest.mark.parametrize("brand_ids", ["example,", "../../brands", "a" + ",a" * 50])
def test_invalid_filters_do_not_query_database(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, brand_ids: str
) -> None:
    get_outlets = MagicMock()
    monkeypatch.setattr("app.api.routes.map.franchises.get_outlets", get_outlets)
    assert client.get("/api/v1/map/outlets", params={"brand_ids": brand_ids}).status_code == 422
    get_outlets.assert_not_called()


def test_brand_filters_and_null_geometry_are_retained(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "id": 1, "geometry": None, "properties": {"brand_id": "example"}}
        ],
    }
    get_outlets = MagicMock(return_value=payload)
    monkeypatch.setattr("app.api.routes.map.franchises.get_outlets", get_outlets)
    response = client.get("/api/v1/map/outlets?brand_ids=example,second,example")
    assert response.status_code == 200
    assert response.json() == payload
    assert get_outlets.call_args.args[1] == ["example", "second"]


@pytest.mark.parametrize(
    "endpoint,method", [("franchises", "get_catalog"), ("outlets", "get_outlets")]
)
def test_database_failures_do_not_expose_connection_details(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, endpoint: str, method: str
) -> None:
    monkeypatch.setattr(
        f"app.api.routes.map.franchises.{method}",
        MagicMock(side_effect=OperationalError("", {}, Exception("password=secret"))),
    )
    response = client.get(f"/api/v1/map/{endpoint}")
    assert response.status_code == 503
    assert response.json() == {"detail": "The franchise database is unavailable"}
