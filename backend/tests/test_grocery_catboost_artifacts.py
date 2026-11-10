import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.cli import train_grocery_baseline as logistic
from app.cli import train_grocery_catboost as training
from app.core.experiment import canonical_bytes, configuration_hash, load_experiment
from app.core.grocery_features import EXTRACTION_VERSION


class NativeFixture:
    def save_model(self, path, format):
        assert format == "cbm"
        Path(path).write_bytes(b"native fixture")


def feature_snapshot(directory):
    directory.mkdir()
    definition = load_experiment()
    for partition, fold, identifier in (("development", 0, "dev"), ("final-holdout", 3, "final")):
        feature = {
            "building_source_id": identifier,
            "extraction_version": EXTRACTION_VERSION,
            "values": dict.fromkeys(definition.primary_predictors, 1.0),
            "observations": dict.fromkeys(definition.primary_predictors, {}),
        }
        members = [
            {
                "building_source_id": identifier,
                "category": "grocery",
                "pool": pool,
                "label": "observed_presence",
                "fold": fold,
                "validation_fold": fold,
                "training_folds": [] if fold == 3 else [1, 2, 3],
            }
            for pool in ("valid_addressed", "all_valid")
        ]
        training.write_records(directory / f"features-{partition}.jsonl", [feature])
        training.write_records(directory / f"memberships-{partition}.jsonl", members)
    for name in ("sources.json", "quality.json"):
        training.write_json(directory / name, {})
    manifest = {
        "identity": {
            "config_sha256": configuration_hash(definition),
            "extraction_version": EXTRACTION_VERSION,
        },
        "experiment": definition.model_dump(mode="json"),
        "status": "extracted_not_trained",
        "training_performed": False,
        "predictor_definitions": dict.fromkeys(definition.primary_predictors, {}),
        "artifacts": {
            name: training.file_hash(directory / name) for name in training.FEATURE_FILES
        },
    }
    training.write_json(directory / "manifest.json", manifest)
    return definition, manifest


def logistic_snapshot(directory, feature_directory, feature_manifest, definition, overrides=None):
    directory.mkdir()
    for name in logistic.TRAINING_FILES - {"development-predictions.jsonl"}:
        (directory / name).write_bytes(b"{}\n")
    training.write_records(directory / "development-predictions.jsonl", [])
    training.write_json(directory / "recipe.json", logistic.expected_recipe(definition))
    identity = {
        "version": logistic.BASELINE_VERSION,
        "config_sha256": configuration_hash(definition),
        "feature_manifest_sha256": training.file_hash(feature_directory / "manifest.json"),
        "feature_identity": feature_manifest["identity"],
        **(overrides or {}),
    }
    training.write_json(
        directory / "lock.json",
        {
            "identity": identity,
            "model_files": {
                name: training.file_hash(directory / name) for name in logistic.MODEL_FILES
            },
        },
    )
    training.write_json(
        directory / "manifest.json",
        {
            "identity": identity,
            "state": "locked_before_final",
            "feature_artifact_hashes": feature_manifest["artifacts"],
            "artifacts": {
                name: training.file_hash(directory / name) for name in logistic.TRAINING_FILES
            },
        },
    )
    evaluation = directory / "evaluation"
    evaluation.mkdir()
    training.write_records(evaluation / "predictions.jsonl", [])
    training.write_json(evaluation / "evaluation.json", {"final_holdout_evaluated": True})
    training.write_json(
        evaluation / "manifest.json",
        {
            "training_manifest_sha256": training.file_hash(directory / "manifest.json"),
            "final_feature_file_sha256": feature_manifest["artifacts"][
                "features-final-holdout.jsonl"
            ],
            "final_membership_file_sha256": feature_manifest["artifacts"][
                "memberships-final-holdout.jsonl"
            ],
            "artifacts": {
                name: training.file_hash(evaluation / name) for name in logistic.EVALUATION_FILES
            },
        },
    )


