import unittest

from download_pois import brand_id, classify, normalize


class PoiNormalizationTests(unittest.TestCase):
    def test_fast_food_pizza_preserves_both_categories(self):
        self.assertEqual(
            classify({"amenity": "fast_food", "cuisine": "italian;pizza"}),
            ("competitor", ["quick-service", "pizza"]),
        )

    def test_hairdresser_is_not_automatically_a_beauty_competitor(self):
        self.assertIsNone(classify({"shop": "hairdresser"}))
        self.assertEqual(classify({"shop": "beauty"}), ("competitor", ["beauty"]))

    def test_closed_businesses_and_private_parking_are_excluded(self):
        self.assertIsNone(classify({"shop": "convenience", "disused": "yes"}))
        self.assertIsNone(classify({"amenity": "parking", "access": "private"}))

    def test_stop_platforms_are_not_rail_stations(self):
        self.assertIsNone(classify({"public_transport": "platform", "train": "yes"}))
        self.assertEqual(
            classify({"public_transport": "platform", "bus": "yes", "tram": "yes"}),
            ("access", ["bus_stop", "tram_stop"]),
        )

    def test_brand_matching_does_not_guess_from_partial_names(self):
        self.assertEqual(brand_id({"name": "Żabka"}), "zabka")
        self.assertEqual(brand_id({"brand": "McDonald's"}), "mcdonalds")
        self.assertIsNone(brand_id({"name": "Subway entrance"}))

    def test_unique_records_keep_real_timestamps_and_location_method(self):
        element = {
            "type": "way",
            "id": 123,
            "timestamp": "2024-02-01T12:00:00Z",
            "center": {"lon": 21.01, "lat": 52.23},
            "tags": {"shop": "beauty", "name": "Example salon"},
        }
        metadata = {
            "retrieved_at": "2026-10-23T12:00:00Z",
            "snapshot_at": "2026-10-22T12:00:00Z",
            "endpoint": "https://example.invalid",
            "sha256": "abc",
        }
        invalid = {**element, "id": 124, "center": {"lon": float("nan"), "lat": 52.23}}
        outside = {**element, "id": 125, "center": {"lon": 19, "lat": 52.23}}
        data, source = normalize(
            {"elements": [element, element, invalid, outside]}, metadata
        )
        self.assertEqual(len(data["features"]), 1)
        properties = data["features"][0]["properties"]
        self.assertEqual(properties["source_id"], "way/123")
        self.assertEqual(properties["updated_at"], element["timestamp"])
        self.assertEqual(properties["location_method"], "way_center")
        self.assertIsNone(source["observed_at"])
        self.assertEqual(source["snapshot_at"], metadata["snapshot_at"])


if __name__ == "__main__":
    unittest.main()
