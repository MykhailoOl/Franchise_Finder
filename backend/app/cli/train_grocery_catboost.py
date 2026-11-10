import argparse
import importlib.metadata
import json
import os
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
from app.cli.train_grocery_baseline import (
    EVALUATION_FILES as LOGISTIC_EVALUATION_FILES,
)
from app.cli.train_grocery_baseline import (
    LOCAL_DIRECTORY,
    SCENARIOS,
    load_feature_manifest,
    load_partition,
    mark_final_read,
    write_records,
)
from app.cli.train_grocery_baseline import (
    load_recipe as load_logistic_recipe,
)
from app.cli.train_grocery_baseline import (
    verify_training as verify_logistic_training,
)
from app.core.config import BACKEND_DIRECTORY
from app.core.experiment import canonical_bytes, configuration_hash, content_hash, load_experiment
from app.core.grocery_baseline import BASELINE_VERSION
from app.core.grocery_catboost import (
    CATBOOST_RECIPE,
    CATBOOST_VERSION,
    CatBoostTrainingError,
    describe_catboost,
    explain_catboost,
    verify_catboost,
)
from app.core.grocery_catboost_evaluation import (
    EVALUATION_VERSION,
    evaluate_catboost_final,
    fit_and_evaluate_catboost_development,
)
from app.core.grocery_evaluation import METRICS, scenario_definitions
from app.core.grocery_features import EXTRACTION_VERSION

RECIPE_PATH = BACKEND_DIRECTORY / "app" / "experiments" / "warsaw-grocery-catboost-v1.json"
MODEL_FILES = {f"{scenario}.{suffix}" for scenario in SCENARIOS for suffix in ("joblib", "cbm")}
TRAINING_FILES = MODEL_FILES | {
    "development-evaluation.json",
    "development-predictions.jsonl",
    "model-details.json",
    "lock.json",
    "recipe.json",
    "feature-sources.json",
    "logistic-lineage.json",
}
EVALUATION_FILES = {"evaluation.json", "predictions.jsonl", "shap-review.json"}


def expected_recipe(definition) -> dict:
    return {
        "version": CATBOOST_VERSION,
        "experiment_version": definition.version,
        "feature_version": EXTRACTION_VERSION,
        "evaluation_version": EVALUATION_VERSION,
        "category": "grocery",
        "family": "CatBoostClassifier",
        "model": CATBOOST_RECIPE,
        "frozen_model_bounds": definition.models["catboost_cpu"],
        "scenarios": scenario_definitions(definition),
        "outer_folds": [0, 1, 2],
        "outer_training": "frozen_training_folds_contains_outer_fold",
        "outer_validation": "frozen_validation_fold_equals_outer_fold",
        "inner_training": "intersection_of_outer_and_inner_frozen_training_memberships",
        "inner_validation": "outer_training_members_with_validation_fold_equal_inner_fold",
        "nested_depth_selection": "highest_mean_inner_AP_ties_shallower_depth",
        "unsupported_inner_fold": "disable_entire_outer_tuning_use_fixed_depth_4_report_reason",
        "final_depth_selection": "highest_mean_fixed_depth_development_fold_AP_ties_shallower",
        "final_training": "development_members_with_3_in_training_folds",
        "final_evaluation": "reused_northeast_fold_3_after_all_models_and_choices_locked",
        "comparison": "exact_paired_saved_logistic_predictions_no_logistic_refitting",
        "metrics": list(METRICS),
        "top_20_ties": "fractional_share_of_boundary_tie_group_no_label_based_tiebreak",
        "uncertainty": "three_paired_region_results_and_descriptive_spread_no_iid_CI",
        "promotion": "always_withheld_reused_holdout_numeric_thresholds_diagnostic_only",
        "sensitivities": "diagnostic_only_never_replace_primary_after_final_evaluation",
        "shap_review": {
            "partition": "final_holdout",
            "rows_per_scenario": 20,
            "selection": (
                "ascending_sha256_of_canonical_building_source_id_independent_of_labels_scores"
            ),
            "reference_data": "none",
            "assertion": "sum_native_contributions_plus_expected_value_equals_raw_margin",
        },
        "target_encoding": {"observed_presence": 1, "unlabelled_background": 0},
        "score_interpretation": "observed_chain_presence_ranking_not_business_success_probability",
        "website_integration": False,
    }


