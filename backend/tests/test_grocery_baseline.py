import io
import json
import warnings
from copy import deepcopy

import joblib
import numpy as np
import pytest
from sklearn.exceptions import ConvergenceWarning
from sklearn.linear_model import LogisticRegression

from app.core.data_contract import PREDICTORS, RADII_M
from app.core.grocery_baseline import (
    BASELINE_RECIPE,
    MISSING_CATEGORY,
    BaselineConvergenceError,
    describe_baseline,
    diagnose_baseline,
    explain_baseline,
    fit_baseline,
    score_baseline,
)
from app.core.grocery_features import EXTRACTION_VERSION, FEATURE_IDS, SOURCE_TABLES

POPULATION = "resident_subtotal_500m"
USE = "mapped_building_use"
DISTANCE = "nearest_bus_stop_distance_m"


def row(population=10, use="h"):
    values = {name: 1.0 for name in FEATURE_IDS}
    values["building_footprint_area_m2"] = 100.0
    values["residential_building_fraction_500m"] = 0.5
    values[POPULATION] = population
    values[USE] = use
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
        "building_source_id": "untrusted-input-id",
        "extraction_version": EXTRACTION_VERSION,
        "values": values,
        "observations": observations,
        "quality": {
            "missingness_by_predictor": {name: value is None for name, value in values.items()},
            "source_available_by_dataset": {name: True for name in SOURCE_TABLES},
            "source_coverage_fraction_by_radius": {
                name: {str(radius): 1.0 for radius in RADII_M} for name in SOURCE_TABLES
            },
            "activity_excluded_onsite_count_500m": 7,
            "neighbor_building_count_500m": 20,
            "source_age_days_by_dataset": {name: 100 for name in SOURCE_TABLES},
        },
    }


def training():
    return [row(value, "h" if value <= 4 else "m") for value in (1, 2, 3, 4, 7, 8, 9, 10)]


def fitted(predictors=(POPULATION, USE)):
    return fit_baseline(training(), [0, 0, 0, 0, 1, 1, 1, 1], predictors, C=1.0)


def set_feature(item, name, value, status="available", coverage=1.0):
    item["values"][name] = value
    item["observations"][name].update(
        {
            "status": status,
            "reason": None if status == "available" else "Incomplete source evidence",
            "coverage_fraction": coverage,
        }
    )
    item["quality"]["missingness_by_predictor"][name] = value is None
    return item


def test_preprocessing_fits_only_training_rows_and_ignores_evaluation_distribution():
    model = fitted()
    before = json.dumps(describe_baseline(model), sort_keys=True)
    assert model["features"].medians_[0] == 5.5
    scores = score_baseline(model, [row(100000, "s"), row(None, None)])
    assert all(np.isfinite(scores))
    assert json.dumps(describe_baseline(model), sort_keys=True) == before
    assert describe_baseline(model)["categories"][USE] == ["h", "m"]
    assert describe_baseline(model)["fit_means"][f"{POPULATION}__value"] == 5.5


def test_training_median_imputation_preserves_values_and_uses_no_zero_substitution():
    rows = [row(value) for value in (1, 2, None, 4, 7, 8, 9, 10)]
    original = deepcopy(rows)
    model = fit_baseline(rows, [0, 0, 0, 0, 1, 1, 1, 1], (POPULATION,), 1.0)
    report = describe_baseline(model)
    assert report["fit_medians"][f"{POPULATION}__value"] == 7
    assert report["fit_means"][f"{POPULATION}__value"] == 6
    assert rows == original
    matrix = model["features"].transform([row(None)])
    column = report["transformed_feature_order"].index(f"{POPULATION}__value")
    assert matrix[0, column] == pytest.approx(
        (7 - 6) / report["fit_scales"][f"{POPULATION}__value"]
    )


def test_all_missing_training_population_is_dropped_and_evaluation_cannot_revive_it():
    rows = [row(None) for _ in range(8)]
    model = fit_baseline(rows, [0, 0, 0, 0, 1, 1, 1, 1], (POPULATION,), 1.0)
    report = describe_baseline(model)
    assert report["dropped_all_missing_features"] == [POPULATION]
    assert f"{POPULATION}__value" not in report["fit_medians"]
    assert f"{POPULATION}__value" not in report["transformed_feature_order"]
    assert f"{POPULATION}__missing" in report["transformed_feature_order"]
    assert score_baseline(model, [row(1)]) == score_baseline(model, [row(100000)])


