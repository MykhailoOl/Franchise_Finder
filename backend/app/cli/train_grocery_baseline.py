import argparse
import importlib.metadata
import json
import platform
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import joblib

from app.cli.extract_grocery_features import (
    OUTPUT_FILES as FEATURE_FILES,
)
from app.cli.extract_grocery_features import (
    file_hash,
    load_json,
    read_records,
    verify_artifacts,
    write_json,
)
from app.core.config import BACKEND_DIRECTORY
from app.core.experiment import canonical_bytes, configuration_hash, content_hash, load_experiment
from app.core.grocery_baseline import (
    BASELINE_RECIPE,
    BASELINE_VERSION,
    BaselineConvergenceError,
    describe_baseline,
)
from app.core.grocery_evaluation import (
    EVALUATION_VERSION,
    METRICS,
    evaluate_final,
    fit_and_evaluate_development,
    scenario_definitions,
    validate_memberships,
)
from app.core.grocery_features import EXTRACTION_VERSION

LOCAL_DIRECTORY = BACKEND_DIRECTORY.parent / ".local"
RECIPE_PATH = BACKEND_DIRECTORY / "app" / "experiments" / "warsaw-grocery-logistic-v1.json"
SCENARIOS = ("primary", "no_mapped_use", "all_valid")
MODELS = ("candidate", "population_only", "access_only")
MODEL_FILES = {f"{scenario}__{model}.joblib" for scenario in SCENARIOS for model in MODELS}
TRAINING_FILES = MODEL_FILES | {
    "development-evaluation.json",
    "development-predictions.jsonl",
    "model-details.json",
    "lock.json",
    "recipe.json",
    "feature-sources.json",
}
EVALUATION_FILES = {"evaluation.json", "predictions.jsonl"}


def expected_recipe(definition) -> dict:
    return {
        "version": BASELINE_VERSION,
        "experiment_version": definition.version,
        "feature_version": EXTRACTION_VERSION,
        "evaluation_version": EVALUATION_VERSION,
        "category": "grocery",
        "family": "logistic_l2",
        "preprocessing": BASELINE_RECIPE,
        "scenarios": scenario_definitions(definition),
        "C_grid": definition.models["logistic_l2"]["C"],
        "restricted_baseline_C": 1.0,
        "outer_folds": [0, 1, 2],
        "outer_training": "frozen_training_folds_contains_outer_fold",
        "outer_validation": "frozen_validation_fold_equals_outer_fold",
        "inner_training": "intersection_of_outer_and_inner_frozen_training_memberships",
        "inner_validation": "outer_training_members_with_validation_fold_equal_inner_fold",
        "nested_C_selection": "highest_mean_inner_average_precision_ties_smaller_C",
        "unsupported_inner_fold": "disable_entire_outer_tuning_use_fixed_C_1_and_report_reason",
        "final_C_selection": "highest_mean_fixed_C_development_fold_AP_ties_smaller_C",
        "final_training": "development_members_with_3_in_training_folds",
        "final_evaluation": "northeast_fold_3_once_after_all_models_and_choices_locked",
        "holdout_reuse": "exploratory_metrics_promotion_withheld_for_different_training_lock",
        "metrics": list(METRICS),
        "average_precision": "non_interpolated_average_precision_with_grouped_score_ties",
        "top_20_ties": "fractional_share_of_boundary_tie_group_no_label_based_tiebreak",
        "references": [
            "population_only",
            "access_only",
            "prevalence_reference",
            "random_reference",
        ],
        "random_reference": "analytic_expected_metrics_for_uniform_random_permutation",
        "uncertainty": "three_paired_region_results_and_descriptive_mean_min_max_no_iid_CI",
        "promotion": "frozen_experiment_thresholds_primary_scenario_only",
        "sensitivities": "diagnostic_only_never_replace_primary_after_final_evaluation",
        "target_encoding": {"observed_presence": 1, "unlabelled_background": 0},
        "score_interpretation": "ranking_observed_chain_presence_not_business_success_probability",
        "catboost_trained": False,
        "website_integration": False,
    }


def load_recipe(path: Path, definition) -> dict:
    recipe = load_json(path)
    if recipe != json.loads(canonical_bytes(expected_recipe(definition))):
        raise ValueError("Training recipe differs from the implemented frozen baseline protocol")
    return recipe


