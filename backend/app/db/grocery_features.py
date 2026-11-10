import math

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.core.data_contract import BUILDING_USE_CODES

SOURCE_IDS = (
    "warsaw-buildings",
    "warsaw-population-2021",
    "warsaw-osm-pois",
    "warsaw-osm-activity",
)
RADII = (300, 500, 1000)

RAW_FEATURE_QUERY = """
WITH selected AS MATERIALIZED (
    SELECT source_id,geometry AS footprint_3857,ST_Transform(geometry,2180) AS footprint,
        ST_PointOnSurface(ST_Transform(geometry,2180)) AS origin,area_m2,building_type,
        properties->'KONDYGNACJE_NADZIEMNE' AS storeys
    FROM buildings WHERE dataset_id='warsaw-buildings' AND source_id=ANY(%(ids)s)
        AND ST_IsValid(geometry) AND NOT ST_IsEmpty(geometry)
), buffers AS MATERIALIZED (
    SELECT s.source_id,radius,ST_Buffer(s.origin,radius,64) AS geometry
    FROM selected s CROSS JOIN unnest(ARRAY[300,500,1000]) AS radius
), bounds AS MATERIALIZED (
    SELECT source_id,CASE WHEN bounds IS NOT NULL THEN ST_Transform(ST_Segmentize(
        ST_MakeEnvelope((bounds->>0)::float8,(bounds->>1)::float8,
            (bounds->>2)::float8,(bounds->>3)::float8,4326),0.001),2180) END AS geometry
    FROM jsonb_to_recordset(%(sources)s::jsonb) AS s(source_id text,bounds jsonb)
), population_cells AS MATERIALIZED (
    SELECT b.source_id,b.radius,p.source_id AS cell_source_id,p.population,p.status,
        CASE WHEN ST_Covers(b.geometry,p.geometry) THEN ST_Area(p.geometry)
            WHEN ST_Covers(p.geometry,b.geometry) THEN ST_Area(b.geometry)
            ELSE ST_Area(ST_Intersection(p.geometry,b.geometry)) END AS overlap_area,
        ST_Area(p.geometry) AS cell_area
    FROM buffers b JOIN profile_population p ON p.geometry && b.geometry
        AND ST_Intersects(p.geometry,b.geometry)
    WHERE %(population_available)s
), population AS (
    SELECT b.source_id,b.radius,
        sum(p.population*p.overlap_area/p.cell_area ORDER BY p.cell_source_id)
            FILTER(WHERE p.status='observed' AND p.overlap_area>0) AS subtotal,
        coalesce(sum(p.overlap_area ORDER BY p.cell_source_id)
            FILTER(WHERE p.status='observed' AND p.overlap_area>0)
            /ST_Area(b.geometry),0) AS published_fraction,
        count(*) FILTER(WHERE p.overlap_area>0) AS total,
        count(*) FILTER(WHERE p.status='observed' AND p.overlap_area>0) AS observed,
        count(*) FILTER(WHERE p.status='missing' AND p.overlap_area>0) AS missing,
        count(*) FILTER(WHERE p.status='suppressed' AND p.overlap_area>0) AS suppressed
    FROM buffers b LEFT JOIN population_cells p ON p.source_id=b.source_id AND p.radius=b.radius
    GROUP BY b.source_id,b.radius,b.geometry
), population_json AS (
    SELECT source_id,jsonb_object_agg(radius,jsonb_build_object('subtotal',subtotal,
        'published_fraction',published_fraction,'total',total,'observed',observed,
        'missing',missing,'suppressed',suppressed)) AS values
    FROM population GROUP BY source_id
), source_coverage AS (
    SELECT b.source_id,s.source_id AS dataset_id,b.radius,
        CASE WHEN s.geometry IS NULL THEN NULL
            WHEN ST_Covers(s.geometry,b.geometry) THEN 1.0
            WHEN NOT ST_Intersects(s.geometry,b.geometry) THEN 0.0
            ELSE ST_Area(ST_Intersection(s.geometry,b.geometry))/ST_Area(b.geometry)
        END AS fraction
    FROM buffers b CROSS JOIN bounds s
), coverage_by_source AS (
    SELECT source_id,dataset_id,jsonb_object_agg(radius,fraction) AS fractions
    FROM source_coverage GROUP BY source_id,dataset_id
), coverage_json AS (
    SELECT source_id,jsonb_object_agg(dataset_id,fractions) AS values
    FROM coverage_by_source GROUP BY source_id
), origins_json AS (
    SELECT s.source_id,
        jsonb_object_agg(b.source_id,ST_Covers(b.geometry,s.origin)) AS origins,
        jsonb_object_agg(b.source_id,ST_Covers(b.geometry,s.footprint)) AS footprints
    FROM selected s CROSS JOIN bounds b GROUP BY s.source_id
)
SELECT s.source_id AS building_source_id,s.area_m2 AS building_footprint_area_m2,
    s.building_type AS mapped_building_use,s.storeys AS mapped_storeys_raw,
    p.values AS population,c.values AS source_coverage_fraction_by_radius,
    o.origins AS source_origin_inside_bounds_by_dataset,
    o.footprints AS source_building_within_bounds_by_dataset,
    access.*,activity.*,residential.*
FROM selected s JOIN population_json p USING(source_id)
    JOIN coverage_json c USING(source_id) JOIN origins_json o USING(source_id)
CROSS JOIN LATERAL (
    SELECT min(ST_Distance(p.geometry,s.origin)) FILTER(WHERE 'bus_stop'=ANY(p.categories))
            AS nearest_bus_stop_distance_m,
        min(ST_Distance(p.geometry,s.origin)) FILTER(WHERE 'tram_stop'=ANY(p.categories))
            AS nearest_tram_stop_distance_m,
        min(ST_Distance(p.geometry,s.origin)) FILTER(WHERE 'rail_station'=ANY(p.categories))
            AS nearest_rail_station_distance_m,
        min(ST_Distance(p.geometry,s.origin)) FILTER(WHERE 'metro_entrance'=ANY(p.categories))
            AS nearest_metro_entrance_distance_m,
        count(*) FILTER(WHERE 'bus_stop'=ANY(p.categories) AND ST_DWithin(p.geometry,s.origin,500))
            AS bus_stop_count_500m,
        count(*) FILTER(WHERE 'parking'=ANY(p.categories) AND ST_DWithin(p.geometry,s.origin,500))
            AS parking_count_500m
    FROM profile_pois p WHERE p.kind='access' AND p.duplicate_of IS NULL
        AND ST_DWithin(p.geometry,s.origin,1000) AND %(pois_available)s
) access
CROSS JOIN LATERAL (
    WITH nearby AS MATERIALIZED (
        SELECT p.categories,p.tags,ST_Covers(s.footprint,p.geometry) AS onsite,
            EXISTS(SELECT 1 FROM jsonb_each_text(p.tags) AS tag(key,value) WHERE
                (tag.key=ANY(ARRAY['closed','disused','abandoned','demolished','razed','removed'])
                    AND lower(tag.value)=ANY(ARRAY['yes','true','1']))
                OR (tag.key=ANY(ARRAY['amenity','office','shop'])
                    AND lower(tag.value)=ANY(ARRAY['closed','disused','abandoned','vacant','no']))
                OR (tag.key ~ ('^(closed|disused|abandoned|demolished|razed|removed):'
                    || '(amenity|office|shop)$')
                    AND lower(tag.value) NOT IN ('','no','false','0'))
            ) AS inactive
        FROM profile_activity p WHERE p.kind='activity' AND p.duplicate_of IS NULL
            AND ST_DWithin(p.geometry,s.origin,500) AND %(activity_available)s
    ) SELECT count(*) FILTER(WHERE NOT onsite AND NOT inactive AND 'school'=ANY(categories))
            AS school_count_500m,
        count(*) FILTER(WHERE NOT onsite AND NOT inactive AND 'university'=ANY(categories))
            AS university_count_500m,
        count(*) FILTER(WHERE NOT onsite AND NOT inactive AND 'office'=ANY(categories))
            AS office_count_500m,
        count(*) FILTER(WHERE NOT onsite AND NOT inactive AND 'retail_destination'=ANY(categories))
            AS retail_destination_count_500m,
        count(*) FILTER(WHERE onsite) AS activity_excluded_onsite_count_500m,
        count(*) FILTER(WHERE NOT onsite AND inactive) AS activity_excluded_inactive_count_500m,
        count(*) FILTER(WHERE NOT onsite AND NOT inactive AND coalesce(tags->>'end_date','')<>'')
            AS activity_lifecycle_ambiguous_count_500m
    FROM nearby
) activity
CROSS JOIN LATERAL (
    SELECT count(*) AS neighbor_building_count_500m,
        count(*) FILTER(WHERE b.building_type=ANY(%(known_uses)s)) AS known_use_building_count_500m,
        count(*) FILTER(WHERE b.building_type='m') AS residential_building_count_500m,
        (count(*) FILTER(WHERE b.building_type='m'))::float8/
            nullif(count(*) FILTER(WHERE b.building_type=ANY(%(known_uses)s)),0)
            AS residential_building_fraction_500m,
        (count(*) FILTER(WHERE b.building_type=ANY(%(known_uses)s)))::float8/nullif(count(*),0)
            AS mapped_building_use_coverage_fraction_500m
    FROM buildings b WHERE b.dataset_id='warsaw-buildings' AND b.source_id<>s.source_id
        AND b.geometry && ST_Transform(ST_Expand(s.origin,501),3857)
        AND ST_DWithin(CASE WHEN ST_IsValid(b.geometry) AND NOT ST_IsEmpty(b.geometry)
            THEN ST_PointOnSurface(ST_Transform(b.geometry,2180)) END,s.origin,500)
) residential
ORDER BY s.source_id
"""


