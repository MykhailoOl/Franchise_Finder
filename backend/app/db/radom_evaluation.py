from datetime import datetime
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import Field, field_validator

from app.cli.import_addresses import ADDRESS_SCHEMA_PATH, SOURCE_ID_PATTERN, UPSERT_ADDRESSES
from app.cli.import_buildings import (
    PREPARE_STAGE,
    SCHEMA_PATH,
    UPSERT_BUILDINGS,
    normalize_feature,
    read_features,
)
from app.cli.import_franchises import FRANCHISE_SCHEMA_PATH, UPSERT_OUTLETS
from app.cli.import_profile import PROFILE_SCHEMA_PATH
from app.core.address_search import fold_polish, normalize_house_number, normalize_street
from app.core.franchise_snapshot import FranchiseSnapshot, OutletRecord, SnapshotModel
from app.core.profile_data import (
    PopulationCell,
    ProfilePoi,
    ProfileSource,
    duplicate_pois,
    normalize_poi_name,
)

SCHEMA_PREFIX = "radom_evaluation_"
SOURCE_BINDINGS = {
    "warsaw-buildings": "radom-buildings",
    "warsaw-addresses": "radom-addresses",
    "warsaw-population-2021": "radom-population-2021",
    "warsaw-osm-pois": "radom-osm-pois",
    "warsaw-osm-activity": "radom-osm-activity",
}


class RadomAddress(SnapshotModel):
    raw: str = Field(min_length=1, max_length=2000)
    street: str | None = Field(default=None, max_length=300)
    house_number: str | None = Field(default=None, max_length=100)
    postal_code: str | None = Field(default=None, max_length=30)
    city: str

    @field_validator("city")
    @classmethod
    def require_radom(cls, value: str) -> str:
        if fold_polish(value.strip()) != "radom":
            raise ValueError(
                "The frozen evaluation accepts only explicitly identified Radom outlets"
            )
        return "Radom"


class RadomOutlet(OutletRecord):
    address: RadomAddress


class RadomSnapshot(FranchiseSnapshot):
    outlets: list[RadomOutlet] = Field(max_length=20_000)


def assert_isolated(connection: psycopg.Connection) -> str:
    schema = connection.execute("SELECT current_schema()").fetchone()[0]
    if not schema.startswith(SCHEMA_PREFIX) or not schema.removeprefix(SCHEMA_PREFIX).isalnum():
        raise ValueError("Fresh evaluation writes require their own isolated Radom schema")
    return schema


def initialize_schema(connection: psycopg.Connection, schema: str, bounds: list[float]) -> None:
    if not schema.startswith(SCHEMA_PREFIX) or not schema.removeprefix(SCHEMA_PREFIX).isalnum():
        raise ValueError("Unexpected isolated evaluation schema name")
    if not connection.execute(
        "SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname='pg_trgm')"
    ).fetchone()[0]:
        raise ValueError("The existing database must already provide pg_trgm")
    connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    connection.execute(sql.SQL("SET search_path={},public").format(sql.Identifier(schema)))
    assert_isolated(connection)
    connection.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
    connection.execute(
        ADDRESS_SCHEMA_PATH.read_text(encoding="utf-8").replace(
            "CREATE EXTENSION IF NOT EXISTS pg_trgm;", ""
        )
    )
    franchise_sql = FRANCHISE_SCHEMA_PATH.read_text(encoding="utf-8")
    original = "ST_MakeEnvelope(20.85,52.09,21.29,52.38,4326)"
    if original not in franchise_sql:
        raise ValueError("The shared franchise matching extent has changed")
    envelope = "ST_MakeEnvelope(" + ",".join(str(float(value)) for value in bounds) + ",4326)"
    connection.execute(
        franchise_sql.replace(original, envelope).replace(
            "supported Warsaw map extent", "frozen Radom source extent"
        )
    )
    connection.execute(PROFILE_SCHEMA_PATH.read_text(encoding="utf-8"))


