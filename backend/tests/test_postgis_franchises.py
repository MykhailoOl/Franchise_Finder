import json
import os
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from app.cli.import_addresses import ADDRESS_SCHEMA_PATH
from app.cli.import_buildings import SCHEMA_PATH
from app.cli.import_franchises import FRANCHISE_SCHEMA_PATH, import_franchises
from app.core.config import Settings

pytestmark = pytest.mark.skipif(
    os.environ.get("MASTERFRANCHISES_TEST_POSTGIS") != "1",
    reason="Set MASTERFRANCHISES_TEST_POSTGIS=1 to run isolated PostgreSQL integration checks",
)


def test_evidence_matching_conflicts_multiple_brands_and_refresh(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = Settings()
    connection = psycopg.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        dbname=settings.postgres_db,
        user=settings.postgres_user,
        password=settings.postgres_password.get_secret_value(),
    )
    try:
        schema = "franchise_test_" + uuid4().hex
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        connection.execute(
            sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(schema))
        )
        for path in (SCHEMA_PATH, ADDRESS_SCHEMA_PATH, FRANCHISE_SCHEMA_PATH):
            connection.execute(path.read_text(encoding="utf-8"))
        connection.execute(
            "INSERT INTO map_sources VALUES"
            " ('warsaw-buildings','Test','https://example.test','Test',NULL,now(),3,'{}'),"
            " ('warsaw-addresses','Test','https://example.test','Test',NULL,now(),4,'{}')"
        )
        for source_id, x, y in (("a", 0, 0), ("b", 20, 0), ("overlap", 5, 5)):
            connection.execute(
                "INSERT INTO buildings (source_id,dataset_id,geometry,properties,imported_at)"
                " VALUES (%s,'warsaw-buildings',ST_Multi(ST_Transform(ST_MakeEnvelope("
                " 638000+%s,486000+%s,638010+%s,486010+%s,2180),3857)),'{}',now())",
                (source_id, x, y, x, y),
            )
        for source_id, house, x, y in (
            ("address-a", "1", 2, 2),
            ("address-b", "2", 25, 2),
            ("duplicate-a", "3", 2, 2),
            ("duplicate-b", "3", 25, 2),
        ):
            connection.execute(
                "INSERT INTO addresses (source_id,dataset_id,street,street_normalized,house_number,"
                " house_number_normalized,postal_code,city,address,geometry,properties,imported_at)"
                " VALUES (%s,'warsaw-addresses','Example','example',%s,%s,'00-001','Warszawa',%s,"
                " ST_Transform(ST_SetSRID(ST_Point(638000+%s,486000+%s),2180),3857),'{}',now())",
                (source_id, house, house, source_id, x, y),
            )
        connection.execute("SELECT refresh_address_building_links()")
        connection.execute("INSERT INTO franchise_categories VALUES ('test','Test')")
        connection.execute(
            "INSERT INTO franchise_brands VALUES"
            " ('first','First','test','https://example.test','https://example.test',now(),"
            " 'complete','Published snapshot','{}'),"
            " ('second','Second','test','https://example.test','https://example.test',now(),"
            " 'complete','Published snapshot','{}')"
        )
        records = [
            ("exact", "first", "1", None, None),
            ("consistent", "first", "1", 2, 2),
            ("near", "second", "1", -5, 2),
            ("too-far", "first", "1", -11, 2),
            ("conflict", "first", "1", 25, 2),
            ("overlapping", "first", "1", 7, 7),
            ("duplicate", "first", "3", None, None),
            ("point-only", "first", None, 25, 2),
            ("no-nearest", "first", None, 35, 2),
        ]
        for source_id, brand, house, x, y in records:
            connection.execute(
                "INSERT INTO franchise_outlets (brand_id,source_id,address_raw,street,"
                " street_normalized,house_number,house_number_normalized,postal_code,city,geometry,"
                " source_url,retrieved_at,status,operating_model,attributes)"
                " VALUES (%s,%s,'Example','Example','example',%s,%s,'00-001','Warszawa',"
                " CASE WHEN %s::float8 IS NOT NULL THEN ST_Transform(ST_SetSRID("
                " ST_Point(638000+%s,486000+%s),2180),3857) END,"
                " 'https://example.test',now(),'listed','unknown','{}')",
                (brand, source_id, house, house, x, x, y),
            )
        connection.execute("SELECT refresh_franchise_links()")
        results = {
            row[0]: row[1:]
            for row in connection.execute(
                "SELECT source_id,match_status,match_method,building_source_id"
                " FROM franchise_outlets"
            )
        }
        assert results == {
            "exact": ("matched", "exact_address", "a"),
            "consistent": ("matched", "address_and_point", "a"),
            "near": ("matched", "exact_address_near_point", "a"),
            "too-far": ("conflict", None, None),
            "conflict": ("conflict", None, None),
            "overlapping": ("ambiguous", None, None),
            "duplicate": ("ambiguous", None, None),
            "point-only": ("matched", "point_inside_building", "b"),
            "no-nearest": ("unmatched", None, None),
        }
        assert connection.execute(
            "SELECT match_distance_m FROM franchise_outlets WHERE source_id='near'"
        ).fetchone()[0] == pytest.approx(5, abs=0.001)
        assert connection.execute(
            "SELECT outlet_count,brand_ids,brand_flags FROM building_franchise_summary"
            " WHERE building_source_id='a'"
        ).fetchone() == (3, ["first", "second"], {"brand_first": True, "brand_second": True})
        identities = dict(connection.execute("SELECT source_id,id FROM franchise_outlets"))
        connection.execute(FRANCHISE_SCHEMA_PATH.read_text(encoding="utf-8"))
        connection.execute("SELECT refresh_franchise_links()")
        assert dict(connection.execute("SELECT source_id,id FROM franchise_outlets")) == identities
        connection.execute(
            "UPDATE buildings SET geometry=ST_Translate(geometry,1000,0) WHERE source_id='a'"
        )
        connection.execute("SELECT refresh_address_building_links()")
        connection.execute("SELECT refresh_franchise_links()")
        assert dict(connection.execute("SELECT source_id,id FROM franchise_outlets")) == identities
        assert (
            connection.execute(
                "SELECT building_source_id FROM franchise_outlets WHERE source_id='exact'"
            ).fetchone()[0]
            is None
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM building_franchise_summary WHERE building_source_id='a'"
            ).fetchone()[0]
            == 0
        )

        @contextmanager
        def isolated_connection(**kwargs):
            with connection.transaction():
                yield connection

        monkeypatch.setattr("app.cli.import_franchises.psycopg.connect", isolated_connection)
        snapshot = {
            "schema_version": 1,
            "categories": [{"id": "test", "name": "Test"}],
            "brands": [
                {
                    "id": "first",
                    "name": "Updated first",
                    "category_id": "test",
                    "website": "https://example.test",
                    "source_url": "https://example.test",
                    "retrieved_at": "2026-10-23T12:00:00Z",
                    "coverage_status": "partial",
                    "coverage_note": "Partial source response",
                }
            ],
            "outlets": [
                {
                    "brand_id": "first",
                    "source_id": "fresh",
                    "address": {"raw": "Example 1", "city": "Warszawa"},
                    "source_url": "https://example.test",
                    "retrieved_at": "2026-10-23T12:00:00Z",
                }
            ],
        }
        path = tmp_path / "snapshot.json"
        path.write_text(json.dumps(snapshot), encoding="utf-8")
        import_franchises([path], settings)
        assert connection.execute("SELECT count(*) FROM franchise_outlets").fetchone()[0] == 10
        refreshed_ids = dict(connection.execute("SELECT source_id,id FROM franchise_outlets"))
        assert all(refreshed_ids[key] == value for key, value in identities.items())
        snapshot["brands"][0]["coverage_status"] = "unavailable"
        existing_outlets = snapshot["outlets"]
        snapshot["outlets"] = []
        path.write_text(json.dumps(snapshot), encoding="utf-8")
        with pytest.raises(ValueError, match="cannot replace existing"):
            import_franchises([path], settings)
        assert connection.execute("SELECT count(*) FROM franchise_outlets").fetchone()[0] == 10
        snapshot["brands"][0]["coverage_status"] = "complete"
        snapshot["outlets"] = existing_outlets
        path.write_text(json.dumps(snapshot), encoding="utf-8")
        import_franchises([path], settings)
        assert dict(connection.execute("SELECT source_id,id FROM franchise_outlets")) == {
            "near": identities["near"],
            "fresh": refreshed_ids["fresh"],
        }
        connection.execute(
            "ALTER TABLE franchise_outlets ADD CONSTRAINT rejected_source"
            " CHECK(source_id<>'rejected')"
        )
        snapshot["brands"][0]["name"] = "Must roll back"
        snapshot["outlets"][0]["source_id"] = "rejected"
        path.write_text(json.dumps(snapshot), encoding="utf-8")
        with pytest.raises(psycopg.errors.CheckViolation):
            import_franchises([path], settings)
        assert (
            connection.execute("SELECT name FROM franchise_brands WHERE id='first'").fetchone()[0]
            == "Updated first"
        )
        assert dict(connection.execute("SELECT source_id,id FROM franchise_outlets")) == {
            "near": identities["near"],
            "fresh": refreshed_ids["fresh"],
        }
    finally:
        connection.rollback()
        connection.close()
