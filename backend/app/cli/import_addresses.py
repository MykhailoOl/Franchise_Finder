import argparse
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import ijson
import psycopg
from psycopg.types.json import Jsonb

from app.cli.import_buildings import DEFAULT_SOURCE_URL, SCHEMA_PATH, read_features
from app.core.address_search import normalize_house_number, normalize_street
from app.core.config import Settings
from app.db.addresses import DATASET_ID
from app.db.profile_cache import invalidate_profiles

ADDRESS_SCHEMA_PATH = SCHEMA_PATH.with_name("address_schema.sql")
SOURCE_ID_PATTERN = re.compile(
    r"^(.*?\.[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.(.+)$"
)

UPSERT_ADDRESSES = (
    "INSERT INTO addresses (source_id, dataset_id, street, street_normalized, house_number,"
    " house_number_normalized, postal_code, city, address, geometry, properties, imported_at)"
    " SELECT source_id, %(dataset_id)s, street, street_normalized, house_number,"
    " house_number_normalized, postal_code, city, address, geometry, properties, %(imported_at)s"
    " FROM address_stage ORDER BY source_id ON CONFLICT (source_id) DO UPDATE SET"
    " dataset_id = EXCLUDED.dataset_id, street = EXCLUDED.street,"
    " street_normalized = EXCLUDED.street_normalized, house_number = EXCLUDED.house_number,"
    " house_number_normalized = EXCLUDED.house_number_normalized,"
    " postal_code = EXCLUDED.postal_code, city = EXCLUDED.city, address = EXCLUDED.address,"
    " geometry = EXCLUDED.geometry, properties = EXCLUDED.properties,"
    " imported_at = EXCLUDED.imported_at"
)


def normalize_address(feature: Any) -> tuple:
    if not isinstance(feature, dict) or feature.get("type") != "Feature":
        raise ValueError("Every address must be a GeoJSON Feature")
    properties = feature.get("properties")
    if not isinstance(properties, dict):
        raise ValueError("Every address requires source properties")
    source_id = SOURCE_ID_PATTERN.fullmatch(str(properties.get("ID_IIP", "")))
    if source_id is None:
        raise ValueError("Every address requires the official versioned EMUiA ID_IIP")
    if properties.get("ID_GMINY") != "1465011":
        raise ValueError("Only official Warsaw municipality address records are supported")
    street = str(properties.get("NAZWA_ULICY") or "").strip()
    house_number = str(properties.get("NUMER_PORZADKOWY") or "").strip()
    city = str(properties.get("NAZWA_MIEJSCOWOSCI") or "").strip()
    postal_code = str(properties.get("KOD_POCZTOWY") or "").strip() or None
    if not street or not house_number or city != "Warszawa":
        raise ValueError(
            "Every address requires an official street, house number and Warsaw locality"
        )
    geometry = feature.get("geometry")
    if not isinstance(geometry, dict) or geometry.get("type") != "Point":
        raise ValueError("Address geometry must be a GeoJSON Point")
    address = f"{street} {house_number}, {postal_code + ' ' if postal_code else ''}{city}"
    return (
        source_id.group(1),
        street,
        normalize_street(street),
        house_number,
        normalize_house_number(house_number),
        postal_code,
        city,
        address,
        Jsonb(geometry),
        Jsonb(properties),
    )