def map_source(connection, alias: str, metadata: dict, table: str, count: int, now: datetime):
    assert_isolated(connection)
    if table not in {"building_stage", "address_stage"}:
        raise ValueError("Unknown staged geometry table")
    bounds = connection.execute(
        "SELECT ARRAY[ST_XMin(bounds),ST_YMin(bounds),ST_XMax(bounds),ST_YMax(bounds)]"
        f" FROM (SELECT ST_Extent(ST_Transform(geometry,4326)) AS bounds FROM {table}) b"
    ).fetchone()[0]
    parameters = {
        "dataset_id": alias,
        "name": metadata["name"],
        "url": metadata["url"],
        "attribution": metadata["attribution"],
        "updated_at": None,
        "imported_at": now,
        "feature_count": count,
        "bounds": bounds,
    }
    connection.execute(
        "INSERT INTO map_sources(id,name,url,attribution,updated_at,imported_at,"
        "feature_count,bounds)"
        " VALUES(%(dataset_id)s,%(name)s,%(url)s,%(attribution)s,NULL,%(imported_at)s,"
        "%(feature_count)s,%(bounds)s)",
        parameters,
    )
    return parameters


def load_buildings(connection, path: Path, metadata: dict, now: datetime) -> dict:
    assert_isolated(connection)
    connection.execute(
        "CREATE TEMP TABLE building_stage_raw(source_id text PRIMARY KEY,geometry_json jsonb,"
        "properties jsonb,building_type text,address text) ON COMMIT DROP"
    )
    count = 0
    with connection.cursor().copy("COPY building_stage_raw FROM STDIN") as stream:
        for feature in read_features(path):
            record = normalize_feature(feature)
            if not record[0].startswith("146301_1."):
                raise ValueError("Building source identity is outside the frozen Radom namespace")
            stream.write_row(record)
            count += 1
    connection.execute(PREPARE_STAGE)
    invalid, repaired = connection.execute(
        "SELECT count(*) FILTER(WHERE ST_IsEmpty(geometry) OR NOT ST_IsValid(geometry)"
        " OR ST_Area(geometry)<=0),count(*) FILTER(WHERE was_repaired) FROM building_stage"
    ).fetchone()
    if invalid:
        raise ValueError("Fresh building snapshot contains unusable footprints")
    parameters = map_source(connection, "warsaw-buildings", metadata, "building_stage", count, now)
    connection.execute(UPSERT_BUILDINGS, parameters)
    connection.execute("ANALYZE buildings")
    return {"count": count, "repaired_geometries": repaired, "bounds": parameters["bounds"]}


def normalize_radom_address(feature: dict) -> tuple:
    properties = feature.get("properties") or {}
    geometry = feature.get("geometry") or {}
    identifier = str(properties.get("source_id") or properties.get("ID_IIP") or "").strip()
    if not identifier or str(properties.get("ID_GMINY")) != "1463011":
        raise ValueError("Address requires an official identity in the frozen Radom municipality")
    match = SOURCE_ID_PATTERN.fullmatch(identifier)
    if match:
        identifier = match.group(1)
    street = str(properties.get("street") or properties.get("NAZWA_ULICY") or "").strip()
    house = str(properties.get("house_number") or properties.get("NUMER_PORZADKOWY") or "").strip()
    postal = str(properties.get("postal_code") or properties.get("KOD_POCZTOWY") or "").strip()
    city = str(properties.get("city") or properties.get("NAZWA_MIEJSCOWOSCI") or "").strip()
    if not house or fold_polish(city) != "radom" or geometry.get("type") != "Point":
        raise ValueError("Radom addresses require house number, locality and point geometry")
    return (
        identifier,
        street,
        normalize_street(street),
        house,
        normalize_house_number(house),
        postal or None,
        "Radom",
        f"{street} {house}, {postal + ' ' if postal else ''}Radom".strip(),
        Jsonb(geometry),
        Jsonb(properties),
    )


def load_addresses(connection, path: Path, metadata: dict, now: datetime) -> dict:
    assert_isolated(connection)
    connection.execute(
        "CREATE TEMP TABLE address_stage_raw(source_id text PRIMARY KEY,street text,"
        "street_normalized text,house_number text,house_number_normalized text,postal_code text,"
        "city text,address text,geometry_json jsonb,properties jsonb) ON COMMIT DROP"
    )
    count = 0
    with connection.cursor().copy("COPY address_stage_raw FROM STDIN") as stream:
        for feature in read_features(path, "address"):
            stream.write_row(normalize_radom_address(feature))
            count += 1
    connection.execute(
        "CREATE TEMP TABLE address_stage ON COMMIT DROP AS SELECT *,"
        "ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(geometry_json),4326),3857)"
        "::geometry(Point,3857) AS geometry FROM address_stage_raw"
    )
    parameters = map_source(connection, "warsaw-addresses", metadata, "address_stage", count, now)
    connection.execute(UPSERT_ADDRESSES, parameters)
    connection.execute("SELECT refresh_address_building_links()")
    connection.execute("ANALYZE addresses")
    return {
        "count": count,
        "matches": dict(
            connection.execute("SELECT match_status,count(*) FROM addresses GROUP BY 1")
        ),
    }


