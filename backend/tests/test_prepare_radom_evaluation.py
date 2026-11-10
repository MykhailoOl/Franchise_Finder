import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest

import app.cli.prepare_radom_evaluation as preparation
from app.core.config import Settings
from app.core.experiment import canonical_bytes
from app.core.grocery_exposure import file_sha256
from app.db.radom_evaluation import RadomSnapshot, assert_isolated, normalize_radom_address


def write(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_bytes(value))


def snapshot(brand, outlets):
    return {
        "schema_version": 1,
        "categories": [{"id": "grocery", "name": "Grocery"}],
        "brands": [
            {
                "id": brand,
                "name": brand,
                "category_id": "grocery",
                "website": "https://example.test",
                "source_url": "https://example.test",
                "retrieved_at": "2026-11-11T00:00:00Z",
                "coverage_status": "complete",
                "coverage_note": "Synthetic complete locator",
            }
        ],
        "outlets": outlets,
        "provenance": {"territory": "Radom"},
    }


def outlet(brand, identifier, house="1", coordinates=None):
    return {
        "brand_id": brand,
        "source_id": identifier,
        "coordinates": coordinates,
        "address": {
            "raw": f"Testowa {house}, 26-600 Radom",
            "street": "Testowa",
            "house_number": house,
            "postal_code": "26-600",
            "city": "Radom",
        },
        "source_url": "https://example.test/outlet",
        "retrieved_at": "2026-11-11T00:00:00Z",
    }


def test_radom_schema_preserves_original_city_rule_and_does_not_accept_warsaw():
    data = snapshot("zabka", [outlet("zabka", "one", "10A/2")])
    parsed = RadomSnapshot.model_validate(data)
    assert parsed.outlets[0].address.city == "Radom"
    assert parsed.outlets[0].address.house_number == "10A/2"
    data["outlets"][0]["address"]["city"] = "Warszawa"
    with pytest.raises(ValueError, match="Radom"):
        RadomSnapshot.model_validate(data)
    address = {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [21.15, 51.4]},
        "properties": {
            "source_id": "official-address",
            "ID_GMINY": "1463011",
            "street": "Testowa",
            "house_number": "10a/2",
            "city": "Radom",
        },
    }
    normalized = normalize_radom_address(address)
    assert normalized[0] == "official-address"
    assert normalized[3] == "10a/2"
    address["properties"]["street"] = ""
    streetless = normalize_radom_address(address)
    assert streetless[1:3] == ("", "")
    assert streetless[3] == "10a/2"
    assert streetless[7] == "10a/2, Radom"
    address["properties"]["ID_GMINY"] = "1465011"
    with pytest.raises(ValueError, match="municipality"):
        normalize_radom_address(address)


def test_source_manifest_must_bind_every_normalized_input_and_reject_tampering(tmp_path):
    digest = "a" * 64
    for name in preparation.REQUIRED_INPUTS:
        write(tmp_path / name, {"fixture": name})
    for kind in preparation.SOURCE_KINDS:
        write(
            tmp_path / f"{kind}.metadata.json",
            {"geojson_sha256": file_sha256(tmp_path / f"{kind}.geojson")},
        )
    manifest = {
        "protocol_sha256": digest,
        "artifacts": {name: file_sha256(tmp_path / name) for name in preparation.REQUIRED_INPUTS},
    }
    write(tmp_path / "manifest.json", manifest)
    assert preparation.verify_sources(tmp_path, digest) == manifest
    with pytest.raises(ValueError, match="another frozen"):
        preparation.verify_sources(tmp_path, "b" * 64)
    write(tmp_path / "zabka.json", {"changed": True})
    with pytest.raises(ValueError, match="changed"):
        preparation.verify_sources(tmp_path, digest)


@pytest.mark.parametrize("distance,overlap", [(2000, False), (1999.99, False), (50000, True)])
def test_freshness_requires_strict_separation_and_no_exposed_ids(distance, overlap):
    class Connection:
        def execute(self, statement, parameters):
            return self

        def fetchone(self):
            return (distance,)

    old = {"bounds_2180": [0, 0, 1, 1], "exposed_ids": ["fresh"] if overlap else ["old"]}
    with pytest.raises(ValueError, match="overlaps"):
        preparation.prove_freshness(Connection(), [{"building_source_id": "fresh"}], old)


