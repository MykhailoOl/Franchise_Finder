import os
from uuid import uuid4

import psycopg
import pytest

from app.cli.import_addresses import ADDRESS_SCHEMA_PATH
from app.cli.import_buildings import SCHEMA_PATH
from app.cli.import_franchises import FRANCHISE_SCHEMA_PATH
from app.cli.import_profile import PROFILE_SCHEMA_PATH
from app.core.config import Settings
from app.core.experiment import load_experiment
from app.db.experiment_readiness import fingerprint_table, prepare_readiness

pytestmark = pytest.mark.skipif(
    os.environ.get("MASTERFRANCHISES_TEST_POSTGIS") != "1",
    reason="Set MASTERFRANCHISES_TEST_POSTGIS=1 for isolated PostgreSQL integration checks",
)


def test_readiness_is_read_only_candidate_independent_and_content_fingerprinted():
    settings = Settings()
    schema = "experiment_test_" + uuid4().hex
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
        for path in (SCHEMA_PATH, ADDRESS_SCHEMA_PATH, FRANCHISE_SCHEMA_PATH, PROFILE_SCHEMA_PATH):
            connection.execute(path.read_text(encoding="utf-8"))
        connection.execute("""
            INSERT INTO map_sources VALUES
                ('warsaw-buildings','Buildings','https://example.test','Test',NULL,now(),1,'{}'),
                ('warsaw-addresses','Addresses','https://example.test','Test',NULL,now(),1,'{}');
            INSERT INTO buildings(source_id,dataset_id,geometry,properties,imported_at)
            VALUES('building','warsaw-buildings',ST_Multi(ST_Transform(
                ST_MakeEnvelope(21.0,52.2,21.001,52.201,4326),3857)),'{}',now());
            INSERT INTO addresses(source_id,dataset_id,street,street_normalized,house_number,
                house_number_normalized,city,address,geometry,properties,imported_at,
                building_source_id,match_status,candidate_count)
            VALUES('address','warsaw-addresses','A','a','1','1','Warszawa','A 1',ST_Transform(
                ST_SetSRID(ST_Point(21.0005,52.2005),4326),3857),'{}',now(),'building','matched',1);
            INSERT INTO franchise_categories VALUES('grocery','Grocery');
            INSERT INTO franchise_brands VALUES('brand','Brand','grocery','https://example.test',
                'https://example.test',now(),'complete','Test','{}');
        """)
        connection.commit()
        with pytest.raises(ValueError, match="read-only repeatable-read"):
            prepare_readiness(connection, load_experiment())
        connection.rollback()
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        before, candidates_before, _ = prepare_readiness(connection, load_experiment())
        assert candidates_before[0]["pools"] == ["valid_addressed", "all_valid"]
        assert before["summary"]["data_readiness"]["status"] == "missing_data"
        assert all(row["status"] == "missing_data" for row in before["summary"]["category_support"])
        original_hash = fingerprint_table(connection, "buildings")
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            connection.execute("DELETE FROM buildings")
        connection.rollback()
        connection.execute("""
            INSERT INTO franchise_outlets(brand_id,source_id,address_raw,city,source_url,
                retrieved_at,status,operating_model,attributes,building_source_id,match_status)
            VALUES('brand','outlet','A 1','Warszawa','https://example.test',now(),'listed',
                'unknown','{}','building','matched');
            UPDATE buildings SET properties='{"changed":true}';
        """)
        connection.commit()
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        after, candidates_after, _ = prepare_readiness(connection, load_experiment())
        assert candidates_before == candidates_after
        assert before["dataset_sha256"] != after["dataset_sha256"]
        changed_hash = fingerprint_table(connection, "buildings")
        assert original_hash["row_count"] == changed_hash["row_count"]
        assert original_hash["sha256"] != changed_hash["sha256"]
        connection.rollback()
    finally:
        connection.rollback()
        connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        connection.commit()
        connection.close()
