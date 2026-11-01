CREATE TABLE IF NOT EXISTS map_sources (
    id text PRIMARY KEY,
    name text NOT NULL,
    url text NOT NULL,
    attribution text NOT NULL,
    updated_at timestamptz,
    imported_at timestamptz NOT NULL,
    feature_count bigint NOT NULL CHECK (feature_count >= 0),
    bounds double precision[] NOT NULL CHECK (array_length(bounds, 1) = 4)
);

CREATE TABLE IF NOT EXISTS buildings (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id text NOT NULL UNIQUE,
    dataset_id text NOT NULL REFERENCES map_sources(id),
    geometry geometry(MultiPolygon, 3857) NOT NULL,
    properties jsonb NOT NULL,
    building_type text,
    address text,
    area_m2 double precision GENERATED ALWAYS AS (
        ST_Area(ST_Transform(geometry, 2180))
    ) STORED,
    imported_at timestamptz NOT NULL,
    CHECK (NOT ST_IsEmpty(geometry))
);

CREATE INDEX IF NOT EXISTS buildings_geometry_gist ON buildings USING gist (geometry);
CREATE INDEX IF NOT EXISTS buildings_dataset_id_idx ON buildings (dataset_id);
