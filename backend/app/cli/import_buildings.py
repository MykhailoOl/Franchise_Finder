import argparse
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import ijson
import psycopg
from psycopg.types.json import Jsonb

from app.core.config import Settings
from app.db.buildings import DATASET_ID
from app.db.profile_cache import invalidate_profiles

DEFAULT_SOURCE_URL = (
    "https://wms2.um.warszawa.pl/geoserver/wfs/wfs?"
    "service=WFS&version=2.0.0&request=GetCapabilities"
)
SCHEMA_PATH = Path(__file__).resolve().parents[1] / "db" / "map_schema.sql"

PREPARE_STAGE = (
    "CREATE TEMP TABLE building_stage ON COMMIT DROP AS "
    "WITH parsed AS ("
    " SELECT source_id, properties, building_type, address,"
    " ST_SetSRID(ST_Force2D(ST_GeomFromGeoJSON(geometry_json)), 4326) AS geometry"
    " FROM building_stage_raw"
    "), repaired AS ("
    " SELECT *, NOT ST_IsValid(geometry) AS was_repaired,"
    " CASE WHEN ST_IsValid(geometry) THEN geometry ELSE ST_MakeValid(geometry)"
    " END AS valid_geometry"
    " FROM parsed"
    ") SELECT source_id, properties, building_type, address, was_repaired,"
    " ST_Transform(ST_Multi(ST_CollectionExtract(valid_geometry, 3)), 3857)"
    " ::geometry(MultiPolygon, 3857) AS geometry FROM repaired"
)

UPSERT_BUILDINGS = (
    "INSERT INTO buildings (source_id, dataset_id, geometry, properties, building_type,"
    " address, imported_at) SELECT source_id, %(dataset_id)s, geometry, properties,"
    " building_type, address, %(imported_at)s FROM building_stage ORDER BY source_id"
    " ON CONFLICT (source_id) DO UPDATE SET dataset_id = EXCLUDED.dataset_id,"
    " geometry = EXCLUDED.geometry, properties = EXCLUDED.properties,"
    " building_type = EXCLUDED.building_type, address = EXCLUDED.address,"
    " imported_at = EXCLUDED.imported_at"
)


def normalize_feature(feature: Any) -> tuple[str, Jsonb, Jsonb, str | None, str | None]:
    if not isinstance(feature, dict) or feature.get("type") != "Feature":
        raise ValueError("Every source item must be a GeoJSON Feature")
    properties = feature.get("properties") or {}
    if not isinstance(properties, dict):
        raise ValueError("Feature properties must be a JSON object")
    source_id = properties.get("ID_BUDYNKU") or feature.get("id")
    if source_id is None or not str(source_id).strip():
        raise ValueError("Every building requires ID_BUDYNKU or a stable GeoJSON feature ID")
    geometry = feature.get("geometry")
    if not isinstance(geometry, dict) or geometry.get("type") not in ("Polygon", "MultiPolygon"):
        raise ValueError(f"Building {source_id} must have a Polygon or MultiPolygon geometry")
    building_type = properties.get("RODZAJ")
    address = properties.get("address")
    return (
        str(source_id),
        Jsonb(geometry),
        Jsonb(properties),
        str(building_type) if building_type is not None else None,
        address if isinstance(address, str) and address.strip() else None,
    )


def read_features(path: Path, dataset_name: str = "building") -> Iterator[Any]:
    collection_metadata: dict[str, Any] = {}

    def capture_metadata(events: Iterator[tuple[str, str, Any]]) -> Iterator[tuple[str, str, Any]]:
        for prefix, event, value in events:
            if prefix in ("type", "numberMatched", "numberReturned", "totalFeatures"):
                if event in ("string", "number"):
                    collection_metadata[prefix] = value
            if prefix == "crs.properties.name" and event == "string":
                if value not in (
                    "urn:ogc:def:crs:EPSG::4326",
                    "EPSG:4326",
                    "urn:ogc:def:crs:OGC:1.3:CRS84",
                ):
                    raise ValueError("Input must use EPSG:4326 longitude, latitude coordinates")
            yield prefix, event, value

    count = 0
    with path.open("rb") as source:
        events = capture_metadata(ijson.parse(source, use_float=True))
        for feature in ijson.items(events, "features.item"):
            count += 1
            yield feature
    if collection_metadata.get("type") != "FeatureCollection":
        raise ValueError("Input must be a GeoJSON FeatureCollection")
    if count == 0:
        raise ValueError(f"Refusing to replace the {dataset_name} dataset with an empty collection")
    for key in ("numberMatched", "numberReturned", "totalFeatures"):
        reported = collection_metadata.get(key)
        if reported is not None and str(reported).isdigit() and int(reported) != count:
            raise ValueError(
                f"Incomplete snapshot: {key} is {reported}, but received {count} features"
            )


