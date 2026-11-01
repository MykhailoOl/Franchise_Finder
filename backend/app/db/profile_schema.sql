CREATE TABLE IF NOT EXISTS profile_sources (
    id text PRIMARY KEY,
    kind text NOT NULL UNIQUE CHECK(kind IN ('population','pois','activity')),
    metadata jsonb NOT NULL,
    imported_at timestamptz NOT NULL DEFAULT now(),
    feature_count integer NOT NULL CHECK(feature_count>=0)
);
ALTER TABLE profile_sources DROP CONSTRAINT IF EXISTS profile_sources_kind_check;
ALTER TABLE profile_sources ADD CONSTRAINT profile_sources_kind_check
    CHECK(kind IN ('population','pois','activity'));

CREATE TABLE IF NOT EXISTS profile_population (
    source_id text PRIMARY KEY,
    geometry geometry(MultiPolygon,2180) NOT NULL,
    population double precision CHECK(population>=0),
    status text NOT NULL CHECK(status IN ('observed','suppressed','missing')),
    observation_year integer NOT NULL,
    resolution_m integer NOT NULL CHECK(resolution_m>0),
    CHECK((status='observed' AND population IS NOT NULL) OR
          (status<>'observed' AND population IS NULL)),
    CHECK(ST_IsValid(geometry) AND NOT ST_IsEmpty(geometry))
);
CREATE INDEX IF NOT EXISTS profile_population_geometry_idx
    ON profile_population USING gist(geometry);

CREATE TABLE IF NOT EXISTS profile_pois (
    source_id text PRIMARY KEY,
    name text,
    normalized_name text NOT NULL,
    kind text NOT NULL CHECK(kind IN ('competitor','access')),
    categories text[] NOT NULL,
    brand_id text,
    geometry geometry(Point,2180) NOT NULL,
    source_url text NOT NULL,
    updated_at timestamptz,
    location_method text NOT NULL,
    tags jsonb NOT NULL,
    duplicate_of text
);
CREATE INDEX IF NOT EXISTS profile_pois_geometry_idx ON profile_pois USING gist(geometry);

CREATE TABLE IF NOT EXISTS profile_activity (
    source_id text PRIMARY KEY,
    name text,
    normalized_name text NOT NULL,
    kind text NOT NULL CHECK(kind='activity'),
    categories text[] NOT NULL,
    brand_id text,
    geometry geometry(Point,2180) NOT NULL,
    source_url text NOT NULL,
    updated_at timestamptz,
    location_method text NOT NULL,
    tags jsonb NOT NULL,
    duplicate_of text
);
CREATE INDEX IF NOT EXISTS profile_activity_geometry_idx ON profile_activity USING gist(geometry);

CREATE TABLE IF NOT EXISTS profile_state (
    id integer PRIMARY KEY CHECK(id=1),
    revision bigint NOT NULL
);
INSERT INTO profile_state VALUES(1,1) ON CONFLICT(id) DO NOTHING;

CREATE TABLE IF NOT EXISTS building_profile_cache (
    building_source_id text NOT NULL,
    radius_m integer NOT NULL CHECK(radius_m IN (300,500,1000)),
    revision bigint NOT NULL,
    schema_version text NOT NULL,
    computed_at timestamptz NOT NULL DEFAULT now(),
    payload jsonb NOT NULL,
    PRIMARY KEY(building_source_id,radius_m)
);

ALTER TABLE building_profile_cache ADD COLUMN IF NOT EXISTS schema_version text NOT NULL DEFAULT 'legacy';

CREATE OR REPLACE FUNCTION invalidate_building_profiles() RETURNS void
LANGUAGE sql AS $$
    UPDATE profile_state SET revision=revision+1 WHERE id=1;
    DELETE FROM building_profile_cache;
$$;
