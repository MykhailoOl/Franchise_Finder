import importlib.util
import json
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts/franchises/download_beauty.py"
SPEC = importlib.util.spec_from_file_location("beauty_extraction", MODULE_PATH)
assert SPEC and SPEC.loader
BEAUTY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BEAUTY)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Warszawa Gocław, ul. Optyków 1/U03", ("Optyków", "1")),
        ("Warszawa Ursus, ul. Starodęby 10/lok. U8", ("Starodęby", "10")),
        ("Warszawa Ursynów, Aleja KEN 83 U14", ("KEN", "83")),
        ("Warszawa Wawer, ul. Żegańska 2a lok. 9", ("Żegańska", "2a")),
        (
            "Warszawa Powiśle, ul. Wybrzeże Kościuszkowskie 45/1",
            ("Wybrzeże Kościuszkowskie", "45/1"),
        ),
        (
            "Warszawa, ul. Domaniewska 39b - wejście od ulicy Rodziny Hiszpańskich",
            ("Domaniewska", "39b"),
        ),
    ],
)
def test_building_numbers_preserve_ambiguous_slashes(raw, expected):
    assert BEAUTY.split_address(raw) == expected


def test_invalid_coordinates_remain_unlocated():
    assert BEAUTY.coordinates({"lat": "0", "lng": "0"}) is None
    assert BEAUTY.coordinates({"lat": "52.2", "lng": "21.0"}) == [21.0, 52.2]


def test_warsaw_selection_uses_city_identity_not_street_name():
    records = [
        {
            "ID": 1,
            "post_title": "DepilConcept Warszawa-Bemowo",
            "post_status": "publish",
            "website": "https://depilconcept.pl/oddzial/bemowo/",
            "agency_address": {
                "address": "Warszawa Bemowo, Bolkowska 1B",
                "lat": 52.24,
                "lng": 20.92,
            },
        },
        {
            "ID": 2,
            "post_title": "DepilConcept Kielce",
            "post_status": "publish",
            "agency_address": {"address": "Warszawska 146, Kielce"},
        },
    ]
    result = BEAUTY.extract_depilconcept(json.dumps({"agencies": records}), "2026-10-23T12:00:00Z")
    assert len(result["outlets"]) == 1
    assert result["outlets"][0]["source_id"] == "1"
