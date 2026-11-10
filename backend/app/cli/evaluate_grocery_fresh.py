import argparse
import json
import math
import os
import re
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from app.cli.extract_grocery_features import file_hash, load_json, read_records, write_json
from app.core.config import BACKEND_DIRECTORY
from app.core.experiment import content_hash, load_experiment
from app.core.grocery_exposure import inventory_exposure
from app.core.grocery_fresh_evaluation import diagnose_previous, evaluate_fresh

ROOT = BACKEND_DIRECTORY.parent
PROTOCOL_PATH = BACKEND_DIRECTORY / "app/experiments/grocery-fresh-evaluation-v1.json"
MODEL_ROLES = {"catboost", "logistic", "population_only", "access_only"}
PREPARED_FILES = {"cohort.jsonl", "features.jsonl", "sources.json", "quality.json"}
RESULT_FILES = {"evaluation.json", "predictions.jsonl", "audit.json", "lock.json"}


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def safe_child(directory: Path, relative: str) -> Path:
    path = (directory / relative).resolve()
    if not path.is_relative_to(directory.resolve()) or path == directory.resolve():
        raise ValueError("Artifact path must stay inside its declared directory")
    return path


def verify_file_set(directory: Path, manifest: dict, required: set[str]) -> None:
    artifacts = manifest.get("artifacts", {})
    if not isinstance(artifacts, dict) or not required <= artifacts.keys():
        raise ValueError("Artifact manifest is missing required files")
    for name, expected in artifacts.items():
        if file_hash(safe_child(directory, name)) != expected:
            raise ValueError(f"Artifact content changed: {name}")


def verify_protocol(path: Path, expected_sha256: str) -> tuple[dict, dict]:
    if file_hash(path) != expected_sha256:
        raise ValueError("Protocol differs from the explicitly frozen SHA256")
    relative = path.resolve().relative_to(ROOT).as_posix()
    committed = subprocess.check_output(["git", "show", f"HEAD:{relative}"], cwd=ROOT)
    if committed != path.read_bytes():
        raise ValueError("Protocol must match its committed bytes")
    commit = subprocess.check_output(
        ["git", "log", "-1", "--format=%H", "--", relative], cwd=ROOT, text=True
    ).strip()
    committed_at = subprocess.check_output(
        ["git", "show", "-s", "--format=%cI", commit], cwd=ROOT, text=True
    ).strip()
    protocol = load_json(path)
    if (
        protocol.get("version") != "grocery-fresh-evaluation-v1"
        or protocol.get("geography") != "Radom"
        or protocol.get("collector", {}).get("municipality_id") != "1463011"
        or protocol.get("candidate", {}).get("namespace") != "146301_1."
        or set(protocol.get("models", {})) != MODEL_ROLES
        or protocol.get("promotion", {}).get("automatic_promotion") is not False
        or protocol.get("promotion", {}).get("rankings_enabled") is not False
    ):
        raise ValueError("Unsupported fresh evaluation protocol or publication policy")
    for model in protocol["models"].values():
        if file_hash(safe_child(ROOT, model["path"])) != model["sha256"]:
            raise ValueError("Frozen model artifact changed")
    for directory, expected in protocol["lineage"]["manifest_sha256"].items():
        if file_hash(safe_child(ROOT, directory) / "manifest.json") != expected:
            raise ValueError("Historical artifact identity changed")
    return protocol, {"git_commit": commit, "committed_at": committed_at}


def historical_audit(protocol: dict) -> dict:
    lineage = protocol["lineage"]
    directories = {
        name: safe_child(ROOT, lineage[f"{name}_directory"]) for name in ("logistic", "catboost")
    }
    exposure = inventory_exposure(
        [safe_child(ROOT, path) for path in lineage["readiness_directories"]],
        safe_child(ROOT, lineage["feature_directory"]),
        directories,
    )
    definition = load_experiment()
    diagnoses = {
        name: diagnose_previous(
            load_json(directory / "development-evaluation.json"),
            load_json(directory / "evaluation/evaluation.json"),
            definition,
        )
        for name, directory in directories.items()
    }
    return {"exposure": exposure, "historical_diagnoses": diagnoses}


