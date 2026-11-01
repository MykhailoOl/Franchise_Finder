import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "brand_requirements", ROOT / "backend/app/core/brand_requirements.py"
)
catalog = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(catalog)


def requirements_for(brand_id, format_id):
    brand = catalog.get_brand_requirements(brand_id)
    selected = next(item for item in brand["formats"] if item["id"] == format_id)
    return {item["id"]: item for item in selected["requirements"]}


class BrandRequirementsTests(unittest.TestCase):
    def test_separate_formats_do_not_inherit_unrelated_minimums(self):
        institute = requirements_for("yasumi", "institute")
        point = requirements_for("yasumi", "beauty-point")
        self.assertEqual(institute["unit-area"]["value"], 100)
        self.assertEqual(institute["unit-area"]["strength"], "mandatory")
        self.assertIsNone(point["unit-area"]["value"])
        self.assertEqual(point["unit-area"]["strength"], "unknown")
        express = requirements_for("da-grasso", "express")
        classic = requirements_for("da-grasso", "classic")
        self.assertEqual(express["unit-area"]["value"], [65, 100])
        self.assertEqual(classic["unit-area"]["value"], [100, 200])
        self.assertEqual(classic["unit-area"]["strength"], "format_range")

    def test_conflicting_guidance_has_no_selected_threshold(self):
        requirements = requirements_for("depilconcept", "salon")
        for identifier in ("unit-area", "town-size"):
            item = requirements[identifier]
            self.assertEqual(item["evidence_status"], "conflicting")
            self.assertIsNone(item["value"])
            self.assertEqual(item["operator"], "not_verified")
            self.assertEqual(len(item["reported_claims"]), 2)
        self.assertEqual(requirements["unit-area"]["reported_claims"][0]["value"], 70)
        self.assertEqual(requirements["unit-area"]["reported_claims"][1]["value"], [40, 70])

    def test_public_footprints_do_not_establish_private_unit_requirements(self):
        for brand in catalog.REQUIREMENTS_BY_BRAND.values():
            for brand_format in brand["formats"]:
                for item in brand_format["requirements"]:
                    self.assertFalse(item["public_building_evaluable"])
                    self.assertNotIn("footprint", item["field"])
                    if item["strength"] == "unknown":
                        self.assertIsNone(item["value"])

    def test_every_claim_resolves_to_dated_evidence_without_invented_publication(self):
        self.assertEqual(len(catalog.REQUIREMENTS_BY_BRAND), 8)
        for brand in catalog.REQUIREMENTS_BY_BRAND.values():
            sources = {item["id"]: item for item in brand["sources"]}
            for source in sources.values():
                self.assertIsNone(source["published_at"])
                self.assertTrue(source["retrieved_at"].startswith("2026-11-01T"))
                self.assertEqual(len(source["reviewed_sha256"]), 64)
            for brand_format in brand["formats"]:
                for item in brand_format["requirements"]:
                    self.assertIn(item["source_id"], sources)
                    for claim in item.get("reported_claims", []):
                        self.assertIn(claim["source_id"], sources)

    def test_callers_cannot_mutate_curated_catalog(self):
        value = catalog.get_brand_requirements("zabka")
        value["formats"][0]["requirements"][0]["value"] = 1
        self.assertEqual(requirements_for("zabka", "standard-store")["total-area"]["value"], 70)


if __name__ == "__main__":
    unittest.main()
