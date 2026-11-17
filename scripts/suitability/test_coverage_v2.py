import importlib.util
import unittest
from pathlib import Path

from pyproj import Transformer


def load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


activity = load("download_activity_v2")
population = load("download_population_context_v2")


class CoverageTests(unittest.TestCase):
    def test_published_zero_and_missing_are_distinct(self):
        self.assertEqual(
            population.parse_cell("PL_CRS3035RES500mN3200000E5100000", 0)["status"], "observed"
        )
        self.assertIsNone(
            population.parse_cell("PL_CRS3035RES500mN3200000E5100000", None)["population"]
        )
        for value in [-1, True, "0", float("nan")]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                population.parse_cell("PL_CRS3035RES500mN3200000E5100000", value)

    def test_education_tags_add_omitted_objects_without_double_counting(self):
        self.assertEqual(activity.categories_for({"education": "school"})[0], ["school"])
        self.assertEqual(
            activity.categories_for({"education": "university", "amenity": "university"})[0],
            ["university"],
        )
        self.assertEqual(activity.categories_for({"education": "college"})[0], [])
        self.assertEqual(
            activity.categories_for({"education": "school", "amenity": "university"})[0], []
        )

    def test_inactive_and_lifecycle_conflicts_are_not_active_evidence(self):
        for tags in [
            {"education": "school", "closed": "true"},
            {"education": "university", "disused:education": "university"},
            {"office": "company", "construction": "1"},
        ]:
            with self.subTest(tags=tags):
                self.assertEqual(activity.categories_for(tags)[0], [])

    def test_halo_covers_projected_candidate_extent_and_retains_previous_source_bounds(self):
        forward = Transformer.from_crs(4326, 2180, always_xy=True)
        for city, region in activity.REGIONS.items():
            with self.subTest(city=city):
                bounds = activity.acquisition_bounds(city)
                original = region["previous_bounds"]
                self.assertLessEqual(bounds[0], original[0])
                self.assertLessEqual(bounds[1], original[1])
                self.assertGreaterEqual(bounds[2], original[2])
                self.assertGreaterEqual(bounds[3], original[3])
                acquisition = forward.transform_bounds(*bounds, densify_pts=101)
                candidate = forward.transform_bounds(*region["candidate_bounds"], densify_pts=101)
                self.assertLessEqual(acquisition[0], candidate[0] - 1000)
                self.assertLessEqual(acquisition[1], candidate[1] - 1000)
                self.assertGreaterEqual(acquisition[2], candidate[2] + 1000)
                self.assertGreaterEqual(acquisition[3], candidate[3] + 1000)

    def test_normalization_preserves_dates_identity_and_education_gain(self):
        raw = {
            "elements": [
                {"type": "node", "id": 1, "lon": 21.1, "lat": 52.2, "tags": {"education": "school"}}
            ]
        }
        raw["elements"].append(dict(raw["elements"][0]))
        acquisition = {
            "coverage_bounds": activity.REGIONS["warsaw"]["previous_bounds"],
            "retrieved_at": "2026-11-17T10:00:00Z",
            "snapshot_at": "2026-11-17T09:59:00Z",
            "query": "fixture",
            "endpoint": activity.ENDPOINT,
            "sha256": "a" * 64,
        }
        collection, metadata = activity.normalize(raw, acquisition, "warsaw")
        self.assertEqual(len(collection["features"]), 1)
        self.assertEqual(
            metadata["additional_category_memberships_from_education_tags"], {"school": 1}
        )
        self.assertEqual(metadata["retrieved_at"], acquisition["retrieved_at"])
        self.assertEqual(metadata["snapshot_at"], acquisition["snapshot_at"])
        self.assertEqual(collection["features"][0]["properties"]["tags"], {"education": "school"})


if __name__ == "__main__":
    unittest.main()