def test_exact_recipe_preserves_frozen_bounds_and_rejects_changed_protocol(tmp_path):
    definition = load_experiment()
    recipe = json.loads(canonical_bytes(training.expected_recipe(definition)))
    assert recipe["frozen_model_bounds"] == definition.models["catboost_cpu"]
    path = tmp_path / "recipe.json"
    training.write_json(path, recipe)
    assert training.load_recipe(path, definition) == recipe
    recipe["model"]["depth_candidates"] = [4, 6, 8]
    training.write_json(path, recipe)
    with pytest.raises(ValueError, match="recipe differs"):
        training.load_recipe(path, definition)


@pytest.mark.parametrize("field", ["config_sha256", "feature_manifest_sha256", "feature_identity"])
def test_lineage_rejects_a_self_consistent_baseline_from_different_inputs(tmp_path, field):
    feature_directory = tmp_path / "features"
    definition, manifest = feature_snapshot(feature_directory)
    baseline = tmp_path / "logistic"
    logistic_snapshot(baseline, feature_directory, manifest, definition, {field: "different"})
    with pytest.raises(ValueError, match="different features or frozen splits"):
        training.load_logistic_lineage(baseline, feature_directory, manifest, definition)


def test_lineage_checks_development_tampering_and_defers_final_predictions(tmp_path):
    feature_directory = tmp_path / "features"
    definition, manifest = feature_snapshot(feature_directory)
    baseline = tmp_path / "logistic"
    logistic_snapshot(baseline, feature_directory, manifest, definition)
    lineage = training.load_logistic_lineage(baseline, feature_directory, manifest, definition)
    final_predictions = baseline / "evaluation" / "predictions.jsonl"
    final_predictions.write_bytes(b"unread until model lock")
    assert (
        training.load_logistic_lineage(baseline, feature_directory, manifest, definition) == lineage
    )
    with pytest.raises(ValueError, match="content has changed"):
        training.load_final_logistic(baseline, lineage)
    (baseline / "development-predictions.jsonl").write_bytes(b"changed")
    with pytest.raises(ValueError, match="content has changed"):
        training.load_logistic_lineage(baseline, feature_directory, manifest, definition)


def test_output_outside_local_is_rejected_before_any_input_or_fitting(tmp_path, monkeypatch):
    monkeypatch.setattr(training, "LOCAL_DIRECTORY", tmp_path / ".local")
    monkeypatch.setattr(training, "load_partition", lambda *args: pytest.fail("Input was read"))
    with pytest.raises(ValueError, match="ignored project .local"):
        training.prepare_training(None, None, tmp_path / "published", None, None, None, None, None)


