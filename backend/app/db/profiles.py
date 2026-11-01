import json
import logging
import math
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import SQLAlchemyError

from app.core.profile_data import ACCESS_CATEGORIES, COMPETITION_CATEGORIES
from app.db.buildings import MapDataUnavailableError

RADII = (300, 500, 1000)
PROFILE_SCHEMA_VERSION = "building-profile-v3"
logger = logging.getLogger(__name__)
UNKNOWN_PREMISES = [
    {"id": "usable_unit_area", "label": "Usable area of a specific commercial unit"},
    {"id": "rent", "label": "Current rent, service charges and lease terms"},
    {"id": "availability", "label": "Whether a unit is currently available"},
    {"id": "entrance", "label": "Customer entrance, street frontage and step-free access"},
    {"id": "utilities", "label": "Power, water, ventilation and other technical services"},
    {"id": "permitted_use", "label": "Permitted use and owner or authority approvals"},
]

POPULATION_QUERY = text("""
WITH area AS (
    SELECT ST_Buffer(ST_SetSRID(ST_Point(:x,:y),2180),:radius,64) AS geometry
), cells AS (
    SELECT p.*,ST_Area(ST_Intersection(p.geometry,area.geometry)) AS overlap_area,
        ST_Area(area.geometry) AS catchment_area
    FROM profile_population p CROSS JOIN area
    WHERE p.geometry && area.geometry AND ST_Intersects(p.geometry,area.geometry)
)
SELECT sum(population*overlap_area/ST_Area(geometry)) FILTER(WHERE status='observed') AS population,
    sum(overlap_area) FILTER(WHERE status='observed') / max(catchment_area) AS coverage,
    min(observation_year) AS observation_year,min(resolution_m) AS resolution_m,
    count(*) AS cell_count,count(*) FILTER(WHERE status='suppressed') AS suppressed_count,
    count(*) FILTER(WHERE status='missing') AS missing_count
FROM cells WHERE overlap_area>0
""")


def table_exists(connection: Connection, table: str) -> bool:
    return bool(
        connection.execute(text("SELECT to_regclass(:table)"), {"table": table}).scalar_one()
    )


def population_profile(
    connection: Connection, source: dict | None, origin: dict, radius: int
) -> dict[str, Any]:
    result = {
        "status": "unavailable",
        "estimated_residents": None,
        "coverage_fraction": None,
        "observation_year": None,
        "resolution_m": None,
        "method": "area_weighted_grid",
        "estimate_scope": "observed_cells_only",
        "source": source,
        "intersected_cell_count": 0,
        "suppressed_cell_count": 0,
        "missing_cell_count": 0,
    }
    if source is None:
        return result
    row = (
        connection.execute(POPULATION_QUERY, {"x": origin["x"], "y": origin["y"], "radius": radius})
        .mappings()
        .one()
    )
    coverage = min(1.0, max(0.0, row["coverage"] or 0.0))
    result.update(
        {
            "status": "unavailable"
            if row["population"] is None
            else "ready"
            if coverage >= 0.999
            else "partial",
            "estimated_residents": round(row["population"])
            if row["population"] is not None
            else None,
            "coverage_fraction": round(coverage, 4),
            "observation_year": row["observation_year"],
            "resolution_m": row["resolution_m"],
            "intersected_cell_count": row["cell_count"],
            "suppressed_cell_count": row["suppressed_count"],
            "missing_cell_count": row["missing_count"],
        }
    )
    return result


