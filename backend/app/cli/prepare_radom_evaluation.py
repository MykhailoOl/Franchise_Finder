import argparse
import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from app.cli.extract_grocery_features import read_records, write_json
from app.core.config import BACKEND_DIRECTORY, Settings
from app.core.experiment import canonical_bytes, content_hash
from app.core.grocery_exposure import file_sha256, inventory_exposure
from app.core.grocery_features import (
    EXTRACTION_VERSION,
    SOURCE_TABLES,
    build_feature_row,
    build_source_context,
    summarize_feature_rows,
)
from app.db.experiment_readiness import fingerprint_table
from app.db.grocery_features import extract_raw_features, valid_bounds
from app.db.radom_evaluation import (
    SCHEMA_PREFIX,
    SOURCE_BINDINGS,
    candidate_cohort,
    initialize_schema,
    load_addresses,
    load_buildings,
    load_outlets,
    load_profile,
)

PREPARATION_VERSION = "radom-grocery-features-v1"
ROOT = BACKEND_DIRECTORY.parent
OUTPUT_FILES = {
    "cohort.jsonl",
    "features.jsonl",
    "sources.json",
    "quality.json",
    "unresolved-outlets.json",
}
SOURCE_KINDS = ("buildings", "addresses", "population", "pois", "activity")
PUBLIC_TABLES = (
    "buildings",
    "addresses",
    "franchise_outlets",
    "franchise_brands",
    "map_sources",
    "profile_population",
    "profile_pois",
    "profile_activity",
    "profile_sources",
)
REQUIRED_INPUTS = {
    f"{name}.{suffix}" for name in SOURCE_KINDS for suffix in ("geojson", "metadata.json")
} | {"zabka.json", "lewiatan.json"}


