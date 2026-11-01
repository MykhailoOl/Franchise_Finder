import re
from typing import Any

from sqlalchemy import Connection, Engine, text

OUTLET_QUERY = """
SELECT o.id,o.brand_id,b.name AS brand_name,b.category_id,o.source_id,o.name,
    o.address_raw AS address,o.street,o.house_number,o.postal_code,o.city,
    buildings.id AS building_id,o.match_status,o.match_method,o.match_note,o.match_distance_m,
    o.candidate_building_ids,o.point_source,o.source_url,o.retrieved_at,o.status,
    o.operating_model,o.operating_model_source_url,
    ST_AsGeoJSON(ST_Transform(o.display_geometry,4326),9)::json AS geometry
FROM franchise_outlets o JOIN franchise_brands b ON b.id=o.brand_id
LEFT JOIN buildings ON buildings.source_id=o.building_source_id
"""


def schema_exists(connection: Connection) -> bool:
    return bool(connection.execute(text("SELECT to_regclass('franchise_outlets')")).scalar_one())


def parse_brand_ids(value: str | None) -> list[str] | None:
    if value is None:
        return None
    values = value.split(",")
    if len(values) > 50 or any(not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", v) for v in values):
        raise ValueError("brand_ids must contain at most 50 comma-separated brand identifiers")
    return list(dict.fromkeys(values))


def get_catalog(engine: Engine) -> dict[str, Any]:
    with engine.begin() as connection:
        connection.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
        connection.execute(text("SET LOCAL statement_timeout = '10s'"))
        if not schema_exists(connection):
            return {
                "status": "empty",
                "categories": [],
                "brands": [],
                "summary": {
                    "total_outlets": 0,
                    "mapped_outlets": 0,
                    "matched_outlets": 0,
                },
            }
        brands = [
            dict(row)
            for row in connection.execute(
                text("""
            SELECT b.id,b.name,b.category_id,b.website,b.source_url,b.retrieved_at,
                b.coverage_status,b.coverage_note,'brand_' || b.id AS tile_property,
                count(o.id) AS outlet_count,count(o.display_geometry) AS mapped_count,
                count(o.building_source_id) AS matched_count
            FROM franchise_brands b LEFT JOIN franchise_outlets o ON o.brand_id=b.id
            GROUP BY b.id ORDER BY b.category_id,b.name
        """)
            ).mappings()
        ]
        categories = [
            dict(row)
            for row in connection.execute(
                text(
                    "SELECT c.id,c.name FROM franchise_categories c WHERE EXISTS"
                    " (SELECT 1 FROM franchise_brands b WHERE b.category_id=c.id) ORDER BY c.name"
                )
            ).mappings()
        ]
    return {
        "status": "ready" if brands else "empty",
        "categories": categories,
        "brands": brands,
        "summary": {
            "total_outlets": sum(b["outlet_count"] for b in brands),
            "mapped_outlets": sum(b["mapped_count"] for b in brands),
            "matched_outlets": sum(b["matched_count"] for b in brands),
        },
    }


def get_outlets(engine: Engine, brand_ids: list[str] | None = None) -> dict[str, Any]:
    with engine.begin() as connection:
        connection.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
        connection.execute(text("SET LOCAL statement_timeout = '10s'"))
        if not schema_exists(connection):
            rows = []
            status = "empty"
        else:
            condition = " WHERE o.brand_id=ANY(:brand_ids)" if brand_ids is not None else ""
            rows = [
                dict(row)
                for row in connection.execute(
                    text(OUTLET_QUERY + condition + " ORDER BY o.id"), {"brand_ids": brand_ids}
                ).mappings()
            ]
            status = "ready"
    features = [
        {"type": "Feature", "id": row.pop("id"), "geometry": row.pop("geometry"), "properties": row}
        for row in rows
    ]
    return {
        "type": "FeatureCollection",
        "status": status,
        "features": features,
        "summary": {
            "total_outlets": len(features),
            "mapped_outlets": sum(f["geometry"] is not None for f in features),
            "matched_outlets": sum(f["properties"]["building_id"] is not None for f in features),
        },
    }


def get_building_franchises(connection: Connection, source_id: str) -> list[dict[str, Any]]:
    if not schema_exists(connection):
        return []
    rows = connection.execute(
        text(OUTLET_QUERY + " WHERE o.building_source_id=:source_id ORDER BY b.name,o.id"),
        {"source_id": source_id},
    ).mappings()
    return [{key: value for key, value in row.items() if key != "geometry"} for row in rows]
