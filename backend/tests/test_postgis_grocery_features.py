import math
import os
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from app.cli.import_buildings import SCHEMA_PATH
from app.cli.import_profile import PROFILE_SCHEMA_PATH
from app.core.config import Settings
from app.db.grocery_features import extract_raw_features

pytestmark = pytest.mark.skipif(
    os.environ.get("MASTERFRANCHISES_TEST_POSTGIS") != "1",
    reason="Set MASTERFRANCHISES_TEST_POSTGIS=1 for isolated PostgreSQL integration checks",
)


@pytest.fixture
def feature_database():
    settings = Settings()
    schema = "grocery_feature_test_" + uuid4().hex
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
        connection.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
        connection.execute(PROFILE_SCHEMA_PATH.read_text(encoding="utf-8"))
        connection.execute("""
            INSERT INTO map_sources VALUES('warsaw-buildings','Buildings',
                'https://example.test','Test',NULL,now(),8,ARRAY[20.6,51.9,21.6,52.6]);
            INSERT INTO buildings(source_id,dataset_id,geometry,properties,
                building_type,imported_at)
            SELECT source_id,'warsaw-buildings',ST_Multi(ST_Transform(ST_MakeEnvelope(
                638000+x-10,486000-10,638000+x+10,486000+10,2180),3857)),
                '{"KONDYGNACJE_NADZIEMNE":0}'::jsonb,building_type,now()
            FROM (VALUES('selected',0,'h'),('residential',100,'m'),('retail',200,'h'),
                ('unclassified',-200,'unknown'),('inside-500',499.9,'m'),
                ('outside-500',550,'m'),('observed-zero',5000,'h'),
                ('absent',10000,'h')) AS fixture(source_id,x,building_type);
            INSERT INTO profile_sources(id,kind,metadata,feature_count) VALUES
                ('warsaw-population-2021','population',
                    '{"coverage_bounds":[20.6,51.9,21.6,52.6]}',3),
                ('warsaw-osm-pois','pois','{"coverage_bounds":[20.6,51.9,21.6,52.6]}',7),
                ('warsaw-osm-activity','activity',
                    '{"coverage_bounds":[20.6,51.9,21.6,52.6]}',9);
        """)
        x, y = connection.execute(
            "SELECT ST_X(origin),ST_Y(origin) FROM (SELECT"
            " ST_PointOnSurface(ST_Transform(geometry,2180)) AS origin FROM buildings"
            " WHERE source_id='selected') s"
        ).fetchone()
        for identifier, west, east, population, status in (
            ("published", x - 2000, x, 800, "observed"),
            ("missing", x, x + 2000, None, "missing"),
            ("zero", x + 3000, x + 7000, 0, "observed"),
        ):
            connection.execute(
                "INSERT INTO profile_population VALUES(%s,"
                "ST_Multi(ST_MakeEnvelope(%s,%s,%s,%s,2180)),%s,%s,2021,125)",
                (identifier, west, y - 2000, east, y + 2000, population, status),
            )
        for identifier, category, distance, duplicate in (
            ("bus", "bus_stop", 100, None),
            ("bus-copy", "bus_stop", 50, "bus"),
            ("bus-boundary", "bus_stop", 500, None),
            ("bus-outside", "bus_stop", 500.1, None),
            ("metro-boundary", "metro_entrance", 1000, None),
            ("rail-outside", "rail_station", 1000.1, None),
            ("parking", "parking", 499, None),
        ):
            connection.execute(
                """
                INSERT INTO profile_pois(source_id,name,normalized_name,kind,categories,geometry,
                    source_url,location_method,tags,duplicate_of)
                VALUES(%s,%s,%s,'access',ARRAY[%s],ST_SetSRID(ST_Point(%s,%s),2180),
                    'https://example.test','node','{}',%s)
            """,
                (identifier, identifier, identifier, category, x + distance, y, duplicate),
            )
        for identifier, category, distance, duplicate, tags in (
            ("onsite", "school", 0, None, {}),
            ("school", "school", 100, None, {}),
            ("school-copy", "school", 80, "school", {}),
            ("inactive-office", "office", 200, None, {"disused": "yes"}),
            ("inactive-shop", "retail_destination", 220, None, {"disused:shop": "mall"}),
            ("university", "university", 250, None, {"end_date": "2019"}),
            ("office", "office", 300, None, {"office": "yes"}),
            ("outside", "school", 501, None, {}),
        ):
            connection.execute(
                """
                INSERT INTO profile_activity(source_id,name,normalized_name,kind,categories,
                    geometry,source_url,location_method,tags,duplicate_of)
                VALUES(%s,%s,%s,'activity',ARRAY[%s],ST_SetSRID(ST_Point(%s,%s),2180),
                    'https://example.test','node',%s,%s)
            """,
                (
                    identifier,
                    identifier,
                    identifier,
                    category,
                    x + distance,
                    y,
                    Jsonb(tags),
                    duplicate,
                ),
            )
        connection.execute("""
            INSERT INTO profile_activity(source_id,name,normalized_name,kind,categories,geometry,
                source_url,location_method,tags)
            SELECT 'boundary-onsite','Boundary','boundary','activity','{school}',
                ST_PointN(ST_ExteriorRing(ST_GeometryN(ST_Transform(geometry,2180),1)),1),
                'https://example.test','node','{}' FROM buildings WHERE source_id='selected'
        """)
        connection.commit()
        yield connection
    finally:
        connection.rollback()
        connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        connection.commit()
        connection.close()


