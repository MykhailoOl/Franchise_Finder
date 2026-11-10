import io
import json
from copy import deepcopy

import joblib
import numpy as np
import pytest
from catboost import CatBoostClassifier, CatBoostError

from app.core.data_contract import PREDICTORS, RADII_M
from app.core.grocery_baseline import MISSING_CATEGORY, _column_definitions, _raw_matrices
from app.core.grocery_catboost import (
    CATBOOST_RECIPE,
    FIXED_PARAMETERS,
    CatBoostTrainingError,
    _pool,
    describe_catboost,
    diagnose_catboost,
    explain_catboost,
    fit_catboost,
    score_catboost,
    verify_catboost,
)
from app.core.grocery_features import EXTRACTION_VERSION, FEATURE_IDS, SOURCE_TABLES

POPULATION = "resident_subtotal_500m"
USE = "mapped_building_use"
LABELS = [0, 0, 0, 0, 1, 1, 1, 1]


def row(population=10, use="h"):
    values = {name: 1.0 for name in FEATURE_IDS}
    values.update(
        {
            "building_footprint_area_m2": 100.0,
            "residential_building_fraction_500m": 0.5,
            POPULATION: population,
            USE: use,
        }
    )
    observations = {
        name: {
            "status": "available" if value is not None else "missing",
            "unit": PREDICTORS[name].unit,
            "reason": None if value is not None else "No published value",
            "coverage_fraction": 1.0 if PREDICTORS[name].radius_m else None,
            "source_refs": list(PREDICTORS[name].sources),
        }
        for name, value in values.items()
    }
    if population is None:
        observations[POPULATION]["coverage_fraction"] = 0.0
    return {
        "building_source_id": "municipal:1",
        "extraction_version": EXTRACTION_VERSION,
        "values": values,
        "observations": observations,
        "quality": {
            "missingness_by_predictor": {name: value is None for name, value in values.items()},
            "source_available_by_dataset": {name: True for name in SOURCE_TABLES},
            "source_coverage_fraction_by_radius": {
                name: {str(radius): 1.0 for radius in RADII_M} for name in SOURCE_TABLES
            },
        },
    }


def training():
    return [row(value, "h" if value <= 4 else "m") for value in (1, 2, 3, 4, 7, 8, 9, 10)]


@pytest.fixture(scope="module")
def fitted():
    return fit_catboost(training(), LABELS, (POPULATION, USE), 4)


def test_recipe_freezes_cpu_iterations_and_native_preprocessing(fitted):
    report = describe_catboost(fitted)
    parameters = report["parameters"]
    assert report["tree_count"] == 300
    assert parameters == {**FIXED_PARAMETERS, "depth": 4}
    assert parameters["boosting_type"] == "Ordered"
    assert parameters["task_type"] == "CPU"
    assert parameters["thread_count"] == 4
    assert parameters["nan_mode"] == "Min"
    assert parameters["use_best_model"] is False
    assert parameters["allow_writing_files"] is False
    assert report["recipe"] == CATBOOST_RECIPE
    assert report["resolved_parameters"]["bootstrap_type"] == "MVS"
    assert report["resolved_parameters"]["one_hot_max_size"] == 2
    json.dumps(report, allow_nan=False)


def test_same_logical_feature_columns_as_logistic_without_transformations():
    names = tuple(FEATURE_IDS)
    rows = [row(None, None), row(12.3456789, "m")]
    numeric, categorical = _raw_matrices(rows, names)
    numeric_names, categorical_names = _column_definitions(names)
    pool = _pool(rows, names)
    assert pool.get_feature_names() == numeric_names + categorical_names
    assert pool.get_cat_feature_indices() == list(
        range(len(numeric_names), len(numeric_names) + len(categorical_names))
    )
    index = numeric_names.index(POPULATION + "__value")
    assert np.isnan(numeric[0, index])
    assert numeric[1, index] == 12.3456789
    assert categorical[:, 0].tolist() == [MISSING_CATEGORY, "m"]
    numeric_only = _pool([row(None), row(0)], (POPULATION,)).get_features()
    assert np.isnan(numeric_only[0, 0])
    assert numeric_only[1, 0] == 0


