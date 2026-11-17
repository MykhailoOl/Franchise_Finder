import os
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest
from psycopg import sql
from test_grocery_matching_v2 import evidence
from test_postgis_grocery_matching_v2 import connect

from app.cli import refresh_grocery_matches_v2 as refresh


def test_review_rejects_modified_decision_even_with_updated_report_hash(monkeypatch):
    monkeypatch.setattr(refresh, "collect", lambda connection: [evidence()])
    report = refresh.make_report(None)
    refresh.validate_report(report)
    changed = deepcopy(report)
    changed["decisions"][0]["building_source_id"] = "unreviewed-building"
    changed["decisions_sha256"] = refresh.stable_hash(changed["decisions"])
    changed["report_sha256"] = refresh.stable_hash(
        {key: value for key, value in changed.items() if key != "report_sha256"}
    )
    with pytest.raises(ValueError, match="cannot be reproduced"):
        refresh.validate_report(changed)


def test_review_rejects_modified_policy(monkeypatch):
    monkeypatch.setattr(refresh, "collect", lambda connection: [evidence()])
    report = deepcopy(refresh.make_report(None))
    report["policy"]["maximum_distance_m"] = 100
    with pytest.raises(ValueError, match="changed"):
        refresh.validate_report(report)


@pytest.fixture
def database():
    if os.environ.get("MASTERFRANCHISES_TEST_POSTGIS") != "1":
        pytest.skip("Set MASTERFRANCHISES_TEST_POSTGIS=1 for isolated PostGIS checks")
    schema = "grocery_refresh_test_" + uuid4().hex
    with connect() as connection:
        try:
            connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            connection.execute(sql.SQL("SET search_path={},public").format(sql.Identifier(schema)))
            connection.execute("SET TIME ZONE 'UTC'")
            for name in (
                "map_schema.sql",
                "address_schema.sql",
                "franchise_schema.sql",
                "profile_schema.sql",
            ):
                query = (Path(__file__).parents[1] / "app/db" / name).read_text(encoding="utf-8")
                connection.execute(query.replace("CREATE EXTENSION IF NOT EXISTS pg_trgm;", ""))
            connection.execute("""
                INSERT INTO map_sources VALUES
                    ('warsaw-buildings','Test','https://example.test','Test',NULL,now(),1,'{}'),
                    ('warsaw-addresses','Test','https://example.test','Test',NULL,now(),1,'{}');
                INSERT INTO buildings(source_id,dataset_id,geometry,properties,imported_at)
                VALUES('one','warsaw-buildings',ST_Multi(ST_Transform(
                    ST_MakeEnvelope(638000,486000,638010,486010,2180),3857)),'{}',now());
                INSERT INTO addresses(source_id,dataset_id,street,street_normalized,
                    house_number,house_number_normalized,city,address,geometry,properties,imported_at)
                VALUES('address','warsaw-addresses','Romualda Traugutta','romualda traugutta',
                    '11','11','Warszawa','Test',ST_Transform(ST_SetSRID(
                        ST_Point(638001,486001),2180),3857),'{}',now());
                SELECT refresh_address_building_links();
                INSERT INTO franchise_categories VALUES('grocery','Grocery'),('pizza','Pizza');
                INSERT INTO franchise_brands VALUES
                    ('zabka','Test','grocery','https://example.test','https://example.test',
                        now(),'complete','Test','{}'),
                    ('pizza','Other','pizza','https://example.test','https://example.test',
                        now(),'complete','Test','{}');
                INSERT INTO franchise_outlets(brand_id,source_id,address_raw,street,
                    street_normalized,house_number,house_number_normalized,city,geometry,
                    source_url,retrieved_at,status,operating_model,attributes)
                VALUES('zabka','new','Traugutta 11','Traugutta','traugutta','11','11','Warszawa',
                    ST_Transform(ST_SetSRID(ST_Point(637998,486005),2180),3857),
                    'https://example.test',now(),'listed','unknown','{}'),
                    ('pizza','other','Test','Test','test','1','1','Warszawa',
                    ST_Transform(ST_SetSRID(ST_Point(638002,486002),2180),3857),
                    'https://example.test',now(),'listed','unknown','{}');
                SELECT refresh_franchise_links();
            """)
            connection.commit()
            yield connection
        finally:
            connection.rollback()
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
            connection.commit()


def test_apply_preserves_other_brands_records_evidence_and_invalidates_cache(database):
    other = database.execute(
        "SELECT row_to_json(o) FROM franchise_outlets o WHERE brand_id='pizza'"
    ).fetchone()[0]
    revision = database.execute("SELECT revision FROM profile_state WHERE id=1").fetchone()[0]
    report = refresh.make_report(database)
    assert report["summary"]["new_associations"] == 1
    result = refresh.apply_report(database, report)
    assert result["training_performed"] is False
    assert database.execute(
        "SELECT match_status,building_source_id FROM franchise_outlets WHERE brand_id='zabka'"
    ).fetchone() == ("matched", "one")
    assert (
        database.execute(
            "SELECT row_to_json(o) FROM franchise_outlets o WHERE brand_id='pizza'"
        ).fetchone()[0]
        == other
    )
    history = database.execute(
        "SELECT before_row,evidence,decision FROM grocery_match_history"
    ).fetchone()
    assert history[0]["match_status"] == "unmatched"
    assert history[1] == report["evidence"][0]
    assert history[2] == report["decisions"][0]
    assert (
        database.execute("SELECT revision FROM profile_state WHERE id=1").fetchone()[0] > revision
    )
    assert database.execute(
        "SELECT outlet_count,brand_ids FROM building_franchise_summary"
        " WHERE building_source_id='one'"
    ).fetchone() == (2, ["pizza", "zabka"])


def test_source_change_after_audit_refuses_application(database):
    report = refresh.make_report(database)
    database.execute("UPDATE franchise_outlets SET house_number='12' WHERE brand_id='zabka'")
    with pytest.raises(ValueError, match="changed after review"):
        refresh.apply_report(database, report)
    assert database.execute(
        "SELECT match_status,building_source_id FROM franchise_outlets WHERE brand_id='zabka'"
    ).fetchone() == ("unmatched", None)
    assert database.execute(
        "SELECT to_regclass(format('%I.grocery_match_history',current_schema())) IS NULL"
    ).fetchone()[0]
