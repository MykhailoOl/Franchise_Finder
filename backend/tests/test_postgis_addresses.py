import os
from datetime import UTC, datetime
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from app.cli.import_addresses import ADDRESS_SCHEMA_PATH, UPSERT_ADDRESSES
from app.cli.import_buildings import SCHEMA_PATH, UPSERT_BUILDINGS
from app.core.config import Settings

pytestmark = pytest.mark.skipif(
    os.environ.get("MASTERFRANCHISES_TEST_POSTGIS") != "1",
    reason="Set MASTERFRANCHISES_TEST_POSTGIS=1 to run isolated PostgreSQL integration checks",
)


def test_spatial_matches_and_import_identity_survive_atomic_refresh() -> None:
    settings = Settings()
    connection = psycopg.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        dbname=settings.postgres_db,
        user=settings.postgres_user,
        password=settings.postgres_password.get_secret_value(),
    )
    try:
        schema = "address_test_" + uuid4().hex
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        connection.execute(
            sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(schema))
        )
        connection.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
        connection.execute(ADDRESS_SCHEMA_PATH.read_text(encoding="utf-8"))
        connection.execute(
            "INSERT INTO map_sources VALUES"
            " ('warsaw-buildings','Test','https://example.test','Test',NULL,now(),2,"
            " ARRAY[20.8,52.1,21.3,52.4]),"
            " ('warsaw-addresses','Test','https://example.test','Test',NULL,now(),4,"
            " ARRAY[20.8,52.1,21.3,52.4])"
        )
        connection.execute(
            "INSERT INTO buildings (source_id,dataset_id,geometry,properties,imported_at) VALUES"
            " ('a','warsaw-buildings',ST_Multi(ST_MakeEnvelope(2330000,6830000,"
            " 2330010,6830010,3857)),'{}',now()),"
            " ('b','warsaw-buildings',ST_Multi(ST_MakeEnvelope(2330005,6830005,"
            " 2330015,6830015,3857)),'{}',now())"
        )
        points = [
            ("inside", 2330002, 6830002),
            ("boundary", 2330000, 6830003),
            ("overlap", 2330007, 6830007),
            ("far", 2330100, 6830100),
        ]
        for source_id, x, y in points:
            connection.execute(
                "INSERT INTO addresses (source_id,dataset_id,street,street_normalized,"
                " house_number,house_number_normalized,city,address,geometry,properties,"
                " imported_at)"
                " VALUES (%s,'warsaw-addresses','Test','test','10','10','Warszawa',%s,"
                " ST_SetSRID(ST_Point(%s,%s),3857),'{}',now())",
                (source_id, source_id, x, y),
            )
        connection.execute("SELECT refresh_address_building_links()")
        rows = {
            row[0]: row[1:]
            for row in connection.execute(
                "SELECT source_id,match_status,building_source_id,candidate_count FROM addresses"
            )
        }
        assert rows == {
            "inside": ("matched", "a", 1),
            "boundary": ("matched", "a", 1),
            "overlap": ("ambiguous", None, 2),
            "far": ("unmatched", None, 0),
        }
        address_ids = dict(connection.execute("SELECT source_id,id FROM addresses"))
        building_ids = dict(connection.execute("SELECT source_id,id FROM buildings"))
        connection.execute("CREATE TEMP TABLE address_stage AS SELECT * FROM addresses")
        connection.execute('UPDATE address_stage SET properties = \'{"ID_IIP":"new-version"}\'')
        connection.execute(
            UPSERT_ADDRESSES,
            {"dataset_id": "warsaw-addresses", "imported_at": datetime.now(UTC)},
        )
        assert dict(connection.execute("SELECT source_id,id FROM addresses")) == address_ids
        assert (
            connection.execute(
                "SELECT building_source_id FROM addresses WHERE source_id='inside'"
            ).fetchone()[0]
            == "a"
        )
        connection.execute("CREATE TEMP TABLE building_stage AS SELECT * FROM buildings")
        connection.execute(
            "UPDATE building_stage SET geometry=ST_Translate(geometry,20,0) WHERE source_id='a'"
        )
        connection.execute(
            UPSERT_BUILDINGS,
            {"dataset_id": "warsaw-buildings", "imported_at": datetime.now(UTC)},
        )
        connection.execute("SELECT refresh_address_building_links()")
        assert dict(connection.execute("SELECT source_id,id FROM buildings")) == building_ids
        assert dict(connection.execute("SELECT source_id,id FROM addresses")) == address_ids
        assert connection.execute(
            "SELECT match_status,building_source_id FROM addresses WHERE source_id='inside'"
        ).fetchone() == ("unmatched", None)
        assert connection.execute(
            "SELECT match_status,building_source_id FROM addresses WHERE source_id='overlap'"
        ).fetchone() == ("matched", "b")
    finally:
        connection.rollback()
        connection.close()