def official_outlets(connection: Connection, building: dict) -> tuple[list[dict], list[dict], bool]:
    if not table_exists(connection, "franchise_outlets"):
        return [], [], False
    rows = [
        dict(row)
        for row in connection.execute(
            text("""
        SELECT o.id,o.name,b.name AS brand_name,o.brand_id,b.category_id,o.building_source_id,
            o.address_raw AS address,o.source_url,o.point_source AS location_method,
            ST_X(ST_Transform(o.display_geometry,2180)) AS x,
            ST_Y(ST_Transform(o.display_geometry,2180)) AS y,
            coalesce(ST_Covers(selected.geometry,o.display_geometry),false) AS inside_selected
        FROM franchise_outlets o JOIN franchise_brands b ON b.id=o.brand_id
        CROSS JOIN buildings selected WHERE selected.id=:building_id
    """),
            {"building_id": building["id"]},
        ).mappings()
    ]
    sources = [
        {
            "id": "brand-" + row["id"],
            "name": row["name"] + " official locator",
            "url": row["source_url"],
            "attribution": row["name"],
            "license": None,
            "retrieved_at": row["retrieved_at"].isoformat(),
            "observed_at": None,
            "snapshot_at": None,
            "notes": [row["coverage_note"]],
        }
        for row in connection.execute(
            text(
                "SELECT id,name,source_url,retrieved_at,coverage_note"
                " FROM franchise_brands ORDER BY id"
            )
        ).mappings()
    ]
    return rows, sources, bool(sources)


def nearby_pois(
    connection: Connection, building: dict, radius: int, source: dict | None
) -> list[dict]:
    if source is None:
        return []
    return [
        dict(row)
        for row in connection.execute(
            text("""
        SELECT p.*,ST_X(p.geometry) AS x,ST_Y(p.geometry) AS y,
            ST_Distance(p.geometry,ST_SetSRID(ST_Point(:x,:y),2180)) AS distance,
            ST_Covers(selected.geometry,ST_Transform(p.geometry,3857)) AS inside_selected,
            matched.building_source_id
        FROM profile_pois p CROSS JOIN buildings selected
        CROSS JOIN LATERAL (
            SELECT CASE WHEN count(*)=1 THEN min(b.source_id) END AS building_source_id
            FROM buildings b WHERE b.geometry && ST_Transform(p.geometry,3857)
                AND ST_Covers(b.geometry,ST_Transform(p.geometry,3857))
        ) matched
        WHERE selected.id=:building_id
            AND ST_DWithin(p.geometry,ST_SetSRID(ST_Point(:x,:y),2180),:radius)
        ORDER BY p.source_id
    """),
            {
                "building_id": building["id"],
                "x": building["x"],
                "y": building["y"],
                "radius": radius,
            },
        ).mappings()
    ]


def venue_details(row: dict) -> dict:
    return {
        "name": row.get("name") or row.get("brand_name") or "Unnamed mapped venue",
        "distance_m": round(row["distance"], 1),
        "source_url": row["source_url"],
        "location_method": row["location_method"],
        "address": row.get("address"),
        "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
    }


def competition_profile(
    official: list[dict],
    official_sources: list[dict],
    official_ready: bool,
    pois: list[dict],
    poi_source: dict | None,
    building: dict,
    radius: int,
) -> dict:
    groups: dict[str, list[dict]] = {key: [] for key in COMPETITION_CATEGORIES}
    onsite = 0
    deduplicated = 0
    for row in official:
        if row["building_source_id"] == building["source_id"] or row["inside_selected"]:
            onsite += 1
            continue
        if row["x"] is None:
            continue
        distance = math.hypot(row["x"] - building["x"], row["y"] - building["y"])
        if distance <= radius and row["category_id"] in groups:
            groups[row["category_id"]].append({**row, "distance": distance, "origin": "official"})
    for row in pois:
        if row["kind"] != "competitor":
            continue
        if row["duplicate_of"]:
            deduplicated += 1
            continue
        matched_official = [
            outlet
            for outlet in official
            if row["brand_id"]
            and outlet["brand_id"] == row["brand_id"]
            and (
                (
                    row["building_source_id"] is not None
                    and outlet["building_source_id"] == row["building_source_id"]
                )
                or (
                    outlet["x"] is not None
                    and math.hypot(outlet["x"] - row["x"], outlet["y"] - row["y"]) <= 20
                )
            )
        ]
        if matched_official:
            deduplicated += 1
            if any(
                outlet["building_source_id"] == building["source_id"] or outlet["inside_selected"]
                for outlet in matched_official
            ):
                continue
        remaining_categories = set(row["categories"]) - {
            outlet["category_id"] for outlet in matched_official
        }
        if not remaining_categories:
            continue
        if row["inside_selected"]:
            onsite += 1
            continue
        for category in remaining_categories:
            groups[category].append({**row, "origin": "osm"})
    categories = []
    for category, name in COMPETITION_CATEGORIES.items():
        records = sorted(groups[category], key=lambda row: (row["distance"], row["source_url"]))
        categories.append(
            {
                "id": category,
                "name": name,
                "count": len(records) if official_ready or poi_source else None,
                "official_count": sum(row["origin"] == "official" for row in records)
                if official_ready
                else None,
                "osm_count": sum(row["origin"] == "osm" for row in records) if poi_source else None,
                "nearest": [venue_details(row) for row in records[:3]],
            }
        )
    return {
        "status": "ready"
        if official_ready and poi_source
        else "partial"
        if official_ready or poi_source
        else "unavailable",
        "categories": categories,
        "onsite_outlet_count": onsite,
        "deduplicated_count": deduplicated,
        "sources": official_sources + ([poi_source] if poi_source else []),
        "deduplication_method": "same_name_category_within_20m; "
        "official_same_brand_within_20m_or_same_building",
    }