def load_feature_manifest(directory: Path, definition) -> dict:
    manifest = load_json(directory / "manifest.json")
    if set(manifest["artifacts"]) != FEATURE_FILES:
        raise ValueError("Feature manifest has an unexpected file set")
    development_files = FEATURE_FILES - {
        "features-final-holdout.jsonl",
        "memberships-final-holdout.jsonl",
    }
    verify_artifacts(
        directory,
        {"artifacts": {name: manifest["artifacts"][name] for name in development_files}},
        development_files,
    )
    identity = manifest["identity"]
    if identity["config_sha256"] != configuration_hash(definition):
        raise ValueError("Feature snapshot belongs to a different experiment configuration")
    if identity["extraction_version"] != EXTRACTION_VERSION:
        raise ValueError("Unsupported feature extraction version")
    if manifest["experiment"] != definition.model_dump(mode="json"):
        raise ValueError("Embedded feature experiment definition has changed")
    if set(manifest["predictor_definitions"]) != set(definition.primary_predictors):
        raise ValueError("Feature snapshot does not contain the frozen 17 predictors")
    if manifest["status"] != "extracted_not_trained" or manifest["training_performed"]:
        raise ValueError("Expected an untrained frozen feature snapshot")
    return manifest


def load_partition(directory: Path, definition, *, final: bool = False) -> tuple[dict, list]:
    partition = "final-holdout" if final else "development"
    memberships = list(read_records(directory / f"memberships-{partition}.jsonl"))
    validate_memberships(memberships, final=final)
    required = {row["building_source_id"] for row in memberships}
    features = {}
    for row in read_records(directory / f"features-{partition}.jsonl"):
        identifier = row["building_source_id"]
        if identifier in features:
            raise ValueError("Feature snapshot repeats a stable building identity")
        if row["extraction_version"] != EXTRACTION_VERSION:
            raise ValueError("Feature row extraction version differs from its manifest")
        if any(
            set(row[field]) != set(definition.primary_predictors)
            for field in ("values", "observations")
        ):
            raise ValueError("Feature row does not match the frozen predictor set")
        features[identifier] = row
    if set(features) != required:
        raise ValueError("Feature rows and frozen memberships do not join exactly")
    return features, memberships


def producer_identity() -> dict:
    paths = (
        "app/cli/train_grocery_baseline.py",
        "app/core/grocery_baseline.py",
        "app/core/grocery_evaluation.py",
        "app/core/experiment.py",
        "app/core/data_contract.py",
        "app/core/grocery_features.py",
        "app/cli/extract_grocery_features.py",
        "app/experiments/warsaw-grocery-logistic-v1.json",
        "pyproject.toml",
        "uv.lock",
    )
    hashes = {name: file_hash(BACKEND_DIRECTORY / name) for name in paths}
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=BACKEND_DIRECTORY,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--", *paths],
            cwd=BACKEND_DIRECTORY,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError):
        commit, dirty = None, None
    return {
        "code_sha256": content_hash(hashes),
        "source_sha256": hashes,
        "git_commit": commit,
        "producer_worktree_status": dirty,
        "python": platform.python_version(),
        "packages": {
            name: importlib.metadata.version(name)
            for name in ("scikit-learn", "numpy", "scipy", "joblib", "threadpoolctl")
        },
    }


def write_records(path: Path, rows) -> None:
    with path.open("xb") as stream:
        for row in rows:
            stream.write(canonical_bytes(row) + b"\n")


def verify_training(directory: Path, identity: dict) -> dict:
    manifest = load_json(directory / "manifest.json")
    if manifest["identity"] != identity or manifest["state"] != "locked_before_final":
        raise ValueError("Stored training run does not match this frozen identity")
    verify_artifacts(directory, manifest, TRAINING_FILES)
    lock = load_json(directory / "lock.json")
    if lock["identity"] != identity:
        raise ValueError("Model lock differs from the training manifest")
    for name in MODEL_FILES:
        if lock["model_files"][name] != manifest["artifacts"][name]:
            raise ValueError("Model binary hash differs from the pre-evaluation lock")
    return manifest


def locked_models(directory: Path) -> dict:
    return {
        scenario: {name: joblib.load(directory / f"{scenario}__{name}.joblib") for name in MODELS}
        for scenario in SCENARIOS
    }