def verify_prepared(
    directory: Path, sources_directory: Path, protocol: dict, protocol_sha256: str, audit: dict
) -> tuple[dict, dict, list[dict]]:
    manifest = load_json(directory / "manifest.json")
    sources_manifest = load_json(sources_directory / "manifest.json")
    if any(
        value.get("protocol_sha256") != protocol_sha256 for value in (manifest, sources_manifest)
    ):
        raise ValueError("Prepared data and acquisition must bind the frozen protocol")
    if manifest.get("sources_manifest_sha256") != file_hash(sources_directory / "manifest.json"):
        raise ValueError("Prepared data points to a different acquisition")
    verify_file_set(directory, manifest, PREPARED_FILES)
    verify_file_set(sources_directory, sources_manifest, {"protocol.json"})
    if file_hash(sources_directory / "protocol.json") != protocol_sha256:
        raise ValueError("Acquisition protocol copy changed")
    freshness = manifest.get("freshness", {})
    minimum_distance = freshness.get("minimum_distance_m")
    if (
        freshness.get("passed") is not True
        or freshness.get("overlapping_ids") != []
        or freshness.get("old_study_geography_overlap") is not False
        or isinstance(minimum_distance, bool)
        or not isinstance(minimum_distance, (int, float))
        or not math.isfinite(minimum_distance)
        or minimum_distance <= protocol["freshness"]["minimum_separation_m"]
        or manifest.get("candidate_frame", {}).get("complete") is not True
    ):
        raise ValueError("Fresh cohort lacks complete spatial and exposure exclusion proof")
    memberships = list(read_records(directory / "cohort.jsonl"))
    ids = [row.get("building_source_id") for row in memberships]
    if len(set(ids)) != len(ids) or any(
        not isinstance(value, str) or not value.startswith(protocol["candidate"]["namespace"])
        for value in ids
    ):
        raise ValueError("Fresh cohort has duplicate or unexpected geographic identities")
    if set(ids) & set(audit["exposure"]["private_ids"]["excluded"]):
        raise ValueError("Previously exposed building IDs cannot enter the fresh cohort")
    positives = sum(row.get("label") == "observed_presence" for row in memberships)
    if any(
        row.get("label") not in {"observed_presence", "unlabelled_background"}
        or any(
            isinstance(row.get(axis), bool)
            or not isinstance(row.get(axis), (int, float))
            or not math.isfinite(row[axis])
            for axis in ("x", "y")
        )
        for row in memberships
    ):
        raise ValueError("Fresh cohort requires valid labels and metric-coordinate origins")
    expected_counts = {"rows": len(ids), "positives": positives, "background": len(ids) - positives}
    if manifest.get("counts") != expected_counts:
        raise ValueError("Fresh cohort support differs from its prepared manifest")
    return manifest, sources_manifest, memberships


