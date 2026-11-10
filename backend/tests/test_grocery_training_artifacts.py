import json

import pytest

from app.cli import train_grocery_baseline as training
from app.core.experiment import canonical_bytes, configuration_hash, load_experiment
from app.core.grocery_features import EXTRACTION_VERSION


def feature_snapshot(directory):
    directory.mkdir()
    definition = load_experiment()
    for partition, fold, identifier in (("development", 0, "dev"), ("final-holdout", 3, "test")):
        feature = {
            "building_source_id": identifier,
            "extraction_version": EXTRACTION_VERSION,
            "values": dict.fromkeys(definition.primary_predictors, 1.0),
            "observations": dict.fromkeys(definition.primary_predictors, {}),
        }
        memberships = [
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
        training.write_records(directory / f"memberships-{partition}.jsonl", memberships)
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


def test_initial_verification_does_not_open_holdout_files(tmp_path, monkeypatch):
    directory = tmp_path / "features"
    definition, manifest = feature_snapshot(directory)
    for name in ("features-final-holdout.jsonl", "memberships-final-holdout.jsonl"):
        (directory / name).write_bytes(b"sealed until lock")
    assert training.load_feature_manifest(directory, definition) == manifest
    features, members = training.load_partition(directory, definition)
    assert set(features) == {"dev"}
    assert all(row["fold"] != 3 for row in members)


def test_recipe_changes_require_a_matching_versioned_protocol(tmp_path):
    definition = load_experiment()
    recipe = json.loads(canonical_bytes(training.expected_recipe(definition)))
    path = tmp_path / "recipe.json"
    training.write_json(path, recipe)
    assert training.load_recipe(path, definition) == recipe
    recipe["outer_folds"] = [0, 1, 3]
    training.write_json(path, recipe)
    with pytest.raises(ValueError, match="recipe differs"):
        training.load_recipe(path, definition)


@pytest.mark.parametrize("previously_used", [False, True])
def test_lock_precedes_holdout_reads_and_completed_run_reuses_models(
    tmp_path, monkeypatch, previously_used
):
    feature_directory = tmp_path / "features"
    definition, manifest = feature_snapshot(feature_directory)
    monkeypatch.setattr(training, "LOCAL_DIRECTORY", tmp_path)
    fit_calls, evaluation_calls = [], []
    models = {
        scenario: {name: {"coefficient": 1.0} for name in training.MODELS}
        for scenario in training.SCENARIOS
    }

    def fit(features, memberships, definition):
        fit_calls.append(set(features))
        assert all(row["fold"] != 3 for row in memberships)
        return {"prediction_rows": [], "scenarios": {}}, models, {"engine": {"state": "locked"}}

    monkeypatch.setattr(training, "fit_and_evaluate_development", fit)
    monkeypatch.setattr(training, "describe_baseline", lambda model: model)
    producer = {"code_sha256": "code", "python": "3.12", "packages": {}}
    recipe = training.expected_recipe(definition)
    output_root = tmp_path / "models"
    directory = training.prepare_training(
        feature_directory, output_root, definition, recipe, manifest, producer
    )
    if previously_used:
        training.mark_final_read(directory, manifest, "earlier-different-lock")
    original_partition = training.load_partition

    def guarded_partition(path, definition, *, final=False):
        if final:
            assert (directory / "lock.json").is_file()
            entries = list((tmp_path / "models" / ".holdout-usage").glob("*/*.json"))
            assert len(entries) == 1 + int(previously_used)
        return original_partition(path, definition, final=final)

    def evaluate(loaded_models, features, memberships, definition, lock):
        assert loaded_models == models
        assert set(features) == {"test"}
        assert all(row["fold"] == 3 for row in memberships)
        evaluation_calls.append(1)
        return {"promotion_gate": {"status": "not_met"}}, []

    monkeypatch.setattr(training, "load_partition", guarded_partition)
    monkeypatch.setattr(training, "evaluate_final", evaluate)
    evaluation = training.finish_evaluation(directory, feature_directory, definition, manifest)
    assert (
        training.prepare_training(
            feature_directory, output_root, definition, recipe, manifest, producer
        )
        == directory
    )
    assert (
        training.finish_evaluation(directory, feature_directory, definition, manifest) == evaluation
    )
    assert fit_calls == [{"dev"}]
    assert evaluation_calls == [1]
    report = training.load_json(evaluation / "evaluation.json")
    assert report["models_refitted"] is False
    assert report["holdout_access"]["previously_accessed_for_other_training_run"] is previously_used
    assert report["confirmatory_for_this_lock"] is not previously_used
    if previously_used:
        assert report["promotion_gate"]["status"] == "withheld_reused_holdout"
        assert report["phase"] == "reused_final_exploratory"
    (feature_directory / "features-final-holdout.jsonl").write_bytes(b"changed")
    with pytest.raises(ValueError, match="content has changed"):
        training.finish_evaluation(directory, feature_directory, definition, manifest)


def test_holdout_access_history_survives_an_interrupted_evaluation(tmp_path, monkeypatch):
    feature_directory = tmp_path / "features"
    _, manifest = feature_snapshot(feature_directory)
    monkeypatch.setattr(training, "LOCAL_DIRECTORY", tmp_path)
    directory = tmp_path / "locked"
    directory.mkdir()
    training.write_json(directory / "lock.json", {"locked": True})
    first = training.mark_final_read(directory, manifest, "first")
    assert training.mark_final_read(directory, manifest, "first") == first
    second = training.mark_final_read(directory, manifest, "second")
    assert second["previously_accessed_for_other_training_run"] is True
    assert second["previous_training_runs"] == ["first"]
