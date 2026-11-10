import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

import app.db.grocery_assessments as assessments
from app.cli.import_buildings import SCHEMA_PATH
from app.cli.import_profile import PROFILE_SCHEMA_PATH
from app.core.config import Settings
from app.db.buildings import MapDataUnavailableError

pytestmark = pytest.mark.skipif(
    os.environ.get("MASTERFRANCHISES_TEST_POSTGIS") != "1",
    reason="Set MASTERFRANCHISES_TEST_POSTGIS=1 for isolated PostgreSQL integration checks",
)


@pytest.fixture
def database():
    url = Settings().database_url
    admin = create_engine(url)
    schema = "grocery_assessment_test_" + uuid4().hex
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema},public"})
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(SCHEMA_PATH.read_text(encoding="utf-8"))
            connection.exec_driver_sql(PROFILE_SCHEMA_PATH.read_text(encoding="utf-8"))
            connection.exec_driver_sql("""
                INSERT INTO map_sources VALUES('warsaw-buildings','Buildings',
                    'https://example.test/buildings','Municipality',NULL,now(),2,
                    ARRAY[20.6,51.9,21.6,52.6]);
                INSERT INTO buildings(source_id,dataset_id,geometry,properties,
                    building_type,imported_at)
                SELECT source_id,'warsaw-buildings',ST_Multi(ST_Transform(ST_MakeEnvelope(
                    637990+x,485990,638010+x,486010,2180),3857)),
                    '{"KONDYGNACJE_NADZIEMNE":3}','h',now()
                FROM (VALUES('selected',0),('unaddressed',2000)) t(source_id,x);
                INSERT INTO profile_sources(id,kind,metadata,feature_count) VALUES
                    ('warsaw-population-2021','population','{}',2),
                    ('warsaw-osm-pois','pois','{}',2),
                    ('warsaw-osm-activity','activity','{}',5);
                UPDATE profile_sources SET metadata=jsonb_build_object(
                    'url','https://example.test/source','name',id,
                    'observed_at',CASE WHEN kind='population' THEN '2021-03-31' END,
                    'retrieved_at','2026-11-01T18:00:00Z',
                    'snapshot_at',CASE WHEN kind<>'population' THEN '2026-11-01T17:00:00Z' END,
                    'coverage_bounds',jsonb_build_array(20.6,51.9,21.6,52.6));
                INSERT INTO profile_population VALUES
                    ('observed',ST_Multi(ST_MakeEnvelope(636000,484000,638000,488000,2180)),
                        800,'observed',2021,125),
                    ('missing',ST_Multi(ST_MakeEnvelope(638000,484000,640000,488000,2180)),
                        NULL,'missing',2021,125);
                INSERT INTO profile_pois(source_id,name,normalized_name,kind,categories,
                    geometry,source_url,location_method,tags)
                SELECT source_id,source_id,source_id,'access',ARRAY[category],
                    ST_SetSRID(ST_Point(638000+x,486000),2180),
                    'https://example.test','node','{}'
                FROM (VALUES('bus','bus_stop',100),('far-tram','tram_stop',1500))
                    t(source_id,category,x);
                INSERT INTO profile_activity(source_id,name,normalized_name,kind,categories,
                    geometry,source_url,location_method,tags,duplicate_of)
                SELECT source_id,source_id,source_id,'activity',ARRAY[category],
                    ST_SetSRID(ST_Point(638000+x,486000),2180),
                    'https://example.test','node',tags::jsonb,duplicate_of
                FROM (VALUES('onsite','school',0,'{}',NULL),
                    ('school','school',100,'{}',NULL),
                    ('duplicate','school',120,'{}','school'),
                    ('inactive','office',200,'{"disused":"yes"}',NULL),
                    ('ambiguous','office',250,'{"end_date":"2019"}',NULL))
                    t(source_id,category,x,tags,duplicate_of);
            """)
            ids = dict(connection.execute(text("SELECT source_id,id FROM buildings")).all())
        assessments.clear_assessment_cache()
        yield engine, ids
    finally:
        assessments.clear_assessment_cache()
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def facts(result):
    return {row["id"]: row for row in result["evidence"]}


def test_live_features_all_buildings_readonly_dedup_and_missing_semantics(database, monkeypatch):
    engine, ids = database
    original = assessments.extract_raw_features
    modes = []

    def inspect(connection, source_ids):
        modes.append(
            connection.execute(
                "SELECT current_setting('transaction_read_only'),"
                "current_setting('transaction_isolation')"
            ).fetchone()
        )
        return original(connection, source_ids)

    monkeypatch.setattr(assessments, "extract_raw_features", inspect)
    result = assessments.get_assessment(engine, ids["selected"])
    evidence = facts(result)
    assert modes == [("on", "repeatable read")]
    assert result["building_source_id"] == "selected"
    assert result["ranking"]["score"] is None
    assert len(result["evidence"]) == 17
    assert evidence["school_count_500m"]["value"] == 1
    assert evidence["office_count_500m"]["value"] == 1
    assert evidence["university_count_500m"]["value"] == 0
    assert evidence["nearest_tram_stop_distance_m"]["value"] is None
    assert evidence["nearest_tram_stop_distance_m"]["status"] == "censored"
    assert evidence["resident_subtotal_500m"]["status"] == "partial"
    assert evidence["resident_subtotal_500m"]["coverage_fraction"] == pytest.approx(0.5)
    assert result["quality"]["activity_lifecycle_ambiguous_count_500m"] == 1
    unaddressed = assessments.get_assessment(engine, ids["unaddressed"])
    assert unaddressed["building_source_id"] == "unaddressed"
    assert facts(unaddressed)["resident_subtotal_300m"]["value"] is None
    assert assessments.get_assessment(engine, 99999999) is None