def load_profile(connection, kind: str, path: Path, metadata: dict, now: datetime) -> dict:
    assert_isolated(connection)
    if kind not in {"population", "pois", "activity"}:
        raise ValueError("Unknown profile kind")
    source = ProfileSource.model_validate(metadata)
    actual_id = {
        "population": "radom-population-2021",
        "pois": "radom-osm-pois",
        "activity": "radom-osm-activity",
    }[kind]
    if source.id != actual_id:
        raise ValueError("Fresh profile source identity differs from its frozen role")
    alias = next(key for key, value in SOURCE_BINDINGS.items() if value == actual_id)
    connection.execute(
        "CREATE TEMP TABLE profile_stage(source_id text PRIMARY KEY,geometry_json jsonb,"
        "properties jsonb) ON COMMIT DROP"
    )
    count = 0
    with connection.cursor().copy("COPY profile_stage FROM STDIN") as stream:
        for feature in read_features(path, kind):
            geometry, properties = feature.get("geometry") or {}, feature.get("properties") or {}
            if kind == "population":
                if geometry.get("type") not in {"Polygon", "MultiPolygon"}:
                    raise ValueError("Population requires polygon cells")
                record = PopulationCell.model_validate(properties)
            else:
                if geometry.get("type") != "Point":
                    raise ValueError("Mapped facilities require point geometry")
                record = ProfilePoi.model_validate(properties)
                if (kind == "activity") != (record.kind == "activity"):
                    raise ValueError("Activity and access/competitor source roles cannot be mixed")
            stream.write_row(
                (record.source_id, Jsonb(geometry), Jsonb(record.model_dump(mode="json")))
            )
            count += 1
    connection.execute("ALTER TABLE profile_stage ADD COLUMN geometry geometry(Geometry,2180)")
    connection.execute(
        "UPDATE profile_stage SET geometry=ST_Transform(ST_SetSRID("
        "ST_Force2D(ST_GeomFromGeoJSON(geometry_json)),4326),2180)"
    )
    if connection.execute(
        "SELECT count(*) FROM profile_stage WHERE ST_IsEmpty(geometry) OR NOT ST_IsValid(geometry)"
    ).fetchone()[0]:
        raise ValueError("Fresh source contains invalid geometries")
    duplicates = {}
    table = {
        "population": "profile_population",
        "pois": "profile_pois",
        "activity": "profile_activity",
    }[kind]
    if kind == "population":
        connection.execute("""
            INSERT INTO profile_population
            SELECT source_id,ST_Multi(geometry),(properties->>'population')::float8,
                properties->>'status',(properties->>'observation_year')::integer,
                (properties->>'resolution_m')::integer FROM profile_stage
        """)
    else:
        connection.execute(f"""
            INSERT INTO {table}(source_id,name,normalized_name,kind,categories,brand_id,
                geometry,source_url,updated_at,location_method,tags)
            SELECT source_id,properties->>'name','',properties->>'kind',
                ARRAY(SELECT jsonb_array_elements_text(properties->'categories')),
                properties->>'brand_id',geometry,properties->>'source_url',
                (properties->>'updated_at')::timestamptz,properties->>'location_method',
                properties->'tags' FROM profile_stage
        """)
        rows = (
            connection.cursor(row_factory=dict_row)
            .execute(
                "SELECT source_id,name,kind,categories,location_method,tags,"
                f"ST_X(geometry) AS x,ST_Y(geometry) AS y FROM {table}"
            )
            .fetchall()
        )
        for row in rows:
            row["normalized_name"] = normalize_poi_name(row["name"])
        duplicates = duplicate_pois(rows)
        with connection.cursor() as cursor:
            cursor.executemany(
                f"UPDATE {table} SET normalized_name=%s,duplicate_of=%s WHERE source_id=%s",
                [
                    (row["normalized_name"], duplicates.get(row["source_id"]), row["source_id"])
                    for row in rows
                ],
            )
    bound_metadata = {
        **source.model_dump(mode="json"),
        "logical_feature_role": alias,
        "actual_source_id": actual_id,
        "territory": "Radom",
    }
    connection.execute(
        "INSERT INTO profile_sources(id,kind,metadata,feature_count,imported_at)"
        " VALUES(%s,%s,%s,%s,%s)",
        (alias, kind, Jsonb(bound_metadata), count, now),
    )
    connection.execute("DROP TABLE profile_stage")
    connection.execute(f"ANALYZE {table}")
    return {"count": count, "duplicate_records": len(duplicates)}


