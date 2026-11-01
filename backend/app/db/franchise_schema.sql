CREATE TABLE IF NOT EXISTS franchise_categories (
    id text PRIMARY KEY,
    name text NOT NULL
);

CREATE TABLE IF NOT EXISTS franchise_brands (
    id text PRIMARY KEY,
    name text NOT NULL,
    category_id text NOT NULL REFERENCES franchise_categories(id),
    website text NOT NULL,
    source_url text NOT NULL,
    retrieved_at timestamptz NOT NULL,
    coverage_status text NOT NULL CHECK (coverage_status IN ('complete','partial','unavailable')),
    coverage_note text NOT NULL,
    provenance jsonb NOT NULL
);

CREATE TABLE IF NOT EXISTS franchise_outlets (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    brand_id text NOT NULL REFERENCES franchise_brands(id),
    source_id text NOT NULL,
    name text,
    address_raw text NOT NULL,
    street text,
    street_normalized text,
    house_number text,
    house_number_normalized text,
    postal_code text,
    city text NOT NULL,
    geometry geometry(Point,3857),
    display_geometry geometry(Point,3857),
    point_source text,
    source_url text NOT NULL,
    retrieved_at timestamptz NOT NULL,
    status text NOT NULL CHECK (status IN ('listed','open','temporarily_closed','unknown')),
    operating_model text NOT NULL CHECK (operating_model IN ('unknown','franchise','company_owned')),
    operating_model_source_url text,
    attributes jsonb NOT NULL,
    building_source_id text REFERENCES buildings(source_id) ON DELETE SET NULL,
    address_source_id text REFERENCES addresses(source_id) ON DELETE SET NULL,
    match_status text NOT NULL DEFAULT 'unmatched'
        CHECK (match_status IN ('matched','unmatched','ambiguous','conflict')),
    match_method text,
    match_distance_m double precision,
    match_note text,
    candidate_building_ids bigint[] NOT NULL DEFAULT '{}',
    UNIQUE(brand_id,source_id)
);

ALTER TABLE franchise_outlets ADD COLUMN IF NOT EXISTS match_distance_m double precision;

CREATE INDEX IF NOT EXISTS franchise_outlets_building_idx
    ON franchise_outlets(building_source_id);
CREATE INDEX IF NOT EXISTS franchise_outlets_geometry_idx
    ON franchise_outlets USING gist(geometry);

CREATE TABLE IF NOT EXISTS building_franchise_summary (
    building_source_id text PRIMARY KEY REFERENCES buildings(source_id) ON DELETE CASCADE,
    outlet_count integer NOT NULL,
    brand_ids text[] NOT NULL,
    brand_flags jsonb NOT NULL
);

