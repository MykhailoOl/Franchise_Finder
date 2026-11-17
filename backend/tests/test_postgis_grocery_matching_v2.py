import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from app.core.config import Settings
from app.core.grocery_matching_v2 import decide_match
from app.db.grocery_matching_v2 import collect_matching_evidence

pytestmark = pytest.mark.skipif(
    os.environ.get("MASTERFRANCHISES_TEST_POSTGIS") != "1",
    reason="Set MASTERFRANCHISES_TEST_POSTGIS=1 for isolated PostGIS checks",
)


def connect():
    settings = Settings()
    return psycopg.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        dbname=settings.postgres_db,
        user=settings.postgres_user,
        password=settings.postgres_password.get_secret_value(),
        connect_timeout=5,
    )


def test_readonly_evidence_real_boundaries_distances_exact_numbers_and_provenance():
    schema = "grocery_matching_test_" + uuid4().hex
    with connect() as connection:
        before = connection.execute(
            "SELECT count(*),md5(string_agg(row_to_json(o)::text,'' ORDER BY o.id))"
            " FROM public.franchise_outlets o"
        ).fetchone()
        try:
            connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            connection.execute(sql.SQL("SET search_path={},public").format(sql.Identifier(schema)))
            for filename in ("map_schema.sql", "address_schema.sql", "franchise_schema.sql"):
                query = (Path(__file__).parents[1] / "app/db" / filename).read_text(
                    encoding="utf-8"
                )
                connection.execute(query.replace("CREATE EXTENSION IF NOT EXISTS pg_trgm;", ""))
            connection.execute(
                "INSERT INTO map_sources VALUES"
                " ('warsaw-buildings','Test','https://example.test','Test',NULL,now(),3,'{}'),"
                " ('warsaw-addresses','Test','https://example.test','Test',NULL,now(),5,'{}')"
            )
            for source_id, x in (("one", 0), ("other", 20), ("overlap", 7)):
                connection.execute(
                    "INSERT INTO buildings(source_id,dataset_id,geometry,properties,imported_at)"
                    " VALUES(%s,'warsaw-buildings',ST_Multi(ST_Transform(ST_MakeEnvelope("
                    " 638000+%s,486000,638010+%s,486010,2180),3857)),'{}',now())",
                    (source_id, x, x),
                )
            for source_id, house, x in (
                ("a", "11", 0),
                ("b", "110", 25),
                ("c", "11A", 25),
                ("d", "12", -1),
                ("e", "13", -14),
            ):
                connection.execute(
                    "INSERT INTO addresses(source_id,dataset_id,street,street_normalized,"
                    " house_number,house_number_normalized,postal_code,city,address,geometry,"
                    " properties,imported_at) VALUES(%s,'warsaw-addresses','Romualda Traugutta',"
                    " 'romualda traugutta',%s,%s,'26-600','Radom','Test',"
                    " ST_Transform(ST_SetSRID(ST_Point(638000+%s,486000+%s),2180),3857),"
                    " '{}',now())",
                    (source_id, house, house, x, 0 if source_id == "a" else 5),
                )
            connection.execute("SELECT refresh_address_building_links()")
            connection.execute("INSERT INTO franchise_categories VALUES('grocery','Grocery')")
            connection.execute(
                "INSERT INTO franchise_brands VALUES('zabka','Test','grocery',"
                " 'https://example.test','https://example.test',now(),'complete','Test','{}')"
            )
            for source_id, house, x, postal in (
                ("boundary", "11", 0, None),
                ("near", "11", -9.5, None),
                ("too-far", "11", -10.1, None),
                ("conflict", "11", 25, None),
                ("overlapping", "11", 8, None),
                ("postal", "11", 0, "26-601"),
                ("slash", "11/1", -1, None),
                ("near-unlinked", "12", 2, None),
                ("far-unlinked", "13", 2, None),
            ):
                connection.execute(
                    "INSERT INTO franchise_outlets(brand_id,source_id,address_raw,street,"
                    " street_normalized,house_number,house_number_normalized,postal_code,city,"
                    " geometry,source_url,retrieved_at,status,operating_model,attributes)"
                    " VALUES('zabka',%s,'Traugutta','Traugutta','traugutta',%s,%s,%s,'Radom',"
                    " ST_Transform(ST_SetSRID(ST_Point(638000+%s,486000+%s),2180),3857),"
                    " 'https://example.test',now(),'listed','unknown','{}')",
                    (
                        source_id,
                        house,
                        house,
                        postal,
                        x,
                        0 if source_id in {"boundary", "postal"} else 5,
                    ),
                )
            connection.commit()
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            connection.execute("SET LOCAL TIME ZONE 'UTC'")
            rows = collect_matching_evidence(
                connection,
                building_dataset_id="warsaw-buildings",
                address_dataset_id="warsaw-addresses",
                bounds=[20.85, 52.09, 21.29, 52.38],
            )
            result = {row["outlet"]["source_id"]: decide_match(row) for row in rows}
            assert result["boundary"]["match_method"] == "expanded_address_and_point"
            assert result["near"]["match_status"] == "matched"
            assert result["near"]["match_distance_m"] == pytest.approx(9.5, abs=1e-6)
            assert result["too-far"]["match_status"] == "conflict"
            assert result["conflict"]["match_status"] == "conflict"
            assert result["overlapping"]["match_status"] == "ambiguous"
            assert result["postal"]["postal_status"] == "disagreement"
            assert result["postal"]["match_status"] == "matched"
            assert result["slash"]["match_status"] == "unmatched"
            assert result["near-unlinked"]["match_method"] == (
                "point_inside_building_near_unlinked_address"
            )
            assert result["near-unlinked"]["address_point_distance_m"] == pytest.approx(1, abs=1e-6)
            assert result["near-unlinked"]["corroborating_address_source_ids"] == ["d"]
            assert result["far-unlinked"]["match_status"] == "unmatched"
            assert (
                connection.execute(
                    "SELECT building_source_id FROM addresses WHERE source_id='d'"
                ).fetchone()[0]
                is None
            )
            assert connection.execute("SHOW transaction_read_only").fetchone()[0] == "on"
            assert all(
                len(row["address_candidates"]) == 1
                for row in rows
                if row["outlet"]["source_id"] != "slash"
            )
            assert len(rows[0]["address_candidates"][0]["building_geometry_sha256"]) == 64
            assert (
                connection.execute(
                    "SELECT count(*),md5(string_agg(row_to_json(o)::text,'' ORDER BY o.id))"
                    " FROM public.franchise_outlets o"
                ).fetchone()
                == before
            )
            connection.rollback()
        finally:
            connection.rollback()
            connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
            )