def valid_bounds(value: object) -> list[float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        west, south, east, north = [float(number) for number in value]
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(number) for number in (west, south, east, north)):
        return None
    if not (-180 <= west < east <= 180 and -85 <= south < north <= 85):
        return None
    return [west, south, east, north]


def extract_raw_features(
    connection: psycopg.Connection, building_source_ids: list[str]
) -> list[dict]:
    with connection.cursor(row_factory=dict_row) as cursor:
        modes = cursor.execute(
            "SELECT current_setting('transaction_read_only') AS read_only,"
            " current_setting('transaction_isolation') AS isolation"
        ).fetchone()
        if modes != {"read_only": "on", "isolation": "repeatable read"}:
            raise ValueError("Feature extraction requires a read-only repeatable-read transaction")
        if any(
            not isinstance(identifier, str) or not identifier for identifier in building_source_ids
        ):
            raise ValueError("Building source IDs must be nonempty strings")
        identifiers = sorted(set(building_source_ids))
        if not identifiers:
            return []
        sources = {source_id: {"source_id": source_id, "bounds": None} for source_id in SOURCE_IDS}
        available = dict.fromkeys(SOURCE_IDS, False)
        for source in cursor.execute(
            "SELECT id,bounds,feature_count FROM map_sources WHERE id='warsaw-buildings'"
        ).fetchall():
            sources[source["id"]]["bounds"] = valid_bounds(source["bounds"])
            available[source["id"]] = source["feature_count"] > 0
        for source in cursor.execute(
            "SELECT id,metadata,feature_count FROM profile_sources WHERE id=ANY(%s)",
            (list(SOURCE_IDS[1:]),),
        ).fetchall():
            sources[source["id"]]["bounds"] = valid_bounds(
                source["metadata"].get("coverage_bounds")
            )
            available[source["id"]] = source["feature_count"] > 0
        rows = cursor.execute(
            RAW_FEATURE_QUERY,
            {
                "ids": identifiers,
                "sources": Jsonb(list(sources.values())),
                "known_uses": sorted(BUILDING_USE_CODES),
                "population_available": available["warsaw-population-2021"],
                "pois_available": available["warsaw-osm-pois"],
                "activity_available": available["warsaw-osm-activity"],
            },
        ).fetchall()
    returned = {row["building_source_id"] for row in rows}
    if returned != set(identifiers):
        raise ValueError(
            f"Missing or invalid municipal buildings: {sorted(set(identifiers) - returned)}"
        )
    for row in rows:
        row["source_available_by_dataset"] = available.copy()
        row["source_coverage_method"] = "declared_source_bbox"
        population = row.pop("population")
        for radius in RADII:
            item = population[str(radius)]
            fraction = item["published_fraction"]
            if not 0 <= fraction <= 1.00000001:
                raise ValueError("Population cell overlap exceeds the radial catchment area")
            has_population = available["warsaw-population-2021"]
            row[f"resident_subtotal_{radius}m"] = item["subtotal"] if has_population else None
            row[f"published_cell_area_fraction_{radius}m"] = (
                min(fraction, 1.0) if has_population else None
            )
            row[f"population_cells_{radius}m"] = {
                key: item[key] if has_population else None
                for key in ("observed", "missing", "suppressed", "total")
            }
        if not available["warsaw-osm-pois"]:
            for key in (
                "nearest_bus_stop_distance_m",
                "nearest_tram_stop_distance_m",
                "nearest_rail_station_distance_m",
                "nearest_metro_entrance_distance_m",
                "bus_stop_count_500m",
                "parking_count_500m",
            ):
                row[key] = None
        if not available["warsaw-osm-activity"]:
            for key in (
                "school_count_500m",
                "university_count_500m",
                "office_count_500m",
                "retail_destination_count_500m",
                "activity_excluded_onsite_count_500m",
                "activity_excluded_inactive_count_500m",
                "activity_lifecycle_ambiguous_count_500m",
            ):
                row[key] = None
        if not available["warsaw-buildings"]:
            for key in (
                "building_footprint_area_m2",
                "mapped_building_use",
                "mapped_storeys_raw",
                "neighbor_building_count_500m",
                "known_use_building_count_500m",
                "residential_building_count_500m",
                "residential_building_fraction_500m",
                "mapped_building_use_coverage_fraction_500m",
            ):
                row[key] = None
    return rows
