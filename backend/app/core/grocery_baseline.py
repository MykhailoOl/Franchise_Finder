import math
import warnings
from numbers import Integral, Real

import numpy as np
import sklearn
from sklearn.base import BaseEstimator, TransformerMixin
from sklearn.exceptions import ConvergenceWarning
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler
from sklearn.utils.validation import check_is_fitted
from threadpoolctl import threadpool_limits

from app.core.data_contract import BUILDING_USE_CODES, PREDICTORS
from app.core.grocery_features import EXTRACTION_VERSION, FEATURE_IDS

BASELINE_VERSION = "warsaw-grocery-logistic-v1"
MISSING_CATEGORY = "__missing__"
RANDOM_SEED = 0
STATUSES = {"available", "partial", "missing", "unavailable", "outside_coverage", "censored"}
BASELINE_RECIPE = {
    "version": BASELINE_VERSION,
    "numeric_imputation": "training_median_only",
    "all_missing_numeric": "drop_training_empty_columns_and_never_revive_at_inference",
    "scaling": "training_mean_and_standard_deviation_after_median_imputation",
    "scaled_columns": "all_retained_numeric_values_and_quality_flags",
    "unscaled_columns": "one_hot_categorical_columns",
    "categorical_encoding": "training_only_one_hot_no_dropped_category_ignore_unseen",
    "each_selected_predictor_flags": ["missing", "censored", "partial", "source_available"],
    "spatial_predictor_flags": ["coverage_fraction", "coverage_missing"],
    "population_and_residential_additional_flags": [
        "source_coverage_fraction",
        "source_coverage_missing",
    ],
    "forbidden_inputs": [
        "building_identifiers",
        "labels",
        "folds",
        "source_identifiers",
        "dates",
        "unselected_predictors",
        "activity_exclusion_counts",
        "neighbor_diagnostic_counts",
    ],
    "solver": "lbfgs",
    "penalty": "l2",
    "random_seed": RANDOM_SEED,
    "cpu_threads": 1,
    "max_iterations": 1000,
    "tolerance": 1e-4,
    "class_weight": None,
    "one_hot_drop": None,
    "column_order": "predictor_order_numeric_values_and_flags_then_training_category_order",
    "shared_quality": "retained_per_selected_predictor_including_repeated_geographic_coverage",
    "convergence_failure": "raise_no_scores_no_automatic_iteration_increase",
}


class BaselineConvergenceError(RuntimeError):
    def __init__(self, max_iter: int, iterations: list[int], messages: list[str]):
        self.details = {"max_iter": max_iter, "iterations": iterations, "warnings": messages}
        super().__init__(f"Logistic baseline did not converge within {max_iter} iterations")


def _numeric(value: object, name: str, *, fraction: bool = False) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a number or null")
    result = float(value)
    if not math.isfinite(result) or result < 0 or (fraction and result > 1):
        raise ValueError(f"{name} is outside its numeric domain")
    return result


def _predictor_names(predictors: tuple[str, ...]) -> tuple[str, ...]:
    names = tuple(predictors)
    if not names or len(names) != len(set(names)) or set(names) - set(FEATURE_IDS):
        raise ValueError("Predictors must be a nonempty unique subset of the frozen 17 features")
    return names


def _source_coverage(row: dict, feature: str) -> float | None:
    definition = PREDICTORS[feature]
    try:
        value = row["quality"]["source_coverage_fraction_by_radius"][definition.sources[0]][
            str(definition.radius_m)
        ]
    except (KeyError, TypeError) as error:
        raise ValueError(f"Missing geographic coverage for {feature}") from error
    return _numeric(value, f"{feature}.source_coverage", fraction=True)


