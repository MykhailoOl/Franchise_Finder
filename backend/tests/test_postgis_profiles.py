import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from app.cli.import_addresses import ADDRESS_SCHEMA_PATH
from app.cli.import_buildings import SCHEMA_PATH
from app.cli.import_franchises import FRANCHISE_SCHEMA_PATH
from app.cli.import_profile import PROFILE_SCHEMA_PATH
from app.core.config import Settings
from app.db.profiles import cache_profile, get_profile

pytestmark = pytest.mark.skipif(
    os.environ.get("MASTERFRANCHISES_TEST_POSTGIS") != "1",
    reason="Set MASTERFRANCHISES_TEST_POSTGIS=1 for isolated PostgreSQL integration checks",
)


def test_population_boundary_dedup_missing_sources_and_cache() -> None:
    url = Settings().database_url
    admin = create_engine(url)
    schema = "profile_test_" + uuid4().hex
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema},public"})
    try:
        with engine.begin() as c:
            for path in (
                SCHEMA_PATH,
                ADDRESS_SCHEMA_PATH,
                FRANCHISE_SCHEMA_PATH,
                PROFILE_SCHEMA_PATH,
            ):
                c.exec_driver_sql(path.read_text(encoding="utf-8"))
            c.execute(
                text("""
                INSERT INTO map_sources VALUES
                ('warsaw-buildings','Test','https://example.test','Test',NULL,now(),1,'{}');
                INSERT INTO buildings(source_id,dataset_id,geometry,properties,imported_at)
                VALUES('selected','warsaw-buildings',ST_Multi(ST_Transform(
                    ST_MakeEnvelope(637995,485995,638005,486005,2180),3857)),'{}',now());
                INSERT INTO profile_sources(id,kind,metadata,feature_count) VALUES
                ('population','population','{"id":"population","name":"Test","notes":[]}',2),
                ('pois','pois','{"id":"pois","name":"Test","notes":[]}',5);
                INSERT INTO profile_population VALUES
                ('observed',ST_Multi(ST_MakeEnvelope(637000,485000,638000,487000,2180)),
                    200,'observed',2021,125),
                ('missing',ST_Multi(ST_MakeEnvelope(638000,485000,639000,487000,2180)),
                    NULL,'missing',2021,125);
                INSERT INTO franchise_categories VALUES('grocery','Grocery');
                INSERT INTO franchise_brands VALUES('test-brand','Test brand','grocery',
                    'https://example.test','https://example.test',now(),'complete','Test','{}');
            """)
            )
            building_id = c.execute(text("SELECT id FROM buildings")).scalar_one()
            for source_id, offset, onsite in (("near", 100, False), ("onsite", 0, True)):
                c.execute(
                    text("""
                    INSERT INTO franchise_outlets(brand_id,source_id,address_raw,city,
                        display_geometry,building_source_id,source_url,retrieved_at,
                        status,operating_model,attributes)
                    VALUES('test-brand',:source_id,'Test address','Warszawa',ST_Transform(
                        ST_SetSRID(ST_Point(638000+:offset,486000),2180),3857),
                        :building_source_id,'https://example.test',now(),'listed','unknown','{}')
                """),
                    {
                        "source_id": source_id,
                        "offset": offset,
                        "building_source_id": "selected" if onsite else None,
                    },
                )
            for source_id, name, offset, kind, category, brand, duplicate in (
                ("official-copy", "Test brand", 105, "competitor", "grocery", "test-brand", None),
                ("boundary", "Other store", 300, "competitor", "grocery", None, None),
                ("outside", "Outside", 301, "competitor", "grocery", None, None),
                ("osm-copy", "Other store", 299, "competitor", "grocery", None, "boundary"),
                ("bus", "Bus stop", 300, "access", "bus_stop", None, None),
                ("tram", "Tram stop", 301, "access", "tram_stop", None, None),
            ):
                c.execute(
                    text("""
                    INSERT INTO profile_pois(source_id,name,normalized_name,kind,categories,
                        brand_id,geometry,source_url,location_method,tags,duplicate_of)
                    VALUES(:source_id,:name,:name,:kind,ARRAY[:category],:brand,
                        ST_SetSRID(ST_Point(638000+:offset,486000),2180),
                        'https://example.test','node','{}',:duplicate)
                """),
                    {
                        "source_id": source_id,
                        "name": name,
                        "offset": offset,
                        "kind": kind,
                        "category": category,
                        "brand": brand,
                        "duplicate": duplicate,
                    },
                )
        first = get_profile(engine, building_id, 300)
        assert first["population"]["estimated_residents"] == 14
        assert first["population"]["coverage_fraction"] == pytest.approx(0.5, abs=0.0001)
        grocery = first["competition"]["categories"][0]
        assert (grocery["count"], grocery["official_count"], grocery["osm_count"]) == (2, 1, 1)
        assert first["competition"]["onsite_outlet_count"] == 1
        assert first["competition"]["deduplicated_count"] == 2
        bus, tram = first["access"]["items"][:2]
        assert bus["count_within_radius"] == 1
        assert bus["nearest"]["distance_m"] == pytest.approx(300, abs=0.01)
        assert tram["count_within_radius"] == 0
        assert tram["nearest"] is None
        assert get_profile(engine, building_id, 300) == first
        assert get_profile(engine, building_id, 500)["population"]["estimated_residents"] == 39
        assert get_profile(engine, 999999999, 300) is None
        with engine.begin() as c:
            c.execute(text("SELECT invalidate_building_profiles()"))
        with ThreadPoolExecutor(max_workers=4) as pool:
            concurrent = list(pool.map(lambda _: get_profile(engine, building_id, 300), range(4)))
        assert all(result["competition"]["categories"][0]["count"] == 2 for result in concurrent)
        old = concurrent[0]
        with engine.begin() as c:
            c.execute(text("SELECT invalidate_building_profiles()"))
        cache_profile(engine, "selected", old)
        with engine.begin() as c:
            assert c.execute(text("SELECT count(*) FROM building_profile_cache")).scalar_one() == 0
            c.execute(text("DELETE FROM profile_sources"))
        missing = get_profile(engine, building_id, 300)
        assert missing["population"]["status"] == "unavailable"
        assert missing["population"]["estimated_residents"] is None
        assert missing["access"]["items"][0]["count_within_radius"] is None
        assert missing["competition"]["status"] == "partial"
        assert missing["competition"]["categories"][0]["osm_count"] is None
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