def load_outlets(connection, paths: list[Path]) -> dict:
    assert_isolated(connection)
    snapshots = [RadomSnapshot.model_validate_json(path.read_bytes()) for path in paths]
    brands = [brand for snapshot in snapshots for brand in snapshot.brands]
    if sorted(brand.id for brand in brands) != ["lewiatan", "zabka"]:
        raise ValueError("Fresh labels require exactly the two frozen grocery brands")
    if any(
        brand.category_id != "grocery" or brand.coverage_status != "complete" for brand in brands
    ):
        raise ValueError("Both frozen brands require complete published locator snapshots")
    connection.execute("INSERT INTO franchise_categories VALUES('grocery','Grocery')")
    for snapshot in snapshots:
        for brand in snapshot.brands:
            connection.execute(
                "INSERT INTO franchise_brands VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    brand.id,
                    brand.name,
                    brand.category_id,
                    str(brand.website),
                    str(brand.source_url),
                    brand.retrieved_at,
                    brand.coverage_status,
                    brand.coverage_note,
                    Jsonb(snapshot.provenance),
                ),
            )
        parameters = []
        for outlet in snapshot.outlets:
            address = outlet.address
            parameters.append(
                {
                    "brand_id": outlet.brand_id,
                    "source_id": outlet.source_id,
                    "name": outlet.name,
                    "address_raw": address.raw,
                    "street": address.street,
                    "street_normalized": normalize_street(address.street)
                    if address.street
                    else None,
                    "house_number": address.house_number,
                    "house_number_normalized": normalize_house_number(address.house_number)
                    if address.house_number
                    else None,
                    "postal_code": address.postal_code.strip() if address.postal_code else None,
                    "city": address.city,
                    "longitude": outlet.coordinates[0] if outlet.coordinates else None,
                    "latitude": outlet.coordinates[1] if outlet.coordinates else None,
                    "source_url": str(outlet.source_url),
                    "retrieved_at": outlet.retrieved_at,
                    "status": outlet.status,
                    "operating_model": outlet.operating_model,
                    "operating_model_source_url": str(outlet.operating_model_source_url)
                    if outlet.operating_model_source_url
                    else None,
                    "attributes": Jsonb(outlet.attributes),
                }
            )
        with connection.cursor() as cursor:
            cursor.executemany(UPSERT_OUTLETS, parameters)
    connection.execute("SELECT refresh_franchise_links()")
    connection.execute("ANALYZE franchise_outlets")
    return {
        "matches": [
            dict(row)
            for row in connection.cursor(row_factory=dict_row).execute(
                "SELECT brand_id,match_status,count(*) AS count FROM franchise_outlets GROUP BY 1,2"
                " ORDER BY 1,2"
            )
        ],
    }


def candidate_cohort(connection) -> list[dict]:
    return (
        connection.cursor(row_factory=dict_row)
        .execute("""
        WITH eligible AS (
            SELECT b.source_id,ST_PointOnSurface(ST_Transform(b.geometry,2180)) AS origin
            FROM buildings b WHERE b.dataset_id='warsaw-buildings'
                AND ST_IsValid(b.geometry) AND NOT ST_IsEmpty(b.geometry)
                AND EXISTS(SELECT 1 FROM addresses a WHERE a.building_source_id=b.source_id
                    AND a.match_status='matched' AND a.candidate_count=1)
        ) SELECT source_id AS building_source_id,ST_X(origin) AS x,ST_Y(origin) AS y,
            CASE WHEN EXISTS(SELECT 1 FROM franchise_outlets o
                WHERE o.building_source_id=e.source_id
                AND o.brand_id IN ('zabka','lewiatan') AND o.match_status='matched')
                THEN 'observed_presence' ELSE 'unlabelled_background' END AS label
        FROM eligible e ORDER BY source_id
    """)
        .fetchall()
    )
