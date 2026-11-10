import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from app.core.data_contract import data_contract
from app.core.experiment import content_hash

EXPOSURE_VERSION = "warsaw-grocery-exposure-v1"
EXPOSURE_POLICY = {
    "version": EXPOSURE_VERSION,
    "exclusion": "any_prior_sampled_feature_extracted_trained_predicted_or_online_profile_ID",
    "minimum_separation_m": 2000,
    "separation_reference": "all_prior_feature_fit_prediction_origins_in_EPSG2180",
    "separation_rule": "strictly_greater_than_2000m",
    "historical_online_exposure": "incomplete_no_durable_full_history",
    "old_study_geography_eligible": False,
    "fresh_geography_requirement": "entirely_new_territory_outside_previous_Warsaw_study_area",
    "readiness_candidate_geometry": "inventoried_not_treated_as_feature_extraction",
    "feature_or_prediction_values_interpreted": False,
}
SCENARIOS = ("primary", "no_mapped_use", "all_valid")
READINESS_FILES = {"candidates.jsonl", "memberships.jsonl", "readiness.json"}
FEATURE_FILES = {
    "features-development.jsonl",
    "features-final-holdout.jsonl",
    "memberships-development.jsonl",
    "memberships-final-holdout.jsonl",
    "quality.json",
    "sources.json",
}
IDENTITY_HEADER = re.compile(r'\s*\{\s*"building_source_id"\s*:\s*')


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _load_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"Expected an artifact object: {path.name}")
    return value


def verify_manifest(directory: Path, expected_files: set[str]) -> dict:
    directory = directory.resolve()
    manifest = _load_json(directory / "manifest.json")
    if set(manifest.get("artifacts", {})) != expected_files:
        raise ValueError(f"Unexpected artifact file set: {directory}")
    for name, expected in manifest["artifacts"].items():
        path = (directory / name).resolve()
        if Path(name).name != name or not path.is_relative_to(directory):
            raise ValueError("Artifact paths must remain in their declared directory")
        if not isinstance(expected, str) or not re.fullmatch("[0-9a-f]{64}", expected):
            raise ValueError("Artifact manifest requires hexadecimal SHA256 fingerprints")
        if file_sha256(path) != expected:
            raise ValueError(f"Artifact content has changed: {name}")
    return manifest