def load_recipe(path: Path, definition) -> dict:
    recipe = load_json(path)
    if recipe != json.loads(canonical_bytes(expected_recipe(definition))):
        raise ValueError("CatBoost recipe differs from the implemented frozen protocol")
    return recipe


def load_logistic_lineage(directory: Path, feature_directory: Path, feature_manifest, definition):
    manifest = load_json(directory / "manifest.json")
    verify_logistic_training(directory, manifest["identity"])
    load_logistic_recipe(directory / "recipe.json", definition)
    identity = manifest["identity"]
    if (
        identity["version"] != BASELINE_VERSION
        or identity["config_sha256"] != configuration_hash(definition)
        or identity["feature_manifest_sha256"] != file_hash(feature_directory / "manifest.json")
        or identity["feature_identity"] != feature_manifest["identity"]
        or manifest["feature_artifact_hashes"] != feature_manifest["artifacts"]
    ):
        raise ValueError("Saved logistic baseline uses different features or frozen splits")
    evaluation_manifest = load_json(directory / "evaluation" / "manifest.json")
    if (
        evaluation_manifest["training_manifest_sha256"] != file_hash(directory / "manifest.json")
        or evaluation_manifest["final_feature_file_sha256"]
        != feature_manifest["artifacts"]["features-final-holdout.jsonl"]
        or evaluation_manifest["final_membership_file_sha256"]
        != feature_manifest["artifacts"]["memberships-final-holdout.jsonl"]
        or set(evaluation_manifest["artifacts"]) != LOGISTIC_EVALUATION_FILES
    ):
        raise ValueError("Saved logistic evaluation does not match the frozen final partition")
    return {
        "version": BASELINE_VERSION,
        "training_manifest_sha256": file_hash(directory / "manifest.json"),
        "evaluation_manifest_sha256": file_hash(directory / "evaluation" / "manifest.json"),
        "development_predictions_sha256": manifest["artifacts"]["development-predictions.jsonl"],
        "final_predictions_sha256": evaluation_manifest["artifacts"]["predictions.jsonl"],
        "identity": identity,
        "logistic_refitted": False,
        "final_holdout_already_evaluated": True,
    }


def producer_identity() -> dict:
    paths = (
        "app/cli/train_grocery_catboost.py",
        "app/core/grocery_catboost.py",
        "app/core/grocery_catboost_evaluation.py",
        "app/cli/train_grocery_baseline.py",
        "app/core/grocery_baseline.py",
        "app/core/grocery_evaluation.py",
        "app/core/experiment.py",
        "app/core/data_contract.py",
        "app/core/grocery_features.py",
        "app/cli/extract_grocery_features.py",
        "app/experiments/warsaw-grocery-catboost-v1.json",
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
        "hardware": {
            "platform": platform.platform(),
            "processor": platform.processor(),
            "logical_cpu_count": os.cpu_count(),
            "training_threads": 4,
        },
        "packages": {
            name: importlib.metadata.version(name)
            for name in ("catboost", "numpy", "scipy", "pandas", "joblib", "scikit-learn")
        },
    }


def verify_training(directory: Path, identity: dict) -> dict:
    manifest = load_json(directory / "manifest.json")
    if manifest["identity"] != identity or manifest["state"] != "locked_before_final":
        raise ValueError("Stored CatBoost run does not match this frozen identity")
    verify_artifacts(directory, manifest, TRAINING_FILES)
    lock = load_json(directory / "lock.json")
    if lock["identity"] != identity or set(lock["model_files"]) != MODEL_FILES:
        raise ValueError("CatBoost model lock differs from the training manifest")
    if any(lock["model_files"][name] != manifest["artifacts"][name] for name in MODEL_FILES):
        raise ValueError("CatBoost binary hash differs from the pre-evaluation lock")
    return manifest


