import math

from app.core.experiment import ExperimentDefinition, configuration_hash, content_hash
from app.core.grocery_catboost import describe_catboost, fit_catboost, score_catboost
from app.core.grocery_evaluation import (
    METRICS,
    REFERENCE_NAMES,
    _counts,
    _distribution,
    _feature_rows,
    _support,
    development_gate,
    random_reference_metrics,
    ranking_metrics,
    scenario_definitions,
    summarize_folds,
    validate_memberships,
)

EVALUATION_VERSION = "grocery-catboost-exploratory-spatial-v1"
SAVED_MODEL_NAMES = {
    "candidate": "logistic",
    "population_only": "population_only",
    "access_only": "access_only",
}


def withheld_promotion() -> dict:
    return {
        "status": "withheld_reused_holdout",
        "eligible_for_promotion": False,
        "reason": "The final geography was already evaluated for logistic baselines",
        "fresh_confirmation": False,
        "sensitivity_models_can_replace_primary": False,
    }


def depth_grid(definition: ExperimentDefinition) -> tuple[int, int]:
    expected = {
        "family": "CatBoostClassifier",
        "task_type": "CPU",
        "thread_count": 4,
        "iterations": 300,
        "depth": [4, 6],
        "learning_rate": 0.05,
        "l2_leaf_reg": 3,
        "max_trials": 2,
    }
    if any(definition.models["catboost_cpu"].get(key) != value for key, value in expected.items()):
        raise ValueError("CatBoost evaluation requires the exact frozen depth-4/depth-6 CPU recipe")
    return 4, 6


def validate_saved_predictions(
    predictions: list[dict], pools: dict, definition: ExperimentDefinition, final: bool = False
) -> tuple[dict, str]:
    specifications = scenario_definitions(definition)
    folds = (3,) if final else definition.split.development_folds
    groups = {}
    normalized = []
    for row in predictions:
        if not isinstance(row, dict):
            raise ValueError("Saved logistic predictions must be records")
        scenario, model, fold = row.get("scenario"), row.get("model"), row.get("fold")
        identifier = row.get("building_source_id")
        if (
            scenario not in specifications
            or model not in SAVED_MODEL_NAMES
            or type(fold) is not int
            or fold not in folds
            or row.get("pool") != specifications[scenario]["pool"]
            or not isinstance(identifier, str)
            or not identifier
        ):
            raise ValueError(
                "Saved logistic prediction has an unexpected scenario/pool/model/fold/ID"
            )
        if row.get("label") not in ("observed_presence", "unlabelled_background"):
            raise ValueError("Saved logistic prediction has an invalid membership label")
        score = row.get("decision_score")
        if type(score) not in (float, int) or not math.isfinite(score):
            raise ValueError("Saved logistic prediction scores must be finite numbers")
        key = (scenario, fold, model)
        group = groups.setdefault(key, {})
        if identifier in group:
            raise ValueError("Duplicate saved logistic prediction for the same building/model/fold")
        group[identifier] = {"label": row.get("label"), "score": float(score)}
        normalized.append(
            {
                key: row[key]
                for key in (
                    "scenario",
                    "pool",
                    "model",
                    "fold",
                    "building_source_id",
                    "label",
                    "decision_score",
                )
            }
        )
    expected_groups = {
        (scenario, fold, model)
        for scenario in specifications
        for fold in folds
        for model in SAVED_MODEL_NAMES
    }
    if set(groups) != expected_groups:
        raise ValueError("Saved logistic predictions must include every frozen comparison group")
    for scenario, specification in specifications.items():
        for fold in folds:
            expected = {
                row["building_source_id"]: row["label"]
                for row in pools[specification["pool"]]
                if row["validation_fold"] == fold
            }
            for model in SAVED_MODEL_NAMES:
                actual = groups[(scenario, fold, model)]
                if set(actual) != set(expected):
                    raise ValueError(
                        "Saved logistic prediction building IDs differ from frozen evaluation rows"
                    )
                if any(actual[key]["label"] != label for key, label in expected.items()):
                    raise ValueError(
                        "Saved logistic prediction labels differ from frozen memberships"
                    )
    normalized.sort(
        key=lambda row: (row["scenario"], row["fold"], row["model"], row["building_source_id"])
    )
    return groups, content_hash(normalized)


