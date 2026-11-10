import importlib.util
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from pyproj import Transformer

SPEC = importlib.util.spec_from_file_location(
    "collect_radom", Path(__file__).with_name("collect_radom.py")
)
collector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(collector)


def gml_feature(kind="buildings", properties=None, closed=True, wrong_crs=False):
    crs = collector.collector_recipe()[kind]["crs"]
    transform = Transformer.from_crs("OGC:CRS84", crs)
    points = [(21.15, 51.4), (21.151, 51.4), (21.151, 51.401), (21.15, 51.4)]
    if not closed:
        points = points[:-1]
    coordinates = " ".join(str(value) for point in points for value in transform.transform(*point))
    declared = "urn:ogc:def:crs:EPSG::4326" if wrong_crs else crs
    if kind == "buildings":
        data = {
            "ID_BUDYNKU": "146301_1.0001.1_BUD",
            "RODZAJ": "m",
            "KONDYGNACJE_NADZIEMNE": "0",
            **(properties or {}),
        }
        geometry = (
            f'<gml:MultiSurface srsName="{declared}"><gml:surfaceMember><gml:Polygon>'
            '<gml:exterior><gml:LinearRing><gml:posList srsDimension="2">'
            f"{coordinates}</gml:posList></gml:LinearRing></gml:exterior>"
            "</gml:Polygon></gml:surfaceMember></gml:MultiSurface>"
        )
    else:
        data = {
            "ID_IIP": "PL.FIXTURE.123.address.20260101",
            "ID_GMINY": "1463011",
            "NAZWA_MIEJSCOWOSCI": "Radom",
            "NAZWA_ULICY": "Testowa",
            "NUMER_PORZADKOWY": "1",
            **(properties or {}),
        }
        coordinate = " ".join(str(value) for value in transform.transform(*points[0]))
        geometry = f'<gml:Point srsName="{declared}"><gml:pos>{coordinate}</gml:pos></gml:Point>'
    fields = "".join(f"<ms:{key}>{value}</ms:{key}>" for key, value in data.items())
    return (
        '<ms:feature xmlns:ms="http://example.test" xmlns:gml="http://www.opengis.net/gml/3.2">'
        "<gml:boundedBy><gml:Envelope/></gml:boundedBy>"
        f"{fields}<ms:geometry>{geometry}</ms:geometry></ms:feature>"
    )


def collection_xml(features, matched=None):
    total = len(features) if matched is None else matched
    members = "".join(f"<wfs:member>{feature}</wfs:member>" for feature in features)
    return (
        '<wfs:FeatureCollection xmlns:wfs="http://www.opengis.net/wfs/2.0" '
        f'numberMatched="{total}" numberReturned="{len(features)}">'
        f"{members}</wfs:FeatureCollection>"
    ).encode()


