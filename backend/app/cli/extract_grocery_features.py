import argparse
import hashlib
import json
import platform
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import psycopg

from app.core.config import BACKEND_DIRECTORY, Settings
from app.core.data_contract import PREDICTORS
from app.core.experiment import (
    CONFIG_PATH,
    canonical_bytes,
    configuration_hash,
    content_hash,
    load_experiment,
)
from app.core.grocery_features import (
    EXTRACTION_POLICY,
    EXTRACTION_VERSION,
    build_feature_row,
    build_source_context,
    summarize_feature_rows,
)
from app.db.experiment_readiness import TABLE_KEYS, fingerprint_table
from app.db.grocery_features import extract_raw_features

LOCAL_DIRECTORY = BACKEND_DIRECTORY.parent / ".local"
READINESS_FILES = {"readiness.json", "candidates.jsonl", "memberships.jsonl"}
OUTPUT_FILES = {
    "features-development.jsonl",
    "features-final-holdout.jsonl",
    "memberships-development.jsonl",
    "memberships-final-holdout.jsonl",
    "quality.json",
    "sources.json",
}


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: dict) -> None:
    normalized = json.loads(canonical_bytes(value))
    path.write_text(
        json.dumps(normalized, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def verify_artifacts(directory: Path, manifest: dict, expected_files: set[str]) -> None:
    if set(manifest.get("artifacts", {})) != expected_files:
        raise ValueError("Artifact manifest has an unexpected file set")
    for name, expected in manifest["artifacts"].items():
        if file_hash(directory / name) != expected:
            raise ValueError(f"Artifact content has changed: {name}")


def read_records(path: Path):
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            yield json.loads(line)


def load_frozen_grocery(directory: Path, definition) -> dict:
    manifest = load_json(directory / "manifest.json")
    verify_artifacts(directory, manifest, READINESS_FILES)
    report = load_json(directory / "readiness.json")
    expected_config = configuration_hash(definition)
    if any(item.get("config_sha256") != expected_config for item in (manifest, report)):
        raise ValueError("Readiness configuration differs from the selected experiment")
    if manifest.get("dataset_sha256") != report.get("dataset_sha256"):
        raise ValueError("Readiness dataset identity does not match its manifest")
    for name, key in (
        ("candidates.jsonl", "candidate_sha256"),
        ("memberships.jsonl", "memberships_sha256"),
    ):
        if manifest["artifacts"][name] != report.get(key):
            raise ValueError("Readiness membership identity is inconsistent")
    if report.get("status") != "readiness_only" or report.get("empty_required_tables"):
        raise ValueError("Readiness contains missing required data")
    supported = {
        row["pool"]
        for row in report["summary"]["category_support"]
        if row["category"] == "grocery" and row["status"] == "ready_for_feature_work"
    }
    if supported != {definition.primary_pool, definition.sensitivity_pool}:
        raise ValueError("Both frozen grocery pools must pass the sample-support checkpoint")
    members = [
        row for row in read_records(directory / "memberships.jsonl") if row["category"] == "grocery"
    ]
    if not members:
        raise ValueError("Frozen experiment contains no grocery memberships")
    requested = {row["building_source_id"] for row in members}
    candidates = {}
    for row in read_records(directory / "candidates.jsonl"):
        identifier = row["building_source_id"]
        if identifier in requested:
            if identifier in candidates:
                raise ValueError("Candidate building identity is duplicated")
            candidates[identifier] = row
    if set(candidates) != requested:
        raise ValueError("A grocery membership is missing its frozen candidate")
    seen = set()
    for member in members:
        identifier = member["building_source_id"]
        candidate = candidates[identifier]
        key = (member["pool"], identifier)
        if key in seen or member["pool"] not in supported:
            raise ValueError("Duplicate or unexpected grocery pool membership")
        seen.add(key)
        if member["pool"] not in candidate["pools"]:
            raise ValueError("Membership is outside its target-independent candidate pool")
        if member["label"] not in {"observed_presence", "unlabelled_background"}:
            raise ValueError("Grocery labels must remain presence or unlabelled background")
        for field in ("fold", "validation_fold", "training_folds"):
            if member[field] != candidate[field]:
                raise ValueError("Membership changes the frozen geographic assignment")
        if candidate["fold"] not in range(4):
            raise ValueError("Invalid geographic fold")
        if candidate["fold"] == 3 and candidate["training_folds"]:
            raise ValueError("Final holdout cannot participate in training")
    return {"manifest": manifest, "report": report, "members": members, "candidates": candidates}


def validate_source_snapshot(connection, report: dict) -> None:
    snapshots = report["source_snapshots"]
    if set(snapshots) != set(TABLE_KEYS):
        raise ValueError("Readiness source snapshot set is incomplete")
    for table in TABLE_KEYS:
        current = fingerprint_table(connection, table)
        expected = snapshots[table]
        if any(current[key] != expected[key] for key in ("row_count", "sha256")):
            raise ValueError(f"Source changed since readiness: {table}; prepare a new checkpoint")


def producer_identity(connection) -> dict:
    names = (
        "app/cli/extract_grocery_features.py",
        "app/core/grocery_features.py",
        "app/db/grocery_features.py",
        "app/core/data_contract.py",
        "app/core/experiment.py",
        "app/db/experiment_readiness.py",
    )
    hashes = {name: file_hash(BACKEND_DIRECTORY / name) for name in names}
    runtime = {
        "python": platform.python_version(),
        "psycopg": psycopg.__version__,
        "database": list(connection.execute("SELECT version(),PostGIS_Full_Version()").fetchone()),
    }
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=BACKEND_DIRECTORY,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--", *names],
            cwd=BACKEND_DIRECTORY,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError):
        commit, dirty = None, None
    return {
        "source_sha256": hashes,
        "code_sha256": content_hash(hashes),
        "runtime": runtime,
        "git_commit": commit,
        "producer_worktree_status": dirty,
    }


def output_identity(bundle: dict, producer: dict, as_of_date: str) -> dict:
    return {
        "extraction_version": EXTRACTION_VERSION,
        "config_sha256": bundle["manifest"]["config_sha256"],
        "dataset_sha256": bundle["manifest"]["dataset_sha256"],
        "memberships_sha256": bundle["manifest"]["artifacts"]["memberships.jsonl"],
        "code_sha256": producer["code_sha256"],
        "runtime": producer["runtime"],
        "as_of_date": as_of_date,
    }


def validate_existing(directory: Path, identity: dict) -> None:
    manifest = load_json(directory / "manifest.json")
    if manifest.get("identity") != identity:
        raise ValueError("Existing feature artifact identity does not match")
    verify_artifacts(directory, manifest, OUTPUT_FILES)


def extract_snapshot(connection, bundle: dict, definition, output_root: Path, batch_size: int):
    if not 1 <= batch_size <= 1000:
        raise ValueError("Batch size must be between 1 and 1000")
    output_root = output_root.resolve()
    if not output_root.is_relative_to(LOCAL_DIRECTORY.resolve()):
        raise ValueError(
            "Generated features must remain inside the ignored project .local directory"
        )
    mode = connection.execute(
        "SELECT current_setting('transaction_read_only'),current_setting('transaction_isolation')"
    ).fetchone()
    if tuple(mode) != ("on", "repeatable read"):
        raise ValueError("Feature extraction requires a read-only repeatable-read transaction")
    started = time.perf_counter()
    validate_source_snapshot(connection, bundle["report"])
    calculated_at = datetime.now(UTC)
    context = build_source_context(bundle["report"], calculated_at)
    producer = producer_identity(connection)
    identity = output_identity(bundle, producer, calculated_at.date().isoformat())
    destination = output_root / ("grocery-v1_" + content_hash(identity)[:24])
    if destination.exists():
        validate_existing(destination, identity)
        return destination
    output_root.mkdir(parents=True, exist_ok=True)
    staging = output_root / (".pending-" + uuid4().hex)
    staging.mkdir(exist_ok=False)
    identifiers = sorted(bundle["candidates"])
    counts = {"development": 0, "final_holdout": 0, "development_quality": 0}
    quality_rows = []
    with (
        (staging / "features-development.jsonl").open("xb") as development,
        (staging / "features-final-holdout.jsonl").open("xb") as final,
    ):
        for offset in range(0, len(identifiers), batch_size):
            batch = identifiers[offset : offset + batch_size]
            raw_rows = extract_raw_features(connection, batch)
            if [row["building_source_id"] for row in raw_rows] != batch:
                raise ValueError(
                    "Extractor did not return exactly the requested building identities"
                )
            for raw in raw_rows:
                row = build_feature_row(raw, context, calculated_at)
                if set(row["values"]) != set(definition.primary_predictors):
                    raise ValueError("Computed predictor set differs from the frozen experiment")
                candidate = bundle["candidates"][row["building_source_id"]]
                partition = "final_holdout" if candidate["fold"] == 3 else "development"
                stream = final if partition == "final_holdout" else development
                stream.write(canonical_bytes(row) + b"\n")
                counts[partition] += 1
                if partition == "development" and (
                    candidate["training_folds"] or candidate["validation_fold"] is not None
                ):
                    quality_rows.append(row)
            print(
                json.dumps({"processed_buildings": offset + len(batch), "total": len(identifiers)}),
                file=sys.stderr,
                flush=True,
            )
    counts["development_quality"] = len(quality_rows)
    for partition, final_partition in (("development", False), ("final-holdout", True)):
        with (staging / f"memberships-{partition}.jsonl").open("xb") as output:
            for row in bundle["members"]:
                if (row["fold"] == 3) == final_partition:
                    output.write(canonical_bytes(row) + b"\n")
    quality = {
        "status": "extracted_not_trained",
        "extraction_version": EXTRACTION_VERSION,
        "calculated_at": calculated_at,
        "as_of_date": calculated_at.date(),
        "row_counts": counts,
        "unique_buildings": len(identifiers),
        "grocery_pool_memberships": len(bundle["members"]),
        "quality_scope": "Development buildings with a training or validation assignment only",
        "final_holdout_distributions_evaluated": False,
        "training_performed": False,
        "learned_preprocessing_performed": False,
        "imputation_performed": False,
        "model_performance_evaluated": False,
        "development_quality": summarize_feature_rows(quality_rows),
        "duration_seconds": round(time.perf_counter() - started, 3),
    }
    write_json(staging / "quality.json", quality)
    write_json(staging / "sources.json", context)
    manifest = {
        "identity": identity,
        "producer": producer,
        "calculated_at": calculated_at,
        "status": "extracted_not_trained",
        "readiness_manifest": bundle["manifest"],
        "experiment": definition.model_dump(mode="json"),
        "policy": EXTRACTION_POLICY,
        "predictor_definitions": {
            name: PREDICTORS[name].model_dump(mode="json") for name in definition.primary_predictors
        },
        "feature_sources": "sources.json",
        "final_holdout": "features-final-holdout.jsonl",
        "membership_join_key": "building_source_id",
        "training_performed": False,
        "artifacts": {name: file_hash(staging / name) for name in sorted(OUTPUT_FILES)},
    }
    write_json(staging / "manifest.json", manifest)
    staging.rename(destination)
    return destination


def run_extraction(settings, bundle, definition, output_root, batch_size):
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
        return extract_snapshot(connection, bundle, definition, output_root, batch_size)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract the frozen grocery environmental features"
    )
    parser.add_argument("--readiness", required=True, type=Path)
    parser.add_argument("--config", default=CONFIG_PATH, type=Path)
    parser.add_argument("--output-root", default=LOCAL_DIRECTORY / "features", type=Path)
    parser.add_argument("--batch-size", default=128, type=int)
    arguments = parser.parse_args()
    try:
        definition = load_experiment(arguments.config)
        bundle = load_frozen_grocery(arguments.readiness, definition)
        destination = run_extraction(
            Settings(), bundle, definition, arguments.output_root, arguments.batch_size
        )
    except (OSError, ValueError, KeyError, TypeError, psycopg.Error) as error:
        parser.exit(1, f"Grocery feature extraction failed: {error}\n")
    print(json.dumps({"status": "extracted_not_trained", "artifact_directory": str(destination)}))


if __name__ == "__main__":
    main()