def test_all_missing_training_coverage_is_dropped_without_zero_filling():
    rows = training()
    for item in rows:
        item["quality"]["source_coverage_fraction_by_radius"]["warsaw-population-2021"]["500"] = (
            None
        )
    model = fit_baseline(rows, [0, 0, 0, 0, 1, 1, 1, 1], (POPULATION,), 1.0)
    report = describe_baseline(model)
    assert f"{POPULATION}__source_coverage_fraction" in report["dropped_all_missing_columns"]
    assert f"{POPULATION}__source_coverage_missing" in report["fit_medians"]


def test_fixed_missing_censored_partial_flags_exist_even_if_absent_during_fit():
    model = fitted((DISTANCE,))
    names = describe_baseline(model)["transformed_feature_order"]
    for suffix in ("missing", "censored", "partial"):
        assert f"{DISTANCE}__{suffix}" in names
    missing = set_feature(row(), DISTANCE, None, "censored")
    partial = set_feature(row(), DISTANCE, 100, "partial", 0.5)
    matrix = model["features"].transform([missing, partial])
    assert matrix[0, names.index(f"{DISTANCE}__missing")] == 1
    assert matrix[0, names.index(f"{DISTANCE}__censored")] == 1
    assert matrix[1, names.index(f"{DISTANCE}__partial")] == 1


def test_missing_category_has_explicit_sentinel_and_unseen_valid_use_is_ignored():
    rows = [row(value, None if value < 4 else "m") for value in (1, 2, 3, 4, 7, 8, 9, 10)]
    model = fit_baseline(rows, [0, 0, 0, 0, 1, 1, 1, 1], (USE,), 1.0)
    report = describe_baseline(model)
    assert MISSING_CATEGORY in report["categories"][USE]
    assert "h" not in report["categories"][USE]
    matrix = model["features"].transform([row(3, "h")])
    encoded = [
        index
        for index, name in enumerate(report["transformed_feature_order"])
        if "__category=" in name
    ]
    assert np.all(matrix[0, encoded] == 0)
    assert diagnose_baseline(model, [row(3, "h")])["unseen_category_counts"] == {USE: {"h": 1}}


def test_restricted_baselines_do_not_use_identifiers_or_unrelated_features_and_flags():
    model = fitted((POPULATION,))
    evaluation = row(5)
    changed = deepcopy(evaluation)
    changed["building_source_id"] = "outlet-label-leak"
    changed["label"] = 1
    changed["fold"] = 3
    changed["values"][USE] = "invalid-unused-category"
    changed["values"][DISTANCE] = 999
    changed["quality"]["activity_excluded_onsite_count_500m"] = 1000
    changed["quality"]["neighbor_building_count_500m"] = 1000
    changed["quality"]["source_age_days_by_dataset"] = {"private-outlet-id": 1}
    assert score_baseline(model, [evaluation]) == score_baseline(model, [changed])
    report = describe_baseline(model)
    assert all(name.startswith(POPULATION + "__") for name in report["transformed_feature_order"])


def test_without_mapped_use_variant_removes_every_mapped_use_column():
    model = fitted(tuple(name for name in FEATURE_IDS if name != USE))
    assert not any(
        name.startswith(USE + "__")
        for name in describe_baseline(model)["transformed_feature_order"]
    )


@pytest.mark.parametrize(
    "kind", ["status", "missingness", "null", "unit", "source", "version", "category"]
)
def test_invalid_input_contract_is_rejected(kind):
    item = row()
    if kind == "status":
        item["observations"][POPULATION]["status"] = "missing"
    elif kind == "missingness":
        item["quality"]["missingness_by_predictor"][POPULATION] = True
    elif kind == "null":
        item["values"][POPULATION] = None
    elif kind == "unit":
        item["observations"][POPULATION]["unit"] = "m"
    elif kind == "source":
        item["quality"]["source_available_by_dataset"]["warsaw-population-2021"] = False
    elif kind == "version":
        item["extraction_version"] = "other"
    else:
        item["values"][USE] = "retail"
    with pytest.raises(ValueError):
        score_baseline(fitted(), [item])


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, True, "1"])
def test_nonfinite_or_invalid_numeric_values_fail(value):
    with pytest.raises(ValueError):
        score_baseline(fitted(), [row(value)])