def mark_final_read(directory: Path, feature_manifest: dict, training_hash: str) -> dict:
    source_hashes = {
        name: feature_manifest["artifacts"][name]
        for name in ("features-final-holdout.jsonl", "memberships-final-holdout.jsonl")
    }
    usage_directory = LOCAL_DIRECTORY / "models" / ".holdout-usage" / content_hash(source_hashes)
    usage_directory.mkdir(parents=True, exist_ok=True)
    entry_path = usage_directory / f"{training_hash}.json"
    if entry_path.exists():
        entry = load_json(entry_path)
        if entry["training_manifest_sha256"] != training_hash or entry["sources"] != source_hashes:
            raise ValueError("Final holdout access record differs from the locked input")
        return entry
    previous = sorted(path.stem for path in usage_directory.glob("*.json"))
    entry = {
        "training_manifest_sha256": training_hash,
        "lock_sha256": file_hash(directory / "lock.json"),
        "sources": source_hashes,
        "started_at": datetime.now(UTC),
        "previous_training_runs": previous,
        "previously_accessed_for_other_training_run": bool(previous),
    }
    staging = usage_directory / (".pending-" + uuid4().hex)
    write_json(staging, entry)
    staging.rename(entry_path)
    return load_json(entry_path)


def prepare_training(
    feature_directory, output_root, definition, recipe, feature_manifest, producer
):
    output_root = output_root.resolve()
    if not output_root.is_relative_to(LOCAL_DIRECTORY.resolve()):
        raise ValueError("Model artifacts must remain inside the ignored project .local directory")
    identity = {
        "version": BASELINE_VERSION,
        "recipe_sha256": content_hash(recipe),
        "config_sha256": configuration_hash(definition),
        "feature_manifest_sha256": file_hash(feature_directory / "manifest.json"),
        "feature_identity": feature_manifest["identity"],
        "code_sha256": producer["code_sha256"],
        "python": producer["python"],
        "packages": producer["packages"],
    }
    destination = output_root / ("grocery-logistic-v1_" + content_hash(identity)[:24])
    if destination.exists():
        verify_training(destination, identity)
        return destination
    output_root.mkdir(parents=True, exist_ok=True)
    staging = output_root / (".pending-" + uuid4().hex)
    staging.mkdir(exist_ok=False)
    started = time.perf_counter()
    features, memberships = load_partition(feature_directory, definition)
    print(json.dumps({"phase": "development", "buildings": len(features)}), flush=True)
    report, models, engine_lock = fit_and_evaluate_development(features, memberships, definition)
    predictions = report.pop("prediction_rows")
    write_records(staging / "development-predictions.jsonl", predictions)
    report["duration_seconds"] = round(time.perf_counter() - started, 3)
    write_json(staging / "development-evaluation.json", report)
    descriptions = {}
    for scenario, items in models.items():
        descriptions[scenario] = {}
        for name, fitted in items.items():
            joblib.dump(fitted, staging / f"{scenario}__{name}.joblib", compress=3)
            descriptions[scenario][name] = describe_baseline(fitted)
    write_json(staging / "model-details.json", descriptions)
    write_json(staging / "recipe.json", recipe)
    write_json(staging / "feature-sources.json", load_json(feature_directory / "sources.json"))
    write_json(
        staging / "lock.json",
        {
            **engine_lock,
            "identity": identity,
            "locked_at": datetime.now(UTC),
            "final_holdout_records_read": False,
            "model_files": {name: file_hash(staging / name) for name in sorted(MODEL_FILES)},
        },
    )
    write_json(
        staging / "manifest.json",
        {
            "identity": identity,
            "state": "locked_before_final",
            "producer": producer,
            "created_at": datetime.now(UTC),
            "feature_artifact_hashes": feature_manifest["artifacts"],
            "website_integration": False,
            "artifacts": {name: file_hash(staging / name) for name in sorted(TRAINING_FILES)},
        },
    )
    staging.rename(destination)
    verify_training(destination, identity)
    return destination


