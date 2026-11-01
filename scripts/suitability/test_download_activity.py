import importlib.util
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "download_activity", Path(__file__).with_name("download_activity.py")
)
activity = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(activity)


class ActivityNormalizationTests(unittest.TestCase):
    def setUp(self):
        self.metadata = {
            "retrieved_at": "2026-11-01T17:20:00Z",
            "snapshot_at": "2026-11-01T17:15:00Z",
            "endpoint": activity.ENDPOINT,
            "sha256": "test",
        }

    def element(self, identifier=1, **overrides):
        return {
            "type": "node",
            "id": identifier,
            "lon": 21.01,
            "lat": 52.23,
            "tags": {"amenity": "school"},
            "timestamp": "2024-01-01T00:00:00Z",
            **overrides,
        }

    def test_categories_do_not_infer_activity_from_building_or_landuse(self):
        self.assertEqual(activity.categories_for({"amenity": "school"}), ["school"])
        self.assertEqual(activity.categories_for({"amenity": "university"}), ["university"])
        self.assertEqual(activity.categories_for({"office": "government"}), ["office"])
        self.assertEqual(activity.categories_for({"shop": "mall"}), ["retail_destination"])
        self.assertEqual(
            activity.categories_for({"shop": "department_store"}),
            ["retail_destination"],
        )
        self.assertEqual(
            activity.categories_for({"amenity": "marketplace"}), ["retail_destination"]
        )
        for tags in (
            {"landuse": "retail"},
            {"building": "office"},
            {"amenity": "kindergarten"},
            {"shop": "supermarket"},
        ):
            self.assertEqual(activity.categories_for(tags), [])

    def test_inactive_and_multicategory_objects(self):
        self.assertEqual(activity.categories_for({"office": "vacant"}), [])
        self.assertEqual(activity.categories_for({"office": "company", "closed": "yes"}), [])
        self.assertEqual(
            activity.categories_for({"amenity": "school", "office": "company"}),
            ["office", "school"],
        )

    def test_provenance_and_geometry_method_survive(self):
        raw = {
            "elements": [
                self.element(),
                self.element(2, type="way", center={"lon": 21.03, "lat": 52.24}),
            ]
        }
        collection, metadata = activity.normalize(raw, self.metadata)
        self.assertEqual(collection["numberReturned"], 2)
        self.assertEqual(metadata["retrieved_at"], self.metadata["retrieved_at"])
        self.assertIsNone(metadata["observed_at"])
        self.assertEqual(metadata["snapshot_at"], self.metadata["snapshot_at"])
        properties = collection["features"][1]["properties"]
        self.assertEqual(properties["location_method"], "way_center")
        self.assertEqual(properties["updated_at"], "2024-01-01T00:00:00Z")
        self.assertEqual(properties["source_url"], "https://www.openstreetmap.org/way/2")

    def test_bad_points_are_reported_and_conflicting_identity_rejected(self):
        raw = {
            "elements": [
                self.element(),
                self.element(2, lon=22),
                self.element(3, lat=float("nan")),
            ]
        }
        collection, metadata = activity.normalize(raw, self.metadata)
        self.assertEqual(collection["numberReturned"], 1)
        self.assertEqual(
            metadata["skipped_counts"],
            {"representative_point_outside_bounds": 1, "invalid_coordinates": 1},
        )
        with self.assertRaisesRegex(ValueError, "Conflicting duplicate"):
            activity.normalize(
                {"elements": [self.element(), self.element(lon=21.02)]}, self.metadata
            )

    def test_incomplete_or_empty_snapshots_fail(self):
        for raw in (
            {"elements": []},
            {"elements": [self.element()], "remark": "timeout"},
        ):
            with self.assertRaises(ValueError):
                activity.normalize(raw, self.metadata)

    def test_end_date_does_not_silently_discard_active_tag(self):
        raw = {
            "elements": [
                self.element(
                    tags={
                        "amenity": "school",
                        "end_date": "2019-07-31",
                        "check_date": "2025-11-14",
                    }
                )
            ]
        }
        collection, metadata = activity.normalize(raw, self.metadata)
        self.assertEqual(collection["numberReturned"], 1)
        self.assertTrue(collection["features"][0]["properties"]["lifecycle_conflict"])
        self.assertEqual(metadata["lifecycle_conflict_count"], 1)
        self.assertEqual(metadata["lifecycle_conflict_source_ids"], ["node/1"])


if __name__ == "__main__":
    unittest.main()