@pytest.fixture
def synthetic_sources(tmp_path):
    if os.environ.get("MASTERFRANCHISES_TEST_POSTGIS") != "1":
        pytest.skip("Set MASTERFRANCHISES_TEST_POSTGIS=1 for isolated PostgreSQL checks")
    settings = Settings()
    with psycopg.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        dbname=settings.postgres_db,
        user=settings.postgres_user,
        password=settings.postgres_password.get_secret_value(),
    ) as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        x, y = connection.execute(
            "SELECT ST_X(p),ST_Y(p) FROM (SELECT ST_Transform("
            "ST_SetSRID(ST_Point(21.15,51.4),4326),2180) AS p) s"
        ).fetchone()

        def geometry(kind, offset=0):
            if kind == "Point":
                return connection.execute(
                    "SELECT ST_AsGeoJSON(ST_Transform("
                    "ST_SetSRID(ST_Point(%s,%s),2180),4326))::json",
                    (x + offset, y),
                ).fetchone()[0]
            return connection.execute(
                "SELECT ST_AsGeoJSON(ST_Transform(ST_MakeEnvelope(%s,%s,%s,%s,2180),4326))::json",
                (x + offset - 10, y - 10, x + offset + 10, y + 10),
            ).fetchone()[0]

        buildings = [
            {
                "type": "Feature",
                "geometry": geometry("Polygon", index * 100),
                "properties": {
                    "ID_BUDYNKU": f"146301_1.fixture.{index}_BUD",
                    "RODZAJ": "h",
                    "KONDYGNACJE_NADZIEMNE": 0,
                },
            }
            for index in range(3)
        ]
        addresses = [
            {
                "type": "Feature",
                "geometry": geometry("Point", index * 100),
                "properties": {
                    "source_id": f"address-{index}",
                    "ID_GMINY": "1463011",
                    "street": "Testowa",
                    "house_number": str(index + 1),
                    "postal_code": "26-600",
                    "city": "Radom",
                },
            }
            for index in range(2)
        ]
        addresses[1]["properties"]["street"] = ""
        large = connection.execute(
            "SELECT ST_AsGeoJSON(ST_Transform(ST_MakeEnvelope(%s,%s,%s,%s,2180),4326))::json",
            (x - 2000, y - 2000, x + 2000, y + 2000),
        ).fetchone()[0]
        population = [
            {
                "type": "Feature",
                "geometry": large,
                "properties": {
                    "source_id": "cell",
                    "population": None,
                    "status": "missing",
                    "observation_year": 2021,
                    "resolution_m": 125,
                },
            }
        ]
        pois = [
            {
                "type": "Feature",
                "geometry": geometry("Point", 250),
                "properties": {
                    "source_id": "node/bus",
                    "kind": "access",
                    "categories": ["bus_stop"],
                    "name": "Bus",
                    "source_url": "https://example.test",
                    "tags": {},
                },
            }
        ]
        activity = [
            {
                "type": "Feature",
                "geometry": geometry("Point", 350),
                "properties": {
                    "source_id": "node/office",
                    "kind": "activity",
                    "categories": ["office"],
                    "name": "Office",
                    "source_url": "https://example.test",
                    "tags": {},
                },
            }
        ]
        near = geometry("Point", 18)["coordinates"]
        conflict = geometry("Point", 100)["coordinates"]
        unaddressed = geometry("Point", 200)["coordinates"]
    directory = tmp_path / "sources"
    for kind, rows in {
        "buildings": buildings,
        "addresses": addresses,
        "population": population,
        "pois": pois,
        "activity": activity,
    }.items():
        write(directory / f"{kind}.geojson", {"type": "FeatureCollection", "features": rows})
        source_id = {
            "population": "radom-population-2021",
            "pois": "radom-osm-pois",
            "activity": "radom-osm-activity",
        }.get(kind, f"radom-{kind}")
        write(
            directory / f"{kind}.metadata.json",
            {
                "id": source_id,
                "name": source_id,
                "url": "https://example.test",
                "attribution": "Synthetic public source",
                "retrieved_at": "2026-11-11T00:00:00Z",
                "observed_at": "2021-03-31" if kind == "population" else None,
                "snapshot_at": "2026-11-11T00:00:00Z" if kind in {"pois", "activity"} else None,
                "coverage_bounds": [21.03, 51.33, 21.28, 51.50],
                "geojson_sha256": file_sha256(directory / f"{kind}.geojson"),
            },
        )
    write(
        directory / "zabka.json",
        snapshot(
            "zabka",
            [
                outlet("zabka", "near", coordinates=near),
                outlet("zabka", "conflict", coordinates=conflict),
                outlet("zabka", "unaddressed", house="99", coordinates=unaddressed),
            ],
        ),
    )
    write(directory / "lewiatan.json", snapshot("lewiatan", [outlet("lewiatan", "same-building")]))
    digest = "a" * 64
    write(
        directory / "manifest.json",
        {
            "collector_version": "radom-public-sources-v1",
            "protocol_sha256": digest,
            "artifacts": {
                name: file_sha256(directory / name) for name in preparation.REQUIRED_INPUTS
            },
        },
    )
    return directory, digest, (x, y), settings