def finish_evaluation(directory, feature_directory, definition, feature_manifest):
    training = load_json(directory / "manifest.json")
    verify_training(directory, training["identity"])
    if (
        file_hash(feature_directory / "manifest.json")
        != training["identity"]["feature_manifest_sha256"]
        or feature_manifest["artifacts"] != training["feature_artifact_hashes"]
    ):
        raise ValueError("Final feature snapshot differs from the locked training input")
    evaluation_directory = directory / "evaluation"
    training_hash = file_hash(directory / "manifest.json")
    if evaluation_directory.exists():
        existing = load_json(evaluation_directory / "manifest.json")
        if existing["training_manifest_sha256"] != training_hash:
            raise ValueError("Evaluation is bound to a different model lock")
        verify_artifacts(feature_directory, feature_manifest, FEATURE_FILES)
        verify_artifacts(evaluation_directory, existing, EVALUATION_FILES)
        return evaluation_directory
    models = locked_models(directory)
    lock = load_json(directory / "lock.json")
    access_record = mark_final_read(directory, feature_manifest, training_hash)
    verify_artifacts(feature_directory, feature_manifest, FEATURE_FILES)
    started = time.perf_counter()
    print(json.dumps({"phase": "locked_final_evaluation", "models_refitted": False}), flush=True)
    features, memberships = load_partition(feature_directory, definition, final=True)
    report, predictions = evaluate_final(models, features, memberships, definition, lock)
    reused = access_record["previously_accessed_for_other_training_run"]
    report["confirmatory_for_this_lock"] = not reused
    if reused:
        report["phase"] = "reused_final_exploratory"
        report["promotion_gate"]["metric_only_status"] = report["promotion_gate"]["status"]
        report["promotion_gate"]["status"] = "withheld_reused_holdout"
        report["promotion_gate"]["reason"] = "Final data was accessed by a different training lock"
    staging = directory / (".evaluation-pending-" + uuid4().hex)
    staging.mkdir(exist_ok=False)
    report.update(
        {
            "duration_seconds": round(time.perf_counter() - started, 3),
            "evaluated_at": datetime.now(UTC),
            "training_manifest_sha256": training_hash,
            "lock_sha256": file_hash(directory / "lock.json"),
            "models_refitted": False,
            "website_integration": False,
            "holdout_access": access_record,
            "interpretation": recipe_interpretation(),
        }
    )
    write_json(staging / "evaluation.json", report)
    write_records(staging / "predictions.jsonl", predictions)
    write_json(
        staging / "manifest.json",
        {
            "training_manifest_sha256": training_hash,
            "final_feature_file_sha256": feature_manifest["artifacts"][
                "features-final-holdout.jsonl"
            ],
            "final_membership_file_sha256": feature_manifest["artifacts"][
                "memberships-final-holdout.jsonl"
            ],
            "state": "evaluated_locked_baseline",
            "artifacts": {name: file_hash(staging / name) for name in sorted(EVALUATION_FILES)},
        },
    )
    staging.rename(evaluation_directory)
    return evaluation_directory


def recipe_interpretation() -> dict:
    return {
        "target": "Observed selected-chain grocery presence versus unlabelled background",
        "scores": "Relative ranking scores, not business-success or profit probabilities",
        "population": "Partial 2021 published-cell subtotals with coverage indicators",
        "uncertainty": "Three geographic development regions; spread is not a confidence interval",
        "mapped_use": "May reflect existing occupancy; no-mapped-use run is diagnostic only",
        "sample": "Selected chains, dominated by Zabka; not evidence for all grocery brands",
        "final_holdout": "Evaluated for locked models; later tuning cannot call it unseen",
        "catboost": "Not trained or compared in this baseline checkpoint",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Train and evaluate the frozen grocery baseline")
    parser.add_argument("--features", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, default=LOCAL_DIRECTORY / "models")
    parser.add_argument("--recipe", type=Path, default=RECIPE_PATH)
    arguments = parser.parse_args()
    try:
        definition = load_experiment()
        recipe = load_recipe(arguments.recipe, definition)
        feature_manifest = load_feature_manifest(arguments.features, definition)
        producer = producer_identity()
        directory = prepare_training(
            arguments.features,
            arguments.output_root,
            definition,
            recipe,
            feature_manifest,
            producer,
        )
        evaluation = finish_evaluation(directory, arguments.features, definition, feature_manifest)
    except (OSError, ValueError, KeyError, TypeError, BaselineConvergenceError) as error:
        parser.exit(1, f"Grocery baseline failed: {error}\n")
    print(
        json.dumps(
            {
                "status": "evaluated_not_integrated",
                "artifact_directory": str(directory),
                "evaluation_report": str(evaluation / "evaluation.json"),
            }
        )
    )


if __name__ == "__main__":
    main()
