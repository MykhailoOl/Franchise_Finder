import json
import math
from dataclasses import dataclass
from numbers import Integral
from pathlib import Path
from tempfile import TemporaryDirectory

import catboost
import numpy as np
from catboost import CatBoostClassifier, CatBoostError, Pool

from app.core.experiment import content_hash
from app.core.grocery_baseline import (
    BASELINE_RECIPE,
    MISSING_CATEGORY,
    _column_definitions,
    _predictor_names,
    _raw_matrices,
)
from app.core.grocery_features import EXTRACTION_VERSION

CATBOOST_VERSION = "warsaw-grocery-catboost-v1"
FIXED_PARAMETERS = {
    "iterations": 300,
    "learning_rate": 0.05,
    "l2_leaf_reg": 3,
    "loss_function": "Logloss",
    "eval_metric": "Logloss",
    "task_type": "CPU",
    "thread_count": 4,
    "random_seed": 0,
    "boosting_type": "Ordered",
    "bootstrap_type": "MVS",
    "subsample": 0.8,
    "sampling_unit": "Object",
    "one_hot_max_size": 2,
    "counter_calc_method": "SkipTest",
    "grow_policy": "SymmetricTree",
    "nan_mode": "Min",
    "use_best_model": False,
    "allow_writing_files": False,
    "verbose": False,
}
CATBOOST_RECIPE = {
    "version": CATBOOST_VERSION,
    "feature_extraction_version": EXTRACTION_VERSION,
    "parameters": FIXED_PARAMETERS,
    "depth_candidates": [4, 6],
    "numeric_missingness": "native_NaN_nan_mode_Min_no_zero_fill_or_imputation",
    "categorical_missingness": MISSING_CATEGORY,
    "categorical_encoding": "native_CatBoost_training_only",
    "all_missing_numeric": "retain_native_NaN_column_record_training_empty_columns",
    "preprocessing": "no_external_scaling_imputation_or_one_hot_encoding",
    "validation_data_during_fit": "none_no_eval_set_no_early_stopping",
    "training_scope": "only_supplied_training_rows",
    "column_order": "same_logical_numeric_values_and_quality_flags_as_logistic_then_raw_categories",
    "each_selected_predictor_flags": BASELINE_RECIPE["each_selected_predictor_flags"],
    "spatial_predictor_flags": BASELINE_RECIPE["spatial_predictor_flags"],
    "population_and_residential_additional_flags": BASELINE_RECIPE[
        "population_and_residential_additional_flags"
    ],
    "shared_quality": BASELINE_RECIPE["shared_quality"],
    "forbidden_inputs": BASELINE_RECIPE["forbidden_inputs"],
    "other_parameters": "package_defaults_resolved_and_recorded_no_additional_search",
    "bagging_temperature": "not_applicable_to_MVS_bootstrap",
    "model_fingerprint": "native_JSON_except_guid_finish_time_plus_resolved_params_classes_schema",
    "incomplete_training": "fail_if_fitted_tree_count_does_not_equal_300",
    "shap": "native_ShapValues_Regular_without_external_reference_data",
    "interpretation": "relative_observed_presence_ranking_not_business_success_or_causality",
}


class CatBoostTrainingError(RuntimeError):
    pass


@dataclass
class GroceryCatBoost:
    model: CatBoostClassifier
    predictors: tuple[str, ...]
    numeric_columns: list[str]
    categorical_columns: list[str]
    training_row_count: int
    training_all_missing_columns: list[str]
    training_categories: dict[str, list[str]]
    model_sha256: str
    version: str = CATBOOST_VERSION


def _schema(fitted: GroceryCatBoost) -> dict:
    names = fitted.numeric_columns + fitted.categorical_columns
    return {
        "version": CATBOOST_VERSION,
        "extraction_version": EXTRACTION_VERSION,
        "predictors": list(fitted.predictors),
        "feature_order": names,
        "categorical_indices": list(range(len(fitted.numeric_columns), len(names))),
        "categorical_missing_value": MISSING_CATEGORY,
        "numeric_missing_value": "NaN",
    }


def _pool(rows: list[dict], predictors: tuple[str, ...], labels: list[int] | None = None) -> Pool:
    numeric, categorical = _raw_matrices(rows, predictors)
    numeric_names, categorical_names = _column_definitions(predictors)
    names = numeric_names + categorical_names
    data = np.empty((len(rows), len(names)), dtype=object)
    data[:, : len(numeric_names)] = numeric
    data[:, len(numeric_names) :] = categorical
    return Pool(
        data=data,
        label=labels,
        feature_names=names,
        cat_features=list(range(len(numeric_names), len(names))),
    )