CREATE OR REPLACE FUNCTION refresh_franchise_links() RETURNS void
LANGUAGE sql AS $$
    WITH candidates AS (
        SELECT o.id,o.geometry,
            a.address_count,a.linked_address_count,a.address_building_count,
            a.address_buildings,a.address_building_ids,a.address_source_id,a.address_geometry,
            ST_Distance(ST_Transform(o.geometry,2180),
                ST_Transform(a.address_building_geometry,2180)) AS address_distance_m,
            p.point_count,p.point_building,p.point_building_ids,
            o.geometry IS NOT NULL AND NOT ST_Covers(
                ST_Transform(ST_MakeEnvelope(20.85,52.09,21.29,52.38,4326),3857),o.geometry
            ) AS outside_warsaw
        FROM franchise_outlets o
        CROSS JOIN LATERAL (
            SELECT count(*) AS address_count,
                count(a.building_source_id) AS linked_address_count,
                count(DISTINCT a.building_source_id) AS address_building_count,
                coalesce(array_agg(DISTINCT a.building_source_id)
                    FILTER(WHERE a.building_source_id IS NOT NULL),'{}'::text[]) AS address_buildings,
                coalesce(array_agg(DISTINCT b.id) FILTER(WHERE b.id IS NOT NULL),
                    '{}'::bigint[]) AS address_building_ids,
                CASE WHEN count(*)=1 THEN min(a.source_id) END AS address_source_id,
                CASE WHEN count(*)=1 THEN (array_agg(a.geometry))[1] END AS address_geometry,
                CASE WHEN count(*)=1 THEN (array_agg(b.geometry))[1] END AS address_building_geometry
            FROM addresses a LEFT JOIN buildings b ON b.source_id=a.building_source_id
            WHERE a.dataset_id='warsaw-addresses'
                AND a.street_normalized=o.street_normalized
                AND a.house_number_normalized=o.house_number_normalized
                AND (o.postal_code IS NULL OR a.postal_code=o.postal_code)
        ) a
        CROSS JOIN LATERAL (
            SELECT count(*) AS point_count,min(b.source_id) AS point_building,
                coalesce(array_agg(b.id),'{}'::bigint[]) AS point_building_ids
            FROM buildings b WHERE b.dataset_id='warsaw-buildings'
                AND b.geometry && o.geometry AND ST_Covers(b.geometry,o.geometry)
        ) p
    ), decisions AS (
        SELECT c.*,
            CASE
                WHEN outside_warsaw THEN 'conflict'
                WHEN geometry IS NULL AND address_count=1
                    AND address_building_count=1 AND linked_address_count=address_count THEN 'matched'
                WHEN geometry IS NULL AND address_count>1 THEN 'ambiguous'
                WHEN geometry IS NULL THEN 'unmatched'
                WHEN address_count=1 AND address_building_count=1 AND point_count=0
                    AND address_distance_m<=10 THEN 'matched'
                WHEN address_count>0 AND address_building_count>0 AND
                    (point_count=0 OR NOT(point_building_ids && address_building_ids)) THEN 'conflict'
                WHEN point_count>1 THEN 'ambiguous'
                WHEN point_count=1 AND address_count=0 THEN 'matched'
                WHEN point_count=1 AND point_building=ANY(address_buildings) THEN 'matched'
                WHEN point_count=1 AND address_count>0 THEN 'conflict'
                WHEN address_count>1 THEN 'ambiguous'
                ELSE 'unmatched'
            END AS resolved_status
        FROM candidates c
    ), resolved AS (
        SELECT d.*,
            CASE WHEN resolved_status='matched' THEN
                CASE WHEN geometry IS NULL OR point_count=0 THEN address_buildings[1]
                    ELSE point_building END
            END AS resolved_building,
            ARRAY(SELECT DISTINCT unnest(address_building_ids || point_building_ids)
                ORDER BY 1) AS all_candidates
        FROM decisions d
    )
    UPDATE franchise_outlets o SET
        building_source_id=r.resolved_building,
        address_source_id=r.address_source_id,
        match_status=r.resolved_status,
        match_method=CASE WHEN r.resolved_status='matched' THEN
            CASE WHEN r.geometry IS NULL THEN 'exact_address'
                WHEN r.point_count=0 THEN 'exact_address_near_point'
                WHEN r.address_count>0 THEN 'address_and_point' ELSE 'point_inside_building' END
            END,
        match_distance_m=CASE WHEN r.resolved_status='matched' AND r.geometry IS NOT NULL
            THEN CASE WHEN r.point_count=0 THEN r.address_distance_m ELSE 0 END END,
        match_note=CASE WHEN r.outside_warsaw THEN 'Official coordinates fall outside the supported Warsaw map extent.'
            WHEN r.resolved_status='conflict' THEN 'Official address and coordinates do not identify the same footprint.'
            WHEN r.resolved_status='ambiguous' THEN 'The available evidence identifies multiple possible buildings.'
            WHEN r.resolved_status='unmatched' THEN 'The available evidence does not identify a building uniquely.'
            WHEN r.geometry IS NOT NULL AND r.point_count=0 THEN
                'Inferred from one exact municipal address; official locator point is within 10 m of its footprint and covers no alternative building.'
            ELSE 'Building association inferred from official locator and municipal geometry.' END,
        candidate_building_ids=r.all_candidates,
        display_geometry=coalesce(r.geometry,r.address_geometry,
            CASE WHEN r.resolved_building IS NOT NULL THEN (
                SELECT ST_PointOnSurface(b.geometry) FROM buildings b
                WHERE b.source_id=r.resolved_building
            ) END),
        point_source=CASE WHEN r.geometry IS NOT NULL THEN 'official_locator'
            WHEN r.address_geometry IS NOT NULL THEN 'municipal_address'
            WHEN r.resolved_building IS NOT NULL THEN 'building_representative_point' END
    FROM resolved r WHERE o.id=r.id;

    DELETE FROM building_franchise_summary;
    INSERT INTO building_franchise_summary(building_source_id,outlet_count,brand_ids,brand_flags)
    SELECT building_source_id,count(*)::integer,array_agg(DISTINCT brand_id ORDER BY brand_id),
        jsonb_object_agg('brand_' || brand_id,true)
    FROM franchise_outlets WHERE match_status='matched' AND building_source_id IS NOT NULL
    GROUP BY building_source_id;
$$;
