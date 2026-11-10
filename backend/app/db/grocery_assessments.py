from collections import OrderedDict
from copy import deepcopy
from datetime import UTC, datetime
from threading import RLock
from time import monotonic

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from sqlalchemy import Engine, text

from app.core.brand_requirements import REQUIREMENTS_VERSION
from app.core.data_contract import BUILDING_USE_CODES, DATA_CONTRACT_VERSION, PREDICTORS
from app.core.experiment import content_hash
from app.core.grocery_assessment import (
    ASSESSMENT_VERSION,
    build_assessment,
    build_live_source_context,
)
from app.core.grocery_features import EXTRACTION_VERSION, build_feature_row
from app.db.buildings import MapDataUnavailableError
from app.db.grocery_features import RADII, RAW_FEATURE_QUERY, extract_raw_features, valid_bounds

CACHE_TTL_SECONDS = 300
CACHE_MAX_ENTRIES = 256
_CACHE = OrderedDict()
_CACHE_LOCK = RLock()
TABLES = (
    "buildings",
    "map_sources",
    "profile_sources",
    "profile_population",
    "profile_pois",
    "profile_activity",
    "profile_state",
)
EMPTY_POPULATION = (
    "SELECT NULL::text AS source_id,NULL::geometry AS geometry,NULL::float8 AS population,"
    "NULL::text AS status WHERE false"
)
EMPTY_POIS = (
    "SELECT NULL::text AS kind,NULL::text AS duplicate_of,NULL::geometry AS geometry,"
    "NULL::text[] AS categories,'{}'::jsonb AS tags WHERE false"
)


def read_snapshot(connection) -> dict:
    with connection.cursor(row_factory=dict_row) as cursor:
        tables = {
            table: cursor.execute(
                "SELECT to_regclass(format('%%I.%%I',current_schema(),%s::text))"
                " IS NOT NULL AS found",
                (table,),
            ).fetchone()["found"]
            for table in TABLES
        }
        if not tables["buildings"]:
            raise MapDataUnavailableError
        metadata = {
            table: cursor.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
            if tables[table]
            else []
            for table in ("map_sources", "profile_sources")
        }
        state = (
            cursor.execute("SELECT revision FROM profile_state WHERE id=1").fetchone()
            if tables["profile_state"]
            else None
        )
    snapshot = {
        "tables": tables,
        "source_metadata": metadata,
        "profile_revision": state["revision"] if state else None,
    }
    snapshot["source_revision"] = content_hash(snapshot)
    return snapshot


def extract_optional_features(connection, source_id: str, snapshot: dict, context: dict) -> dict:
    if all(snapshot["tables"][table] for table in TABLES if table != "profile_state"):
        return mask_unavailable(extract_raw_features(connection, [source_id])[0], context)
    query = RAW_FEATURE_QUERY
    empty_relations = []
    for table in ("profile_population", "profile_pois", "profile_activity"):
        if not snapshot["tables"][table]:
            statement = EMPTY_POPULATION if table == "profile_population" else EMPTY_POIS
            empty_relations.append(f"{table} AS ({statement})")
    if empty_relations:
        query = query.replace(
            "WITH selected", "WITH " + ",".join(empty_relations) + ", selected", 1
        )
    available = {name: source["available"] for name, source in context["sources"].items()}
    sources = [
        {"source_id": name, "bounds": valid_bounds(source["coverage_bounds"])}
        for name, source in context["sources"].items()
    ]
    with connection.cursor(row_factory=dict_row) as cursor:
        row = cursor.execute(
            query,
            {
                "ids": [source_id],
                "sources": Jsonb(sources),
                "known_uses": sorted(BUILDING_USE_CODES),
                "population_available": available["warsaw-population-2021"],
                "pois_available": available["warsaw-osm-pois"],
                "activity_available": available["warsaw-osm-activity"],
            },
        ).fetchone()
    if row is None:
        raise ValueError("The selected municipal footprint is empty or invalid")
    row["source_available_by_dataset"] = available
    row["source_coverage_method"] = "declared_source_bbox"
    population = row.pop("population")
    for radius in RADII:
        item = population[str(radius)]
        if not 0 <= item["published_fraction"] <= 1.00000001:
            raise ValueError("Population cell overlap exceeds the radial catchment area")
        present = available["warsaw-population-2021"]
        row[f"resident_subtotal_{radius}m"] = item["subtotal"] if present else None
        row[f"published_cell_area_fraction_{radius}m"] = (
            min(item["published_fraction"], 1.0) if present else None
        )
        row[f"population_cells_{radius}m"] = {
            key: item[key] if present else None
            for key in ("observed", "missing", "suppressed", "total")
        }
    return mask_unavailable(row, context)


