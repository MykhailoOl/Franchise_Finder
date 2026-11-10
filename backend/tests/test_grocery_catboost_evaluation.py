import copy
import math
import random
from collections import defaultdict

import pytest

import app.core.grocery_catboost_evaluation as evaluation
import app.core.grocery_evaluation as logistic_evaluation
from app.core.experiment import SupportDefinition, content_hash, load_experiment


def definition():
    return load_experiment().model_copy(
        update={
            "support": SupportDefinition(
                minimum_train_positives=5, minimum_evaluation_positives=5, minimum_background=5
            )
        }
    )


def frozen_rows():
    development, final, features = [], [], {}
    for fold in range(4):
        for index in range(42):
            identifier = f"fold-{fold}-building-{index:03}"
            positive = index < 12
            features[identifier] = {"building_source_id": identifier, "signal": int(positive)}
            for pool in ("valid_addressed", "all_valid"):
                row = {
                    "pool": pool,
                    "category": "grocery",
                    "building_source_id": identifier,
                    "fold": fold,
                    "validation_fold": fold,
                    "training_folds": [other for other in range(4) if other != fold]
                    if fold != 3
                    else [],
                    "label": "observed_presence" if positive else "unlabelled_background",
                }
                (final if fold == 3 else development).append(row)
    return features, development, final


def saved_predictions(memberships):
    return [
        {
            "scenario": scenario,
            "pool": row["pool"],
            "model": model,
            "fold": row["validation_fold"],
            "building_source_id": row["building_source_id"],
            "label": row["label"],
            "decision_score": 0.0,
        }
        for scenario, specification in evaluation.scenario_definitions(definition()).items()
        for row in memberships
        if row["pool"] == specification["pool"] and row["validation_fold"] is not None
        for model in evaluation.SAVED_MODEL_NAMES
    ]


@pytest.fixture
def fake_catboost(monkeypatch):
    calls = []

    def fit(rows, labels, predictors, depth):
        identifiers = [row["building_source_id"] for row in rows]
        assert len(set(identifiers)) == len(identifiers)
        assert all(not identifier.startswith("fold-3-") for identifier in identifiers)
        assert labels == [row["signal"] for row in rows]
        calls.append({"ids": identifiers, "predictors": tuple(predictors), "depth": depth})
        return {"predictors": list(predictors), "depth": depth, "train_ids": identifiers}

    def score(model, rows):
        assert not set(model["train_ids"]) & {row["building_source_id"] for row in rows}
        direction = 1 if len(model["predictors"]) == 17 else -1
        return [float(direction * row["signal"]) for row in rows]

    monkeypatch.setattr(evaluation, "fit_catboost", fit)
    monkeypatch.setattr(evaluation, "score_catboost", score)
    monkeypatch.setattr(evaluation, "describe_catboost", lambda model: copy.deepcopy(model))
    monkeypatch.setattr(
        logistic_evaluation,
        "fit_baseline",
        lambda *args, **kwargs: pytest.fail("Saved logistic models must not be refitted"),
    )
    return calls


