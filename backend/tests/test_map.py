import math
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError

from app.core.config import Settings
from app.db.buildings import MapDataUnavailableError
from app.main import create_app


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    engine = MagicMock(spec=Engine)
    monkeypatch.setattr("app.main.create_database_engine", lambda settings: engine)
    with TestClient(create_app(Settings(_env_file=None))) as test_client:
        yield test_client


def warsaw_tile_path() -> str:
    z = 16
    x = int((21.01 + 180) / 360 * 2**z)
    y = int((1 - math.asinh(math.tan(math.radians(52.23))) / math.pi) / 2 * 2**z)
    return f"/api/v1/map/tiles/{z}/{x}/{y}.pbf"


@pytest.mark.parametrize("path", ["12/0/0", "19/0/0", "13/8192/0", "13/0/8192", "13/-1/0"])
def test_invalid_tile_coordinates_do_not_query_database(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    get_tile = MagicMock()
    monkeypatch.setattr("app.api.routes.map.buildings.get_tile", get_tile)

    assert client.get(f"/api/v1/map/tiles/{path}.pbf").status_code == 422
    get_tile.assert_not_called()


def test_tiles_outside_warsaw_are_empty_without_a_database_query(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    get_tile = MagicMock()
    monkeypatch.setattr("app.api.routes.map.buildings.get_tile", get_tile)

    response = client.get("/api/v1/map/tiles/13/0/0.pbf")

    assert response.status_code == 200
    assert response.content == b""
    get_tile.assert_not_called()


def test_tile_etag_revalidates_and_changes_with_data(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    get_tile = MagicMock(return_value=b"first-tile")
    monkeypatch.setattr("app.api.routes.map.buildings.get_tile", get_tile)
    first = client.get(warsaw_tile_path())
    cached = client.get(warsaw_tile_path(), headers={"If-None-Match": first.headers["etag"]})
    get_tile.return_value = b"updated-tile"
    updated = client.get(warsaw_tile_path(), headers={"If-None-Match": first.headers["etag"]})

    assert first.headers["content-type"] == "application/vnd.mapbox-vector-tile"
    assert first.headers["cache-control"] == "public, max-age=60, must-revalidate"
    assert cached.status_code == 304
    assert cached.content == b""
    assert updated.status_code == 200
    assert updated.headers["etag"] != first.headers["etag"]
    assert updated.content == b"updated-tile"


def test_unimported_building_data_returns_actionable_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.api.routes.map.buildings.get_tile", MagicMock(side_effect=MapDataUnavailableError)
    )

    response = client.get(warsaw_tile_path())

    assert response.status_code == 503
    assert "import" in response.json()["detail"]


def test_map_database_error_does_not_leak_credentials(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.api.routes.map.buildings.get_metadata",
        MagicMock(side_effect=OperationalError("", {}, Exception("password=secret"))),
    )

    response = client.get("/api/v1/map/metadata")

    assert response.status_code == 503
    assert response.json() == {"detail": "The map database is unavailable"}


def test_missing_building_returns_404(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.api.routes.map.buildings.get_building", MagicMock(return_value=None))

    assert client.get("/api/v1/map/buildings/123").status_code == 404