@pytest.mark.parametrize(
    "predictors", [(), ("grocery_count_500m",), (POPULATION, POPULATION), ("label",)]
)
def test_only_declared_unique_environmental_predictors_can_enter_model(predictors):
    with pytest.raises(ValueError):
        fit_baseline(training(), [0, 0, 0, 0, 1, 1, 1, 1], predictors, 1.0)


@pytest.mark.parametrize(
    "labels", [[0] * 8, [1] * 8, [0, 1], [False] * 4 + [True] * 4, [0] * 4 + [0.5] * 4]
)
def test_training_labels_require_both_binary_integer_classes(labels):
    with pytest.raises(ValueError):
        fit_baseline(training(), labels, (POPULATION,), 1.0)


@pytest.mark.parametrize(
    "C,max_iter", [(0, 1000), (float("nan"), 1000), (True, 1000), (1, 0), (1, 1001), (1, True)]
)
def test_training_rejects_unbounded_or_invalid_solver_parameters(C, max_iter):
    with pytest.raises(ValueError):
        fit_baseline(training(), [0, 0, 0, 0, 1, 1, 1, 1], (POPULATION,), C, max_iter)


def test_convergence_warning_fails_instead_of_returning_a_scoring_model(monkeypatch):
    original = LogisticRegression.fit

    def warning_fit(self, X, y, **kwargs):
        result = original(self, X, y, **kwargs)
        warnings.warn("Forced convergence failure", ConvergenceWarning, stacklevel=2)
        return result

    monkeypatch.setattr(LogisticRegression, "fit", warning_fit)
    with pytest.raises(BaselineConvergenceError) as error:
        fitted()
    assert error.value.details["max_iter"] == 1000
    assert error.value.details["warnings"] == ["Forced convergence failure"]


def test_real_iteration_budget_exhaustion_is_reported():
    with pytest.raises(BaselineConvergenceError) as error:
        fit_baseline(training(), [0, 0, 0, 0, 1, 1, 1, 1], (POPULATION, USE), 1.0, max_iter=1)
    assert error.value.details["max_iter"] == 1
    assert error.value.details["iterations"] == [1]


def test_fit_and_joblib_roundtrip_are_deterministic():
    model = fitted()
    assert describe_baseline(model) == describe_baseline(fitted())
    stream = io.BytesIO()
    joblib.dump(model, stream)
    stream.seek(0)
    restored = joblib.load(stream)
    evaluation = [row(6, "s"), row(None, None)]
    assert score_baseline(model, evaluation) == score_baseline(restored, evaluation)
    assert describe_baseline(model) == describe_baseline(restored)
    json.dumps(describe_baseline(model), allow_nan=False)


def test_coefficients_reconstruct_scores_with_intercept_and_grouped_contributions():
    model = fitted()
    rows = [row(1), row(None, None), row(100, "s")]
    scores = score_baseline(model, rows)
    explanations = explain_baseline(model, rows)
    for score, item in zip(scores, explanations, strict=True):
        assert item["score"] == score
        assert item["intercept"] + sum(
            item["contributions_by_predictor"].values()
        ) == pytest.approx(score)
        assert item["intercept"] + sum(
            value["contribution"] for value in item["transformed_contributions"]
        ) == pytest.approx(score)
        assert item["interpretation"] == "model_contributions_not_causal_business_effects"


def test_empty_scoring_and_explanation_are_empty_and_recipe_is_explicit():
    model = fitted()
    assert score_baseline(model, []) == []
    assert explain_baseline(model, []) == []
    assert describe_baseline(model)["recipe"] == BASELINE_RECIPE
    assert BASELINE_RECIPE["cpu_threads"] == 1