def test_heldout_categories_values_and_missingness_never_refit_model(fitted):
    report = describe_catboost(fitted)
    before = json.dumps(report, sort_keys=True)
    rows = [row(100000, "s"), row(None, None)]
    scores = score_catboost(fitted, rows)
    assert len(scores) == 2 and np.isfinite(scores).all()
    assert json.dumps(describe_catboost(fitted), sort_keys=True) == before
    assert fitted.training_categories == {USE: ["h", "m"]}
    diagnostics = diagnose_catboost(fitted, rows)
    assert diagnostics["unseen_category_counts"] == {USE: {"s": 1, MISSING_CATEGORY: 1}}
    assert diagnostics["native_missing_numeric_counts"][POPULATION + "__value"] == 1


def test_all_missing_training_column_remains_nan_and_cannot_learn_later_values():
    rows = [row(None, "h" if index < 4 else "m") for index in range(8)]
    original = deepcopy(rows)
    model = fit_catboost(rows, LABELS, (POPULATION, USE), 4)
    assert POPULATION + "__value" in model.training_all_missing_columns
    assert POPULATION + "__value" in describe_catboost(model)["feature_schema"]["feature_order"]
    assert score_catboost(model, [row(1, "m")]) == score_catboost(model, [row(100000, "m")])
    assert rows == original


def test_fitting_uses_only_training_pool_without_eval_set_or_best_model(monkeypatch):
    original = CatBoostClassifier.fit
    calls = []

    def fit_capture(self, X, *args, **kwargs):
        calls.append((X.num_row(), args, kwargs.copy(), self.get_params()))
        return original(self, X, *args, **kwargs)

    monkeypatch.setattr(CatBoostClassifier, "fit", fit_capture)
    model = fit_catboost(training(), LABELS, (POPULATION, USE), 6)
    assert model.model.tree_count_ == 300
    assert len(calls) == 1
    count, args, kwargs, parameters = calls[0]
    assert count == 8 and args == () and kwargs == {}
    assert parameters["depth"] == 6
    assert parameters["use_best_model"] is False
    assert "eval_set" not in parameters
    assert "early_stopping_rounds" not in parameters


def test_restricted_predictors_ignore_identifiers_labels_folds_and_other_quality():
    model = fit_catboost(training(), LABELS, (POPULATION,), 4)
    first = row(5)
    second = deepcopy(first)
    second["building_source_id"] = "target-leak"
    second["label"] = 1
    second["fold"] = 3
    second["values"][USE] = "invalid-unused"
    second["quality"]["activity_excluded_onsite_count_500m"] = 100
    second["quality"]["neighbor_building_count_500m"] = 100
    assert score_catboost(model, [first]) == score_catboost(model, [second])
    assert all(name.startswith(POPULATION + "__") for name in model.model.feature_names_)


@pytest.mark.parametrize("kind", ["null", "status", "missingness", "source", "version", "category"])
def test_invalid_rows_are_rejected_before_native_scoring(fitted, kind):
    item = row()
    if kind == "null":
        item["values"][POPULATION] = None
    elif kind == "status":
        item["observations"][POPULATION]["status"] = "missing"
    elif kind == "missingness":
        item["quality"]["missingness_by_predictor"][POPULATION] = True
    elif kind == "source":
        item["quality"]["source_available_by_dataset"]["warsaw-population-2021"] = False
    elif kind == "version":
        item["extraction_version"] = "different"
    else:
        item["values"][USE] = "unrecognized"
    with pytest.raises(ValueError):
        score_catboost(fitted, [item])


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, True, "3"])
def test_invalid_raw_numbers_are_rejected_but_null_has_native_nan_mapping(fitted, value):
    with pytest.raises(ValueError):
        score_catboost(fitted, [row(value)])


@pytest.mark.parametrize("depth", [0, 3, 5, 8, True, 4.0])
def test_depth_is_the_only_bounded_candidate_parameter(depth):
    with pytest.raises(ValueError):
        fit_catboost(training(), LABELS, (POPULATION,), depth)


@pytest.mark.parametrize("labels", [[0] * 8, [1] * 8, [0, 1], [False] * 4 + [True] * 4])
def test_training_requires_both_binary_integer_classes(labels):
    with pytest.raises(ValueError):
        fit_catboost(training(), labels, (POPULATION,), 4)