def import_buildings(
    path: Path,
    settings: Settings,
    source_name: str,
    source_url: str,
    attribution: str,
    source_updated_at: datetime | None = None,
    expected_count: int | None = None,
) -> dict[str, Any]:
    imported_at = datetime.now(UTC)
    parameters = {
        "dataset_id": DATASET_ID,
        "name": source_name,
        "url": source_url,
        "attribution": attribution,
        "updated_at": source_updated_at,
        "imported_at": imported_at,
    }
    with psycopg.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        dbname=settings.postgres_db,
        user=settings.postgres_user,
        password=settings.postgres_password.get_secret_value(),
        connect_timeout=5,
    ) as connection:
        connection.execute("SELECT pg_advisory_xact_lock(hashtext('masterfranchises-warsaw-map'))")
        connection.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
        connection.execute(
            "CREATE TEMP TABLE building_stage_raw (source_id text PRIMARY KEY,"
            " geometry_json jsonb NOT NULL, properties jsonb NOT NULL, building_type text,"
            " address text) ON COMMIT DROP"
        )
        count = 0
        with connection.cursor().copy(
            "COPY building_stage_raw (source_id, geometry_json, properties, building_type, address)"
            " FROM STDIN"
        ) as copy:
            for feature in read_features(path):
                copy.write_row(normalize_feature(feature))
                count += 1
                if count % 20_000 == 0:
                    print(f"Staged {count:,} buildings", flush=True)
        if expected_count is not None and count != expected_count:
            raise ValueError(f"Expected {expected_count} buildings, received {count}")
        connection.execute(PREPARE_STAGE)
        invalid = connection.execute(
            "SELECT COUNT(*) FROM building_stage WHERE ST_IsEmpty(geometry)"
            " OR NOT ST_IsValid(geometry) OR ST_Area(geometry) <= 0"
            " OR NOT ST_CoveredBy(geometry, ST_Transform(ST_MakeEnvelope(20.85, 52.09,"
            " 21.29, 52.38, 4326), 3857))"
        ).fetchone()[0]
        if invalid:
            raise ValueError(f"Import rejected: {invalid} invalid, empty or non-Warsaw geometries")
        repaired = connection.execute(
            "SELECT COUNT(*) FROM building_stage WHERE was_repaired"
        ).fetchone()[0]
        bounds = connection.execute(
            "SELECT ARRAY[ST_XMin(bounds), ST_YMin(bounds), ST_XMax(bounds), ST_YMax(bounds)]"
            " FROM (SELECT ST_Extent(ST_Transform(geometry, 4326)) AS bounds FROM building_stage) b"
        ).fetchone()[0]
        parameters.update({"feature_count": count, "bounds": bounds})
        connection.execute(
            "INSERT INTO map_sources (id, name, url, attribution, updated_at, imported_at,"
            " feature_count, bounds) VALUES (%(dataset_id)s, %(name)s, %(url)s, %(attribution)s,"
            " %(updated_at)s, %(imported_at)s, %(feature_count)s, %(bounds)s)"
            " ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, url = EXCLUDED.url,"
            " attribution = EXCLUDED.attribution, updated_at = EXCLUDED.updated_at,"
            " imported_at = EXCLUDED.imported_at, feature_count = EXCLUDED.feature_count,"
            " bounds = EXCLUDED.bounds",
            parameters,
        )
        connection.execute(UPSERT_BUILDINGS, parameters)
        connection.execute(
            "DELETE FROM buildings WHERE dataset_id = %(dataset_id)s AND NOT EXISTS"
            " (SELECT 1 FROM building_stage WHERE building_stage.source_id = buildings.source_id)",
            parameters,
        )
        connection.execute("ANALYZE buildings")
        if connection.execute(
            "SELECT to_regprocedure('refresh_address_building_links()') IS NOT NULL"
        ).fetchone()[0]:
            connection.execute("SELECT refresh_address_building_links()")
        if connection.execute(
            "SELECT to_regprocedure('refresh_franchise_links()') IS NOT NULL"
        ).fetchone()[0]:
            connection.execute("SELECT refresh_franchise_links()")
        invalidate_profiles(connection)
    return {
        "feature_count": count,
        "repaired_geometries": repaired,
        "bounds": bounds,
        "imported_at": imported_at.isoformat(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import a complete Warsaw building GeoJSON snapshot"
    )
    parser.add_argument("path", type=Path)
    parser.add_argument("--source-name", default="Warsaw municipal building register")
    parser.add_argument("--source-url", default=DEFAULT_SOURCE_URL)
    parser.add_argument("--attribution", default="Urząd m.st. Warszawy — EGiB")
    parser.add_argument("--source-updated-at", type=datetime.fromisoformat)
    parser.add_argument("--expected-count", type=int)
    arguments = parser.parse_args()
    try:
        result = import_buildings(
            arguments.path,
            Settings(),
            arguments.source_name,
            arguments.source_url,
            arguments.attribution,
            arguments.source_updated_at,
            arguments.expected_count,
        )
    except (OSError, ValueError, psycopg.Error, ijson.JSONError) as error:
        parser.exit(1, f"Import failed; previous dataset retained: {error}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