def _validated_feature(row: dict, feature: str) -> tuple[object, dict, bool]:
    try:
        value = row["values"][feature]
        observation = row["observations"][feature]
        missing_flag = row["quality"]["missingness_by_predictor"][feature]
        source_available = row["quality"]["source_available_by_dataset"][
            PREDICTORS[feature].sources[0]
        ]
    except (KeyError, TypeError) as error:
        raise ValueError(f"Missing frozen feature fields for {feature}") from error
    if not isinstance(observation, dict):
        raise ValueError(f"Invalid observation record for {feature}")
    status = observation.get("status")
    definition = PREDICTORS[feature]
    if status not in STATUSES or observation.get("unit") != definition.unit:
        raise ValueError(f"Invalid observation status or unit for {feature}")
    if (status in {"available", "partial"}) != (value is not None):
        raise ValueError(f"Observation status and value disagree for {feature}")
    if type(missing_flag) is not bool or missing_flag != (value is None):
        raise ValueError(f"Missingness flag and value disagree for {feature}")
    if type(source_available) is not bool or (value is not None and not source_available):
        raise ValueError(f"Source availability disagrees with the value for {feature}")
    if status != "available" and not isinstance(observation.get("reason"), str):
        raise ValueError(f"Incomplete observation requires a reason for {feature}")
    if status != "available" and not observation["reason"].strip():
        raise ValueError(f"Incomplete observation requires a reason for {feature}")
    if status == "censored" and not feature.startswith("nearest_"):
        raise ValueError("Only nearest-distance observations may be censored")
    coverage = _numeric(observation.get("coverage_fraction"), feature, fraction=True)
    if value is not None:
        if definition.unit == "category":
            if not isinstance(value, str) or value not in BUILDING_USE_CODES:
                raise ValueError("Mapped building use must be a known municipal code or null")
        else:
            value = _numeric(value, feature, fraction=definition.unit == "fraction")
            if definition.unit in {"count", "storeys"} and not value.is_integer():
                raise ValueError(f"{feature} requires an integer value")
            if definition.unit in {"storeys", "m2"} and value <= 0:
                raise ValueError(f"{feature} requires a positive value")
            if feature.startswith("nearest_") and value > definition.radius_m:
                raise ValueError("Nearest distance exceeds its frozen search radius")
        if feature.startswith("resident_subtotal_"):
            if coverage is None or coverage <= 0:
                raise ValueError("Population subtotals require positive published-cell coverage")
            if status == "available" and coverage < 0.999:
                raise ValueError("Incomplete population coverage requires partial status")
    return value, observation, source_available


def _column_definitions(predictors: tuple[str, ...]) -> tuple[list[str], list[str]]:
    numeric, categorical = [], []
    for feature in predictors:
        definition = PREDICTORS[feature]
        if definition.unit == "category":
            categorical.append(feature)
        else:
            numeric.append(f"{feature}__value")
        numeric.extend(
            f"{feature}__{suffix}"
            for suffix in ("missing", "censored", "partial", "source_available")
        )
        if definition.radius_m is not None:
            numeric.extend((f"{feature}__coverage_fraction", f"{feature}__coverage_missing"))
        if (
            feature.startswith("resident_subtotal_")
            or feature == "residential_building_fraction_500m"
        ):
            numeric.extend(
                (f"{feature}__source_coverage_fraction", f"{feature}__source_coverage_missing")
            )
    return numeric, categorical


def _raw_matrices(rows: list[dict], predictors: tuple[str, ...]) -> tuple[np.ndarray, np.ndarray]:
    numeric_rows, category_rows = [], []
    for row in rows:
        if row.get("extraction_version") != EXTRACTION_VERSION:
            raise ValueError("Baseline rows must use the frozen feature extraction version")
        numeric, categorical = [], []
        for feature in predictors:
            value, observation, available = _validated_feature(row, feature)
            definition = PREDICTORS[feature]
            if definition.unit == "category":
                categorical.append(value if value is not None else MISSING_CATEGORY)
            else:
                numeric.append(np.nan if value is None else value)
            numeric.extend(
                (
                    float(value is None),
                    float(observation["status"] == "censored"),
                    float(observation["status"] == "partial"),
                    float(available),
                )
            )
            if definition.radius_m is not None:
                coverage = observation.get("coverage_fraction")
                numeric.extend((np.nan if coverage is None else coverage, float(coverage is None)))
            if (
                feature.startswith("resident_subtotal_")
                or feature == "residential_building_fraction_500m"
            ):
                coverage = _source_coverage(row, feature)
                numeric.extend((np.nan if coverage is None else coverage, float(coverage is None)))
        numeric_rows.append(numeric)
        category_rows.append(categorical)
    columns, categories = _column_definitions(predictors)
    return (
        np.asarray(numeric_rows, dtype=np.float64).reshape(len(rows), len(columns)),
        np.asarray(category_rows, dtype=object).reshape(len(rows), len(categories)),
    )