def test_published_lock_precedes_final_reads_and_completed_run_never_refits(tmp_path, monkeypatch):
    feature_directory = tmp_path / "features"
    definition, manifest = feature_snapshot(feature_directory)
    baseline = tmp_path / "logistic"
    logistic_snapshot(baseline, feature_directory, manifest, definition)
    monkeypatch.setattr(training, "LOCAL_DIRECTORY", tmp_path)
    monkeypatch.setattr(logistic, "LOCAL_DIRECTORY", tmp_path)
    output_root = tmp_path / "models"
    protected = {
        feature_directory / "features-final-holdout.jsonl",
        feature_directory / "memberships-final-holdout.jsonl",
        baseline / "evaluation" / "predictions.jsonl",
        baseline / "evaluation" / "evaluation.json",
    }
    original_open = Path.open
    final_reads = []

    def guarded_open(path, *args, **kwargs):
        if path in protected:
            assert list(output_root.glob("grocery-catboost-v1_*/lock.json"))
            assert list((output_root / ".holdout-usage").glob("*/*.json"))
            final_reads.append(path.name)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    training.load_feature_manifest(feature_directory, definition)
    lineage = training.load_logistic_lineage(baseline, feature_directory, manifest, definition)
    fit_calls, evaluation_calls, shap_calls = [], [], []
    models = {
        scenario: SimpleNamespace(model=NativeFixture(), name=scenario)
        for scenario in training.SCENARIOS
    }

    def fit(features, memberships, definition, predictions):
        assert set(features) == {"dev"}
        assert all(row["fold"] != 3 for row in memberships)
        assert not final_reads
        fit_calls.append(1)
        return {"prediction_rows": []}, models, {"engine": {"state": "locked"}}

    def evaluate(loaded, features, memberships, definition, lock, predictions):
        assert set(features) == {"final"}
        assert all(row["fold"] == 3 for row in memberships)
        assert set(loaded) == set(models)
        evaluation_calls.append(1)
        return {"promotion_gate": {"status": "withheld_reused_holdout"}}, []

    monkeypatch.setattr(training, "fit_and_evaluate_catboost_development", fit)
    monkeypatch.setattr(training, "describe_catboost", lambda model: {"name": model.name})
    monkeypatch.setattr(training, "verify_catboost", lambda model: {"verified": True})
    monkeypatch.setattr(training, "evaluate_catboost_final", evaluate)
    monkeypatch.setattr(training, "shap_review", lambda *args: shap_calls.append(1) or {})
    recipe = training.expected_recipe(definition)
    producer = {"code_sha256": "fixture", "python": "3.12", "packages": {}}
    directory = training.prepare_training(
        feature_directory, baseline, output_root, definition, recipe, manifest, lineage, producer
    )
    assert not final_reads
    evaluation = training.finish_evaluation(
        directory, feature_directory, baseline, definition, manifest
    )
    assert final_reads
    report = training.load_json(evaluation / "evaluation.json")
    assert report["confirmatory_for_this_lock"] is False
    assert report["models_refitted"] is False
    assert report["website_integration"] is False
    assert report["promotion_gate"]["status"] == "withheld_reused_holdout"
    assert (
        training.prepare_training(
            feature_directory,
            baseline,
            output_root,
            definition,
            recipe,
            manifest,
            lineage,
            producer,
        )
        == directory
    )
    assert (
        training.finish_evaluation(directory, feature_directory, baseline, definition, manifest)
        == evaluation
    )
    assert fit_calls == evaluation_calls == shap_calls == [1]
    (directory / "primary.cbm").write_bytes(b"changed")
    with pytest.raises(ValueError, match="content has changed"):
        training.finish_evaluation(directory, feature_directory, baseline, definition, manifest)


def test_shap_selection_ignores_order_labels_scores_and_keeps_building_identity(monkeypatch):
    definition = load_experiment()
    identifiers = [f"building-{index:03}" for index in range(30)]
    features = {
        identifier: {
            "building_source_id": identifier,
            "values": dict.fromkeys(definition.primary_predictors, 1.0),
            "observations": dict.fromkeys(definition.primary_predictors, {}),
        }
        for identifier in identifiers
    }
    members = [
        {"building_source_id": identifier, "pool": pool, "label": "observed_presence"}
        for identifier in identifiers
        for pool in ("valid_addressed", "all_valid")
    ]
    selected = []

    def explain(model, rows):
        selected.append([row["building_source_id"] for row in rows])
        return [{"score": 1.0, "expected_value": 1.0} for _ in rows]

    monkeypatch.setattr(training, "explain_catboost", explain)
    models = dict.fromkeys(training.SCENARIOS)
    first = training.shap_review(models, features, members, definition)
    second = training.shap_review(
        models,
        features,
        [
            {**row, "label": "unlabelled_background", "decision_score": index}
            for index, row in enumerate(reversed(members))
        ],
        definition,
    )
    assert first == second
    assert all(len(rows) == 20 and len(set(rows)) == 20 for rows in selected)
    for scenario, rows in first["scenarios"].items():
        assert [row["building_source_id"] for row in rows] == selected[0]
        assert all(row["values"] for row in rows)
        if scenario == "no_mapped_use":
            assert all("mapped_building_use" not in row["values"] for row in rows)
    assert "not_causal" in first["interpretation"]