def access_profile(
    connection: Connection, source: dict | None, building: dict, radius: int, pois: list[dict]
) -> dict:
    items = []
    for category, label in ACCESS_CATEGORIES.items():
        nearby = [
            row
            for row in pois
            if row["kind"] == "access"
            and category in row["categories"]
            and row["duplicate_of"] is None
        ]
        nearest = None
        if source:
            row = (
                connection.execute(
                    text("""
                SELECT name,source_url,location_method,updated_at,
                    ST_Distance(geometry,ST_SetSRID(ST_Point(:x,:y),2180)) AS distance
                FROM profile_pois WHERE kind='access' AND :category=ANY(categories)
                    AND duplicate_of IS NULL
                    AND ST_DWithin(geometry,ST_SetSRID(ST_Point(:x,:y),2180),:radius)
                ORDER BY geometry <-> ST_SetSRID(ST_Point(:x,:y),2180),source_id LIMIT 1
            """),
                    {
                        "x": building["x"],
                        "y": building["y"],
                        "category": category,
                        "radius": radius,
                    },
                )
                .mappings()
                .first()
            )
            if row is not None:
                nearest = venue_details(dict(row))
        items.append(
            {
                "id": category,
                "name": label,
                "count_within_radius": len(nearby) if source else None,
                "nearest": nearest,
            }
        )
    return {
        "status": "ready" if source else "unavailable",
        "method": "straight_line",
        "nearest_search_scope": "selected_radius",
        "items": items,
        "source": source,
    }


