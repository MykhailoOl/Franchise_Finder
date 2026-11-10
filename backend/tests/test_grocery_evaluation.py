import copy
import itertools
import math
import random
from collections import defaultdict

import pytest
from sklearn.metrics import average_precision_score

import app.core.grocery_evaluation as evaluation
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


@pytest.fixture
def fake_baselines(monkeypatch):
    calls = []

    def fit(rows, labels, predictors, C, max_iter):
        identifiers = [row["building_source_id"] for row in rows]
        assert all(not identifier.startswith("fold-3-") for identifier in identifiers)
        calls.append({"ids": identifiers, "predictors": tuple(predictors), "C": C})
        return {"predictors": list(predictors), "C": C, "train_ids": identifiers}

    def score(model, rows):
        direction = (
            1 if len(model["predictors"]) == 17 else -1 if len(model["predictors"]) == 16 else 0
        )
        return [float(direction * row["signal"]) for row in rows]

    monkeypatch.setattr(evaluation, "fit_baseline", fit)
    monkeypatch.setattr(evaluation, "score_baseline", score)
    monkeypatch.setattr(evaluation, "describe_baseline", lambda model: copy.deepcopy(model))
    return calls


def test_grouped_average_precision_and_fractional_cutoff_ties_are_order_independent():
    labels = [1] * 5 + [0] * 25
    scores = [0.0] * 30
    result = evaluation.ranking_metrics(labels, scores)
    assert result["average_precision"] == pytest.approx(5 / 30)
    assert result["known_presence_precision_at_20"] == pytest.approx(5 / 30)
    assert result["known_presence_recall_at_20"] == pytest.approx(20 / 30)
    assert result["cutoff_tie"] == {"score": 0.0, "group_size": 30, "selected_fraction": 20 / 30}
    random.Random(42).shuffle(labels)
    assert evaluation.ranking_metrics(labels, scores) == result
    labels = [1, 0, 1, 0, 1, 0]
    scores = [0.8, 0.8, 0.4, 0.3, 0.3, -0.1]
    assert evaluation.ranking_metrics(labels, scores)["average_precision"] == pytest.approx(
        average_precision_score(labels, scores)
    )
    with pytest.raises(ValueError, match="finite"):
        evaluation.ranking_metrics([1, 0], [math.nan, 1.0])
    with pytest.raises(ValueError, match="at least one"):
        evaluation.ranking_metrics([0, 0], [1.0, 2.0])


@pytest.mark.parametrize("n", range(1, 7))
def test_random_ranking_analytic_ap_matches_exhaustive_permutations(n):
    for positives in range(1, n + 1):
        aps = []
        for positive_positions in itertools.combinations(range(n), positives):
            labels = [int(index in positive_positions) for index in range(n)]
            aps.append(
                evaluation.ranking_metrics(labels, list(range(n, 0, -1)))["average_precision"]
            )
        expected = evaluation.random_reference_metrics([1] * positives + [0] * (n - positives))
        assert expected["average_precision"] == pytest.approx(math.fsum(aps) / len(aps))
        assert expected["known_presence_precision_at_20"] == pytest.approx(positives / n)
        assert expected["known_presence_recall_at_20"] == pytest.approx(min(20, n) / n)