def test_cache_is_copy_safe_invalidates_revision_and_rebinds_numeric_identity(
    database, monkeypatch
):
    engine, ids = database
    original = assessments.extract_raw_features
    calls = []

    def inspect(connection, source_ids):
        calls.append(source_ids)
        return original(connection, source_ids)

    monkeypatch.setattr(assessments, "extract_raw_features", inspect)
    first = assessments.get_assessment(engine, ids["selected"])
    first["ranking"]["score"] = 99
    first["_runtime"]["source_context"]["sources"].clear()
    cached = assessments.get_assessment(engine, ids["selected"])
    assert cached["ranking"]["score"] is None
    assert cached["_runtime"]["source_context"]["sources"]
    assert len(calls) == 1
    with engine.begin() as connection:
        connection.execute(text("UPDATE profile_state SET revision=revision+1 WHERE id=1"))
    refreshed = assessments.get_assessment(engine, ids["selected"])
    assert len(calls) == 2
    assert refreshed["versions"]["source_revision"] != cached["versions"]["source_revision"]
    with engine.begin() as connection:
        new_id = connection.execute(
            text("UPDATE buildings SET id=DEFAULT WHERE source_id='selected' RETURNING id")
        ).scalar_one()
    rebound = assessments.get_assessment(engine, new_id)
    assert rebound["building_id"] == new_id
    assert rebound["building_source_id"] == "selected"
    assert len(calls) == 3


def test_missing_optional_tables_use_shared_query_and_preserve_other_sources(database):
    engine, ids = database
    before = assessments.get_assessment(engine, ids["selected"])
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE profile_activity"))
    missing_activity = assessments.get_assessment(engine, ids["selected"])
    evidence = facts(missing_activity)
    assert evidence["school_count_500m"]["value"] is None
    assert evidence["school_count_500m"]["status"] == "unavailable"
    assert evidence["resident_subtotal_500m"] == facts(before)["resident_subtotal_500m"]
    assert evidence["nearest_bus_stop_distance_m"] == facts(before)["nearest_bus_stop_distance_m"]
    assert missing_activity["quality"]["activity_excluded_onsite_count_500m"] is None
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE profile_population,profile_pois,profile_sources"))
    missing_all = assessments.get_assessment(engine, ids["selected"])
    evidence = facts(missing_all)
    assert evidence["building_footprint_area_m2"]["value"] == pytest.approx(400)
    assert evidence["mapped_storeys"]["value"] == 3
    assert evidence["resident_subtotal_500m"]["value"] is None
    assert evidence["nearest_bus_stop_distance_m"]["value"] is None
    assert evidence["bus_stop_count_500m"]["value"] is None
    assert evidence["bus_stop_count_500m"]["status"] == "unavailable"
    assert all(isinstance(source["name"], str) for source in missing_all["sources"])


def test_invalid_metadata_with_complete_schema_marks_source_and_values_unavailable(database):
    engine, ids = database
    with engine.begin() as connection:
        connection.execute(
            text(
                'UPDATE profile_sources SET metadata=metadata||\'{"snapshot_at":"invalid"}\''
                " WHERE id='warsaw-osm-activity'"
            )
        )
    result = assessments.get_assessment(engine, ids["selected"])
    assert facts(result)["school_count_500m"]["status"] == "unavailable"
    assert facts(result)["school_count_500m"]["value"] is None
    assert result["quality"]["source_available_by_dataset"]["warsaw-osm-activity"] is False
    assert result["quality"]["activity_excluded_onsite_count_500m"] is None
    assert facts(result)["nearest_bus_stop_distance_m"]["value"] == pytest.approx(100)


def test_metadata_change_and_bounded_ttl_cache(database, monkeypatch):
    engine, ids = database
    monkeypatch.setattr(assessments, "CACHE_MAX_ENTRIES", 1)
    first = assessments.get_assessment(engine, ids["selected"])
    with engine.begin() as connection:
        connection.execute(
            text('UPDATE profile_sources SET metadata=metadata||\'{"name":"Refreshed"}\'')
        )
    changed = assessments.get_assessment(engine, ids["selected"])
    assert first["versions"]["source_revision"] != changed["versions"]["source_revision"]
    assessments.get_assessment(engine, ids["unaddressed"])
    assert len(assessments._CACHE) == 1
    original = assessments.extract_raw_features
    calls = []

    def inspect(connection, source_ids):
        calls.append(source_ids)
        return original(connection, source_ids)

    monkeypatch.setattr(assessments, "extract_raw_features", inspect)
    assessments.get_assessment(engine, ids["selected"])
    monkeypatch.setattr(assessments, "CACHE_TTL_SECONDS", 0)
    assessments.get_assessment(engine, ids["selected"])
    assert len(calls) == 2
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE buildings"))
    with pytest.raises(MapDataUnavailableError):
        assessments.get_assessment(engine, ids["selected"])