def claim_checkpoint(ledger_directory: Path, city_key: str, identity: dict) -> dict:
    if re.fullmatch(r"[0-9a-f]{64}", city_key) is None:
        raise ValueError("Consumption ledger requires a canonical geographic digest")
    ledger_directory.mkdir(parents=True, exist_ok=True)
    path = ledger_directory / f"{city_key}.json"
    claim = {"state": "consumed", "consumed_at": utc_now(), "identity": identity}
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(claim, stream, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        previous = load_json(path)
        if previous.get("identity") != identity or previous.get("state") != "complete":
            raise ValueError(
                "This geography has already been consumed; a new fresh run is forbidden"
            ) from None
        return previous
    return claim


def complete_checkpoint(ledger_directory: Path, city_key: str, claim: dict, output: Path) -> None:
    if re.fullmatch(r"[0-9a-f]{64}", city_key) is None:
        raise ValueError("Consumption ledger requires a canonical geographic digest")
    path = ledger_directory / f"{city_key}.json"
    if load_json(path) != claim or claim.get("state") != "consumed":
        raise ValueError("Evaluation consumption ledger changed during scoring")
    completed = {
        **claim,
        "state": "complete",
        "completed_at": utc_now(),
        "output_directory": str(output.resolve()),
        "manifest_sha256": file_hash(output / "manifest.json"),
    }
    temporary = path.with_suffix(".part")
    with temporary.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(completed, stream, sort_keys=True, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def reuse_completed(claim: dict, output: Path) -> dict:
    if (
        claim.get("state") != "complete"
        or claim.get("output_directory") != str(output.resolve())
        or claim.get("manifest_sha256") != file_hash(output / "manifest.json")
    ):
        raise ValueError("Completed checkpoint identity or result manifest changed")
    manifest = load_json(output / "manifest.json")
    verify_file_set(output, manifest, RESULT_FILES)
    if manifest.get("identity") != claim["identity"]:
        raise ValueError("Completed result identity differs from the consumption ledger")
    return load_json(output / "evaluation.json")


def producer_identity() -> dict:
    status = subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=all"], cwd=ROOT, text=True
    ).strip()
    if status:
        raise ValueError("Commit the tested producer before consuming the fresh checkpoint")
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    return {"git_commit": commit, "worktree_clean": True}


def verify_acquisition_dates(sources: Path, manifest: dict, protocol: dict, freeze: dict) -> None:
    frozen_at = datetime.fromisoformat(freeze["committed_at"])
    expected_roles = {
        "buildings",
        "addresses",
        "population",
        "pois",
        "activity",
        "zabka",
        "lewiatan",
    }
    if (
        frozen_at.tzinfo is None
        or manifest.get("collector_version") != protocol["collector"]["version"]
        or set(manifest.get("sources", {})) != expected_roles
    ):
        raise ValueError("Acquisition must identify every frozen source and its collector")
    times = [manifest["collected_at"]]
    raw_metadata = {
        name
        for name in manifest["artifacts"]
        if name.startswith("raw/") and name.endswith(".metadata.json")
    }
    required = {
        "raw/pois.json.metadata.json",
        "raw/activity.json.metadata.json",
        "raw/zabka-stores.raw.json.metadata.json",
        "raw/lewiatan-stores.raw.json.metadata.json",
        "raw/buildings/hits.xml.metadata.json",
        "raw/buildings/hits-after.xml.metadata.json",
        "raw/addresses/hits.xml.metadata.json",
        "raw/addresses/hits-after.xml.metadata.json",
    }
    if not required <= raw_metadata:
        raise ValueError("Acquisition is missing required raw request timestamps")
    for kind in ("buildings", "addresses"):
        payloads = {
            name
            for name in manifest["artifacts"]
            if name.startswith(f"raw/{kind}/") and not name.endswith(".metadata.json")
        }
        pages = payloads - {f"raw/{kind}/hits.xml", f"raw/{kind}/hits-after.xml"}
        if not pages or any(name + ".metadata.json" not in raw_metadata for name in payloads):
            raise ValueError("Every WFS page requires its own raw request timestamp")
    for name in raw_metadata:
        metadata = load_json(safe_child(sources, name))
        raw_name = name.removesuffix(".metadata.json")
        if metadata.get("sha256") != manifest["artifacts"].get(raw_name):
            raise ValueError("Raw request metadata does not match its frozen payload")
        times.append(metadata["retrieved_at"])
    times.extend(
        manifest["sources"][role]["retrieved_at"] for role in expected_roles - {"population"}
    )
    times.append(manifest["sources"]["population"]["generated_at"])
    for value in times:
        observed = datetime.fromisoformat(value)
        if observed.tzinfo is None or observed <= frozen_at:
            raise ValueError(
                "Fresh acquisition and regional extraction must follow protocol commit"
            )


def evaluate_checkpoint(
    protocol_path: Path, protocol_sha256: str, prepared: Path, sources: Path, output: Path
) -> dict:
    protocol, freeze = verify_protocol(protocol_path, protocol_sha256)
    output = output.resolve()
    allowed_root = (ROOT / ".local/evaluations").resolve()
    if output == allowed_root or not output.is_relative_to(allowed_root):
        raise ValueError("Evaluation results must stay in isolated ignored local storage")
    audit = historical_audit(protocol)
    prepared_manifest, sources_manifest, memberships = verify_prepared(
        prepared, sources, protocol, protocol_sha256, audit
    )
    verify_acquisition_dates(sources, sources_manifest, protocol, freeze)
    input_producers = {
        "preparation": prepared_manifest.get("producer"),
        "acquisition": load_json(sources / "producer.json")
        if "producer.json" in sources_manifest["artifacts"]
        else None,
    }
    for role, base in (("preparation", BACKEND_DIRECTORY), ("acquisition", ROOT)):
        if input_producers[role] is not None:
            for path, expected in input_producers[role]["source_sha256"].items():
                if file_hash(safe_child(base, path)) != expected:
                    raise ValueError("Data producer changed after the frozen source preparation")
    identity = {
        "protocol_sha256": protocol_sha256,
        "prepared_manifest_sha256": file_hash(prepared / "manifest.json"),
        "sources_manifest_sha256": file_hash(sources / "manifest.json"),
        "models": protocol["models"],
    }
    city_key = content_hash({"municipality_id": "1463011"})
    ledger_directory = allowed_root / ".consumption"
    ledger_path = ledger_directory / f"{city_key}.json"
    if ledger_path.exists():
        return reuse_completed(claim_checkpoint(ledger_directory, city_key, identity), output)
    if output.exists():
        raise ValueError("Fresh evaluation output directory must not already exist")
    producer = producer_identity()
    feature_rows = list(read_records(prepared / "features.jsonl"))
    features = {row["building_source_id"]: row for row in feature_rows}
    if len(features) != len(feature_rows) or set(features) != {
        row["building_source_id"] for row in memberships
    }:
        raise ValueError("Fresh features must exactly cover the complete candidate frame")
    claim = claim_checkpoint(ledger_directory, city_key, identity)
    if claim["state"] == "complete":
        return reuse_completed(claim, output)
    import joblib

    started = time.perf_counter()
    models = {name: joblib.load(ROOT / model["path"]) for name, model in protocol["models"].items()}
    report, predictions = evaluate_fresh(features, memberships, models, protocol)
    for model in protocol["models"].values():
        if file_hash(ROOT / model["path"]) != model["sha256"]:
            raise ValueError("Frozen model artifact changed during evaluation")
    report.update(
        {
            "identity": identity,
            "protocol_freeze": freeze,
            "producer": producer,
            "input_producers": input_producers,
            "evaluated_at": utc_now(),
            "evaluation_seconds": time.perf_counter() - started,
            "freshness": prepared_manifest["freshness"],
            "candidate_frame": prepared_manifest["candidate_frame"],
            "source_quality": load_json(prepared / "quality.json"),
            "sources": load_json(prepared / "sources.json"),
            "source_dates": sources_manifest["sources"],
            "checkpoint_usage": {
                "consumed_before_scoring": True,
                "reruns": "immutable_result_reuse_only",
            },
        }
    )
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "evaluation.json", report)
    write_json(output / "audit.json", audit)
    write_json(output / "lock.json", {"identity": identity, "producer": producer, "freeze": freeze})
    with (output / "predictions.jsonl").open("w", encoding="utf-8", newline="\n") as stream:
        for row in predictions:
            stream.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
    write_json(
        output / "manifest.json",
        {
            "identity": identity,
            "created_at": utc_now(),
            "artifacts": {name: file_hash(output / name) for name in sorted(RESULT_FILES)},
            "rankings_enabled": False,
        },
    )
    complete_checkpoint(ledger_directory, city_key, claim, output)
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--protocol", type=Path, default=PROTOCOL_PATH)
    parser.add_argument("--protocol-sha256", required=True)
    parser.add_argument("--prepared-dir", type=Path, required=True)
    parser.add_argument("--sources-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    arguments = parser.parse_args()
    report = evaluate_checkpoint(
        arguments.protocol,
        arguments.protocol_sha256,
        arguments.prepared_dir,
        arguments.sources_dir,
        arguments.output_dir,
    )
    print(
        json.dumps(
            {
                "status": report["status"],
                "support": report["support"],
                "numeric_gate": report["numeric_gate"],
                "promotion_gate": report["promotion_gate"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
