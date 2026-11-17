import math

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.core.grocery_matching_v2 import StreetCatalog, normalized_input

EVIDENCE_QUERY = """
WITH requests AS (
    SELECT * FROM jsonb_to_recordset(%(requests)s::jsonb)
    AS r(id bigint,street_keys text[],house_number text)
)
SELECT o.id,
    o.geometry IS NOT NULL AND NOT ST_Covers(
        ST_Transform(ST_MakeEnvelope(%(west)s,%(south)s,%(east)s,%(north)s,4326),3857),
        o.geometry
    ) AS outside_extent,
    coalesce(a.address_candidates,'[]'::jsonb) AS address_candidates,
    coalesce(p.point_buildings,'[]'::jsonb) AS point_buildings
FROM requests r JOIN franchise_outlets o ON o.id=r.id
CROSS JOIN LATERAL (
    SELECT jsonb_agg(jsonb_build_object(
        'source_id',a.source_id,'street',a.street,'house_number',a.house_number,
        'postal_code',a.postal_code,'city',a.city,
        'municipality_id',a.properties->>'ID_GMINY',
        'street_registry_id',a.properties->>'ID_ULICY',
        'building_source_id',b.source_id,'building_id',b.id,
        'address_match_status',a.match_status,
        'address_geometry_sha256',encode(sha256(ST_AsEWKB(a.geometry)),'hex'),
        'building_geometry_sha256',encode(sha256(ST_AsEWKB(b.geometry)),'hex'),
        'locator_covering_distance_m',(
            SELECT CASE WHEN count(*)=1 THEN min(ST_Distance(
                ST_Transform(a.geometry,2180),ST_Transform(covered.geometry,2180))) END
            FROM buildings covered WHERE covered.dataset_id=%(buildings)s
                AND covered.geometry && o.geometry AND ST_Covers(covered.geometry,o.geometry)
        ),
        'address_to_locator_distance_m',ST_Distance(
            ST_Transform(a.geometry,2180),ST_Transform(o.geometry,2180)
        ),
        'building_distance_m',ST_Distance(
            ST_Transform(o.geometry,2180),ST_Transform(b.geometry,2180)
        )
    ) ORDER BY a.source_id) AS address_candidates
    FROM addresses a LEFT JOIN buildings b
        ON b.source_id=a.building_source_id AND b.dataset_id=%(buildings)s
    WHERE a.dataset_id=%(addresses)s AND a.city=o.city
        AND a.street_normalized=ANY(r.street_keys)
        AND a.house_number_normalized=r.house_number
) a
CROSS JOIN LATERAL (
    SELECT jsonb_agg(jsonb_build_object('id',b.id,'source_id',b.source_id,
        'geometry_sha256',encode(sha256(ST_AsEWKB(b.geometry)),'hex'))
        ORDER BY b.source_id) AS point_buildings
    FROM buildings b WHERE b.dataset_id=%(buildings)s AND b.geometry && o.geometry
        AND ST_Covers(b.geometry,o.geometry)
) p
ORDER BY o.id
"""


def collect_matching_evidence(
    connection,
    *,
    building_dataset_id: str,
    address_dataset_id: str,
    bounds: list[float],
    brand_ids: tuple[str, ...] = ("zabka", "lewiatan"),
) -> list[dict]:
    if (
        len(bounds) != 4
        or not all(math.isfinite(value) for value in bounds)
        or not -180 <= bounds[0] < bounds[2] <= 180
        or not -85 <= bounds[1] < bounds[3] <= 85
    ):
        raise ValueError("Matching requires valid EPSG:4326 dataset bounds")
    with connection.cursor(row_factory=dict_row) as cursor:
        sources = cursor.execute(
            "SELECT id,name,url,attribution,updated_at::text,imported_at::text,feature_count"
            " FROM map_sources WHERE id=ANY(%s) ORDER BY id",
            ([building_dataset_id, address_dataset_id],),
        ).fetchall()
        if {row["id"] for row in sources} != {building_dataset_id, address_dataset_id}:
            raise ValueError("Both municipal source datasets must exist")
        streets = cursor.execute(
            "SELECT DISTINCT city,street,street_normalized FROM addresses"
            " WHERE dataset_id=%s ORDER BY city,street,street_normalized",
            (address_dataset_id,),
        ).fetchall()
        catalog = StreetCatalog(streets)
        outlets = cursor.execute(
            "SELECT id,brand_id,source_id,address_raw,street,house_number,postal_code,city,"
            " geometry IS NOT NULL AS has_coordinates,source_url,retrieved_at::text,"
            " ST_AsGeoJSON(ST_Transform(geometry,4326))::jsonb->'coordinates' AS coordinates,"
            " match_status AS previous_match_status,building_source_id AS"
            " previous_building_source_id FROM franchise_outlets WHERE brand_id=ANY(%s)"
            " ORDER BY brand_id,source_id",
            (list(brand_ids),),
        ).fetchall()
        result = []
        for start in range(0, len(outlets), 256):
            batch = outlets[start : start + 256]
            prepared = {
                outlet["id"]: {
                    "outlet": outlet,
                    "normalized_input": normalized_input(outlet),
                    "street_resolution": catalog.resolve(outlet["street"], outlet["city"]),
                    "source_references": {
                        "municipal": sources,
                        "locator": {
                            "url": outlet["source_url"],
                            "retrieved_at": outlet["retrieved_at"],
                        },
                        "bounds": bounds,
                        "bounds_semantics": "declared_dataset_extent_not_administrative_boundary",
                    },
                }
                for outlet in batch
            }
            requests = [
                {
                    "id": key,
                    "street_keys": value["street_resolution"]["stored_keys"],
                    "house_number": value["normalized_input"]["house_number"],
                }
                for key, value in prepared.items()
            ]
            parameters = dict(
                zip(("west", "south", "east", "north"), bounds, strict=True),
                requests=Jsonb(requests),
                buildings=building_dataset_id,
                addresses=address_dataset_id,
            )
            for row in cursor.execute(EVIDENCE_QUERY, parameters).fetchall():
                result.append({**prepared[row.pop("id")], **row})
    return sorted(result, key=lambda row: (row["outlet"]["brand_id"], row["outlet"]["source_id"]))