class FeatureMatrix(TransformerMixin, BaseEstimator):
    def __init__(self, predictors: tuple[str, ...]):
        self.predictors = predictors

    def fit(self, X: list[dict], y=None):
        names = _predictor_names(self.predictors)
        if not X:
            raise ValueError("Cannot fit preprocessing without training rows")
        numeric, categorical = _raw_matrices(X, names)
        columns, category_names = _column_definitions(names)
        self.predictors_ = names
        self.numeric_columns_ = columns
        self.categorical_columns_ = category_names
        self.keep_numeric_ = ~np.isnan(numeric).all(axis=0)
        self.dropped_columns_ = [
            name for name, keep in zip(columns, self.keep_numeric_, strict=True) if not keep
        ]
        self.kept_columns_ = [
            name for name, keep in zip(columns, self.keep_numeric_, strict=True) if keep
        ]
        kept = numeric[:, self.keep_numeric_]
        self.medians_ = np.nanmedian(kept, axis=0)
        filled = np.where(np.isnan(kept), self.medians_, kept)
        self.scaler_ = StandardScaler().fit(filled)
        self.encoder_ = None
        self.output_columns_ = list(self.kept_columns_)
        if category_names:
            self.encoder_ = OneHotEncoder(handle_unknown="ignore", sparse_output=False)
            self.encoder_.fit(categorical)
            self.output_columns_.extend(
                f"{name}__category={value}"
                for name, values in zip(category_names, self.encoder_.categories_, strict=True)
                for value in values
            )
        self.training_row_count_ = len(X)
        return self

    def transform(self, X: list[dict]) -> np.ndarray:
        check_is_fitted(self, "output_columns_")
        numeric, categorical = _raw_matrices(X, self.predictors_)
        if not X:
            return np.empty((0, len(self.output_columns_)))
        kept = numeric[:, self.keep_numeric_]
        filled = np.where(np.isnan(kept), self.medians_, kept)
        transformed = self.scaler_.transform(filled)
        if self.encoder_ is not None:
            transformed = np.hstack((transformed, self.encoder_.transform(categorical)))
        if not np.isfinite(transformed).all():
            raise ValueError("Preprocessing produced a nonfinite feature value")
        return transformed

    def get_feature_names_out(self, input_features=None) -> np.ndarray:
        check_is_fitted(self, "output_columns_")
        return np.asarray(self.output_columns_, dtype=object)


def fit_baseline(
    rows: list[dict], labels: list[int], predictors: tuple[str, ...], C: float, max_iter: int = 1000
) -> Pipeline:
    names = _predictor_names(predictors)
    if isinstance(C, bool) or not isinstance(C, Real) or not math.isfinite(C) or C <= 0:
        raise ValueError("Regularization C must be finite and positive")
    if (
        isinstance(max_iter, bool)
        or not isinstance(max_iter, Integral)
        or not 1 <= max_iter <= 1000
    ):
        raise ValueError("Iteration budget must be an integer between one and 1000")
    if len(rows) != len(labels) or not rows:
        raise ValueError("Training rows and labels must have the same nonzero length")
    if any(
        isinstance(label, bool) or not isinstance(label, Integral) or label not in {0, 1}
        for label in labels
    ) or set(labels) != {0, 1}:
        raise ValueError("Training requires integer binary labels and both classes")
    pipeline = Pipeline(
        [
            ("features", FeatureMatrix(names)),
            (
                "classifier",
                LogisticRegression(
                    C=float(C),
                    solver="lbfgs",
                    l1_ratio=0.0,
                    max_iter=int(max_iter),
                    class_weight=None,
                    random_state=RANDOM_SEED,
                    tol=1e-4,
                ),
            ),
        ]
    )
    with warnings.catch_warnings(record=True) as recorded, threadpool_limits(limits=1):
        warnings.simplefilter("always", ConvergenceWarning)
        pipeline.fit(rows, np.asarray(labels, dtype=np.int64))
    failures = [
        str(item.message) for item in recorded if issubclass(item.category, ConvergenceWarning)
    ]
    if failures:
        raise BaselineConvergenceError(
            int(max_iter), pipeline["classifier"].n_iter_.tolist(), failures
        )
    pipeline.baseline_version_ = BASELINE_VERSION
    return pipeline


def _fitted_parts(fitted: Pipeline) -> tuple[FeatureMatrix, LogisticRegression]:
    if (
        not isinstance(fitted, Pipeline)
        or getattr(fitted, "baseline_version_", None) != BASELINE_VERSION
    ):
        raise ValueError("Expected a fitted frozen logistic baseline")
    features, classifier = fitted["features"], fitted["classifier"]
    check_is_fitted(features, "output_columns_")
    check_is_fitted(classifier, "coef_")
    return features, classifier