def test_nested_folds_same_baseline_rows_lock_and_final_phase_without_retraining(
    fake_baselines, monkeypatch
):
    features, development, final = frozen_rows()
    dev_features = {key: row for key, row in features.items() if not key.startswith("fold-3-")}
    final_features = {key: row for key, row in features.items() if key.startswith("fold-3-")}
    report, models, lock = evaluation.fit_and_evaluate_development(
        dev_features, development, definition()
    )
    assert report["final_holdout_evaluated"] is False
    assert report["catboost_trained"] is False
    assert report["development_gate"]["status"] == "passed_development_only"
    assert all(scenario["final_C"] == 0.1 for scenario in report["scenarios"].values())
    assert all(
        fold["selected_C"] == 0.1
        for scenario in report["scenarios"].values()
        for fold in scenario["folds"]
    )
    assert all(
        fold["tuning"]["status"] == "nested_geographic_selection"
        for scenario in report["scenarios"].values()
        for fold in scenario["folds"]
    )
    by_evaluation = defaultdict(lambda: defaultdict(set))
    for prediction in report["prediction_rows"]:
        by_evaluation[(prediction["scenario"], prediction["fold"])][prediction["model"]].add(
            prediction["building_source_id"]
        )
    for by_model in by_evaluation.values():
        assert set(by_model) == {"candidate", "population_only", "access_only"}
        assert by_model["candidate"] == by_model["population_only"] == by_model["access_only"]
    for call in fake_baselines:
        assert len(call["ids"]) == len(set(call["ids"]))
        assert all(not identifier.startswith("fold-3-") for identifier in call["ids"])
    monkeypatch.setattr(
        evaluation, "fit_baseline", lambda *args, **kwargs: pytest.fail("Final retraining")
    )
    final_report, predictions = evaluation.evaluate_final(
        models, final_features, final, definition(), lock
    )
    assert final_report["promotion_gate"]["status"] == "passed"
    assert final_report["promotion_gate"]["primary_scenario_only"] is True
    assert (
        final_report["scenarios"]["no_mapped_use"]["metrics"]["candidate"]["average_precision"] < 1
    )
    assert all(row["fold"] == 3 for row in predictions)
    damaged = copy.deepcopy(models)
    damaged["primary"]["candidate"]["C"] = 99
    with pytest.raises(ValueError, match="model changed"):
        evaluation.evaluate_final(damaged, final_features, final, definition(), lock)
    damaged_lock = copy.deepcopy(lock)
    damaged_lock["engine"]["state"] = "changed"
    with pytest.raises(ValueError, match="metadata has changed"):
        evaluation.evaluate_final(models, final_features, final, definition(), damaged_lock)


def test_inner_tuning_falls_back_whole_procedure_without_dropping_fold(fake_baselines):
    features, development, _ = frozen_rows()
    outer_train = [
        copy.deepcopy(row)
        for row in development
        if row["pool"] == "valid_addressed" and 0 in row["training_folds"]
    ]
    for row in outer_train:
        row["training_folds"] = [fold for fold in row["training_folds"] if fold != 2]
    selected, tuning = evaluation._choose_c(
        features, outer_train, 0, definition().primary_predictors, definition()
    )
    assert selected == 1
    assert tuning["status"] == "unavailable_fixed_c_1"
    assert len(tuning["inner_folds"]) == 2
    assert tuning["trials"] == []
    assert fake_baselines == []


def test_membership_leakage_and_unsupported_outer_fold_abort_before_fitting(fake_baselines):
    features, development, final = frozen_rows()
    with pytest.raises(ValueError, match="separate phases"):
        evaluation.fit_and_evaluate_development(features, development + final, definition())
    damaged = copy.deepcopy(development)
    damaged[0]["training_folds"].append(damaged[0]["fold"])
    with pytest.raises(ValueError, match="overlap"):
        evaluation.fit_and_evaluate_development(features, damaged, definition())
    damaged = [
        row for row in development if not (row["fold"] == 0 and row["label"] == "observed_presence")
    ]
    with pytest.raises(ValueError, match="Insufficient outer-fold support"):
        evaluation.fit_and_evaluate_development(features, damaged, definition())
    assert fake_baselines == []


def test_gate_cannot_hide_bad_region_behind_mean_or_skip_a_region():
    gains = {
        name: {
            metric: {"values": [0.5, 0.5, -0.01], "mean": 100, "min": 100}
            for metric in evaluation.METRICS
        }
        for name in evaluation.REFERENCE_NAMES
    }
    summary = {"fold_order": [0, 1, 2], "paired_region_gains": gains}
    assert evaluation.development_gate(summary, definition())["status"] == "failed_development"
    summary["fold_order"] = [0, 1]
    with pytest.raises(ValueError, match="omit"):
        evaluation.development_gate(summary, definition())


def test_final_cannot_overlap_locked_training_ids(fake_baselines):
    features, development, final = frozen_rows()
    _, models, lock = evaluation.fit_and_evaluate_development(features, development, definition())
    lock["engine"]["scenarios"]["primary"]["training_ids"].append(final[0]["building_source_id"])
    lock["engine_sha256"] = content_hash(lock["engine"])
    with pytest.raises(ValueError, match="overlaps"):
        evaluation.evaluate_final(models, features, final, definition(), lock)
