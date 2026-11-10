import hashlib
from typing import Any

import psycopg
from psycopg.rows import dict_row

from app.core.config import Settings
from app.core.experiment import (
    ExperimentDefinition,
    build_memberships,
    canonical_bytes,
    configuration_hash,
    content_hash,
)

TABLE_KEYS = {
    "map_sources": "id",
    "buildings": "source_id",
    "addresses": "source_id",
    "franchise_categories": "id",
    "franchise_brands": "id",
    "franchise_outlets": "brand_id,source_id",
    "profile_sources": "id",
    "profile_population": "source_id",
    "profile_pois": "source_id",
    "profile_activity": "source_id",
}


def fingerprint_table(connection: psycopg.Connection, table: str) -> dict[str, Any]:
    if table not in TABLE_KEYS:
        raise ValueError("Unsupported snapshot table")
    digest = hashlib.sha256()
    count = 0
    expression = "to_jsonb(t)"
    if table in {
        "buildings",
        "addresses",
        "franchise_outlets",
        "profile_population",
        "profile_pois",
        "profile_activity",
    }:
        expression = "(to_jsonb(t)-'geometry') || jsonb_build_object("
        expression += "'geometry_ewkb',encode(ST_AsEWKB(t.geometry),'hex'))"
        if table == "franchise_outlets":
            expression = f"({expression})-'display_geometry' || jsonb_build_object("
            expression += "'display_geometry_ewkb',encode(ST_AsEWKB(t.display_geometry),'hex'))"
    with connection.cursor(name="experiment_snapshot", row_factory=dict_row) as cursor:
        cursor.execute(
            f"SELECT encode(sha256(convert_to(({expression})::text,'UTF8')),'hex') AS hash"
            f" FROM {table} t ORDER BY {TABLE_KEYS[table]}"
        )
        for row in cursor:
            digest.update(bytes.fromhex(row["hash"]))
            count += 1
    return {
        "row_count": count,
        "sha256": digest.hexdigest(),
        "method": "SHA256 of ordered SHA256 row digests; full stored rows including EWKB geometry",
    }