def producer_identity() -> dict:
    paths = (
        "app/cli/prepare_radom_evaluation.py",
        "app/db/radom_evaluation.py",
        "app/core/grocery_exposure.py",
        "app/db/grocery_features.py",
        "app/core/grocery_features.py",
        "app/core/data_contract.py",
        "app/core/experiment.py",
        "app/core/profile_data.py",
        "app/core/address_search.py",
        "app/core/franchise_snapshot.py",
        "app/cli/import_buildings.py",
        "app/cli/import_addresses.py",
        "app/cli/import_franchises.py",
        "app/db/map_schema.sql",
        "app/db/address_schema.sql",
        "app/db/franchise_schema.sql",
        "app/db/profile_schema.sql",
        "app/db/experiment_readiness.py",
        "pyproject.toml",
        "uv.lock",
    )
    hashes = {path: file_sha256(BACKEND_DIRECTORY / path) for path in paths}
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=BACKEND_DIRECTORY,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    status = subprocess.run(
        ["git", "status", "--porcelain", "--", *paths],
        cwd=BACKEND_DIRECTORY,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    return {
        "source_sha256": hashes,
        "code_sha256": content_hash(hashes),
        "git_commit": commit,
        "tracked_worktree_status": status,
    }


def public_fingerprints(connection) -> dict:
    if connection.execute("SELECT current_schema()").fetchone()[0] != "public":
        raise ValueError("Public-state verification requires the public schema")
    return {table: fingerprint_table(connection, table) for table in PUBLIC_TABLES}


def load_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


def validate_protocol(path: Path, expected_sha256: str) -> dict:
    if not re.fullmatch("[0-9a-f]{64}", expected_sha256) or file_sha256(path) != expected_sha256:
        raise ValueError("Frozen protocol SHA256 mismatch")
    protocol = load_json(path)
    candidate, freshness = protocol["candidate"], protocol["freshness"]
    if (
        protocol.get("geography") != "Radom"
        or candidate.get("namespace") != "146301_1."
        or candidate.get("brands") != ["zabka", "lewiatan"]
        or candidate.get("valid_geometry") is not True
        or candidate.get("unique_official_address") is not True
        or candidate.get("background_sampling") != "all_eligible_buildings"
        or candidate.get("matching_maximum_distance_m") != 10
        or freshness.get("minimum_separation_m") != 2000
        or freshness.get("old_geography_eligible") is not False
        or protocol["features"].get("version") != EXTRACTION_VERSION
        or valid_bounds(protocol["collector"].get("city_service_bounds")) is None
    ):
        raise ValueError("Preparation differs from the frozen Radom candidate and feature policy")
    for directory, digest in protocol["lineage"]["manifest_sha256"].items():
        if file_sha256(ROOT / directory / "manifest.json") != digest:
            raise ValueError("A prior exposure artifact changed after protocol freeze")
    return protocol


def verify_sources(directory: Path, protocol_sha256: str) -> dict:
    directory = directory.resolve()
    manifest = load_json(directory / "manifest.json")
    if manifest.get("protocol_sha256") != protocol_sha256 or not REQUIRED_INPUTS <= set(
        manifest.get("artifacts", {})
    ):
        raise ValueError("Source bundle is incomplete or belongs to another frozen protocol")
    for name, digest in manifest["artifacts"].items():
        path = (directory / name).resolve()
        if not path.is_relative_to(directory) or file_sha256(path) != digest:
            raise ValueError(f"Frozen source artifact changed: {name}")
    for kind in SOURCE_KINDS:
        metadata = load_json(directory / f"{kind}.metadata.json")
        if metadata.get("geojson_sha256") != manifest["artifacts"][f"{kind}.geojson"]:
            raise ValueError(f"Source metadata fingerprint differs from {kind} geometry")
    return manifest


def verify_output(directory: Path, protocol_sha256: str, source_sha256: str) -> dict:
    manifest = load_json(directory / "manifest.json")
    if (
        manifest.get("version") != PREPARATION_VERSION
        or manifest.get("protocol_sha256") != protocol_sha256
        or manifest.get("sources_manifest_sha256") != source_sha256
        or set(manifest.get("artifacts", {})) != OUTPUT_FILES
    ):
        raise ValueError("Prepared artifact has a different immutable identity")
    for name, digest in manifest["artifacts"].items():
        if file_sha256(directory / name) != digest:
            raise ValueError("Prepared evaluation artifact was modified")
    return manifest


def old_study_reference(connection, protocol: dict) -> dict:
    connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
    connection.execute("SET LOCAL TIME ZONE 'UTC'")
    connection.execute("SET LOCAL statement_timeout='120s'")
    lineage = protocol["lineage"]
    online = [
        row[0]
        for row in connection.execute(
            "SELECT DISTINCT building_source_id FROM public.building_profile_cache"
        )
    ]
    exposure = inventory_exposure(
        [ROOT / path for path in lineage["readiness_directories"]],
        ROOT / lineage["feature_directory"],
        {name: ROOT / lineage[f"{name}_directory"] for name in ("logistic", "catboost")},
        online,
    )
    prior_ids = exposure["private_ids"]["excluded"]
    found = {
        row[0]
        for row in connection.execute(
            "SELECT source_id FROM public.buildings WHERE dataset_id='warsaw-buildings'"
            " AND source_id=ANY(%s)",
            (prior_ids,),
        )
    }
    if set(prior_ids) != found:
        raise ValueError("Historical exposed building origins are missing from the old study table")
    expected = load_json(ROOT / lineage["feature_directory"] / "sources.json")["sources"][
        "warsaw-buildings"
    ]["data_sha256"]
    public_state = public_fingerprints(connection)
    snapshot = public_state["buildings"]
    if snapshot["sha256"] != expected:
        raise ValueError("The old study building geometry differs from its frozen feature snapshot")
    bounds = connection.execute(
        "SELECT ARRAY[ST_XMin(box),ST_YMin(box),ST_XMax(box),ST_YMax(box)] FROM"
        " (SELECT ST_Extent(ST_Transform(geometry,2180)) AS box FROM public.buildings"
        " WHERE dataset_id='warsaw-buildings') b"
    ).fetchone()[0]
    connection.commit()
    return {
        "bounds_2180": bounds,
        "snapshot": snapshot,
        "exposure_summary": exposure["summary"],
        "exposed_ids": prior_ids,
        "exposure_sha256": content_hash(exposure),
        "public_snapshots": public_state,
    }


def prove_freshness(connection, cohort: list[dict], old: dict) -> dict:
    identifiers = {row["building_source_id"] for row in cohort}
    overlaps = sorted(identifiers & set(old["exposed_ids"]))
    minimum_distance = connection.execute(
        "SELECT ST_Distance(ST_SetSRID(ST_Extent(ST_Transform(geometry,2180))::geometry,2180),"
        " ST_MakeEnvelope(%s,%s,%s,%s,2180)) FROM buildings WHERE dataset_id='warsaw-buildings'",
        tuple(old["bounds_2180"]),
    ).fetchone()[0]
    if not cohort or overlaps or minimum_distance is None or minimum_distance <= 2000:
        raise ValueError("Fresh cohort overlaps prior exposure or its frozen 2 km exclusion")
    return {
        "passed": True,
        "minimum_distance_m": minimum_distance,
        "distance_semantics": "lower_bound_between_complete_old_and_new_footprint_envelopes",
        "overlapping_ids": overlaps,
        "old_study_geography_overlap": False,
        "reference": "old_study_envelope",
        "old_study_bounds_2180": old["bounds_2180"],
        "old_buildings_sha256": old["snapshot"]["sha256"],
        "verified_prior_exposed_ids": len(old["exposed_ids"]),
        "exposure_sha256": old["exposure_sha256"],
        "historical_online_exposure": "incomplete_entire_old_geography_excluded",
    }


def prepare(
    protocol_path: Path,
    protocol_sha256: str,
    sources_dir: Path,
    output_dir: Path,
    settings: Settings | None = None,
) -> Path:
    protocol = validate_protocol(protocol_path, protocol_sha256)
    sources_manifest = verify_sources(sources_dir, protocol_sha256)
    sources_manifest_sha = file_sha256(sources_dir / "manifest.json")
    output_dir = output_dir.resolve()
    if not output_dir.is_relative_to((ROOT / ".local" / "evaluations").resolve()):
        raise ValueError(
            "Prepared external evaluation must remain inside ignored .local/evaluations"
        )
    if output_dir.exists():
        verify_output(output_dir, protocol_sha256, sources_manifest_sha)
        return output_dir
    metadata = {kind: load_json(sources_dir / f"{kind}.metadata.json") for kind in SOURCE_KINDS}
    for kind in ("buildings", "addresses", "pois", "activity"):
        retrieved = datetime.fromisoformat(metadata[kind]["retrieved_at"])
        if retrieved < datetime.fromisoformat(protocol["created_at"]):
            raise ValueError("Fresh external sources must be retrieved after protocol freeze")
    settings = settings or Settings()
    schema = SCHEMA_PREFIX + uuid4().hex
    created = False
    now = datetime.now(UTC)
    producer = producer_identity()
    staging = output_dir.parent / (".prepared-pending-" + uuid4().hex)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with psycopg.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        dbname=settings.postgres_db,
        user=settings.postgres_user,
        password=settings.postgres_password.get_secret_value(),
        connect_timeout=5,
        options="-csearch_path=public -ctimezone=UTC",
    ) as connection:
        old = old_study_reference(connection, protocol)
        try:
            initialize_schema(connection, schema, protocol["collector"]["city_service_bounds"])
            created = True
            imported = {
                "buildings": load_buildings(
                    connection, sources_dir / "buildings.geojson", metadata["buildings"], now
                ),
                "addresses": load_addresses(
                    connection, sources_dir / "addresses.geojson", metadata["addresses"], now
                ),
            }
            for kind in ("population", "pois", "activity"):
                imported[kind] = load_profile(
                    connection, kind, sources_dir / f"{kind}.geojson", metadata[kind], now
                )
            imported["outlets"] = load_outlets(
                connection, [sources_dir / "zabka.json", sources_dir / "lewiatan.json"]
            )
            connection.commit()
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            connection.execute("SET LOCAL statement_timeout='120s'")
            cohort = candidate_cohort(connection)
            freshness = prove_freshness(connection, cohort, old)
            cursor = connection.cursor(row_factory=dict_row)
            source_metadata = {
                table: cursor.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
                for table in ("map_sources", "profile_sources")
            }
            for record in source_metadata["map_sources"]:
                original = metadata[
                    "buildings" if record["id"] == "warsaw-buildings" else "addresses"
                ]
                record.update(
                    {
                        key: original.get(key)
                        for key in ("retrieved_at", "observed_at", "snapshot_at")
                    }
                )
                record.update(actual_source_id=original["id"], territory="Radom")
            snapshots = {
                table: fingerprint_table(connection, table) for _, table in SOURCE_TABLES.values()
            }
            context = build_source_context(
                {"source_metadata": source_metadata, "source_snapshots": snapshots}, now
            )
            context["logical_source_bindings"] = SOURCE_BINDINGS
            context["territory"] = "Radom"
            context["coverage_limitation"] = (
                "Declared service-envelope coverage, not municipal-boundary completeness; "
                "city-only buildings omit neighboring municipalities and boundary holes."
            )
            unresolved = cursor.execute(
                "SELECT brand_id,source_id,match_status,match_method,match_note,building_source_id,"
                "candidate_building_ids FROM franchise_outlets WHERE match_status<>'matched'"
                " OR building_source_id IS NULL ORDER BY brand_id,source_id"
            ).fetchall()
            outside_frame = cursor.execute(
                "SELECT brand_id,source_id,building_source_id FROM franchise_outlets"
                " WHERE match_status='matched' AND NOT(building_source_id=ANY(%s))"
                " ORDER BY brand_id,source_id",
                ([row["building_source_id"] for row in cohort],),
            ).fetchall()
            staging.mkdir(exist_ok=False)
            with (staging / "cohort.jsonl").open("xb") as stream:
                for row in cohort:
                    stream.write(canonical_bytes(row) + b"\n")
            identifiers = [row["building_source_id"] for row in cohort]
            with (staging / "features.jsonl").open("xb") as stream:
                for start in range(0, len(identifiers), 128):
                    batch = identifiers[start : start + 128]
                    for raw in extract_raw_features(connection, batch):
                        stream.write(canonical_bytes(build_feature_row(raw, context, now)) + b"\n")
                    print(
                        f"Prepared {min(start + 128, len(identifiers))}"
                        f"/{len(identifiers)} buildings",
                        flush=True,
                    )
            write_json(staging / "sources.json", context)
            quality = summarize_feature_rows(read_records(staging / "features.jsonl"))
            quality.pop("final_holdout_statistics_computed", None)
            quality.update(
                scope="external_full_candidate_frame",
                external_cohort_statistics_computed=True,
                previous_warsaw_holdout_statistics_computed=False,
                source_imports=imported,
                source_coverage_limitation=context["coverage_limitation"],
            )
            write_json(staging / "quality.json", quality)
            write_json(
                staging / "unresolved-outlets.json",
                {
                    "outlets": unresolved,
                    "matched_outside_candidate_frame": outside_frame,
                    "outside_frame_reason": "No uniquely matched official municipal address",
                },
            )
            positives = sum(row["label"] == "observed_presence" for row in cohort)
            manifest = {
                "version": PREPARATION_VERSION,
                "created_at": now,
                "protocol_sha256": protocol_sha256,
                "sources_manifest_sha256": sources_manifest_sha,
                "source_collector_version": sources_manifest.get("collector_version"),
                "feature_extraction_version": EXTRACTION_VERSION,
                "source_bindings": SOURCE_BINDINGS,
                "freshness": freshness,
                "counts": {
                    "rows": len(cohort),
                    "positives": positives,
                    "background": len(cohort) - positives,
                },
                "candidate_frame": {
                    "complete": True,
                    "namespace": "146301_1.",
                    "unique_official_address": True,
                    "background_sampling": "all_eligible_buildings",
                    "labels_used_for_eligibility": False,
                },
                "schema_isolation": "ephemeral_schema_no_public_data_mutations",
                "producer": producer,
                "training_performed": False,
                "scoring_performed": False,
                "artifacts": {name: file_sha256(staging / name) for name in sorted(OUTPUT_FILES)},
            }
            connection.commit()
        finally:
            connection.rollback()
            if created:
                connection.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
                )
                connection.commit()
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        after = public_fingerprints(connection)
        if after != old["public_snapshots"]:
            raise ValueError("Public source tables changed during isolated feature preparation")
        if producer_identity()["source_sha256"] != producer["source_sha256"]:
            raise ValueError("Preparation source code changed while features were being computed")
        manifest["public_tables_unchanged"] = True
        manifest["public_table_fingerprints"] = after
        write_json(staging / "manifest.json", manifest)
        staging.rename(output_dir)
    verify_output(output_dir, protocol_sha256, sources_manifest_sha)
    return output_dir


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare the isolated frozen Radom evaluation cohort"
    )
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--protocol-sha256", required=True)
    parser.add_argument("--sources-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = prepare(args.protocol, args.protocol_sha256, args.sources_dir, args.output_dir)
    except (OSError, ValueError, KeyError, psycopg.Error) as error:
        parser.exit(1, f"External feature preparation failed: {error}\n")
    print(json.dumps({"status": "prepared_not_scored", "directory": str(result)}))


if __name__ == "__main__":
    main()
