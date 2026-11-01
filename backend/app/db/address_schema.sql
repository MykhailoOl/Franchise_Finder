CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS addresses (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id text NOT NULL UNIQUE,
    dataset_id text NOT NULL REFERENCES map_sources(id),
    street text NOT NULL,
    street_normalized text NOT NULL,
    house_number text NOT NULL,
    house_number_normalized text NOT NULL,
    postal_code text,
    city text NOT NULL,
    address text NOT NULL,
    geometry geometry(Point, 3857) NOT NULL,
    properties jsonb NOT NULL,
    imported_at timestamptz NOT NULL,
    building_source_id text REFERENCES buildings(source_id) ON DELETE SET NULL,
    match_status text NOT NULL DEFAULT 'unmatched'
        CHECK (match_status IN ('matched', 'unmatched', 'ambiguous')),
    match_method text,
    distance_m double precision,
    candidate_count integer NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS addresses_street_trgm ON addresses
    USING gin (street_normalized gin_trgm_ops);
CREATE INDEX IF NOT EXISTS addresses_street_prefix ON addresses
    (street_normalized text_pattern_ops);
CREATE INDEX IF NOT EXISTS addresses_house_street ON addresses
    (house_number_normalized, street_normalized);
CREATE INDEX IF NOT EXISTS addresses_postal_house ON addresses
    (postal_code, house_number_normalized);
CREATE INDEX IF NOT EXISTS addresses_geometry_gist ON addresses USING gist (geometry);
CREATE INDEX IF NOT EXISTS addresses_building_source ON addresses (building_source_id);

CREATE OR REPLACE FUNCTION refresh_address_building_links() RETURNS void
LANGUAGE sql AS $$
    WITH matches AS (
        SELECT addresses.id, candidates.candidate_count, candidates.building_source_id
        FROM addresses
        CROSS JOIN LATERAL (
            SELECT count(*) AS candidate_count, min(buildings.source_id) AS building_source_id
            FROM buildings
            WHERE buildings.dataset_id = 'warsaw-buildings'
                AND buildings.geometry && addresses.geometry
                AND ST_Covers(buildings.geometry, addresses.geometry)
        ) candidates
        WHERE addresses.dataset_id = 'warsaw-addresses'
    )
    UPDATE addresses SET
        building_source_id = CASE WHEN matches.candidate_count = 1
            THEN matches.building_source_id END,
        match_status = CASE WHEN matches.candidate_count = 1 THEN 'matched'
            WHEN matches.candidate_count > 1 THEN 'ambiguous' ELSE 'unmatched' END,
        match_method = CASE WHEN matches.candidate_count = 1 THEN 'point_inside_building' END,
        distance_m = CASE WHEN matches.candidate_count = 1 THEN 0 END,
        candidate_count = matches.candidate_count
    FROM matches WHERE addresses.id = matches.id;
$$;