def mask_unavailable(row: dict, context: dict) -> dict:
    available = {
        name: present and context["sources"][name]["available"]
        for name, present in row["source_available_by_dataset"].items()
    }
    row["source_available_by_dataset"] = available
    if not available["warsaw-population-2021"]:
        for radius in RADII:
            row[f"published_cell_area_fraction_{radius}m"] = None
            row[f"population_cells_{radius}m"] = dict.fromkeys(
                ("observed", "missing", "suppressed", "total")
            )
    for name, definition in PREDICTORS.items():
        if definition.sources[0] in available and not available[definition.sources[0]]:
            row["mapped_storeys_raw" if name == "mapped_storeys" else name] = None
    for source_id, fields in (
        (
            "warsaw-osm-activity",
            (
                "activity_excluded_onsite_count_500m",
                "activity_excluded_inactive_count_500m",
                "activity_lifecycle_ambiguous_count_500m",
            ),
        ),
        (
            "warsaw-buildings",
            (
                "neighbor_building_count_500m",
                "known_use_building_count_500m",
                "residential_building_count_500m",
                "mapped_building_use_coverage_fraction_500m",
            ),
        ),
    ):
        if not available[source_id]:
            row.update(dict.fromkeys(fields))
    return row


def clear_assessment_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def get_assessment(engine: Engine, building_id: int) -> dict | None:
    if type(building_id) is not int or building_id < 1:
        raise ValueError("Building ID must be a positive integer")
    with engine.begin() as sql_connection:
        sql_connection.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        sql_connection.execute(text("SET LOCAL statement_timeout='10s'"))
        sql_connection.execute(text("SET LOCAL TIME ZONE 'UTC'"))
        connection = sql_connection.connection.driver_connection
        snapshot = read_snapshot(connection)
        with connection.cursor(row_factory=dict_row) as cursor:
            building = cursor.execute(
                "SELECT id,source_id,ST_AsGeoJSON(ST_Transform("
                "ST_PointOnSurface(ST_Transform(geometry,2180)),4326))::json AS origin"
                " FROM buildings WHERE id=%s AND dataset_id='warsaw-buildings'",
                (building_id,),
            ).fetchone()
        if building is None:
            return None
        now = datetime.now(UTC)
        key = (
            engine,
            building_id,
            building["source_id"],
            snapshot["source_revision"],
            ASSESSMENT_VERSION,
            EXTRACTION_VERSION,
            DATA_CONTRACT_VERSION,
            REQUIREMENTS_VERSION,
            now.date().isoformat(),
        )
        with _CACHE_LOCK:
            cached = _CACHE.get(key)
            if cached and monotonic() - cached[0] < CACHE_TTL_SECONDS:
                _CACHE.move_to_end(key)
                return deepcopy(cached[1])
            _CACHE.pop(key, None)
        context = build_live_source_context(snapshot, now)
        raw = extract_optional_features(connection, building["source_id"], snapshot, context)
        feature_row = build_feature_row(raw, context, now)
        result = build_assessment(
            feature_row,
            context,
            building_id,
            {
                "coordinates": building["origin"]["coordinates"],
                "method": "building_point_on_surface",
            },
            snapshot["source_revision"],
            now,
        )
        result["_runtime"] = {
            **snapshot,
            "source_context": context,
            "fingerprint_scope": "source_metadata_and_import_revision",
        }
    with _CACHE_LOCK:
        _CACHE[key] = (monotonic(), deepcopy(result))
        _CACHE.move_to_end(key)
        while len(_CACHE) > CACHE_MAX_ENTRIES:
            _CACHE.popitem(last=False)
    return result