def test_constant_training_input_keeps_native_nan_without_inventing_feature_values():
    rows = [row(None) for _ in range(8)]
    model = fit_catboost(rows, LABELS, (POPULATION,), 4)
    assert model.training_all_missing_columns == [POPULATION + "__value"]
    assert len(set(score_catboost(model, [row(None), row(1), row(10000)]))) == 1
    assert all(item["values"][POPULATION] is None for item in rows)


def test_native_training_failure_has_no_fallback_or_iteration_increase(monkeypatch):
    calls = []

    def fail_fit(self, X, *args, **kwargs):
        calls.append(self.get_params())
        raise CatBoostError("Synthetic training failure")

    monkeypatch.setattr(CatBoostClassifier, "fit", fail_fit)
    with pytest.raises(CatBoostTrainingError, match="Synthetic training failure"):
        fit_catboost(training(), LABELS, (POPULATION,), 4)
    assert len(calls) == 1 and calls[0]["iterations"] == 300


def test_joblib_roundtrip_preserves_native_predictions_shap_and_fingerprint(fitted):
    stream = io.BytesIO()
    joblib.dump(fitted, stream)
    stream.seek(0)
    restored = joblib.load(stream)
    rows = [row(None, None), row(7, "s")]
    assert verify_catboost(restored)["verified"]
    assert score_catboost(fitted, rows) == score_catboost(restored, rows)
    assert describe_catboost(fitted) == describe_catboost(restored)
    assert explain_catboost(fitted, rows) == explain_catboost(restored, rows)


def test_native_categorical_statistics_survive_reload_beyond_one_hot_threshold():
    rows = [row(10, use) for use in ("h", "m", "g", "s") for _ in range(4)]
    labels = [1] * 8 + [0] * 8
    model = fit_catboost(rows, labels, (USE,), 4)
    assert len(model.training_categories[USE]) > FIXED_PARAMETERS["one_hot_max_size"]
    stream = io.BytesIO()
    joblib.dump(model, stream)
    stream.seek(0)
    restored = joblib.load(stream)
    evaluation = [row(10, "h"), row(10, "g"), row(10, "b"), row(10, None)]
    scores = score_catboost(model, evaluation)
    assert len(set(scores)) > 1
    assert scores == score_catboost(restored, evaluation)
    assert describe_catboost(model) == describe_catboost(restored)
    for item in explain_catboost(restored, evaluation):
        assert item["expected_value"] + sum(
            item["contributions_by_predictor"].values()
        ) == pytest.approx(item["score"])


def test_repeat_fit_has_same_semantic_fingerprint_and_predictions(fitted):
    repeated = fit_catboost(training(), LABELS, (POPULATION, USE), 4)
    assert repeated.model_sha256 == fitted.model_sha256
    assert describe_catboost(repeated) == describe_catboost(fitted)
    assert score_catboost(repeated, training()) == score_catboost(fitted, training())


def test_fingerprint_verification_detects_changed_model_or_schema(fitted):
    changed = deepcopy(fitted)
    changed.model.set_scale_and_bias(2.0, 1.0)
    with pytest.raises(ValueError, match="fingerprint"):
        score_catboost(changed, [row()])
    changed = deepcopy(fitted)
    changed.model.shrink(299)
    with pytest.raises(ValueError, match="tree count"):
        verify_catboost(changed)
    changed = deepcopy(fitted)
    changed.numeric_columns[0] = "private-id"
    with pytest.raises(ValueError, match="schema"):
        verify_catboost(changed)


def test_native_shap_with_no_reference_data_reconstructs_raw_formula_scores(fitted):
    rows = [row(1), row(None, None), row(100, "s")]
    for score, explanation in zip(
        score_catboost(fitted, rows), explain_catboost(fitted, rows), strict=True
    ):
        assert explanation["score"] == score
        assert explanation["expected_value"] + sum(
            explanation["contributions_by_predictor"].values()
        ) == pytest.approx(score)
        assert explanation["expected_value"] + sum(
            item["contribution"] for item in explanation["feature_contributions"]
        ) == pytest.approx(score)
        assert explanation["interpretation"] == "model_contributions_not_causal_business_effects"
        assert set(explanation["contributions_by_predictor"]) == {POPULATION, USE}


def test_empty_scoring_explanation_and_diagnostics_remain_empty(fitted):
    assert score_catboost(fitted, []) == []
    assert explain_catboost(fitted, []) == []
    assert diagnose_catboost(fitted, [])["row_count"] == 0