def saved_metrics(groups: dict, scenario: str, fold: int, evaluate: list[dict]) -> dict:
    labels = [int(row["label"] == "observed_presence") for row in evaluate]
    metrics = {
        name: ranking_metrics(
            labels,
            [
                groups[(scenario, fold, saved_name)][row["building_source_id"]]["score"]
                for row in evaluate
            ],
        )
        for saved_name, name in SAVED_MODEL_NAMES.items()
    }
    metrics["prevalence_reference"] = ranking_metrics(labels, [0.0] * len(labels))
    metrics["random_reference"] = random_reference_metrics(labels)
    return metrics


def _fit(features, train, predictors, depth):
    rows, labels = _feature_rows(features, train)
    return fit_catboost(rows, labels, tuple(predictors), depth=depth)


def _evaluate(model, features, evaluate):
    rows, labels = _feature_rows(features, evaluate)
    scores = score_catboost(model, rows)
    return ranking_metrics(labels, scores), scores


def choose_depth(features, outer_train, outer_fold, predictors, definition):
    inner = []
    for fold in definition.split.development_folds:
        if fold == outer_fold:
            continue
        train = [row for row in outer_train if fold in row["training_folds"]]
        evaluate = [row for row in outer_train if row["validation_fold"] == fold]
        inner.append((fold, train, evaluate, _support(train, evaluate, definition)))
    counts = [{"fold": fold, **support} for fold, _, _, support in inner]
    if not all(item[3]["sufficient"] for item in inner):
        return 4, {
            "status": "unavailable_fixed_depth_4",
            "inner_folds": counts,
            "reason": "An inner geographic fold lacks support; no inner folds were dropped",
            "trials": [],
        }
    trials = []
    for depth in depth_grid(definition):
        scores = []
        for fold, train, evaluate, _ in inner:
            print(f"  Inner fold {fold}, depth {depth}, training rows {len(train)}", flush=True)
            model = _fit(features, train, predictors, depth)
            scores.append(_evaluate(model, features, evaluate)[0]["average_precision"])
        trials.append(
            {
                "depth": depth,
                "inner_average_precision": scores,
                "mean_average_precision": math.fsum(scores) / len(scores),
            }
        )
    selected = max(trials, key=lambda trial: (trial["mean_average_precision"], -trial["depth"]))
    return selected["depth"], {
        "status": "nested_geographic_selection",
        "inner_folds": counts,
        "trials": trials,
        "selection_metric": "mean_inner_average_precision",
        "tie_break": "shallower_depth",
    }


def summarize_comparison(folds: list[dict]) -> dict:
    summary = summarize_folds(folds)
    summary["metrics"]["logistic"] = {
        metric: _distribution([row["metrics"]["logistic"][metric] for row in folds])
        for metric in METRICS
    }
    summary["paired_region_gains"]["logistic"] = {
        metric: _distribution(
            [
                row["metrics"]["candidate"][metric] - row["metrics"]["logistic"][metric]
                for row in folds
            ]
        )
        for metric in METRICS
    }
    summary["logistic_comparison_scope"] = (
        "Descriptive paired comparison; not a promotion criterion"
    )
    return summary