def prepare_readiness(connection: psycopg.Connection, definition: ExperimentDefinition) -> tuple:
    cursor = connection.cursor(row_factory=dict_row)
    modes = cursor.execute(
        "SELECT current_setting('transaction_read_only') AS read_only,"
        " current_setting('transaction_isolation') AS isolation"
    ).fetchone()
    if modes != {"read_only": "on", "isolation": "repeatable read"}:
        raise ValueError("Readiness requires a read-only repeatable-read transaction")
    missing = [
        table
        for table in TABLE_KEYS
        if not cursor.execute(
            "SELECT to_regclass(format('%%I.%%I',current_schema(),%s::text)) AS name", (table,)
        ).fetchone()["name"]
    ]
    if missing:
        raise ValueError(f"Required datasets are not imported: {', '.join(missing)}")
    center_row = cursor.execute(
        "SELECT ST_X(point) AS x,ST_Y(point) AS y FROM ("
        " SELECT ST_Transform(ST_SetSRID(ST_Point(%s,%s),4326),2180) AS point) center",
        (definition.split.center_longitude, definition.split.center_latitude),
    ).fetchone()
    center = (center_row["x"], center_row["y"])
    buildings = cursor.execute("""
        WITH validity AS MATERIALIZED (
            SELECT source_id,building_type,geometry,
                ST_IsValid(geometry) AND NOT ST_IsEmpty(geometry) AS valid_geometry
            FROM buildings WHERE dataset_id='warsaw-buildings'
        ), addressed AS MATERIALIZED (
            SELECT DISTINCT building_source_id FROM addresses
            WHERE dataset_id='warsaw-addresses' AND match_status='matched'
                AND candidate_count=1 AND building_source_id IS NOT NULL
        ), locations AS (
            SELECT v.*,a.building_source_id IS NOT NULL AS has_unique_address,
                CASE WHEN valid_geometry THEN ST_PointOnSurface(ST_Transform(geometry,2180)) END
                    AS origin FROM validity v
                LEFT JOIN addressed a ON a.building_source_id=v.source_id
        ) SELECT source_id,building_type,valid_geometry,has_unique_address,
            ST_X(origin) AS x,ST_Y(origin) AS y FROM locations ORDER BY source_id
    """).fetchall()
    observations = cursor.execute("""
        SELECT o.building_source_id,b.category_id,
            array_agg(DISTINCT o.brand_id ORDER BY o.brand_id) AS brand_ids,
            jsonb_agg(jsonb_build_object('brand_id',o.brand_id,'source_id',o.source_id,
                'match_method',o.match_method) ORDER BY o.brand_id,o.source_id) AS source_outlets
        FROM franchise_outlets o JOIN franchise_brands b ON b.id=o.brand_id
        WHERE o.match_status='matched' AND o.building_source_id IS NOT NULL
        GROUP BY 1,2 ORDER BY 1,2
    """).fetchall()
    unresolved = cursor.execute("""
        SELECT match_status,count(*) AS count FROM franchise_outlets
        WHERE match_status<>'matched' OR building_source_id IS NULL GROUP BY 1 ORDER BY 1
    """).fetchall()
    sources = {
        table: cursor.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
        for table in ("map_sources", "franchise_brands", "profile_sources")
    }
    fingerprints = {table: fingerprint_table(connection, table) for table in TABLE_KEYS}
    for table, snapshot in fingerprints.items():
        snapshot["snapshot_id"] = f"{table}:{snapshot['sha256']}"
    summary, candidates, memberships = build_memberships(
        buildings, observations, center, definition
    )
    candidate_hash = hashlib.sha256()
    for row in candidates:
        candidate_hash.update(canonical_bytes(row) + b"\n")
    membership_hash = hashlib.sha256()
    for row in memberships:
        membership_hash.update(canonical_bytes(row) + b"\n")
    dataset_hash = content_hash(
        {
            "sources": fingerprints,
            "candidate_sha256": candidate_hash.hexdigest(),
            "memberships_sha256": membership_hash.hexdigest(),
            "observations": observations,
        }
    )
    source_issues = []
    for source in sources["map_sources"]:
        source_issues.append(
            {
                "source_id": source["id"],
                "issue": ("Observation/retrieval dates are unknown in DB; import is not retrieval"),
            }
        )
    unavailable = [table for table, snapshot in fingerprints.items() if snapshot["row_count"] == 0]
    summary["data_readiness"] = {
        "status": "missing_data" if unavailable else "sources_present",
        "empty_required_tables": unavailable,
        "predictor_quality_evaluated": False,
    }
    if unavailable:
        for category in summary["category_support"]:
            category["support_status"] = category["status"]
            category["status"] = "missing_data"
    report = {
        "status": "readiness_only",
        "database_read_only": True,
        "transaction_isolation": "repeatable read",
        "config_sha256": configuration_hash(definition),
        "dataset_sha256": dataset_hash,
        "candidate_sha256": candidate_hash.hexdigest(),
        "memberships_sha256": membership_hash.hexdigest(),
        "experiment": definition.model_dump(mode="json"),
        "summary": summary,
        "projected_split_center": {"crs": 2180, "x": center[0], "y": center[1]},
        "source_snapshots": fingerprints,
        "source_metadata": sources,
        "source_date_limitations": source_issues,
        "empty_required_tables": unavailable,
        "unresolved_outlets": unresolved,
        "observed_building_category_pairs": len(observations),
        "promotion_gate": {
            "status": "unevaluated",
            "trained_models": 0,
            "computed_predictor_values": 0,
            "final_holdout_evaluated": False,
        },
        "limitations": [
            "Candidates are mapped buildings; commercial unit availability is unknown",
            "Sampled background labels are unknown, not failed or unsuitable businesses",
            "Four regions and 2km separation are planning choices, not estimated independence",
            "Support thresholds are operational minima, not statistical power guarantees",
            "Sources span 2021 census and 2026 snapshots; no historical opening backtest",
            "Mapped use can reflect occupancy; a sensitivity analysis without it is needed",
            "Absent population rows remain unknown; footfall and commercial outcomes are absent",
            "Competition is deferred; future features must remove focal venues and activity",
            "Source hashes describe stored content; they do not verify public-source completeness",
        ],
    }
    if unavailable:
        report["status"] = "missing_data"
    return report, candidates, memberships


def run_readiness(settings: Settings, definition: ExperimentDefinition) -> tuple:
    with psycopg.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        dbname=settings.postgres_db,
        user=settings.postgres_user,
        password=settings.postgres_password.get_secret_value(),
        connect_timeout=5,
    ) as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        connection.execute("SET LOCAL statement_timeout='180s'")
        connection.execute("SET LOCAL TIME ZONE 'UTC'")
        return prepare_readiness(connection, definition)