def test_full_preparation_isolated_complete_frame_shared_features_and_immutable_output(
    synthetic_sources, tmp_path, monkeypatch
):
    directory, digest, center, settings = synthetic_sources
    protocol = {
        "created_at": "2026-11-10T00:00:00Z",
        "collector": {"city_service_bounds": [21.03, 51.33, 21.28, 51.50]},
    }
    monkeypatch.setattr(preparation, "ROOT", tmp_path)
    monkeypatch.setattr(preparation, "validate_protocol", lambda path, sha: copy.deepcopy(protocol))
    reference = {
        "bounds_2180": [center[0] - 1000, center[1] + 50000, center[0] + 1000, center[1] + 51000],
        "snapshot": {"sha256": "b" * 64},
        "exposed_ids": ["old-warsaw"],
        "exposure_sha256": "c" * 64,
    }

    def old_reference(connection, config):
        value = {**reference, "public_snapshots": preparation.public_fingerprints(connection)}
        connection.commit()
        return value

    monkeypatch.setattr(preparation, "old_study_reference", old_reference)
    output = tmp_path / ".local/evaluations/prepared"
    result = preparation.prepare(tmp_path / "protocol.json", digest, directory, output, settings)
    manifest = json.loads((result / "manifest.json").read_text())
    assert manifest["counts"] == {"rows": 2, "positives": 1, "background": 1}
    assert manifest["candidate_frame"]["complete"] is True
    assert manifest["freshness"]["minimum_distance_m"] > 2000
    assert manifest["scoring_performed"] is False
    assert manifest["public_tables_unchanged"] is True
    quality = json.loads((result / "quality.json").read_text())
    assert quality["scope"] == "external_full_candidate_frame"
    assert quality["external_cohort_statistics_computed"] is True
    assert "final_holdout_statistics_computed" not in quality
    rows = [json.loads(line) for line in (result / "features.jsonl").read_text().splitlines()]
    assert len(rows) == 2
    assert all(len(row["values"]) == 17 for row in rows)
    assert all(row["values"]["resident_subtotal_500m"] is None for row in rows)
    assert all(row["values"]["mapped_storeys"] is None for row in rows)
    assert all(row["values"]["office_count_500m"] == 1 for row in rows)
    unresolved = json.loads((result / "unresolved-outlets.json").read_text())
    assert any(row["match_status"] == "conflict" for row in unresolved["outlets"])
    assert len(unresolved["matched_outside_candidate_frame"]) == 1
    assert (
        preparation.prepare(tmp_path / "protocol.json", digest, directory, output, settings)
        == result
    )
    with (result / "features.jsonl").open("ab") as stream:
        stream.write(b"changed")
    with pytest.raises(ValueError, match="modified"):
        preparation.prepare(tmp_path / "protocol.json", digest, directory, output, settings)
    with psycopg.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        dbname=settings.postgres_db,
        user=settings.postgres_user,
        password=settings.postgres_password.get_secret_value(),
    ) as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        assert (
            connection.execute(
                "SELECT count(*) FROM public.buildings WHERE source_id LIKE '146301_1.fixture.%'"
            ).fetchone()[0]
            == 0
        )
        with pytest.raises(ValueError, match="isolated Radom"):
            assert_isolated(connection)


def test_duplicate_source_identity_aborts_and_cleans_temporary_schema(
    synthetic_sources, tmp_path, monkeypatch
):
    directory, digest, center, settings = synthetic_sources
    protocol = {
        "created_at": "2026-11-10T00:00:00Z",
        "collector": {"city_service_bounds": [21.03, 51.33, 21.28, 51.50]},
    }
    monkeypatch.setattr(preparation, "ROOT", tmp_path)
    monkeypatch.setattr(preparation, "validate_protocol", lambda path, sha: protocol)
    monkeypatch.setattr(preparation, "old_study_reference", lambda connection, config: {})
    monkeypatch.setattr(preparation, "uuid4", lambda: SimpleNamespace(hex="duplicatefixture"))
    path = directory / "buildings.geojson"
    data = json.loads(path.read_text())
    data["features"].append(copy.deepcopy(data["features"][0]))
    write(path, data)
    metadata = json.loads((directory / "buildings.metadata.json").read_text())
    metadata["geojson_sha256"] = file_sha256(path)
    write(directory / "buildings.metadata.json", metadata)
    manifest = json.loads((directory / "manifest.json").read_text())
    manifest["artifacts"] = {
        name: file_sha256(directory / name) for name in preparation.REQUIRED_INPUTS
    }
    write(directory / "manifest.json", manifest)
    output = tmp_path / ".local/evaluations/prepared"
    with pytest.raises(psycopg.errors.UniqueViolation):
        preparation.prepare(tmp_path / "protocol.json", digest, directory, output, settings)
    assert not output.exists()
    with psycopg.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        dbname=settings.postgres_db,
        user=settings.postgres_user,
        password=settings.postgres_password.get_secret_value(),
    ) as connection:
        assert (
            connection.execute(
                "SELECT to_regnamespace('radom_evaluation_duplicatefixture')"
            ).fetchone()[0]
            is None
        )
