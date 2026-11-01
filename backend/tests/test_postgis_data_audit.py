import json
import os
from contextlib import contextmanager
from uuid import uuid4

import psycopg
import pytest

from app.cli.audit_data import audit_database
from app.cli.import_addresses import ADDRESS_SCHEMA_PATH
from app.cli.import_buildings import SCHEMA_PATH
from app.cli.import_franchises import FRANCHISE_SCHEMA_PATH
from app.cli.import_profile import PROFILE_SCHEMA_PATH, import_profile
from app.core.config import Settings

pytestmark = pytest.mark.skipif(
    os.environ.get("MASTERFRANCHISES_TEST_POSTGIS") != "1",
    reason="Set MASTERFRANCHISES_TEST_POSTGIS=1 for isolated PostgreSQL integration checks",
)


def test_activity_refresh_isolated_atomic_and_audit_deduplicates_presence(tmp_path, monkeypatch):
    settings = Settings()
    schema = "data_audit_test_" + uuid4().hex
    connection = psycopg.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        dbname=settings.postgres_db,
        user=settings.postgres_user,
        password=settings.postgres_password.get_secret_value(),
        connect_timeout=5,
    )
    try:
        connection.execute(f'CREATE SCHEMA "{schema}"')
        connection.execute(f'SET search_path="{schema}",public')
        empty_report = audit_database(connection, 1)
        assert empty_report["datasets"] == {}
        assert not any(empty_report["available_tables"].values())
        for path in (SCHEMA_PATH, ADDRESS_SCHEMA_PATH, FRANCHISE_SCHEMA_PATH, PROFILE_SCHEMA_PATH):
            connection.execute(path.read_text(encoding="utf-8"))
        connection.execute("""
            INSERT INTO map_sources VALUES
                ('warsaw-buildings','Buildings','https://example.test','Test',NULL,now(),1,'{}');
            INSERT INTO buildings(source_id,dataset_id,geometry,properties,imported_at)
            VALUES('building-a','warsaw-buildings',ST_Multi(ST_Transform(
                ST_MakeEnvelope(20.9999,52.1999,21.0001,52.2001,4326),3857)),
                '{"ID_BUDYNKU":"building-a"}',now());
            INSERT INTO profile_sources(id,kind,metadata,feature_count) VALUES
                ('population','population','{}',1),('pois','pois','{}',1);
            INSERT INTO profile_population VALUES('cell',ST_Multi(ST_Transform(
                ST_MakeEnvelope(20.9,52.1,21.1,52.3,4326),2180)),0,'observed',2021,125);
            INSERT INTO profile_pois(source_id,normalized_name,kind,categories,geometry,
                source_url,location_method,tags) VALUES('node/8','shop','competitor',
                '{grocery}',ST_Transform(ST_SetSRID(ST_Point(21,52.2),4326),2180),
                'https://example.test','node','{}');
            INSERT INTO franchise_categories VALUES('grocery','Grocery'),('beauty','Beauty');
            INSERT INTO franchise_brands VALUES
                ('first','First','grocery','https://example.test','https://example.test',
                    now(),'complete','Complete published locator','{}'),
                ('second','Second','beauty','https://example.test','https://example.test',
                    now(),'partial','Partial source','{}');
            INSERT INTO franchise_outlets(brand_id,source_id,address_raw,city,source_url,
                retrieved_at,status,operating_model,attributes,building_source_id,match_status)
            VALUES('first','1','A','Warszawa','https://example.test',now(),'listed','unknown',
                    '{}','building-a','matched'),
                ('first','2','B','Warszawa','https://example.test',now(),'listed','unknown',
                    '{}','building-a','matched'),
                ('second','3','C','Warszawa','https://example.test',now(),'listed','unknown',
                    '{}','building-a','matched'),
                ('second','4','D','Warszawa','https://example.test',now(),'listed','unknown',
                    '{}',NULL,'conflict');
            UPDATE franchise_outlets SET geometry=ST_Transform(
                ST_SetSRID(ST_Point(21,52.2),4326),3857),match_method='point_inside_building'
                WHERE match_status='matched';
        """)
        connection.commit()

        @contextmanager
        def isolated_connection(**kwargs):
            with connection.transaction():
                yield connection

        monkeypatch.setattr("app.cli.import_profile.psycopg.connect", isolated_connection)
        metadata = tmp_path / "activity.metadata.json"
        metadata.write_text(
            json.dumps(
                {
                    "id": "activity",
                    "name": "Activity",
                    "url": "https://example.test",
                    "attribution": "Test",
                    "retrieved_at": "2026-11-01T12:00:00Z",
                    "coverage_bounds": [20.82, 52.07, 21.32, 52.40],
                }
            ),
            encoding="utf-8",
        )
        path = tmp_path / "activity.geojson"
        features = [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [21, 52.2]},
                "properties": {
                    "source_id": f"node/{identifier}",
                    "name": "Same school",
                    "kind": "activity",
                    "categories": ["school"],
                    "source_url": "https://example.test",
                },
            }
            for identifier in (1, 2)
        ]

        def write_features(selected):
            path.write_text(
                json.dumps({"type": "FeatureCollection", "features": selected}), encoding="utf-8"
            )

        write_features(features)
        result = import_profile([("activity", path, metadata)], settings)
        assert result == {"activity": 2, "activity_duplicate_records": 1}
        assert connection.execute("SELECT count(*) FROM profile_pois").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM profile_population").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM profile_sources").fetchone()[0] == 3
        connection.commit()
        write_features(features[:1])
        import_profile([("activity", path, metadata)], settings)
        assert connection.execute("SELECT count(*) FROM profile_activity").fetchone()[0] == 1
        connection.commit()
        with pytest.raises(ValueError, match="snapshots are separate"):
            import_profile([("pois", path, metadata)], settings)
        assert connection.execute("SELECT count(*) FROM profile_pois").fetchone()[0] == 1
        connection.commit()
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        report = audit_database(connection, 1)
        assert report["database_read_only"] is True
        assert report["status"] == "audited"
        franchises = report["datasets"]["franchises"]
        assert franchises["count"] == 4
        assert len(franchises["presence_observations"]) == 2
        grocery = next(
            row for row in franchises["presence_observations"] if row["category_id"] == "grocery"
        )
        assert len(grocery["source_outlets"]) == 2
        assert len(franchises["excluded_unresolved_outlets"]) == 1
        assert report["datasets"]["population"]["explicit_zero_cells"] == 1
        assert report["datasets"]["activity"]["count"] == 1
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            connection.execute("DELETE FROM profile_activity")
        connection.rollback()
        connection.execute("""
            UPDATE franchise_outlets SET match_method='exact_address_near_point',
                match_distance_m=1 WHERE source_id='1'
        """)
        connection.commit()
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        invalid_report = audit_database(connection, 1)
        assert invalid_report["status"] == "integrity_failures"
        assert invalid_report["datasets"]["franchises"]["association_evidence_violations"] == 1
        connection.rollback()
    finally:
        connection.rollback()
        connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        connection.commit()
        connection.close()