def import_addresses(
    path: Path,
    settings: Settings,
    source_name: str = "Warsaw municipal address register",
    source_url: str = DEFAULT_SOURCE_URL,
    attribution: str = "Urząd m.st. Warszawy — EMUiA",
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
        connection.execute(ADDRESS_SCHEMA_PATH.read_text(encoding="utf-8"))
        connection.execute(
            "CREATE TEMP TABLE address_stage_raw (source_id text PRIMARY KEY, street text NOT NULL,"
            " street_normalized text NOT NULL, house_number text NOT NULL,"
            " house_number_normalized text NOT NULL, postal_code text, city text NOT NULL,"
            " address text NOT NULL, geometry_json jsonb NOT NULL, properties jsonb NOT NULL)"
            " ON COMMIT DROP"
        )
        count = 0
        with connection.cursor().copy("COPY address_stage_raw FROM STDIN") as copy:
            for feature in read_features(path, "address"):
                copy.write_row(normalize_address(feature))
                count += 1
                if count % 20_000 == 0:
                    print(f"Staged {count:,} addresses", flush=True)
        if expected_count is not None and count != expected_count:
            raise ValueError(f"Expected {expected_count} addresses, received {count}")
        connection.execute(
            "CREATE TEMP TABLE address_stage ON COMMIT DROP AS SELECT *,"
            " ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(geometry_json), 4326), 3857)"
            " ::geometry(Point, 3857) AS geometry FROM address_stage_raw"
        )
        invalid = connection.execute(
            "SELECT COUNT(*) FROM address_stage WHERE ST_IsEmpty(geometry) OR NOT ST_CoveredBy("
            " geometry, ST_Transform(ST_MakeEnvelope(20.8,52.05,21.35,52.4,4326),3857))"
        ).fetchone()[0]
        if invalid:
            raise ValueError(f"Import rejected: {invalid} empty or non-Warsaw address points")
        bounds = connection.execute(
            "SELECT ARRAY[ST_XMin(bounds),ST_YMin(bounds),ST_XMax(bounds),ST_YMax(bounds)]"
            " FROM (SELECT ST_Extent(ST_Transform(geometry,4326)) AS bounds FROM address_stage) b"
        ).fetchone()[0]
        parameters.update({"feature_count": count, "bounds": bounds})
        connection.execute(
            "INSERT INTO map_sources (id,name,url,attribution,updated_at,imported_at,feature_count,"
            " bounds) VALUES (%(dataset_id)s,%(name)s,%(url)s,%(attribution)s,%(updated_at)s,"
            " %(imported_at)s,%(feature_count)s,%(bounds)s) ON CONFLICT (id) DO UPDATE SET"
            " name=EXCLUDED.name,url=EXCLUDED.url,attribution=EXCLUDED.attribution,"
            " updated_at=EXCLUDED.updated_at,imported_at=EXCLUDED.imported_at,"
            " feature_count=EXCLUDED.feature_count,bounds=EXCLUDED.bounds",
            parameters,
        )
        connection.execute(UPSERT_ADDRESSES, parameters)
        connection.execute(
            "DELETE FROM addresses WHERE dataset_id=%(dataset_id)s AND NOT EXISTS"
            " (SELECT 1 FROM address_stage WHERE address_stage.source_id=addresses.source_id)",
            parameters,
        )
        connection.execute("SELECT refresh_address_building_links()")
        if connection.execute(
            "SELECT to_regprocedure('refresh_franchise_links()') IS NOT NULL"
        ).fetchone()[0]:
            connection.execute("SELECT refresh_franchise_links()")
        invalidate_profiles(connection)
        connection.execute("ANALYZE addresses")
        matches = dict(connection.execute("SELECT match_status,COUNT(*) FROM addresses GROUP BY 1"))
        linked_buildings = connection.execute(
            "SELECT COUNT(DISTINCT building_source_id) FROM addresses"
        ).fetchone()[0]
    return {
        "feature_count": count,
        "matches": matches,
        "linked_buildings": linked_buildings,
        "bounds": bounds,
        "imported_at": imported_at.isoformat(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import a complete official Warsaw address snapshot"
    )
    parser.add_argument("path", type=Path)
    parser.add_argument("--source-name", default="Warsaw municipal address register")
    parser.add_argument("--source-url", default=DEFAULT_SOURCE_URL)
    parser.add_argument("--attribution", default="Urząd m.st. Warszawy — EMUiA")
    parser.add_argument("--source-updated-at", type=datetime.fromisoformat)
    parser.add_argument("--expected-count", type=int)
    arguments = parser.parse_args()
    try:
        result = import_addresses(
            arguments.path,
            Settings(),
            arguments.source_name,
            arguments.source_url,
            arguments.attribution,
            arguments.source_updated_at,
            arguments.expected_count,
        )
    except (OSError, ValueError, psycopg.Error, ijson.JSONError) as error:
        parser.exit(1, f"Import failed; previous address dataset retained: {error}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