def test_development_recomputes_saved_scores_and_final_never_fits_or_promotes(
    fake_catboost, monkeypatch
):
    features, development, final = frozen_rows()
    dev_features = {key: row for key, row in features.items() if not key.startswith("fold-3-")}
    final_features = {key: row for key, row in features.items() if key.startswith("fold-3-")}
    saved = saved_predictions(development)
    for row in saved:
        row["reported_average_precision"] = 1.0
    report, models, lock = evaluation.fit_and_evaluate_catboost_development(
        dev_features, development, definition(), saved
    )
    assert report["phase"] == "exploratory_development"
    assert report["logistic_retrained"] is False
    assert report["final_holdout_evaluated"] is False
    assert report["promotion_gate"]["status"] == "withheld_reused_holdout"
    assert report["reference_gate_diagnostic"]["status"] == "passed_development_only"
    assert all(scenario["final_depth"] == 4 for scenario in report["scenarios"].values())
    for scenario in report["scenarios"].values():
        for fold in scenario["folds"]:
            assert fold["selected_depth"] == 4
            assert fold["tuning"]["status"] == "nested_geographic_selection"
            for name in ("logistic", "population_only", "access_only"):
                assert fold["metrics"][name]["average_precision"] == pytest.approx(12 / 42)
    by_group = defaultdict(set)
    for row in report["prediction_rows"]:
        assert row["model"] == "catboost"
        by_group[(row["scenario"], row["fold"])].add(row["building_source_id"])
    for row in saved:
        assert row["building_source_id"] in by_group[(row["scenario"], row["fold"])]
    assert len(report["prediction_rows"]) == 3 * 3 * 42
    assert len(fake_catboost) == 57
    assert all(call["depth"] in (4, 6) for call in fake_catboost)
    monkeypatch.setattr(
        evaluation, "fit_catboost", lambda *args, **kwargs: pytest.fail("Reused final retraining")
    )
    result, predictions = evaluation.evaluate_catboost_final(
        models, final_features, final, definition(), lock, saved_predictions(final)
    )
    assert result["phase"] == "exploratory_reused_final"
    assert result["reference_gate_diagnostic"]["thresholds_met"] is True
    assert result["reference_gate_diagnostic"]["eligible_for_promotion"] is False
    assert result["promotion_gate"]["status"] == "withheld_reused_holdout"
    assert result["promotion_gate"]["eligible_for_promotion"] is False
    assert result["promotion_gate"]["sensitivity_models_can_replace_primary"] is False
    assert result["scenarios"]["no_mapped_use"]["metrics"]["candidate"]["average_precision"] < 1
    assert len(predictions) == 3 * 42
    assert all(row["fold"] == 3 and row["model"] == "catboost" for row in predictions)


def test_saved_prediction_and_membership_order_do_not_affect_reports_or_lock(fake_catboost):
    features, development, _ = frozen_rows()
    saved = saved_predictions(development)
    first_report, _, first_lock = evaluation.fit_and_evaluate_catboost_development(
        features, development, definition(), saved
    )
    random.Random(42).shuffle(saved)
    random.Random(57).shuffle(development)
    second_report, _, second_lock = evaluation.fit_and_evaluate_catboost_development(
        dict(reversed(list(features.items()))), development, definition(), saved
    )
    assert first_report == second_report
    assert first_lock == second_lock


@pytest.mark.parametrize(
    "damage",
    [
        "duplicate",
        "missing",
        "extra_id",
        "wrong_label",
        "missing_label",
        "wrong_pool",
        "wrong_fold",
        "wrong_scenario",
        "wrong_model",
        "nonfinite",
        "missing_group",
    ],
)
def test_invalid_saved_prediction_groups_fail_before_training(fake_catboost, damage):
    features, development, _ = frozen_rows()
    saved = saved_predictions(development)
    if damage == "duplicate":
        saved.append(copy.deepcopy(saved[0]))
    elif damage == "missing":
        saved.pop()
    elif damage == "extra_id":
        saved.append({**saved[0], "building_source_id": "unknown-building"})
    elif damage == "wrong_label":
        saved[0]["label"] = "unlabelled_background"
    elif damage == "missing_label":
        del saved[0]["label"]
    elif damage == "wrong_pool":
        saved[0]["pool"] = "all_valid"
    elif damage == "wrong_fold":
        saved[0]["fold"] = 3
    elif damage == "wrong_scenario":
        saved[0]["scenario"] = "new_scenario"
    elif damage == "wrong_model":
        saved[0]["model"] = "catboost"
    elif damage == "nonfinite":
        saved[0]["decision_score"] = math.nan
    else:
        saved = [row for row in saved if row["model"] != "candidate"]
    with pytest.raises(ValueError, match="Saved|Duplicate"):
        evaluation.fit_and_evaluate_catboost_development(features, development, definition(), saved)
    assert fake_catboost == []