def begin_read_only(connection):
    connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")


def test_population_subtotal_missing_observed_zero_and_stable_batch_order(feature_database):
    c = feature_database
    with pytest.raises(ValueError, match="read-only repeatable-read"):
        extract_raw_features(c, ["selected"])
    c.rollback()
    begin_read_only(c)
    rows = extract_raw_features(c, ["selected", "observed-zero", "absent", "selected"])
    assert [row["building_source_id"] for row in rows] == ["absent", "observed-zero", "selected"]
    selected = rows[2]
    assert selected == extract_raw_features(c, ["selected"])[0]
    expected = math.pi * 300**2 / 2 * 800 / (2000 * 4000)
    assert selected["resident_subtotal_300m"] == pytest.approx(expected, rel=0.0002)
    assert selected["resident_subtotal_300m"] != round(selected["resident_subtotal_300m"])
    assert selected["published_cell_area_fraction_300m"] == pytest.approx(0.5)
    assert selected["population_cells_300m"] == {
        "observed": 1,
        "missing": 1,
        "suppressed": 0,
        "total": 2,
    }
    assert rows[0]["resident_subtotal_300m"] is None
    assert rows[0]["published_cell_area_fraction_300m"] == 0
    assert rows[1]["resident_subtotal_300m"] == 0
    assert rows[1]["published_cell_area_fraction_300m"] == pytest.approx(1)
    assert selected["mapped_storeys_raw"] == 0
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        c.execute("DELETE FROM buildings")


def test_access_activity_dedup_focal_boundary_and_residential_denominator(feature_database):
    c = feature_database
    begin_read_only(c)
    row = extract_raw_features(c, ["selected"])[0]
    assert row["nearest_bus_stop_distance_m"] == pytest.approx(100)
    assert row["bus_stop_count_500m"] == 2
    assert row["parking_count_500m"] == 1
    assert row["nearest_metro_entrance_distance_m"] == pytest.approx(1000)
    assert row["nearest_rail_station_distance_m"] is None
    assert row["nearest_tram_stop_distance_m"] is None
    assert row["school_count_500m"] == 1
    assert row["university_count_500m"] == 1
    assert row["office_count_500m"] == 1
    assert row["retail_destination_count_500m"] == 0
    assert row["activity_excluded_onsite_count_500m"] == 2
    assert row["activity_excluded_inactive_count_500m"] == 2
    assert row["activity_lifecycle_ambiguous_count_500m"] == 1
    assert row["neighbor_building_count_500m"] == 4
    assert row["known_use_building_count_500m"] == 3
    assert row["residential_building_count_500m"] == 2
    assert row["residential_building_fraction_500m"] == pytest.approx(2 / 3)
    assert row["mapped_building_use_coverage_fraction_500m"] == pytest.approx(0.75)


def test_source_bounds_partial_outside_unknown_and_missing_do_not_create_full_coverage(
    feature_database,
):
    c = feature_database
    longitude = c.execute(
        "SELECT ST_X(ST_Transform(ST_PointOnSurface(ST_Transform(geometry,2180)),"
        "4326)) FROM buildings WHERE source_id='selected'"
    ).fetchone()[0]
    c.execute(
        "UPDATE profile_sources SET metadata=%s WHERE kind='pois'",
        (Jsonb({"coverage_bounds": [longitude, 51.9, 21.6, 52.6]}),),
    )
    c.execute(
        "UPDATE profile_sources SET metadata=%s WHERE kind='activity'",
        (Jsonb({"coverage_bounds": [20.6, 51.9, 20.7, 52.0]}),),
    )
    c.commit()
    begin_read_only(c)
    row = extract_raw_features(c, ["selected"])[0]
    coverage = row["source_coverage_fraction_by_radius"]
    assert coverage["warsaw-osm-pois"]["1000"] == pytest.approx(0.5, abs=0.001)
    assert coverage["warsaw-osm-activity"]["500"] == 0
    assert row["source_origin_inside_bounds_by_dataset"]["warsaw-osm-activity"] is False
    assert row["source_building_within_bounds_by_dataset"]["warsaw-osm-activity"] is False
    assert row["nearest_tram_stop_distance_m"] is None
    c.rollback()
    c.execute("UPDATE profile_sources SET metadata='{}' WHERE kind='pois'")
    c.execute("DELETE FROM profile_sources WHERE kind='activity'")
    c.execute("DELETE FROM profile_sources WHERE kind='population'")
    c.commit()
    begin_read_only(c)
    row = extract_raw_features(c, ["selected"])[0]
    assert row["source_coverage_fraction_by_radius"]["warsaw-osm-pois"]["500"] is None
    assert row["source_available_by_dataset"]["warsaw-osm-activity"] is False
    assert row["school_count_500m"] is None
    assert row["activity_excluded_inactive_count_500m"] is None
    assert row["resident_subtotal_300m"] is None
    assert row["published_cell_area_fraction_300m"] is None
    assert all(value is None for value in row["population_cells_300m"].values())