def score_baseline(fitted: Pipeline, rows: list[dict]) -> list[float]:
    _fitted_parts(fitted)
    if not rows:
        return []
    with threadpool_limits(limits=1):
        scores = fitted.decision_function(rows)
    if not np.isfinite(scores).all():
        raise ValueError("Baseline produced a nonfinite ranking score")
    return scores.tolist()


def describe_baseline(fitted: Pipeline) -> dict:
    features, classifier = _fitted_parts(fitted)
    coefficients = classifier.coef_[0].tolist()
    return {
        "version": BASELINE_VERSION,
        "sklearn_version": sklearn.__version__,
        "score_type": "decision_function_relative_presence_ranking_not_success_probability",
        "predictors": list(features.predictors_),
        "training_row_count": features.training_row_count_,
        "C": classifier.C,
        "solver": classifier.solver,
        "penalty": "l2",
        "max_iter": classifier.max_iter,
        "iterations": classifier.n_iter_.tolist(),
        "converged": True,
        "random_seed": RANDOM_SEED,
        "fit_medians": dict(zip(features.kept_columns_, features.medians_.tolist(), strict=True)),
        "fit_means": dict(
            zip(features.kept_columns_, features.scaler_.mean_.tolist(), strict=True)
        ),
        "fit_scales": dict(
            zip(features.kept_columns_, features.scaler_.scale_.tolist(), strict=True)
        ),
        "categories": dict(
            zip(
                features.categorical_columns_,
                [values.tolist() for values in features.encoder_.categories_],
                strict=True,
            )
        )
        if features.encoder_ is not None
        else {},
        "missing_category": MISSING_CATEGORY,
        "unknown_category_policy": "ignore_unseen_one_hot_categories_keep_fixed_missingness",
        "dropped_all_missing_features": [
            name.removesuffix("__value")
            for name in features.dropped_columns_
            if name.endswith("__value")
        ],
        "dropped_all_missing_columns": features.dropped_columns_,
        "transformed_feature_order": features.output_columns_,
        "coefficients": dict(zip(features.output_columns_, coefficients, strict=True)),
        "intercept": float(classifier.intercept_[0]),
        "preprocessing_scope": "training_rows_only",
        "recipe": BASELINE_RECIPE,
    }


def diagnose_baseline(fitted: Pipeline, rows: list[dict]) -> dict:
    features, _ = _fitted_parts(fitted)
    _, categorical = _raw_matrices(rows, features.predictors_)
    unseen = {}
    if features.encoder_ is not None:
        for index, (name, known) in enumerate(
            zip(features.categorical_columns_, features.encoder_.categories_, strict=True)
        ):
            counts = {}
            for value in categorical[:, index]:
                if value not in known:
                    counts[value] = counts.get(value, 0) + 1
            unseen[name] = counts
    return {
        "row_count": len(rows),
        "unseen_category_counts": unseen,
        "dropped_training_empty_features": [
            name.removesuffix("__value")
            for name in features.dropped_columns_
            if name.endswith("__value")
        ],
    }


def explain_baseline(fitted: Pipeline, rows: list[dict]) -> list[dict]:
    features, classifier = _fitted_parts(fitted)
    matrix = features.transform(rows)
    scores = score_baseline(fitted, rows)
    result = []
    for values, score in zip(matrix, scores, strict=True):
        contributions = values * classifier.coef_[0]
        intercept = float(classifier.intercept_[0])
        if not math.isclose(
            intercept + math.fsum(contributions), score, rel_tol=1e-10, abs_tol=1e-10
        ):
            raise ValueError("Coefficient contributions do not reconstruct the ranking score")
        grouped = {name: 0.0 for name in features.predictors_}
        transformed = []
        for name, value, coefficient, contribution in zip(
            features.output_columns_, values, classifier.coef_[0], contributions, strict=True
        ):
            grouped[name.split("__", 1)[0]] += float(contribution)
            transformed.append(
                {
                    "feature": name,
                    "transformed_value": float(value),
                    "coefficient": float(coefficient),
                    "contribution": float(contribution),
                }
            )
        result.append(
            {
                "score": score,
                "intercept": intercept,
                "contributions_by_predictor": grouped,
                "transformed_contributions": transformed,
                "interpretation": "model_contributions_not_causal_business_effects",
            }
        )
    return result
