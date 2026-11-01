from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError

from app.core.address_search import normalize_house_number, normalize_street, parse_address_query
from app.core.config import Settings
from app.main import create_app


@pytest.mark.parametrize(
    ("query", "street", "house", "postal"),
    [
        ("  ul. ZŁOTA 44, Warszawa ", "zlota", "44", None),
        ("44 Zlota Warsaw", "zlota", "44", None),
        ("al. Jerozolimskie 56c", "jerozolimskie", "56c", None),
        ("pl. Konstytucji 1", "konstytucji", "1", None),
        ("Marszalkowska 104/122", "marszalkowska", "104/122", None),
        ("Jagielska 63A/8", "jagielska", "63a/8", None),
        ("Wspólna 27, 00-519 Warszawa", "wspolna", "27", "00-519"),
        ("00-519 Wspolna 27", "wspolna", "27", "00-519"),
        ("Miedziana 10 A", "miedziana", "10a", None),
        ("Miedziana", "miedziana", None, None),
    ],
)
def test_address_query_normalization(query: str, street: str, house: str, postal: str) -> None:
    parsed = parse_address_query(query)

    assert (parsed.street, parsed.house_number, parsed.postal_code) == (street, house, postal)


def test_polish_and_ascii_street_forms_match() -> None:
    assert normalize_street("ulica ŁÓDZKA") == normalize_street("UL. lodzka")
    assert normalize_house_number("10a") != normalize_house_number("10")
    assert normalize_house_number("100") != normalize_house_number("10")
    assert normalize_house_number("104/122") == "104/122"


def test_conflicting_postal_codes_are_not_silently_ignored() -> None:
    with pytest.raises(ValueError, match="one postal code"):
        parse_address_query("Wspolna 27 00-519 05-075")


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.main.create_database_engine", lambda settings: MagicMock(spec=Engine))
    with TestClient(create_app(Settings(_env_file=None))) as test_client:
        yield test_client


@pytest.mark.parametrize("parameters", [{"q": "x"}, {"q": "x" * 121}, {"q": "Zlota", "limit": 21}])
def test_search_rejects_unbounded_queries(client: TestClient, parameters: dict) -> None:
    assert client.get("/api/v1/map/addresses/search", params=parameters).status_code == 422


def test_search_database_errors_do_not_expose_secrets(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.api.routes.map.addresses.search_addresses",
        MagicMock(side_effect=OperationalError("", {}, Exception("password=secret"))),
    )

    response = client.get("/api/v1/map/addresses/search", params={"q": "Zlota"})

    assert response.status_code == 503
    assert response.json() == {"detail": "The address database is unavailable"}


def test_conflicting_postcodes_return_422(client: TestClient) -> None:
    response = client.get("/api/v1/map/addresses/search", params={"q": "Wspolna 27 00-519 05-075"})

    assert response.status_code == 422
    assert "one postal code" in response.json()["detail"]