def get_profile(engine: Engine, building_id: int, radius: int) -> dict | None:
    if radius not in RADII:
        raise ValueError("Radius must be 300, 500 or 1000 metres")
    with engine.begin() as connection:
        connection.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
        connection.execute(text("SET LOCAL statement_timeout='10s'"))
        if not table_exists(connection, "buildings"):
            raise MapDataUnavailableError
        building = (
            connection.execute(
                text("""
            SELECT id,source_id,ST_X(origin) AS x,ST_Y(origin) AS y,
                ST_AsGeoJSON(ST_Transform(origin,4326))::json AS origin_geojson
            FROM (SELECT id,source_id,ST_PointOnSurface(ST_Transform(geometry,2180)) AS origin
                  FROM buildings WHERE id=:building_id) b
        """),
                {"building_id": building_id},
            )
            .mappings()
            .first()
        )
        if building is None:
            return None
        building = dict(building)
        has_profile_schema = table_exists(connection, "profile_sources")
        sources = {}
        revision = None
        if has_profile_schema:
            revision = connection.execute(
                text("SELECT revision FROM profile_state WHERE id=1")
            ).scalar_one()
            cached = connection.execute(
                text(
                    "SELECT payload FROM building_profile_cache WHERE building_source_id=:source_id"
                    " AND radius_m=:radius AND revision=:revision"
                    " AND schema_version=:schema_version"
                    " AND computed_at>now()-interval '15 minutes'"
                ),
                {
                    "source_id": building["source_id"],
                    "radius": radius,
                    "revision": revision,
                    "schema_version": PROFILE_SCHEMA_VERSION,
                },
            ).scalar_one_or_none()
            if cached is not None:
                return cached
            sources = {
                row["kind"]: row["metadata"]
                for row in connection.execute(
                    text("SELECT kind,metadata FROM profile_sources")
                ).mappings()
            }
        population = population_profile(connection, sources.get("population"), building, radius)
        official, official_sources, official_ready = official_outlets(connection, building)
        pois = nearby_pois(connection, building, radius, sources.get("pois"))
        competition = competition_profile(
            official, official_sources, official_ready, pois, sources.get("pois"), building, radius
        )
        access = access_profile(connection, sources.get("pois"), building, radius, pois)
        notes = [
            "Catchments are circular straight-line buffers from a point inside the footprint, "
            "not walking areas or an entrance.",
            "Building footprint area does not establish usable unit area or availability.",
            "Business counts combine selected official chains and mapped OSM venues; "
            "they are not a complete commercial census.",
            "Onsite records are excluded from nearby business counts; "
            "building associations are inferred.",
            "OSM source edits and snapshot dates do not verify current on-site trading or access.",
            "Business categories can overlap; a venue may count once in each applicable category.",
        ]
        if population["status"] == "partial":
            notes.append(
                "Population is an area-weighted subtotal for observed cells; "
                "missing or suppressed cells are not zero and are not extrapolated."
            )
        if population["status"] == "unavailable" and sources.get("population") is None:
            notes.append(
                "Population data has not been imported; no resident estimate is available."
            )
        elif population["status"] == "unavailable":
            notes.append(
                "No observed population cells intersect this catchment; "
                "the resident count is unknown, not zero."
            )
        if sources.get("pois") is None:
            notes.append(
                "Broader POI and access data has not been imported; "
                "selected-chain counts alone are partial."
            )
        result = {
            "building_id": building_id,
            "radius_m": radius,
            "available_radii_m": list(RADII),
            "origin": {
                "coordinates": building["origin_geojson"]["coordinates"],
                "method": "building_point_on_surface",
            },
            "computed_at": datetime.now(UTC).isoformat(),
            "schema_version": PROFILE_SCHEMA_VERSION,
            "snapshot_revision": revision,
            "context_status": "ready"
            if all(part["status"] == "ready" for part in (population, competition, access))
            else "partial",
            "population": population,
            "competition": competition,
            "access": access,
            "premises": {"status": "unknown", "unknown_fields": UNKNOWN_PREMISES},
            "quality_notes": notes,
        }
    if has_profile_schema:
        cache_profile(engine, building["source_id"], result)
    return result


def cache_profile(engine: Engine, source_id: str, result: dict) -> None:
    try:
        with engine.begin() as connection:
            connection.execute(text("SET LOCAL statement_timeout='2s'"))
            connection.execute(
                text("""
                INSERT INTO building_profile_cache(
                    building_source_id,radius_m,revision,schema_version,payload)
                SELECT :source_id,:radius,revision,:schema_version,CAST(:payload AS jsonb)
                FROM profile_state WHERE id=1 AND revision=:revision
                ON CONFLICT(building_source_id,radius_m) DO UPDATE SET
                    revision=EXCLUDED.revision,schema_version=EXCLUDED.schema_version,
                    computed_at=now(),payload=EXCLUDED.payload
                WHERE building_profile_cache.revision<=EXCLUDED.revision
            """),
                {
                    "source_id": source_id,
                    "radius": result["radius_m"],
                    "revision": result["snapshot_revision"],
                    "schema_version": PROFILE_SCHEMA_VERSION,
                    "payload": json.dumps(result, ensure_ascii=False),
                },
            )
    except SQLAlchemyError:
        logger.warning("Could not cache building profile; returning the computed result")
