import hashlib
import json
import os
from copy import deepcopy
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

import app.cli.import_population_context_v2 as importer
from app.cli.import_population_context_v2 import load_snapshot
from app.core.config import Settings
from app.db.population_context_v2 import SCHEMA, SOURCE_ID, VERSION, population_context


def publish(directory, snapshot):
    payload = json.dumps(snapshot).encode()
    (directory / "population-context.json").write_bytes(payload)
    (directory / "manifest.json").write_text(
        json.dumps(
            {
                "version": VERSION,
                "source": snapshot["source"],
                "cells": len(snapshot["cells"]),
                "artifacts": {"population-context.json": hashlib.sha256(payload).hexdigest()},
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture
def snapshot():
    return {
        "version": VERSION,
        "source": {
            "id": SOURCE_ID,
            "city": "warsaw",
            "resolution_m": 500,
            "population_definition": "national_definition",
            "coordinate_reference_system": "EPSG:3035",
            "observed_at": "2021-03-31",
            "retrieved_at": "2026-11-17T10:00:00Z",
            "url": "https://geo.stat.gov.pl/atom-web/download/?fileId=fixture",
            "absence_means_zero": False,
            "grid_support_bounds_epsg3035": [5100000, 3200000, 5101500, 3200500],
        },
        "cells": [
            {
                "cell_id": "PL_CRS3035RES500mN3200000E5100000",
                "easting": 5100000,
                "northing": 3200000,
                "population": 0,
                "status": "observed",
            },
            {
                "cell_id": "PL_CRS3035RES500mN3200000E5100500",
                "easting": 5100500,
                "northing": 3200000,
                "population": None,
                "status": "missing",
            },
        ],
    }


def test_explicit_population_zero_and_missing_remain_distinct(tmp_path, snapshot):
    publish(tmp_path, snapshot)
    loaded, digest = load_snapshot(tmp_path)
    assert loaded == snapshot
    assert loaded["cells"][0]["population"] == 0
    assert loaded["cells"][1]["population"] is None
    assert len(digest) == 64


def test_population_context_detects_content_changes(tmp_path, snapshot):
    publish(tmp_path, snapshot)
    with (tmp_path / "population-context.json").open("ab") as stream:
        stream.write(b" ")
    with pytest.raises(ValueError, match="changed"):
        load_snapshot(tmp_path)


@pytest.mark.parametrize(
    "damage",
    [
        "duplicate",
        "negative",
        "boolean",
        "unknown_zero",
        "alignment",
        "identity",
        "definition",
        "naive_date",
        "outside_bounds",
    ],
)
def test_self_consistent_invalid_population_contract_is_rejected(tmp_path, snapshot, damage):
    if damage == "duplicate":
        snapshot["cells"].append(deepcopy(snapshot["cells"][0]))
    elif damage == "negative":
        snapshot["cells"][0]["population"] = -1
    elif damage == "boolean":
        snapshot["cells"][0]["population"] = True
    elif damage == "unknown_zero":
        snapshot["cells"][1]["population"] = 0
    elif damage == "alignment":
        snapshot["cells"][0]["easting"] += 1
    elif damage == "identity":
        snapshot["cells"][0]["cell_id"] = "PL_CRS3035RES500mN3200500E5100000"
    elif damage == "definition":
        snapshot["source"]["population_definition"] = "usual_residents"
    elif damage == "naive_date":
        snapshot["source"]["retrieved_at"] = "2026-11-17T10:00:00"
    else:
        snapshot["source"]["grid_support_bounds_epsg3035"][0] += 500
    publish(tmp_path, snapshot)
    with pytest.raises(ValueError):
        load_snapshot(tmp_path)


@pytest.mark.skipif(
    os.environ.get("MASTERFRANCHISES_TEST_POSTGIS") != "1", reason="Requires PostGIS"
)
def test_lookup_uses_exact_containing_square_preserves_unknown_and_ignores_radius(snapshot):
    admin = create_engine(Settings().database_url)
    schema = "population_context_test_" + uuid4().hex
    with admin.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    engine = create_engine(
        Settings().database_url, connect_args={"options": f"-csearch_path={schema},public"}
    )
    try:
        with engine.begin() as connection:
            assert population_context(connection, {"x": 638000, "y": 486000}) is None
            connection.exec_driver_sql(SCHEMA)
            connection.execute(
                text(
                    "INSERT INTO population_context_sources(id,metadata) "
                    "VALUES(:id,CAST(:data AS jsonb))"
                ),
                {"id": SOURCE_ID, "data": json.dumps(snapshot["source"])},
            )
            for cell in snapshot["cells"]:
                connection.execute(
                    text(
                        "INSERT INTO population_context_cells "
                        "VALUES(:source_id,:cell_id,:easting,:northing,:population,:status)"
                    ),
                    {"source_id": SOURCE_ID, **cell},
                )

            def point(x):
                return (
                    connection.execute(
                        text(
                            "SELECT ST_X(p) x,ST_Y(p) y FROM (SELECT ST_Transform("
                            "ST_SetSRID(ST_MakePoint(:x,3200250),3035),2180) p) s"
                        ),
                        {"x": x},
                    )
                    .mappings()
                    .one()
                )

            zero = population_context(connection, point(5100250), 300)
            assert zero["status"] == "available" and zero["population"] == 0
            assert zero["scope"] == "containing_census_cell"
            assert zero == population_context(connection, point(5100250), 1000)
            unknown = population_context(connection, point(5100750), 500)
            assert unknown["status"] == "unavailable" and unknown["population"] is None
            absent = population_context(connection, point(5101250), 500)
            assert absent["population"] is None and absent["cell_id"] is None
            assert (
                connection.execute(
                    text("SELECT count(*) FROM pg_tables WHERE schemaname=current_schema()")
                ).scalar_one()
                == 2
            )
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        admin.dispose()


@pytest.mark.skipif(
    os.environ.get("MASTERFRANCHISES_TEST_POSTGIS") != "1", reason="Requires PostGIS"
)
def test_import_is_separate_from_fine_population_and_invalidates_cached_profiles(
    tmp_path, snapshot, monkeypatch
):
    settings = Settings()
    admin = create_engine(settings.database_url)
    schema = "population_import_test_" + uuid4().hex
    with admin.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    engine = create_engine(
        settings.database_url, connect_args={"options": f"-csearch_path={schema},public"}
    )
    connect = importer.psycopg.connect

    def isolated_connect(**kwargs):
        return connect(**kwargs, options=f"-csearch_path={schema},public")

    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("""
                CREATE TABLE profile_sources(id text,kind text);
                CREATE TABLE profile_population(source_id text,population float8);
                CREATE TABLE profile_state(revision integer);
                CREATE TABLE building_profile_cache(id integer);
                INSERT INTO profile_sources VALUES('fine-grid','population');
                INSERT INTO profile_population VALUES('unknown-cell',NULL);
                INSERT INTO profile_state VALUES(1);
                INSERT INTO building_profile_cache VALUES(1);
                CREATE FUNCTION invalidate_building_profiles() RETURNS void
                LANGUAGE sql AS $$
                    UPDATE profile_state SET revision=revision+1;
                    DELETE FROM building_profile_cache;
                $$;
            """)
        publish(tmp_path, snapshot)
        with monkeypatch.context() as isolated:
            isolated.setattr(importer.psycopg, "connect", isolated_connect)
            result = importer.import_context(tmp_path, settings)
        assert result["cells"] == 2
        with engine.begin() as connection:
            assert connection.execute(text("SELECT * FROM profile_sources")).all() == [
                ("fine-grid", "population")
            ]
            assert connection.execute(text("SELECT * FROM profile_population")).all() == [
                ("unknown-cell", None)
            ]
            assert connection.execute(text("SELECT revision FROM profile_state")).scalar_one() == 2
            assert (
                connection.execute(text("SELECT count(*) FROM building_profile_cache")).scalar_one()
                == 0
            )
            assert (
                connection.execute(
                    text("SELECT count(*) FROM population_context_cells")
                ).scalar_one()
                == 2
            )
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        admin.dispose()
