from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError

from app.core.config import Settings
from app.core.profile_data import PopulationCell, duplicate_pois
from app.db.profiles import competition_profile
from app.main import create_app


def test_unknown_population_cannot_be_silently_zeroed() -> None:
    with pytest.raises(ValidationError, match="Only observed cells"):
        PopulationCell(
            source_id="cell",
            population=0,
            observation_year=2021,
            resolution_m=125,
            status="missing",
        )
    with pytest.raises(ValidationError, match="Only observed cells"):
        PopulationCell(
            source_id="cell",
            population=None,
            observation_year=2021,
            resolution_m=125,
            status="observed",
        )
    assert (
        PopulationCell(
            source_id="cell",
            population=0,
            observation_year=2021,
            resolution_m=125,
            status="observed",
        ).population
        == 0
    )


def test_poi_duplicates_are_local_named_and_do_not_chain_merge() -> None:
    def row(identifier: str, x: int, name: str = "shop", kind: str = "competitor") -> dict:
        return {
            "source_id": identifier,
            "x": x,
            "y": 0,
            "normalized_name": name,
            "categories": ["grocery"],
            "kind": kind,
            "location_method": "node",
        }

    rows = [
        row("a", 0),
        row("b", 20),
        row("c", 40),
        row("d", 0, "different"),
        row("e", 0, ""),
        row("f", 0, ""),
        row("g", 0, kind="access"),
    ]
    assert duplicate_pois(rows) == {"b": "a"}


def test_transit_references_preserve_directions_and_prefer_platforms() -> None:
    def row(identifier: str, x: int, reference: str, transport_role: str) -> dict:
        return {
            "source_id": identifier,
            "x": x,
            "y": 0,
            "normalized_name": "fort wola",
            "categories": ["tram_stop"],
            "kind": "access",
            "location_method": "node",
            "tags": {"ref": reference, "public_transport": transport_role},
        }

    rows = [
        row("stop", 25, "502103", "stop_position"),
        row("platform", 0, "502103", "platform"),
        row("opposite", 2, "502104", "platform"),
        row("distant", 110, "502103", "stop_position"),
    ]
    assert duplicate_pois(rows) == {"stop": "platform"}


def test_official_duplicate_preserves_additional_osm_category() -> None:
    official = [
        {
            "id": 1,
            "brand_id": "dominos",
            "category_id": "pizza",
            "building_source_id": "other",
            "inside_selected": False,
            "x": 100,
            "y": 0,
            "name": "Domino's",
            "source_url": "https://example.test",
            "location_method": "municipal_address",
        }
    ]
    pois = [
        {
            "source_id": "node/1",
            "name": "Domino's",
            "brand_id": "dominos",
            "kind": "competitor",
            "categories": ["pizza", "quick-service"],
            "duplicate_of": None,
            "building_source_id": "other",
            "inside_selected": False,
            "x": 105,
            "y": 0,
            "distance": 105,
            "source_url": "https://osm.example.test",
            "location_method": "node",
        }
    ]
    result = competition_profile(
        official, [], True, pois, {"id": "osm"}, {"source_id": "selected", "x": 0, "y": 0}, 300
    )
    counts = {category["id"]: category["count"] for category in result["categories"]}
    assert counts["pizza"] == 1
    assert counts["quick-service"] == 1
    assert result["deduplicated_count"] == 1


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    engine = MagicMock(spec=Engine)
    monkeypatch.setattr("app.main.create_database_engine", lambda settings: engine)
    with TestClient(create_app(Settings(_env_file=None))) as test_client:
        yield test_client


@pytest.mark.parametrize("radius", [300, 500, 1000])
def test_query_radii_are_parsed_as_integers(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, radius: int
) -> None:
    get_profile = MagicMock(return_value={"radius_m": radius})
    monkeypatch.setattr("app.api.routes.map.profiles.get_profile", get_profile)
    response = client.get(f"/api/v1/map/buildings/1/profile?radius_m={radius}")
    assert response.status_code == 200
    assert get_profile.call_args.args[2] == radius


@pytest.mark.parametrize("radius", [0, 400, 1001, "invalid"])
def test_invalid_radii_do_not_query_database(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, radius: int | str
) -> None:
    get_profile = MagicMock()
    monkeypatch.setattr("app.api.routes.map.profiles.get_profile", get_profile)
    assert client.get(f"/api/v1/map/buildings/1/profile?radius_m={radius}").status_code == 422
    get_profile.assert_not_called()


def test_profile_404_and_safe_database_failure(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    get_profile = MagicMock(return_value=None)
    monkeypatch.setattr("app.api.routes.map.profiles.get_profile", get_profile)
    assert client.get("/api/v1/map/buildings/1/profile").status_code == 404
    get_profile.side_effect = OperationalError("", {}, Exception("password=secret"))
    response = client.get("/api/v1/map/buildings/1/profile")
    assert response.status_code == 503
    assert response.json() == {"detail": "The profile database is unavailable"}