def _fingerprint(fitted: GroceryCatBoost) -> str:
    with TemporaryDirectory(prefix="franchise-catboost-") as temporary:
        path = Path(temporary) / "model.json"
        fitted.model.save_model(str(path), format="json")
        payload = json.loads(path.read_bytes())
    metadata = payload.get("model_info", {})
    metadata.pop("model_guid", None)
    metadata.pop("train_finish_time", None)
    return content_hash(
        {
            "schema": _schema(fitted),
            "resolved_parameters": fitted.model.get_all_params(),
            "classes": fitted.model.classes_.tolist(),
            "model": payload,
        }
    )


def verify_catboost(fitted: GroceryCatBoost) -> dict:
    if not isinstance(fitted, GroceryCatBoost) or fitted.version != CATBOOST_VERSION:
        raise ValueError("Expected the frozen grocery CatBoost wrapper")
    if not fitted.model.is_fitted() or fitted.model.tree_count_ != FIXED_PARAMETERS["iterations"]:
        raise ValueError("CatBoost model must contain the complete frozen tree count")
    parameters = fitted.model.get_params()
    depth = parameters.get("depth")
    if depth not in {4, 6} or parameters != {**FIXED_PARAMETERS, "depth": depth}:
        raise ValueError("CatBoost parameters differ from the frozen recipe")
    if fitted.model.classes_.tolist() != [0, 1]:
        raise ValueError("CatBoost class order must preserve binary presence labels")
    resolved = fitted.model.get_all_params()
    native_parameters = json.loads(fitted.model.get_metadata()["params"])["flat_params"]
    for name, expected in {**FIXED_PARAMETERS, "depth": depth}.items():
        actual = resolved.get(name, native_parameters.get(name))
        matches = (
            (
                isinstance(actual, (int, float))
                and math.isclose(actual, expected, rel_tol=1e-7, abs_tol=1e-12)
            )
            if type(expected) is float
            else actual == expected
        )
        if not matches:
            raise ValueError(f"Resolved CatBoost parameter {name} differs from the frozen recipe")
    numeric_names, categorical_names = _column_definitions(_predictor_names(fitted.predictors))
    if fitted.numeric_columns != numeric_names or fitted.categorical_columns != categorical_names:
        raise ValueError("CatBoost wrapper schema differs from its frozen predictors")
    schema = _schema(fitted)
    if fitted.model.feature_names_ != schema["feature_order"]:
        raise ValueError("CatBoost tree feature order differs from the wrapper schema")
    if fitted.model.get_cat_feature_indices() != schema["categorical_indices"]:
        raise ValueError("CatBoost categorical indices differ from the wrapper schema")
    digest = _fingerprint(fitted)
    if digest != fitted.model_sha256:
        raise ValueError("CatBoost tree or schema fingerprint changed")
    return {"model_sha256": digest, "tree_count": fitted.model.tree_count_, "verified": True}


def fit_catboost(
    rows: list[dict], labels: list[int], predictors: tuple[str, ...], depth: int
) -> GroceryCatBoost:
    names = _predictor_names(predictors)
    if isinstance(depth, bool) or not isinstance(depth, Integral) or depth not in {4, 6}:
        raise ValueError("CatBoost depth must be one of the frozen candidates 4 and 6")
    if not rows or len(rows) != len(labels):
        raise ValueError("Training rows and labels must have the same nonzero length")
    if any(
        isinstance(label, bool) or not isinstance(label, Integral) or label not in {0, 1}
        for label in labels
    ) or set(labels) != {0, 1}:
        raise ValueError("Training requires integer binary labels and both classes")
    numeric, categorical = _raw_matrices(rows, names)
    numeric_names, categorical_names = _column_definitions(names)
    training_empty = [
        name for index, name in enumerate(numeric_names) if np.isnan(numeric[:, index]).all()
    ]
    categories = {
        name: sorted(set(categorical[:, index])) for index, name in enumerate(categorical_names)
    }
    model = CatBoostClassifier(**FIXED_PARAMETERS, depth=int(depth))
    try:
        model.fit(_pool(rows, names, labels))
    except CatBoostError as error:
        raise CatBoostTrainingError(f"CatBoost training failed: {error}") from error
    if model.tree_count_ != FIXED_PARAMETERS["iterations"]:
        raise CatBoostTrainingError("CatBoost stopped before completing the frozen 300 trees")
    fitted = GroceryCatBoost(
        model=model,
        predictors=names,
        numeric_columns=numeric_names,
        categorical_columns=categorical_names,
        training_row_count=len(rows),
        training_all_missing_columns=training_empty,
        training_categories=categories,
        model_sha256="",
    )
    fitted.model_sha256 = _fingerprint(fitted)
    verify_catboost(fitted)
    return fitted


