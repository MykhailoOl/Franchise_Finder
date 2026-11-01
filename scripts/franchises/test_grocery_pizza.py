import json
import unittest

from download_grocery_pizza import (
    address_parts,
    build_snapshot,
    coordinates,
    extract_da_grasso,
    extract_lewiatan,
    extract_zabka,
)
from municipal_address_aliases import apply_municipal_aliases
from normalize_dominos import normalize_snapshot

RETRIEVED_AT = "2026-10-23T18:00:00Z"


def zabka_row(identifier: str, **changes: object) -> dict:
    return {
        "locationId": identifier,
        "town": "Warszawa",
        "street": "Puławska 22/24",
        "active": "true",
        "lon": 21.02,
        "lat": 52.2,
        "storeUrl": f"https://www.zabka.pl/sklepy/{identifier}",
        **changes,
    }


def da_grasso_row(identifier: str, street: str, suffix: str, city: str = "Warszawa") -> str:
    return (
        '<div class="s4d-store-result">'
        f'<span class="s4d-store-result-title">{city}</span>'
        f'<div class="small">{street}<sup>{suffix}</sup><br>00-001 {city}<br></div>'
        f'<span data-store-id="{identifier}"></span>'
        f'<a href="https://www.dagrasso.pl/Store/Details?storeName={identifier}">Menu</a>'
        "</div>"
    )