def prepare_training(
    feature_directory,
    logistic_directory,
    output_root,
    definition,
    recipe,
    feature_manifest,
    lineage,
    producer,
):
    output_root = output_root.resolve()
    if not output_root.is_relative_to(LOCAL_DIRECTORY.resolve()):
        raise ValueError("Model artifacts must remain inside the ignored project .local directory")
    identity = {
        "version": CATBOOST_VERSION,
        "recipe_sha256": content_hash(recipe),
        "config_sha256": configuration_hash(definition),
        "feature_manifest_sha256": file_hash(feature_directory / "manifest.json"),
        "feature_identity": feature_manifest["identity"],
        "logistic_lineage_sha256": content_hash(lineage),
        "code_sha256": producer["code_sha256"],
        "python": producer["python"],
        "packages": producer["packages"],
    }
    destination = output_root / ("grocery-catboost-v1_" + content_hash(identity)[:24])
    if destination.exists():
        verify_training(destination, identity)
        return destination
    output_root.mkdir(parents=True, exist_ok=True)
    staging = output_root / (".pending-" + uuid4().hex)
    staging.mkdir(exist_ok=False)
    started = time.perf_counter()
    features, memberships = load_partition(feature_directory, definition)
    logistic_predictions = list(read_records(logistic_directory / "development-predictions.jsonl"))
    print(json.dumps({"phase": "development", "buildings": len(features)}), flush=True)
    report, models, engine_lock = fit_and_evaluate_catboost_development(
        features, memberships, definition, logistic_predictions
    )
    write_records(staging / "development-predictions.jsonl", report.pop("prediction_rows"))
    report["duration_seconds"] = round(time.perf_counter() - started, 3)
    write_json(staging / "development-evaluation.json", report)
    descriptions = {}
    for scenario, fitted in models.items():
        joblib.dump(fitted, staging / f"{scenario}.joblib", compress=3)
        fitted.model.save_model(str(staging / f"{scenario}.cbm"), format="cbm")
        descriptions[scenario] = describe_catboost(fitted)
    write_json(staging / "model-details.json", descriptions)
    write_json(staging / "recipe.json", recipe)
    write_json(staging / "feature-sources.json", load_json(feature_directory / "sources.json"))
    write_json(staging / "logistic-lineage.json", lineage)
    write_json(
        staging / "lock.json",
        {
            **engine_lock,
            "identity": identity,
            "locked_at": datetime.now(UTC),
            "final_holdout_records_read": False,
            "final_holdout_previously_evaluated_by_logistic": True,
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


def load_final_logistic(directory, lineage):
    if (
        file_hash(directory / "manifest.json") != lineage["training_manifest_sha256"]
        or file_hash(directory / "evaluation" / "manifest.json")
        != lineage["evaluation_manifest_sha256"]
    ):
        raise ValueError("Saved logistic artifact changed after CatBoost model lock")
    manifest = load_json(directory / "evaluation" / "manifest.json")
    verify_artifacts(directory / "evaluation", manifest, LOGISTIC_EVALUATION_FILES)
    if manifest["artifacts"]["predictions.jsonl"] != lineage["final_predictions_sha256"]:
        raise ValueError("Saved final logistic predictions differ from the CatBoost lock")
    return list(read_records(directory / "evaluation" / "predictions.jsonl"))


def shap_review(models, features, memberships, definition):
    result = {}
    for scenario, specification in scenario_definitions(definition).items():
        identifiers = {
            row["building_source_id"] for row in memberships if row["pool"] == specification["pool"]
        }
        selected = sorted(
            identifiers, key=lambda identifier: (content_hash(identifier), identifier)
        )[:20]
        explanations = explain_catboost(models[scenario], [features[key] for key in selected])
        result[scenario] = [
            {
                "building_source_id": identifier,
                "values": {
                    name: features[identifier]["values"][name]
                    for name in specification["predictors"]
                },
                "observations": {
                    name: features[identifier]["observations"][name]
                    for name in specification["predictors"]
                },
                **explanation,
            }
            for identifier, explanation in zip(selected, explanations, strict=True)
        ]
    return {
        "selection": "20_smallest_stable_ID_hashes_per_scenario_independent_of_labels_scores",
        "partition": "reused_final_holdout",
        "interpretation": "contributions_explain_model_raw_margin_not_causal_business_effects",
        "scenarios": result,
    }


def finish_evaluation(
    directory, feature_directory, logistic_directory, definition, feature_manifest
):
    training = load_json(directory / "manifest.json")
    verify_training(directory, training["identity"])
    if (
        file_hash(feature_directory / "manifest.json")
        != training["identity"]["feature_manifest_sha256"]
        or feature_manifest["artifacts"] != training["feature_artifact_hashes"]
    ):
        raise ValueError("Final feature snapshot differs from the locked training input")
    lineage = load_json(directory / "logistic-lineage.json")
    if content_hash(lineage) != training["identity"]["logistic_lineage_sha256"]:
        raise ValueError("Logistic comparison lineage differs from the CatBoost lock")
    training_hash = file_hash(directory / "manifest.json")
    evaluation_directory = directory / "evaluation"
    if evaluation_directory.exists():
        existing = load_json(evaluation_directory / "manifest.json")
        if existing["training_manifest_sha256"] != training_hash:
            raise ValueError("Evaluation is bound to a different CatBoost lock")
        verify_artifacts(feature_directory, feature_manifest, FEATURE_FILES)
        load_final_logistic(logistic_directory, lineage)
        verify_artifacts(evaluation_directory, existing, EVALUATION_FILES)
        return evaluation_directory
    models = {scenario: joblib.load(directory / f"{scenario}.joblib") for scenario in SCENARIOS}
    for fitted in models.values():
        verify_catboost(fitted)
    lock = load_json(directory / "lock.json")
    access_record = mark_final_read(directory, feature_manifest, training_hash)
    verify_artifacts(feature_directory, feature_manifest, FEATURE_FILES)
    logistic_predictions = load_final_logistic(logistic_directory, lineage)
    started = time.perf_counter()
    print(json.dumps({"phase": "locked_final_comparison", "models_refitted": False}), flush=True)
    features, memberships = load_partition(feature_directory, definition, final=True)
    report, predictions = evaluate_catboost_final(
        models, features, memberships, definition, lock, logistic_predictions
    )
    explanations = shap_review(models, features, memberships, definition)
    staging = directory / (".evaluation-pending-" + uuid4().hex)
    staging.mkdir(exist_ok=False)
    report.update(
        {
            "duration_seconds": round(time.perf_counter() - started, 3),
            "evaluated_at": datetime.now(UTC),
            "training_manifest_sha256": training_hash,
            "lock_sha256": file_hash(directory / "lock.json"),
            "logistic_lineage": lineage,
            "confirmatory_for_this_lock": False,
            "models_refitted": False,
            "website_integration": False,
            "holdout_access": access_record,
            "interpretation": {
                "target": "Observed selected-chain grocery presence versus unlabelled background",
                "scores": "Relative ranking scores, not business-success or profit probabilities",
                "population": "Partial 2021 published-cell subtotals with coverage indicators",
                "uncertainty": "Three geographic regions; spread is not a confidence interval",
                "mapped_use": "May reflect occupancy; no-mapped-use run is diagnostic only",
                "sample": "Selected chains dominated by Zabka; not evidence for all grocery brands",
                "final_holdout": "Already evaluated for logistic; comparison is exploratory",
                "promotion": "Withheld; independent confirmation requires new untouched data",
            },
        }
    )
    write_json(staging / "evaluation.json", report)
    write_records(staging / "predictions.jsonl", predictions)
    write_json(staging / "shap-review.json", explanations)
    write_json(
        staging / "manifest.json",
        {
            "training_manifest_sha256": training_hash,
            "state": "evaluated_locked_catboost_exploratory",
            "artifacts": {name: file_hash(staging / name) for name in sorted(EVALUATION_FILES)},
        },
    )
    staging.rename(evaluation_directory)
    return evaluation_directory


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare frozen grocery CatBoost and logistic models"
    )
    parser.add_argument("--features", type=Path, required=True)
    parser.add_argument("--logistic", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, default=LOCAL_DIRECTORY / "models")
    parser.add_argument("--recipe", type=Path, default=RECIPE_PATH)
    arguments = parser.parse_args()
    try:
        definition = load_experiment()
        recipe = load_recipe(arguments.recipe, definition)
        feature_manifest = load_feature_manifest(arguments.features, definition)
        lineage = load_logistic_lineage(
            arguments.logistic, arguments.features, feature_manifest, definition
        )
        directory = prepare_training(
            arguments.features,
            arguments.logistic,
            arguments.output_root,
            definition,
            recipe,
            feature_manifest,
            lineage,
            producer_identity(),
        )
        evaluation = finish_evaluation(
            directory, arguments.features, arguments.logistic, definition, feature_manifest
        )
    except (OSError, ValueError, KeyError, TypeError, CatBoostTrainingError) as error:
        parser.exit(1, f"Grocery CatBoost failed: {error}\n")
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