def score_catboost(fitted: GroceryCatBoost, rows: list[dict]) -> list[float]:
    verify_catboost(fitted)
    if not rows:
        return []
    values = fitted.model.predict(
        _pool(rows, fitted.predictors),
        prediction_type="RawFormulaVal",
        thread_count=FIXED_PARAMETERS["thread_count"],
    )
    scores = np.asarray(values, dtype=np.float64)
    if scores.shape != (len(rows),) or not np.isfinite(scores).all():
        raise ValueError("CatBoost produced invalid ranking scores")
    return scores.tolist()


def describe_catboost(fitted: GroceryCatBoost) -> dict:
    verified = verify_catboost(fitted)
    return {
        "version": CATBOOST_VERSION,
        "catboost_version": catboost.__version__,
        "recipe": CATBOOST_RECIPE,
        "predictors": list(fitted.predictors),
        "training_row_count": fitted.training_row_count,
        "depth": fitted.model.get_all_params()["depth"],
        "tree_count": fitted.model.tree_count_,
        "parameters": fitted.model.get_params(),
        "resolved_parameters": fitted.model.get_all_params(),
        "feature_schema": _schema(fitted),
        "training_all_missing_columns": fitted.training_all_missing_columns,
        "training_categories_for_diagnostics_only": fitted.training_categories,
        "numeric_borders": {
            str(index): values for index, values in fitted.model.get_borders().items()
        },
        "model_sha256": verified["model_sha256"],
        "fingerprint_verified": True,
        "score_type": "raw_formula_relative_presence_ranking_not_success_probability",
        "preprocessing_scope": "training_rows_only",
    }


def diagnose_catboost(fitted: GroceryCatBoost, rows: list[dict]) -> dict:
    verify_catboost(fitted)
    numeric, categorical = _raw_matrices(rows, fitted.predictors)
    unseen = {}
    for index, name in enumerate(fitted.categorical_columns):
        counts = {}
        for value in categorical[:, index]:
            if value not in fitted.training_categories[name]:
                counts[value] = counts.get(value, 0) + 1
        unseen[name] = counts
    return {
        "row_count": len(rows),
        "unseen_category_counts": unseen,
        "native_missing_numeric_counts": {
            name: int(np.isnan(numeric[:, index]).sum())
            for index, name in enumerate(fitted.numeric_columns)
        },
        "training_all_missing_columns": fitted.training_all_missing_columns,
    }


def explain_catboost(fitted: GroceryCatBoost, rows: list[dict]) -> list[dict]:
    verify_catboost(fitted)
    if not rows:
        return []
    pool = _pool(rows, fitted.predictors)
    shap = np.asarray(
        fitted.model.get_feature_importance(
            pool,
            type="ShapValues",
            thread_count=FIXED_PARAMETERS["thread_count"],
            shap_calc_type="Regular",
        ),
        dtype=np.float64,
    )
    scores = score_catboost(fitted, rows)
    names = _schema(fitted)["feature_order"]
    if shap.shape != (len(rows), len(names) + 1) or not np.isfinite(shap).all():
        raise ValueError("CatBoost returned an invalid SHAP matrix")
    explanations = []
    for contributions, score in zip(shap, scores, strict=True):
        bias = float(contributions[-1])
        if not math.isclose(
            bias + math.fsum(contributions[:-1]), score, rel_tol=1e-8, abs_tol=1e-8
        ):
            raise ValueError("CatBoost SHAP contributions do not reconstruct the ranking score")
        grouped = {name: 0.0 for name in fitted.predictors}
        for name, contribution in zip(names, contributions[:-1], strict=True):
            grouped[name.split("__", 1)[0]] += float(contribution)
        explanations.append(
            {
                "score": score,
                "expected_value": bias,
                "contributions_by_predictor": grouped,
                "feature_contributions": [
                    {"feature": name, "contribution": float(value)}
                    for name, value in zip(names, contributions[:-1], strict=True)
                ],
                "method": "CatBoost_ShapValues_Regular",
                "interpretation": "model_contributions_not_causal_business_effects",
            }
        )
    return explanations
