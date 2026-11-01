import argparse
import json
from pathlib import Path
from typing import Any

import ijson
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.cli.import_buildings import SCHEMA_PATH, read_features
from app.core.config import Settings
from app.core.profile_data import (
    PopulationCell,
    ProfilePoi,
    ProfileSource,
    duplicate_pois,
    normalize_poi_name,
)

PROFILE_SCHEMA_PATH = SCHEMA_PATH.with_name("profile_schema.sql")


def import_profile(inputs: list[tuple[str, Path, Path]], settings: Settings) -> dict[str, Any]:
    sources = [
        ProfileSource.model_validate_json(metadata.read_bytes()) for _, _, metadata in inputs
    ]
    if len({kind for kind, _, _ in inputs}) != len(inputs):
        raise ValueError("Only one snapshot per profile dataset kind is permitted")
    result = {}
    with psycopg.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        dbname=settings.postgres_db,
        user=settings.postgres_user,
        password=settings.postgres_password.get_secret_value(),
        connect_timeout=5,
    ) as connection:
        connection.execute("SELECT pg_advisory_xact_lock(hashtext('masterfranchises-warsaw-map'))")
        connection.execute(PROFILE_SCHEMA_PATH.read_text(encoding="utf-8"))
        for (kind, path, _), source in zip(inputs, sources, strict=True):
            if kind not in ("population", "pois"):
                raise ValueError("Unsupported profile dataset kind")
            connection.execute(
                "CREATE TEMP TABLE profile_stage(source_id text PRIMARY KEY,geometry_json jsonb,"
                " properties jsonb) ON COMMIT DROP"
            )
            count = 0
            with connection.cursor().copy("COPY profile_stage FROM STDIN") as copy:
                for feature in read_features(path, kind):
                    geometry = feature.get("geometry") or {}
                    properties = feature.get("properties") or {}
                    if kind == "population":
                        if geometry.get("type") not in ("Polygon", "MultiPolygon"):
                            raise ValueError("Population cells require polygon geometry")
                        properties["source_id"] = properties.get("source_id") or properties.get(
                            "grid_id"
                        )
                        record = PopulationCell.model_validate(properties)
                    else:
                        if geometry.get("type") != "Point":
                            raise ValueError("POIs require point geometry")
                        record = ProfilePoi.model_validate(properties)
                    copy.write_row(
                        (record.source_id, Jsonb(geometry), Jsonb(record.model_dump(mode="json")))
                    )
                    count += 1
            connection.execute(
                "ALTER TABLE profile_stage ADD COLUMN geometry geometry(Geometry,2180)"
            )
            connection.execute(
                "UPDATE profile_stage SET geometry=ST_Transform("
                " ST_SetSRID(ST_Force2D(ST_GeomFromGeoJSON(geometry_json)),4326),2180)"
            )
            invalid = connection.execute(
                "SELECT count(*) FROM profile_stage WHERE ST_IsEmpty(geometry)"
                " OR NOT ST_IsValid(geometry) OR NOT ST_CoveredBy(geometry,"
                " ST_Transform(ST_MakeEnvelope(20.6,51.9,21.6,52.6,4326),2180))"
            ).fetchone()[0]
            if invalid:
                raise ValueError(f"Rejected {invalid} invalid or out-of-area profile geometries")
            if kind == "population":
                connection.execute("DELETE FROM profile_population")
                connection.execute("""
                    INSERT INTO profile_population
                    SELECT source_id,ST_Multi(geometry),(properties->>'population')::float8,
                        properties->>'status',(properties->>'observation_year')::integer,
                        (properties->>'resolution_m')::integer FROM profile_stage
                """)
                connection.execute("ANALYZE profile_population")
            else:
                connection.execute("DELETE FROM profile_pois")
                connection.execute("""
                    INSERT INTO profile_pois(source_id,name,normalized_name,kind,categories,
                        brand_id,geometry,source_url,updated_at,location_method,tags)
                    SELECT source_id,properties->>'name','',properties->>'kind',
                        ARRAY(SELECT jsonb_array_elements_text(properties->'categories')),
                        properties->>'brand_id',geometry,properties->>'source_url',
                        (properties->>'updated_at')::timestamptz,
                        properties->>'location_method',properties->'tags' FROM profile_stage
                """)
                rows = (
                    connection.cursor(row_factory=dict_row)
                    .execute(
                        "SELECT source_id,name,normalized_name,kind,categories,"
                        " location_method,tags,"
                        " ST_X(geometry) AS x,ST_Y(geometry) AS y FROM profile_pois"
                    )
                    .fetchall()
                )
                for row in rows:
                    row["normalized_name"] = normalize_poi_name(row["name"])
                with connection.cursor() as cursor:
                    cursor.executemany(
                        "UPDATE profile_pois SET normalized_name=%s WHERE source_id=%s",
                        [(row["normalized_name"], row["source_id"]) for row in rows],
                    )
                    duplicates = duplicate_pois(rows)
                    cursor.executemany(
                        "UPDATE profile_pois SET duplicate_of=%s WHERE source_id=%s",
                        [(retained, omitted) for omitted, retained in duplicates.items()],
                    )
                result["osm_duplicate_records"] = len(duplicates)
                connection.execute("ANALYZE profile_pois")
            connection.execute("DELETE FROM profile_sources WHERE kind=%s", (kind,))
            connection.execute(
                "INSERT INTO profile_sources(id,kind,metadata,feature_count) VALUES(%s,%s,%s,%s)",
                (source.id, kind, Jsonb(source.model_dump(mode="json")), count),
            )
            connection.execute("DROP TABLE profile_stage")
            result[kind] = count
        connection.execute("SELECT invalidate_building_profiles()")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Import public Warsaw building profile snapshots")
    for kind in ("population", "pois"):
        parser.add_argument(f"--{kind}", type=Path)
        parser.add_argument(f"--{kind}-metadata", type=Path)
    arguments = parser.parse_args()
    inputs = []
    for kind in ("population", "pois"):
        path, metadata = getattr(arguments, kind), getattr(arguments, f"{kind}_metadata")
        if bool(path) != bool(metadata):
            parser.error(f"--{kind} and --{kind}-metadata are required together")
        if path:
            inputs.append((kind, path, metadata))
    if not inputs:
        parser.error("Provide at least one population or POI snapshot")
    try:
        result = import_profile(inputs, Settings())
    except (OSError, ValueError, psycopg.Error, ijson.JSONError) as error:
        parser.exit(1, f"Import failed; previous profile datasets retained: {error}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