def fit_and_evaluate_catboost_development(
    feature_rows_by_id: dict[str, dict],
    memberships: list[dict],
    definition: ExperimentDefinition,
    logistic_dev_predictions: list[dict],
) -> tuple[dict, dict, dict]:
    depths = depth_grid(definition)
    pools = validate_memberships(memberships)
    saved, predictions_hash = validate_saved_predictions(
        logistic_dev_predictions, pools, definition
    )
    for pool, rows in pools.items():
        for fold in definition.split.development_folds:
            train = [row for row in rows if fold in row["training_folds"]]
            evaluate = [row for row in rows if row["validation_fold"] == fold]
            if not _support(train, evaluate, definition)["sufficient"]:
                raise ValueError(f"Insufficient outer-fold support: {pool}, fold {fold}")
        counts = _counts([row for row in rows if 3 in row["training_folds"]])
        if (
            counts["positives"] < definition.support.minimum_train_positives
            or counts["background"] < definition.support.minimum_background
        ):
            raise ValueError(f"Insufficient final-training development support: {pool}")
    report = {
        "version": EVALUATION_VERSION,
        "phase": "exploratory_development",
        "scenarios": {},
        "prediction_rows": [],
        "logistic_retrained": False,
        "final_holdout_evaluated": False,
        "final_holdout_previously_used": True,
        "promotion_gate": withheld_promotion(),
        "logistic_predictions_sha256": predictions_hash,
    }
    models, locked_scenarios, used_ids = {}, {}, set()
    for scenario, specification in scenario_definitions(definition).items():
        rows, predictors = pools[specification["pool"]], specification["predictors"]
        folds, fixed_depth_results = [], {depth: [] for depth in depths}
        for fold in definition.split.development_folds:
            print(f"CatBoost scenario {scenario}, outer fold {fold}", flush=True)
            train = [row for row in rows if fold in row["training_folds"]]
            evaluate = [row for row in rows if row["validation_fold"] == fold]
            used_ids.update(row["building_source_id"] for row in (*train, *evaluate))
            selected_depth, tuning = choose_depth(
                feature_rows_by_id, train, fold, predictors, definition
            )
            candidate = _fit(feature_rows_by_id, train, predictors, selected_depth)
            candidate_metrics, scores = _evaluate(candidate, feature_rows_by_id, evaluate)
            metrics = {
                "candidate": candidate_metrics,
                **saved_metrics(saved, scenario, fold, evaluate),
            }
            for depth in depths:
                if depth == selected_depth:
                    result = candidate_metrics
                else:
                    print(f"  Fixed-depth development trial {depth}", flush=True)
                    fixed = _fit(feature_rows_by_id, train, predictors, depth)
                    result = _evaluate(fixed, feature_rows_by_id, evaluate)[0]
                fixed_depth_results[depth].append(result["average_precision"])
            report["prediction_rows"].extend(
                {
                    "scenario": scenario,
                    "pool": specification["pool"],
                    "model": "catboost",
                    "fold": fold,
                    "building_source_id": row["building_source_id"],
                    "label": row["label"],
                    "decision_score": score,
                }
                for row, score in zip(evaluate, scores, strict=True)
            )
            folds.append(
                {
                    "fold": fold,
                    "support": _support(train, evaluate, definition),
                    "training_ids_sha256": content_hash(
                        [row["building_source_id"] for row in train]
                    ),
                    "evaluation_ids_sha256": content_hash(
                        [row["building_source_id"] for row in evaluate]
                    ),
                    "selected_depth": selected_depth,
                    "tuning": tuning,
                    "metrics": metrics,
                }
            )
        final_depth = max(
            depths, key=lambda depth: (math.fsum(fixed_depth_results[depth]) / 3, -depth)
        )
        final_train = [row for row in rows if 3 in row["training_folds"]]
        used_ids.update(row["building_source_id"] for row in final_train)
        print(
            f"Locking scenario {scenario}, depth {final_depth}, training rows {len(final_train)}",
            flush=True,
        )
        final_model = _fit(feature_rows_by_id, final_train, predictors, final_depth)
        description = describe_catboost(final_model)
        models[scenario] = final_model
        report["scenarios"][scenario] = {
            "pool": specification["pool"],
            "predictors": list(predictors),
            "folds": folds,
            "summary": summarize_comparison(folds),
            "final_depth": final_depth,
            "final_training_support": _counts(final_train),
            "final_model": description,
            "final_depth_selection": {
                "method": "fixed_depth_development_mean_AP",
                "folds": [0, 1, 2],
                "tie_break": "shallower_depth",
                "trials": [
                    {
                        "depth": depth,
                        "average_precision_by_fold": results,
                        "mean_average_precision": math.fsum(results) / 3,
                    }
                    for depth, results in fixed_depth_results.items()
                ],
            },
        }
        locked_scenarios[scenario] = {
            "pool": specification["pool"],
            "predictors": list(predictors),
            "final_depth": final_depth,
            "training_ids": [row["building_source_id"] for row in final_train],
            "description": description,
            "description_sha256": content_hash(description),
        }
    report["reference_gate_diagnostic"] = development_gate(
        report["scenarios"]["primary"]["summary"], definition
    )
    engine = {
        "version": EVALUATION_VERSION,
        "state": "models_locked_before_reused_final",
        "config_sha256": configuration_hash(definition),
        "scenarios": locked_scenarios,
        "development_memberships_sha256": content_hash(pools),
        "development_features_sha256": content_hash(
            [feature_rows_by_id[key] for key in sorted(used_ids)]
        ),
        "logistic_development_predictions_sha256": predictions_hash,
        "primary_development_summary": report["scenarios"]["primary"]["summary"],
        "final_holdout_evaluated": False,
        "promotion_status": "withheld_reused_holdout",
    }
    return report, models, {"engine": engine, "engine_sha256": content_hash(engine)}


