from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError

from app.core.config import Settings
from app.main import create_app


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    database_engine = MagicMock(spec=Engine)
    monkeypatch.setattr("app.main.create_database_engine", lambda settings: database_engine)
    return database_engine


def test_liveness_and_docs_work_without_database_connection(engine: MagicMock) -> None:
    engine.connect.side_effect = OperationalError("", {}, Exception("Database is offline"))

    with TestClient(create_app(Settings(_env_file=None))) as client:
        response = client.get("/api/v1/health")

        assert response.status_code == 200
        assert response.json() == {"status": "ok", "service": "MasterFranchises API"}
        assert client.get("/docs").status_code == 200
        assert client.get("/openapi.json").status_code == 200
        engine.connect.assert_not_called()

    engine.dispose.assert_called_once()


def test_readiness_checks_postgis(engine: MagicMock) -> None:
    connection = engine.connect.return_value.__enter__.return_value
    connection.execute.return_value.scalar_one.return_value = "3.5 USE_GEOS=1 USE_PROJ=1"

    with TestClient(create_app(Settings(_env_file=None))) as client:
        response = client.get("/api/v1/health/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "database": "ok",
        "postgis_version": "3.5 USE_GEOS=1 USE_PROJ=1",
    }
    assert str(connection.execute.call_args.args[0]) == "SELECT PostGIS_Version()"


def test_readiness_does_not_expose_database_errors(engine: MagicMock) -> None:
    engine.connect.side_effect = OperationalError(
        "SELECT PostGIS_Version()", {}, Exception("password=secret host=private")
    )

    with TestClient(create_app(Settings(_env_file=None))) as client:
        response = client.get("/api/v1/health/ready")

    assert response.status_code == 503
    assert response.json() == {"detail": "Database or PostGIS is unavailable"}


def test_cors_accepts_configured_origin(engine: MagicMock) -> None:
    settings = Settings(_env_file=None, api_cors_origins=["http://localhost:5173"])

    with TestClient(create_app(settings)) as client:
        allowed = client.get("/api/v1/health", headers={"Origin": "http://localhost:5173"})
        denied = client.get("/api/v1/health", headers={"Origin": "https://unconfigured.example"})

    assert allowed.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert "access-control-allow-origin" not in denied.headers