class RadomGmlTests(unittest.TestCase):
    def test_authority_axes_transform_to_longitude_latitude(self):
        node = ET.fromstring(gml_feature())
        feature = collector.normalize_gml_feature(
            node, "buildings", collector.collector_recipe()["buildings"]["crs"]
        )
        point = feature["geometry"]["coordinates"][0][0][0]
        self.assertAlmostEqual(point[0], 21.15, places=7)
        self.assertAlmostEqual(point[1], 51.4, places=7)
        self.assertEqual(feature["properties"]["KONDYGNACJE_NADZIEMNE"], "0")
        self.assertEqual(feature["properties"]["source_id"], "146301_1.0001.1_BUD")

    def test_address_original_fields_and_normalized_fields_survive(self):
        feature = collector.normalize_gml_feature(
            ET.fromstring(gml_feature("addresses")),
            "addresses",
            collector.collector_recipe()["addresses"]["crs"],
        )
        self.assertEqual(feature["properties"]["street"], "Testowa")
        self.assertEqual(feature["properties"]["ID_GMINY"], "1463011")
        self.assertEqual(feature["geometry"]["type"], "Point")

    def test_wrong_municipality_rejected(self):
        for kind, properties in (
            ("buildings", {"ID_BUDYNKU": "146501_1.0001.1_BUD"}),
            ("addresses", {"ID_GMINY": "1465011"}),
        ):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                collector.normalize_gml_feature(
                    ET.fromstring(gml_feature(kind, properties)),
                    kind,
                    collector.collector_recipe()[kind]["crs"],
                )

    def test_invalid_ring_or_crs_rejected(self):
        for changes in ({"closed": False}, {"wrong_crs": True}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                collector.normalize_gml_feature(
                    ET.fromstring(gml_feature(**changes)),
                    "buildings",
                    collector.collector_recipe()["buildings"]["crs"],
                )

    def test_wfs_errors_entities_and_unknown_counts_rejected(self):
        for content in (b"<ExceptionReport/>", b"<!DOCTYPE a><FeatureCollection/>"):
            with self.assertRaises(ValueError):
                collector.parse_xml(content)
        for count in ("unknown", "-1", "1.2"):
            root = ET.fromstring(f'<FeatureCollection numberMatched="{count}"/>')
            with self.assertRaises(ValueError):
                collector.wfs_count(root, "numberMatched")

    def test_full_wfs_download_rejects_duplicate_page_identities(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            (output / "raw").mkdir()
            responses = [collection_xml([], 2), collection_xml([gml_feature()], 2)]

            def fake_fetch(url, destination, body=None):
                destination.write_bytes(
                    responses.pop(0) if responses else collection_xml([gml_feature()], 2)
                )
                return {"retrieved_at": "2026-11-10T12:00:00Z", "sha256": "fixture"}

            with (
                patch.object(collector, "fetch", fake_fetch),
                self.assertRaisesRegex(ValueError, "Duplicate"),
            ):
                collector.collect_wfs(output, "buildings")

    def test_full_wfs_snapshot_counts_and_provenance(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            (output / "raw").mkdir()

            def fake_fetch(url, destination, body=None):
                destination.write_bytes(
                    collection_xml([], 1)
                    if "resultType=hits" in url
                    else collection_xml([gml_feature()]).replace(
                        b'numberMatched="1"', b'numberMatched="unknown"'
                    )
                )
                return {"retrieved_at": "2026-11-10T12:00:00Z", "sha256": "fixture"}

            with patch.object(collector, "fetch", fake_fetch):
                metadata = collector.collect_wfs(output, "buildings")
            self.assertEqual(metadata["feature_count"], 1)
            self.assertIsNone(metadata["observed_at"])
            self.assertIsNone(metadata["snapshot_at"])
            self.assertEqual(
                metadata["geojson_sha256"], collector.sha256(output / "buildings.geojson")
            )


class RadomGroceryTests(unittest.TestCase):
    def fixture(self, brand="zabka", **changes):
        if brand == "zabka":
            row = {
                "town": "Radom",
                "locationId": "test",
                "street": "Testowa 1 lok. 2",
                "lon": 21.15,
                "lat": 51.4,
                "active": True,
                "storeUrl": "/test",
                **changes,
            }
            return json.dumps([{"town": "Other"} for _ in range(1000)] + [row]).encode()
        row = {
            "city": "Radom",
            "id": "test",
            "address": "Testowa 1",
            "longitude": None,
            "latitude": None,
            "post_code": "26-600",
            "url": "/test",
            **changes,
        }
        return json.dumps({"data": [{"city": "Other"} for _ in range(1000)] + [row]}).encode()

    def test_ordinary_active_outlets_and_address_units(self):
        snapshot = collector.normalize_grocery("zabka", self.fixture(), "2026-11-10T12:00:00Z")
        outlet = snapshot["outlets"][0]
        self.assertEqual(outlet["address"]["city"], "Radom")
        self.assertEqual(outlet["address"]["house_number"], "1")
        self.assertEqual(outlet["attributes"]["source_unit"], "2")
        self.assertEqual(outlet["operating_model"], "unknown")
        self.assertEqual(outlet["coordinates"], [21.15, 51.4])
        self.assertIsNone(snapshot["provenance"]["source_updated_at"])

    def test_inactive_nonordinary_and_other_city_excluded(self):
        for changes in (
            {"isNano": True},
            {"isVending": True},
            {"locatorPlanned": True},
            {"active": False},
            {"isVisible": False},
            {"town": "Radomsko"},
        ):
            with self.subTest(changes=changes):
                snapshot = collector.normalize_grocery(
                    "zabka", self.fixture(**changes), "2026-11-10T12:00:00Z"
                )
                self.assertEqual(snapshot["outlets"], [])

    def test_outside_coordinates_are_null_and_flagged(self):
        snapshot = collector.normalize_grocery(
            "zabka", self.fixture(lon=21.0, lat=52.2), "2026-11-10T12:00:00Z"
        )
        outlet = snapshot["outlets"][0]
        self.assertIsNone(outlet["coordinates"])
        self.assertTrue(outlet["attributes"]["coordinates_outside_context_bounds"])

    def test_missing_lewiatan_coordinates_remain_missing(self):
        snapshot = collector.normalize_grocery(
            "lewiatan", self.fixture("lewiatan"), "2026-11-10T12:00:00Z"
        )
        self.assertIsNone(snapshot["outlets"][0]["coordinates"])
        self.assertEqual(snapshot["outlets"][0]["address"]["postal_code"], "26-600")

    def test_truncated_wrong_shape_nonfinite_and_duplicate_rejected(self):
        bad = [b"[]", b"{}", self.fixture(lon=float("nan"))]
        duplicated = json.loads(self.fixture())
        duplicated.append(duplicated[-1])
        bad.append(json.dumps(duplicated).encode())
        for body in bad:
            with self.subTest(body=body[:40]), self.assertRaises(ValueError):
                collector.normalize_grocery("zabka", body, "2026-11-10T12:00:00Z")
        with self.assertRaises(ValueError):
            collector.normalize_grocery("lewiatan", b"[]", "2026-11-10T12:00:00Z")

    def test_flag_schema_drift_fails_closed(self):
        for flag in ("locatorPlanned", "isNano", "isVending", "isVisible"):
            for value in ("true", "false", 1, 0):
                with (
                    self.subTest(flag=flag, value=value),
                    self.assertRaisesRegex(ValueError, "flag"),
                ):
                    collector.normalize_grocery(
                        "zabka", self.fixture(**{flag: value}), "2026-11-10T12:00:00Z"
                    )


class RadomFreezeTests(unittest.TestCase):
    def test_protocol_hash_and_recipe_are_required_before_source_reads(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "protocol.json"
            path.write_text(json.dumps({"collector": collector.collector_recipe()}))
            with patch.object(collector, "GUS_INPUTS", {}):
                path.write_text(json.dumps({"collector": collector.collector_recipe()}))
                protocol = collector.validate_protocol(path, collector.sha256(path))
                self.assertEqual(protocol["collector"]["municipality_id"], "1463011")
            with self.assertRaisesRegex(ValueError, "SHA256 mismatch"):
                collector.validate_protocol(path, "0" * 64)
            with patch.object(collector, "GUS_INPUTS", {}):
                path.write_text(json.dumps({"collector": {"version": "changed"}}))
                with self.assertRaisesRegex(ValueError, "recipe"):
                    collector.validate_protocol(path, collector.sha256(path))

    def test_mutated_cached_population_source_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.zip"
            source.write_bytes(b"changed")
            locked = {"archive": {"path": str(source), "sha256": "0" * 64}}
            with patch.object(collector, "GUS_INPUTS", locked):
                path = Path(temporary) / "protocol.json"
                path.write_text(json.dumps({"collector": collector.collector_recipe()}))
                with self.assertRaisesRegex(ValueError, "GUS input"):
                    collector.validate_protocol(path, collector.sha256(path))

    def test_output_cannot_target_live_data_directory(self):
        with patch.object(collector, "validate_protocol", return_value={}):
            with self.assertRaisesRegex(ValueError, "isolated"):
                collector.collect("fixture", "0" * 64, collector.ROOT / ".local/data")

    def test_osm_queries_and_module_bounds_are_separate_from_warsaw(self):
        for kind in ("pois", "activity"):
            module, query = collector.osm_query(kind)
            self.assertIn("[bbox:51.33,21.03,51.5,21.28]", query)
            self.assertEqual(module.BOUNDS, [20.82, 52.07, 21.32, 52.40])

    def test_cached_raw_is_reused_only_with_same_hash_and_request(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "raw.json"
            path.write_bytes(b"fixture")
            metadata = {
                "url": "https://example.test/source",
                "sha256": collector.sha256(path),
                "bytes": 7,
                "request_body_sha256": None,
                "retrieved_at": "2026-11-10T12:00:00Z",
            }
            collector.write_json(path.with_suffix(".json.metadata.json"), metadata)
            with patch.object(collector, "urlopen", side_effect=AssertionError("network")):
                self.assertEqual(collector.fetch(metadata["url"], path), metadata)
                with self.assertRaises(ValueError):
                    collector.fetch("https://example.test/changed", path)
                path.write_bytes(b"changed")
                with self.assertRaises(ValueError):
                    collector.fetch(metadata["url"], path)

    def test_population_missing_cells_never_become_zero(self):
        module = collector.load_helper("scripts/profile/download_population.py")

        def fake_extract(archive, bounds):
            return (
                {(bounds[0], bounds[1]): {"population": 0.0, "source_total_raw": "0"}},
                {},
                [],
                {},
            )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "output"
            output.mkdir()
            inputs = {
                "archive": {"path": "archive.zip", "sha256": "a" * 64},
                "archive_metadata": {"path": "archive.metadata.json", "sha256": "fixture"},
                "feed": {"path": "feed.xml", "sha256": "fixture"},
            }
            (root / "archive.metadata.json").write_text(
                json.dumps({"sha256": "a" * 64, "retrieved_at": "2026-10-23T12:00:00Z"})
            )
            (root / "feed.xml").write_text(
                '<feed xmlns="http://www.w3.org/2005/Atom"><rights>GUS</rights>'
                '<entry><link href="https://example.test/population.zip"/>'
                "<updated>2026-05-26</updated></entry></feed>"
            )
            with (
                patch.object(collector, "ROOT", root),
                patch.object(collector, "GUS_INPUTS", inputs),
                patch.object(collector, "CONTEXT_BOUNDS", [21.15, 51.4, 21.151, 51.401]),
                patch.object(collector, "load_helper", return_value=module),
                patch.object(module, "extract_observations", side_effect=fake_extract),
            ):
                metadata = collector.collect_population(output)
            rows = json.loads((output / "population.geojson").read_text())["features"]
            observed = [
                row["properties"] for row in rows if row["properties"]["status"] == "observed"
            ]
            missing = [
                row["properties"] for row in rows if row["properties"]["status"] == "missing"
            ]
            self.assertEqual(len(observed), 1)
            self.assertEqual(observed[0]["population"], 0.0)
            self.assertTrue(missing)
            self.assertTrue(all(row["population"] is None for row in missing))
            self.assertEqual(metadata["observed_at"], "2021-03-31")
            self.assertEqual(metadata["retrieved_at"], "2026-10-23T12:00:00Z")


if __name__ == "__main__":
    unittest.main()