def evaluate_catboost_final(
    locked_models: dict,
    final_feature_rows_by_id: dict[str, dict],
    memberships: list[dict],
    definition: ExperimentDefinition,
    lock_payload: dict,
    logistic_final_predictions: list[dict],
) -> tuple[dict, list[dict]]:
    depth_grid(definition)
    engine = lock_payload["engine"]
    if content_hash(engine) != lock_payload["engine_sha256"]:
        raise ValueError("Locked CatBoost evaluation metadata has changed")
    if (
        engine["version"] != EVALUATION_VERSION
        or engine["state"] != "models_locked_before_reused_final"
        or engine["final_holdout_evaluated"]
        or engine["config_sha256"] != configuration_hash(definition)
        or engine["promotion_status"] != "withheld_reused_holdout"
    ):
        raise ValueError("Reused-holdout evaluation requires matching locked exploratory models")
    pools = validate_memberships(memberships, final=True)
    saved, predictions_hash = validate_saved_predictions(
        logistic_final_predictions, pools, definition, final=True
    )
    specifications = scenario_definitions(definition)
    if set(locked_models) != set(specifications) or set(engine["scenarios"]) != set(specifications):
        raise ValueError("Every predeclared CatBoost scenario must be locked before final scoring")
    for scenario, specification in specifications.items():
        locked = engine["scenarios"][scenario]
        if locked["pool"] != specification["pool"] or locked["predictors"] != list(
            specification["predictors"]
        ):
            raise ValueError("Locked CatBoost scenario differs from the predeclared definition")
        if content_hash(describe_catboost(locked_models[scenario])) != locked["description_sha256"]:
            raise ValueError("CatBoost fitted model changed after development lock")
        rows = pools[specification["pool"]]
        if set(locked["training_ids"]) & {row["building_source_id"] for row in rows}:
            raise ValueError("Reused final evaluation overlaps locked training buildings")
        counts = _counts(rows)
        if (
            counts["positives"] < definition.support.minimum_evaluation_positives
            or counts["background"] < definition.support.minimum_background
        ):
            raise ValueError(f"Insufficient reused final-holdout support: {scenario}")
    report = {
        "version": EVALUATION_VERSION,
        "phase": "exploratory_reused_final",
        "scenarios": {},
        "final_holdout_evaluated": True,
        "final_holdout_previously_used": True,
        "logistic_retrained": False,
        "promotion_gate": withheld_promotion(),
        "logistic_predictions_sha256": predictions_hash,
    }
    predictions = []
    for scenario, specification in specifications.items():
        rows = pools[specification["pool"]]
        candidate_metrics, scores = _evaluate(
            locked_models[scenario], final_feature_rows_by_id, rows
        )
        metrics = {"candidate": candidate_metrics, **saved_metrics(saved, scenario, 3, rows)}
        report["scenarios"][scenario] = {
            "support": _counts(rows),
            "metrics": metrics,
            "evaluation_ids_sha256": content_hash([row["building_source_id"] for row in rows]),
            "paired_gains": {
                name: {
                    metric: metrics["candidate"][metric] - metrics[name][metric]
                    for metric in METRICS
                }
                for name in ("logistic", *REFERENCE_NAMES)
            },
        }
        predictions.extend(
            {
                "scenario": scenario,
                "pool": specification["pool"],
                "model": "catboost",
                "fold": 3,
                "building_source_id": row["building_source_id"],
                "label": row["label"],
                "decision_score": score,
            }
            for row, score in zip(rows, scores, strict=True)
        )
    dev_gate = development_gate(engine["primary_development_summary"], definition)
    primary = report["scenarios"]["primary"]["metrics"]
    checks = [
        {
            "baseline": name,
            "metric": metric,
            "gain": primary["candidate"][metric] - primary[name][metric],
            "passed": primary["candidate"][metric] > primary[name][metric] + 1e-12,
        }
        for name in REFERENCE_NAMES
        for metric in METRICS
    ]
    report["reference_gate_diagnostic"] = {
        "development": dev_gate,
        "final_checks": checks,
        "thresholds_met": dev_gate["status"] == "passed_development_only"
        and all(check["passed"] for check in checks),
        "eligible_for_promotion": False,
    }
    return report, predictions