def test_inner_tuning_whole_procedure_falls_back_when_one_fold_is_unsupported(fake_catboost):
    features, development, _ = frozen_rows()
    train = [
        copy.deepcopy(row)
        for row in development
        if row["pool"] == "valid_addressed" and 0 in row["training_folds"]
    ]
    for row in train:
        row["training_folds"] = [fold for fold in row["training_folds"] if fold != 2]
    depth, tuning = evaluation.choose_depth(
        features, train, 0, definition().primary_predictors, definition()
    )
    assert depth == 4
    assert tuning["status"] == "unavailable_fixed_depth_4"
    assert len(tuning["inner_folds"]) == 2
    assert tuning["trials"] == []
    assert fake_catboost == []


def test_depth_selection_uses_mean_development_ap_and_prefers_better_depth(
    fake_catboost, monkeypatch
):
    features, development, _ = frozen_rows()
    monkeypatch.setattr(
        evaluation,
        "score_catboost",
        lambda model, rows: [
            float(row["signal"] * (1 if model["depth"] == 6 else -1)) for row in rows
        ],
    )
    report, _, lock = evaluation.fit_and_evaluate_catboost_development(
        features, development, definition(), saved_predictions(development)
    )
    for scenario in report["scenarios"].values():
        assert scenario["final_depth"] == 6
        assert all(fold["selected_depth"] == 6 for fold in scenario["folds"])
        assert scenario["final_depth_selection"]["method"] == "fixed_depth_development_mean_AP"
    assert all(item["final_depth"] == 6 for item in lock["engine"]["scenarios"].values())


def test_frozen_split_and_support_rejections_precede_training(fake_catboost):
    features, development, final = frozen_rows()
    with pytest.raises(ValueError, match="separate phases"):
        evaluation.fit_and_evaluate_catboost_development(
            features, development + final, definition(), saved_predictions(development)
        )
    damaged = copy.deepcopy(development)
    damaged[0]["training_folds"].append(damaged[0]["fold"])
    with pytest.raises(ValueError, match="overlap"):
        evaluation.fit_and_evaluate_catboost_development(
            features, damaged, definition(), saved_predictions(damaged)
        )
    sparse = [
        row for row in development if not (row["fold"] == 0 and row["label"] == "observed_presence")
    ]
    with pytest.raises(ValueError, match="Insufficient outer-fold support"):
        evaluation.fit_and_evaluate_catboost_development(
            features, sparse, definition(), saved_predictions(sparse)
        )
    assert fake_catboost == []


@pytest.mark.parametrize("damage", ["model", "lock", "overlap", "predictions"])
def test_final_rejects_changed_models_locks_or_rows_before_scoring(
    fake_catboost, monkeypatch, damage
):
    features, development, final = frozen_rows()
    _, models, lock = evaluation.fit_and_evaluate_catboost_development(
        features, development, definition(), saved_predictions(development)
    )
    saved = saved_predictions(final)
    if damage == "model":
        models["primary"]["depth"] = 6
    elif damage == "lock":
        lock["engine"]["state"] = "changed"
    elif damage == "overlap":
        lock["engine"]["scenarios"]["primary"]["training_ids"].append(
            final[0]["building_source_id"]
        )
        lock["engine_sha256"] = content_hash(lock["engine"])
    else:
        saved[0]["label"] = "unlabelled_background"
    monkeypatch.setattr(
        evaluation, "score_catboost", lambda *args, **kwargs: pytest.fail("Invalid final scoring")
    )
    with pytest.raises(ValueError):
        evaluation.evaluate_catboost_final(models, features, final, definition(), lock, saved)


@pytest.mark.parametrize(
    ("key", "value"),
    [("depth", [4, 6, 8]), ("iterations", 500), ("thread_count", 8), ("max_trials", 3)],
)
def test_unfrozen_catboost_configuration_is_rejected_before_training(fake_catboost, key, value):
    features, development, _ = frozen_rows()
    changed = definition().model_copy(deep=True)
    changed.models["catboost_cpu"][key] = value
    with pytest.raises(ValueError, match="exact frozen"):
        evaluation.fit_and_evaluate_catboost_development(
            features, development, changed, saved_predictions(development)
        )
    assert fake_catboost == []
