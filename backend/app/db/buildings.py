import json
from collections.abc import Mapping
from typing import Any

from sqlalchemy import Connection, Engine, text

from app.db.addresses import get_building_addresses
from app.db.franchises import get_building_franchises

DATASET_ID = "warsaw-buildings"
WARSAW_BOUNDS = [20.85, 52.09, 21.29, 52.38]
MIN_ZOOM = 13
MAX_ZOOM = 18
BUILDING_TYPE_LABELS = {
    "m": "Residential",
    "g": "Production, service and agricultural utility",
    "t": "Transport and communications",
    "k": "Education, science, culture and sports",
    "z": "Hospital and healthcare",
    "b": "Office",
    "h": "Retail and services",
    "p": "Industrial",
    "s": "Tank, silo and warehouse",
    "i": "Other non-residential",
}

SOURCE_QUERY = text(
    "SELECT name, url, attribution, updated_at, imported_at, feature_count, bounds "
    "FROM map_sources WHERE id = :dataset_id"
)

TILE_QUERY = text(
    "WITH tile AS ("
    " SELECT ST_TileEnvelope(:z, :x, :y) AS bounds,"
    " ST_TileEnvelope(:z, :x, :y, margin => (64.0 / 4096)) AS buffered_bounds"
    "), features AS ("
    " SELECT buildings.id, buildings.building_type,"
    " ST_AsMVTGeom(buildings.geometry, tile.bounds, 4096, 64, true) AS geom"
    " FROM buildings CROSS JOIN tile"
    " WHERE buildings.dataset_id = :dataset_id"
    " AND buildings.geometry && tile.buffered_bounds"
    " ORDER BY buildings.id"
    ") SELECT ST_AsMVT(features.*, 'buildings', 4096, 'geom', 'id') FROM features"
)

FRANCHISE_TILE_QUERY = text(
    str(TILE_QUERY)
    .replace(
        "buildings.id, buildings.building_type,",
        "buildings.id, buildings.building_type, coalesce(f.outlet_count,0) AS franchise_count,"
        " coalesce(f.brand_flags,'{}'::jsonb) AS brands,",
    )
    .replace(
        "FROM buildings CROSS JOIN tile",
        "FROM buildings LEFT JOIN building_franchise_summary f"
        " ON f.building_source_id=buildings.source_id CROSS JOIN tile",
    )
)

DETAIL_QUERY = text(
    "SELECT id, source_id, properties, building_type, address, area_m2, imported_at,"
    " ST_AsGeoJSON(ST_ForcePolygonCCW(ST_Transform(geometry, 4326)), 9, 0)::json"
    " AS geojson_geometry FROM buildings WHERE id = :building_id AND dataset_id = :dataset_id"
)


class MapDataUnavailableError(Exception):
    pass


def schema_exists(connection: Connection) -> bool:
    return bool(
        connection.execute(
            text(
                "SELECT to_regclass('public.buildings') IS NOT NULL "
                "AND to_regclass('public.map_sources') IS NOT NULL"
            )
        ).scalar_one()
    )


def source_metadata(row: Mapping[str, Any]) -> dict[str, Any]:
    return {key: row[key] for key in ("name", "url", "attribution", "updated_at")}


def require_source(connection: Connection) -> Mapping[str, Any]:
    if not schema_exists(connection):
        raise MapDataUnavailableError
    source = connection.execute(SOURCE_QUERY, {"dataset_id": DATASET_ID}).mappings().first()
    if source is None or source["feature_count"] == 0:
        raise MapDataUnavailableError
    return source


def get_metadata(engine: Engine) -> dict[str, Any]:
    with engine.begin() as connection:
        connection.execute(text("SET LOCAL statement_timeout = '10s'"))
        try:
            source = require_source(connection)
        except MapDataUnavailableError:
            return {
                "status": "empty",
                "bounds": WARSAW_BOUNDS,
                "minzoom": MIN_ZOOM,
                "maxzoom": MAX_ZOOM,
                "feature_count": 0,
                "last_imported_at": None,
                "source": None,
            }
    return {
        "status": "ready",
        "bounds": source["bounds"],
        "minzoom": MIN_ZOOM,
        "maxzoom": MAX_ZOOM,
        "feature_count": source["feature_count"],
        "last_imported_at": source["imported_at"],
        "source": source_metadata(source),
    }


def get_tile(engine: Engine, z: int, x: int, y: int) -> bytes:
    with engine.begin() as connection:
        connection.execute(text("SET LOCAL statement_timeout = '10s'"))
        require_source(connection)
        has_franchises = connection.execute(
            text("SELECT to_regclass('building_franchise_summary') IS NOT NULL")
        ).scalar_one()
        tile = connection.execute(
            FRANCHISE_TILE_QUERY if has_franchises else TILE_QUERY,
            {"z": z, "x": x, "y": y, "dataset_id": DATASET_ID},
        ).scalar_one()
    return bytes(tile or b"")


def get_building(engine: Engine, building_id: int) -> dict[str, Any] | None:
    with engine.begin() as connection:
        connection.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
        connection.execute(text("SET LOCAL statement_timeout = '10s'"))
        source = require_source(connection)
        building = (
            connection.execute(DETAIL_QUERY, {"building_id": building_id, "dataset_id": DATASET_ID})
            .mappings()
            .first()
        )
        if building is not None:
            addresses, address_status = get_building_addresses(connection, building["source_id"])
            franchises = get_building_franchises(connection, building["source_id"])
    if building is None:
        return None
    geometry = building["geojson_geometry"]
    if isinstance(geometry, str):
        geometry = json.loads(geometry)
    return {
        "type": "Feature",
        "id": building["id"],
        "geometry": geometry,
        "properties": {
            "source_id": building["source_id"],
            "source": source_metadata(source),
            "building_type": building["building_type"],
            "building_type_label": BUILDING_TYPE_LABELS.get(building["building_type"]),
            "address": addresses[0]["address"] if addresses else building["address"],
            "addresses": addresses,
            "address_status": address_status,
            "franchises": franchises,
            "area_m2": round(building["area_m2"], 2),
            "imported_at": building["imported_at"],
            "attributes": building["properties"],
        },
    }
