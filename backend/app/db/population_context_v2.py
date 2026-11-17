from sqlalchemy import text

VERSION = "gus-population-context-v2"
SOURCE_ID = "warsaw-gus-national-population-500m-2021-v2"
SCHEMA = """
CREATE TABLE IF NOT EXISTS population_context_sources (
    id text PRIMARY KEY,
    metadata jsonb NOT NULL,
    imported_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS population_context_cells (
    source_id text NOT NULL REFERENCES population_context_sources(id),
    cell_id text NOT NULL,
    easting integer NOT NULL CHECK(mod(easting,500)=0),
    northing integer NOT NULL CHECK(mod(northing,500)=0),
    population integer CHECK(population>=0),
    status text NOT NULL CHECK(status IN ('observed','missing')),
    PRIMARY KEY(source_id,cell_id),
    UNIQUE(source_id,easting,northing),
    CHECK((status='observed' AND population IS NOT NULL) OR
        (status='missing' AND population IS NULL))
);
"""


def population_context(connection, building, radius=None):
    tables = connection.execute(
        text(
            "SELECT count(*)=2 FROM pg_tables WHERE schemaname=current_schema() AND "
            "tablename IN ('population_context_sources','population_context_cells')"
        )
    ).scalar_one()
    if not tables:
        return None
    source = connection.execute(
        text("SELECT metadata FROM population_context_sources WHERE id=:source_id"),
        {"source_id": SOURCE_ID},
    ).scalar_one_or_none()
    if source is None:
        return None
    cell = (
        connection.execute(
            text(
                "WITH origin AS (SELECT ST_Transform(ST_SetSRID(ST_MakePoint(:x,:y),2180),3035) p) "
                "SELECT c.cell_id,c.population,c.status FROM population_context_cells c,origin o "
                "WHERE c.source_id=:source_id AND c.easting=floor(ST_X(o.p)/500)*500 "
                "AND c.northing=floor(ST_Y(o.p)/500)*500"
            ),
            {"x": building["x"], "y": building["y"], "source_id": SOURCE_ID},
        )
        .mappings()
        .one_or_none()
    )
    available = cell is not None and cell["status"] == "observed"
    return {
        "version": VERSION,
        "status": "available" if available else "unavailable",
        "scope": "containing_census_cell",
        "population": cell["population"] if available else None,
        "resolution_m": 500,
        "observation_year": 2021,
        "cell_id": cell["cell_id"] if cell else None,
        "source": {
            key: source.get(key)
            for key in (
                "id",
                "name",
                "url",
                "attribution",
                "retrieved_at",
                "observed_at",
                "source_published_at",
            )
        },
        "method": "official_national_population_grid",
    }