def _identifier(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Exposure records require a nonempty stable building identity")
    return value


def record_identifiers(path: Path, *, unique: bool = False) -> tuple[set[str], int]:
    identifiers, count = set(), 0
    decoder = json.JSONDecoder()
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            header = IDENTITY_HEADER.match(line)
            if header is None:
                raise ValueError("Expected canonical building identity before any value fields")
            identifier, end = decoder.raw_decode(line, header.end())
            identifier = _identifier(identifier)
            if line[end:].lstrip()[:1] not in {",", "}"}:
                raise ValueError("Invalid canonical building identity header")
            if unique and identifier in identifiers:
                raise ValueError("Feature artifact repeats a stable building identity")
            identifiers.add(identifier)
            count += 1
    return identifiers, count


def _memberships(path: Path) -> list[dict]:
    result, seen = [], set()
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            identifier = _identifier(row.get("building_source_id"))
            key = (row.get("category"), row.get("pool"), identifier)
            if key in seen:
                raise ValueError("Membership artifact repeats a building/category/pool")
            if row.get("label") not in {"observed_presence", "unlabelled_background"}:
                raise ValueError("Exposure membership has an unknown observation label")
            seen.add(key)
            result.append(row)
    return result


def _artifact_record(directory: Path, kind: str, manifest: dict, **extra) -> dict:
    return {
        "kind": kind,
        "directory": str(directory.resolve()),
        "manifest_sha256": file_sha256(directory / "manifest.json"),
        "artifact_sha256": manifest["artifacts"],
        **extra,
    }


def _model_files(family: str) -> tuple[set[str], set[str]]:
    if family == "logistic":
        binaries = {
            f"{scenario}__{model}.joblib"
            for scenario in SCENARIOS
            for model in ("candidate", "population_only", "access_only")
        }
    elif family == "catboost":
        binaries = {
            f"{scenario}.{suffix}" for scenario in SCENARIOS for suffix in ("joblib", "cbm")
        }
    else:
        raise ValueError("Exposure inventory supports the existing logistic and CatBoost runs")
    files = binaries | {
        "development-evaluation.json",
        "development-predictions.jsonl",
        "model-details.json",
        "lock.json",
        "recipe.json",
        "feature-sources.json",
    }
    if family == "catboost":
        files.add("logistic-lineage.json")
    return binaries, files


def inventory_exposure(
    readiness_directories: Sequence[Path],
    feature_directory: Path,
    model_directories: Mapping[str, Path],
    online_building_source_ids: Iterable[str] = (),
) -> dict:
    if not readiness_directories or set(model_directories) != {"logistic", "catboost"}:
        raise ValueError(
            "Inventory requires prior readiness artifacts and both existing model runs"
        )
    feature_directory = Path(feature_directory)
    sets = {
        key: set()
        for key in (
            "sampled",
            "feature_extracted",
            "model_training",
            "predicted",
            "online",
            "known_grocery_positive",
        )
    }
    sets["online"] = {_identifier(value) for value in online_building_source_ids}
    artifacts, readiness_manifests = [], []
    for directory in sorted({Path(path).resolve() for path in readiness_directories}):
        manifest = verify_manifest(directory, READINESS_FILES)
        report = _load_json(directory / "readiness.json")
        for key in ("config_sha256", "dataset_sha256"):
            if manifest.get(key) != report.get(key) or not manifest.get(key):
                raise ValueError("Readiness report identity differs from its manifest")
        if (
            content_hash({"experiment": report["experiment"], "data_contract": data_contract()})
            != manifest["config_sha256"]
            or report["candidate_sha256"] != manifest["artifacts"]["candidates.jsonl"]
            or report["memberships_sha256"] != manifest["artifacts"]["memberships.jsonl"]
        ):
            raise ValueError("Readiness configuration or memberships have inconsistent identity")
        rows = _memberships(directory / "memberships.jsonl")
        identifiers = {row["building_source_id"] for row in rows}
        positives = {
            row["building_source_id"]
            for row in rows
            if row["category"] == "grocery" and row["label"] == "observed_presence"
        }
        sets["sampled"].update(identifiers)
        sets["known_grocery_positive"].update(positives)
        readiness_manifests.append(manifest)
        artifacts.append(
            _artifact_record(
                directory,
                "readiness",
                manifest,
                config_sha256=manifest["config_sha256"],
                dataset_sha256=manifest["dataset_sha256"],
                sampled_ids=len(identifiers),
                known_grocery_positive_ids=len(positives),
            )
        )
    feature_manifest = verify_manifest(feature_directory, FEATURE_FILES)
    identity = feature_manifest["identity"]
    if (
        feature_manifest["readiness_manifest"] not in readiness_manifests
        or identity["config_sha256"] != feature_manifest["readiness_manifest"]["config_sha256"]
        or identity["dataset_sha256"] != feature_manifest["readiness_manifest"]["dataset_sha256"]
        or identity["memberships_sha256"]
        != feature_manifest["readiness_manifest"]["artifacts"]["memberships.jsonl"]
        or content_hash(
            {"experiment": feature_manifest["experiment"], "data_contract": data_contract()}
        )
        != identity["config_sha256"]
    ):
        raise ValueError("Feature artifact is not bound to a supplied readiness inventory")
    partitions = {}
    for partition in ("development", "final-holdout"):
        rows = _memberships(feature_directory / f"memberships-{partition}.jsonl")
        if any(row["category"] != "grocery" for row in rows):
            raise ValueError("Grocery feature memberships contain another category")
        identifiers, count = record_identifiers(
            feature_directory / f"features-{partition}.jsonl", unique=True
        )
        if identifiers != {row["building_source_id"] for row in rows}:
            raise ValueError("Feature IDs do not match their frozen membership partition")
        if sets["feature_extracted"] & identifiers:
            raise ValueError("Development and final feature partitions overlap")
        sets["feature_extracted"].update(identifiers)
        sets["model_training"].update(
            row["building_source_id"] for row in rows if row["training_folds"]
        )
        partitions[partition] = rows
        artifacts.append(
            {
                "kind": "feature_partition",
                "partition": partition,
                "record_count": count,
                "unique_ids": len(identifiers),
                "file_sha256": feature_manifest["artifacts"][f"features-{partition}.jsonl"],
            }
        )
    artifacts.append(_artifact_record(feature_directory, "features", feature_manifest))
    if not sets["feature_extracted"] <= sets["sampled"]:
        raise ValueError("Extracted features contain IDs outside supplied readiness inventories")
    feature_manifest_sha = file_sha256(feature_directory / "manifest.json")
    models = {}
    for family, path in sorted(model_directories.items()):
        directory = Path(path)
        binary_files, files = _model_files(family)
        manifest = verify_manifest(directory, files)
        model_identity = manifest["identity"]
        if (
            manifest["state"] != "locked_before_final"
            or model_identity["config_sha256"] != identity["config_sha256"]
            or model_identity["feature_identity"] != identity
            or model_identity["feature_manifest_sha256"] != feature_manifest_sha
            or manifest["feature_artifact_hashes"] != feature_manifest["artifacts"]
        ):
            raise ValueError("Model artifact is not bound to the inventoried feature snapshot")
        lock = _load_json(directory / "lock.json")
        engine = lock["engine"]
        if (
            lock["identity"] != model_identity
            or lock["engine_sha256"] != content_hash(engine)
            or engine["config_sha256"] != identity["config_sha256"]
            or set(lock["model_files"]) != binary_files
            or any(lock["model_files"][key] != manifest["artifacts"][key] for key in binary_files)
            or set(engine["scenarios"]) != set(SCENARIOS)
        ):
            raise ValueError("Model lock identity or binary fingerprints are inconsistent")
        recipe = _load_json(directory / "recipe.json")
        if content_hash(recipe) != model_identity["recipe_sha256"]:
            raise ValueError("Model recipe differs from its frozen identity")
        for scenario, record in engine["scenarios"].items():
            expected_pool = "all_valid" if scenario == "all_valid" else "valid_addressed"
            expected = {
                row["building_source_id"]
                for row in partitions["development"]
                if row["pool"] == expected_pool and 3 in row["training_folds"]
            }
            found = {_identifier(key) for key in record["training_ids"]}
            if record["pool"] != expected_pool or found != expected:
                raise ValueError("Locked training IDs differ from frozen development memberships")
            sets["model_training"].update(found)
        for partition, prediction_path in (
            ("development", directory / "development-predictions.jsonl"),
            ("final-holdout", directory / "evaluation" / "predictions.jsonl"),
        ):
            if partition == "final-holdout":
                evaluation_files = {"evaluation.json", "predictions.jsonl"}
                if family == "catboost":
                    evaluation_files.add("shap-review.json")
                evaluation_manifest = verify_manifest(directory / "evaluation", evaluation_files)
                if evaluation_manifest["training_manifest_sha256"] != file_sha256(
                    directory / "manifest.json"
                ):
                    raise ValueError("Evaluation is not bound to its locked training artifact")
                artifacts.append(
                    _artifact_record(
                        directory / "evaluation", family + "_evaluation", evaluation_manifest
                    )
                )
            identifiers, _ = record_identifiers(prediction_path)
            expected = {
                row["building_source_id"]
                for row in partitions[partition]
                if row["validation_fold"] is not None
            }
            if identifiers != expected:
                raise ValueError("Prediction IDs differ from the frozen evaluated partition")
            sets["predicted"].update(identifiers)
        artifacts.append(_artifact_record(directory, family, manifest))
        models[family] = (directory, manifest)
    catboost_directory, catboost_manifest = models["catboost"]
    logistic_directory, _ = models["logistic"]
    lineage = _load_json(catboost_directory / "logistic-lineage.json")
    if (
        content_hash(lineage) != catboost_manifest["identity"]["logistic_lineage_sha256"]
        or lineage["training_manifest_sha256"] != file_sha256(logistic_directory / "manifest.json")
        or lineage["evaluation_manifest_sha256"]
        != file_sha256(logistic_directory / "evaluation" / "manifest.json")
    ):
        raise ValueError("CatBoost comparison lineage differs from the supplied logistic run")
    sets["excluded"] = set().union(
        *(values for name, values in sets.items() if name != "known_grocery_positive")
    )
    positives = sets["known_grocery_positive"]
    private_ids = {key: sorted(values) for key, values in sets.items()}
    summary = {
        "counts": {name: len(values) for name, values in sets.items()},
        "known_grocery_positive_overlap_count": len(positives & sets["excluded"]),
        "feature_extracted_grocery_positive_count": len(positives & sets["feature_extracted"]),
        "untouched_known_grocery_positive_count": len(positives - sets["excluded"]),
        "historical_online_exposure": "incomplete",
        "old_study_geography_eligible": False,
        "fresh_study_geography_required": True,
        "id_set_sha256": {key: content_hash(value) for key, value in private_ids.items()},
    }
    return {
        "version": EXPOSURE_VERSION,
        "policy": dict(EXPOSURE_POLICY),
        "summary": summary,
        "artifacts": artifacts,
        "private_ids": private_ids,
        "limitations": [
            "Online assessment memory caches expire and are not a durable exposure ledger.",
            "Docker access logs and the current profile cache cannot prove complete past exposure.",
            "A 2 km rule separates 1 km catchments; "
            "it is not evidence of statistical independence.",
            "No new candidate labels, feature values or prediction scores are interpreted; "
            "model binaries are hashed without deserialization.",
        ],
    }