class GroceryPizzaExtractionTests(unittest.TestCase):
    def test_reviewed_alias_preserves_raw_evidence_and_rejects_moved_outlet(self) -> None:
        record = {
            "brand_id": "da-grasso",
            "source_id": "76",
            "address": {"street": "Taylora", "house_number": "7", "raw": "Taylora 7"},
            "attributes": {},
        }
        apply_municipal_aliases([record])
        self.assertEqual(record["address"]["street"], "ulica Karola Taylora")
        self.assertEqual(record["address"]["raw"], "Taylora 7")
        self.assertEqual(record["attributes"]["source_parsed_address"]["street"], "Taylora")
        record["address"]["house_number"] = "8"
        with self.assertRaises(ValueError):
            apply_municipal_aliases([record])

    def test_house_ranges_are_preserved_and_explicit_units_separated(self) -> None:
        cases = {
            "Jana Pawła II 22/24": ("Jana Pawła II", "22/24", None),
            "Jana Kazimierza 9/9E": ("Jana Kazimierza", "9/9E", None),
            "PUŁAWSKA 67 LOK. 41": ("PUŁAWSKA", "67", "41"),
            "Grochowska 174/176 lok. 107": ("Grochowska", "174/176", "107"),
            "1 Praskiego Pułku WP 118": ("1 Praskiego Pułku WP", "118", None),
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(address_parts(raw), expected)

    def test_bad_official_coordinates_are_not_plotted(self) -> None:
        self.assertIsNone(coordinates(17.976527, 50.48089))
        self.assertIsNone(coordinates(18.297559, 50.122282))
        self.assertIsNone(coordinates(None, None))
        self.assertEqual(coordinates("21.02", "52.2"), [21.02, 52.2])
        with self.assertRaises(ValueError):
            coordinates(float("nan"), 52.2)

    def test_zabka_filters_formats_and_keeps_case_insensitive_city(self) -> None:
        records = [zabka_row(str(index), town="Kraków") for index in range(1000)]
        records += [
            zabka_row("ordinary", town="WARSZAWA"),
            zabka_row("nano", isNano=True),
            zabka_row("vending", isVending=True),
            zabka_row("planned", locatorPlanned=True),
            zabka_row("hidden", isVisible=False),
            zabka_row("inactive", active="false"),
        ]
        outlets, report = extract_zabka(json.dumps(records).encode(), RETRIEVED_AT)
        self.assertEqual([row["source_id"] for row in outlets], ["ordinary"])
        self.assertEqual(report["warsaw_source_count"], 6)
        self.assertEqual(sum(report["excluded_warsaw_records"].values()), 5)
        self.assertEqual(outlets[0]["status"], "listed")
        self.assertEqual(outlets[0]["operating_model"], "unknown")

    def test_duplicate_outlet_ids_reject_replacement_snapshot(self) -> None:
        records = [zabka_row(str(index), town="Kraków") for index in range(1000)]
        records += [zabka_row("same"), zabka_row("same")]
        with self.assertRaises(ValueError):
            build_snapshot("zabka", json.dumps(records).encode(), RETRIEVED_AT)

    def test_lewiatan_city_variants_and_bad_coordinates_preserve_outlets(self) -> None:
        records = [{"city": "Kraków"} for _ in range(1000)]
        records += [
            {
                "id": 12063,
                "city": "Warszawa (Ursus)",
                "address": 'KOMPANII AK "KORDIAN" 1',
                "post_code": "02-495",
                "longitude": 18.297559,
                "latitude": 50.122282,
                "url": "/sklep/12063",
            },
            {"city": "Warszawa Zachodnia"},
        ]
        outlets, report = extract_lewiatan(json.dumps({"data": records}).encode(), RETRIEVED_AT)
        self.assertEqual(report["warsaw_source_count"], 1)
        self.assertEqual(outlets[0]["source_id"], "12063")
        self.assertIsNone(outlets[0]["coordinates"])
        self.assertEqual(outlets[0]["attributes"]["source_longitude"], 18.297559)

    def test_da_grasso_suffixes_do_not_create_nonexistent_house_numbers(self) -> None:
        entries = [da_grasso_row(str(index), "Testowa 1", "", "Kraków") for index in range(100)]
        entries += [
            da_grasso_row("a", "Ostródzka 73", "A"),
            da_grasso_row("b", "Al. Niepodleglosci 121", "/123"),
            da_grasso_row("c", "Grochowska 174/176", "107"),
            da_grasso_row("d", "Smyczkowa 21", "Lok. U1"),
        ]
        outlets, _ = extract_da_grasso("".join(entries).encode(), RETRIEVED_AT)
        self.assertEqual(
            [row["address"]["house_number"] for row in outlets], ["73A", "121/123", "174/176", "21"]
        )
        self.assertEqual(outlets[2]["attributes"]["source_unit"], "107")
        self.assertEqual(outlets[3]["attributes"]["source_unit"], "U1")
        self.assertTrue(all(row["coordinates"] is None for row in outlets))

    def test_dominos_keeps_captions_raw_and_slashes_in_house_number(self) -> None:
        source = {
            "source_url": "https://www.dominospizza.pl/lokale/Warszawa",
            "retrieved_at": RETRIEVED_AT,
            "pages_verified": 1,
            "outlets": [
                {
                    "address": "al. Jana Pawła II 22/24 (CH Test)",
                    "url": "https://www.dominospizza.pl/lokale/Warszawa/Jana-Pawla",
                }
            ],
        }
        snapshot = normalize_snapshot(source, json.dumps(source).encode())
        row = snapshot["outlets"][0]
        self.assertEqual(row["address"]["house_number"], "22/24")
        self.assertIn("(CH Test)", row["address"]["raw"])
        self.assertEqual(row["attributes"]["source_location_captions"], ["CH Test"])
        self.assertIsNone(row["coordinates"])
        source["outlets"] *= 2
        with self.assertRaises(ValueError):
            normalize_snapshot(source, json.dumps(source).encode())

    def test_dominos_rejects_inconsistent_page_capture(self) -> None:
        source = {
            "source_url": "https://www.dominospizza.pl/lokale/Warszawa",
            "retrieved_at": RETRIEVED_AT,
            "pages_verified": 4,
            "outlets": [],
        }
        with self.assertRaises(ValueError):
            normalize_snapshot(source, json.dumps(source).encode())


if __name__ == "__main__":
    unittest.main()
