import math
from collections import Counter
from itertools import groupby
from statistics import pstdev

from app.core.experiment import ExperimentDefinition, configuration_hash, content_hash
from app.core.grocery_baseline import describe_baseline, fit_baseline, score_baseline

EVALUATION_VERSION = "grocery-logistic-spatial-v1"
METRICS = ("average_precision", "known_presence_precision_at_20", "known_presence_recall_at_20")
REFERENCE_NAMES = ("population_only", "access_only", "prevalence_reference", "random_reference")


def ranking_metrics(labels: list[int], scores: list[float], k: int = 20) -> dict:
    if not labels or len(labels) != len(scores) or k < 1:
        raise ValueError("Ranking metrics require aligned nonempty labels and scores")
    if any(type(label) is not int or label not in (0, 1) for label in labels):
        raise ValueError("Labels must encode observed presence or unlabelled background as 1 or 0")
    if not all(math.isfinite(float(score)) for score in scores):
        raise ValueError("Ranking scores must be finite")
    positives = sum(labels)
    if positives == 0:
        raise ValueError("Ranking metrics require at least one observed presence")
    groups = [
        (score, [label for _, label in items])
        for score, items in groupby(
            sorted(zip(scores, labels, strict=True), key=lambda pair: -pair[0]),
            key=lambda pair: pair[0],
        )
    ]
    count = len(labels)
    effective_k = min(k, count)
    seen = found = 0
    ap_terms = []
    top_positives = 0.0
    cutoff = None
    for score, group in groups:
        group_positives = sum(group)
        seen += len(group)
        found += group_positives
        ap_terms.append(group_positives / positives * found / seen)
        slots = max(0, min(len(group), effective_k - (seen - len(group))))
        top_positives += group_positives * slots / len(group)
        if slots and seen >= effective_k:
            cutoff = {
                "score": float(score),
                "group_size": len(group),
                "selected_fraction": slots / len(group),
            }
    return {
        "average_precision": math.fsum(ap_terms),
        "known_presence_precision_at_20": top_positives / effective_k,
        "known_presence_recall_at_20": top_positives / positives,
        "rows": count,
        "positives": positives,
        "background": count - positives,
        "requested_k": k,
        "effective_k": effective_k,
        "cutoff_tie": cutoff,
        "score_tie_groups": sum(len(group) > 1 for _, group in groups),
        "rows_in_score_ties": sum(len(group) for _, group in groups if len(group) > 1),
        "tie_policy": "Grouped AP; fractional allocation of top-k boundary ties",
    }


def random_reference_metrics(labels: list[int], k: int = 20) -> dict:
    constant = ranking_metrics(labels, [0.0] * len(labels), k)
    n, positives = len(labels), sum(labels)
    harmonic = math.fsum(1 / rank for rank in range(1, n + 1))
    expected_ap = 1.0 if n == 1 else harmonic / n + (positives - 1) / (n - 1) * (1 - harmonic / n)
    return {
        **{
            key: constant[key]
            for key in (*METRICS, "rows", "positives", "background", "requested_k", "effective_k")
        },
        "average_precision": expected_ap,
        "method": "Analytic expectation over uniformly random distinct rankings",
    }


def scenario_definitions(definition: ExperimentDefinition) -> dict:
    return {
        "primary": {"pool": definition.primary_pool, "predictors": definition.primary_predictors},
        "no_mapped_use": {
            "pool": definition.primary_pool,
            "predictors": tuple(
                name for name in definition.primary_predictors if name != "mapped_building_use"
            ),
        },
        "all_valid": {
            "pool": definition.sensitivity_pool,
            "predictors": definition.primary_predictors,
        },
    }


def validate_memberships(memberships: list[dict], final: bool = False) -> dict[str, list[dict]]:
    pools = {"valid_addressed": [], "all_valid": []}
    seen = set()
    buildings = {}
    for row in memberships:
        if row.get("category") != "grocery":
            continue
        pool, identifier = row.get("pool"), row.get("building_source_id")
        if pool not in pools or not isinstance(identifier, str) or not identifier:
            raise ValueError("Grocery membership requires a declared pool and stable building ID")
        key = (pool, identifier)
        if key in seen:
            raise ValueError("Duplicate building membership within a pool")
        seen.add(key)
        fold, evaluation, training = (
            row.get("fold"),
            row.get("validation_fold"),
            row.get("training_folds"),
        )
        if type(fold) is not int or fold not in range(4) or not isinstance(training, list):
            raise ValueError("Membership contains an invalid spatial assignment")
        if (
            len(set(training)) != len(training)
            or any(type(item) is not int or item not in range(4) for item in training)
            or fold in training
            or evaluation not in (None, fold)
        ):
            raise ValueError("Spatial training and evaluation assignments overlap or are invalid")
        if final != (fold == 3) or (final and (training or evaluation != 3)):
            raise ValueError("Development and final memberships must be loaded in separate phases")
        if not final and evaluation is None and training:
            raise ValueError("Rows excluded by the final spatial buffer cannot enter training")
        if row.get("label") not in ("observed_presence", "unlabelled_background"):
            raise ValueError("Membership labels must preserve positive-unlabelled semantics")
        assignment = (fold, evaluation, tuple(sorted(training)), row["label"])
        if identifier in buildings and buildings[identifier] != assignment:
            raise ValueError("One building cannot have conflicting labels or folds across pools")
        buildings[identifier] = assignment
        pools[pool].append(row)
    if any(not rows for rows in pools.values()):
        raise ValueError("Both frozen grocery pools must be present")
    return {
        pool: sorted(rows, key=lambda row: row["building_source_id"])
        for pool, rows in pools.items()
    }


def _counts(rows: list[dict]) -> dict:
    labels = Counter(row["label"] for row in rows)
    return {
        "rows": len(rows),
        "positives": labels["observed_presence"],
        "background": labels["unlabelled_background"],
    }


def _support(train: list[dict], evaluate: list[dict], definition: ExperimentDefinition) -> dict:
    training, evaluation = _counts(train), _counts(evaluate)
    return {
        "train": training,
        "evaluation": evaluation,
        "sufficient": training["positives"] >= definition.support.minimum_train_positives
        and evaluation["positives"] >= definition.support.minimum_evaluation_positives
        and min(training["background"], evaluation["background"])
        >= definition.support.minimum_background,
    }


def _feature_rows(
    features: dict[str, dict], memberships: list[dict]
) -> tuple[list[dict], list[int]]:
    rows, labels = [], []
    for membership in memberships:
        identifier = membership["building_source_id"]
        if (
            identifier not in features
            or features[identifier].get("building_source_id") != identifier
        ):
            raise ValueError(f"Missing or mismatched frozen feature row: {identifier}")
        rows.append(features[identifier])
        labels.append(int(membership["label"] == "observed_presence"))
    return rows, labels


def _fit(features, train, predictors, c_value, definition):
    rows, labels = _feature_rows(features, train)
    return fit_baseline(
        rows,
        labels,
        tuple(predictors),
        C=c_value,
        max_iter=definition.models["logistic_l2"]["max_iter"],
    )


def _evaluate(model, features, evaluate):
    rows, labels = _feature_rows(features, evaluate)
    scores = score_baseline(model, rows)
    return ranking_metrics(labels, scores), scores


def _distribution(values: list[float]) -> dict:
    return {
        "values": values,
        "mean": math.fsum(values) / len(values),
        "min": min(values),
        "max": max(values),
        "descriptive_population_std": pstdev(values),
        "uncertainty_scope": "Descriptive regional heterogeneity; no independence or CI claim",
    }


def summarize_folds(folds: list[dict]) -> dict:
    if [row["fold"] for row in folds] != [0, 1, 2]:
        raise ValueError("All three geographic development folds are required")
    metrics = {
        name: {
            metric: _distribution([row["metrics"][name][metric] for row in folds])
            for metric in METRICS
        }
        for name in ("candidate", *REFERENCE_NAMES)
    }
    gains = {
        name: {
            metric: _distribution(
                [
                    row["metrics"]["candidate"][metric] - row["metrics"][name][metric]
                    for row in folds
                ]
            )
            for metric in METRICS
        }
        for name in REFERENCE_NAMES
    }
    return {
        "metrics": metrics,
        "paired_region_gains": gains,
        "fold_order": [0, 1, 2],
        "iid_bootstrap_performed": False,
    }


def development_gate(primary_summary: dict, definition: ExperimentDefinition) -> dict:
    if primary_summary.get("fold_order") != [0, 1, 2]:
        raise ValueError("Promotion gate cannot omit a development region")
    thresholds = dict(
        zip(
            METRICS,
            (
                definition.promotion_gate["minimum_mean_ap_gain"],
                definition.promotion_gate["minimum_mean_precision_gain"],
                definition.promotion_gate["minimum_mean_recall_gain"],
            ),
            strict=True,
        )
    )
    checks = []
    for baseline in REFERENCE_NAMES:
        for metric, threshold in thresholds.items():
            gains = primary_summary["paired_region_gains"][baseline][metric]
            if len(gains["values"]) != 3 or not all(
                math.isfinite(value) for value in gains["values"]
            ):
                raise ValueError("Promotion gains must include three finite paired regional values")
            mean_gain, minimum_gain = math.fsum(gains["values"]) / 3, min(gains["values"])
            checks.append(
                {
                    "baseline": baseline,
                    "metric": metric,
                    "mean_gain": mean_gain,
                    "minimum_region_gain": minimum_gain,
                    "required_mean_gain": threshold,
                    "passed": mean_gain + 1e-12 >= threshold
                    and (
                        minimum_gain >= -1e-12
                        or not definition.promotion_gate[
                            "require_nonnegative_gain_every_development_fold"
                        ]
                    ),
                }
            )
    return {
        "status": "passed_development_only"
        if all(check["passed"] for check in checks)
        else "failed_development",
        "checks": checks,
        "final_status": "unevaluated",
    }


def _choose_c(features, outer_train, outer_fold, predictors, definition):
    inner = []
    for fold in definition.split.development_folds:
        if fold == outer_fold:
            continue
        train = [row for row in outer_train if fold in row["training_folds"]]
        evaluate = [row for row in outer_train if row["validation_fold"] == fold]
        inner.append((fold, train, evaluate, _support(train, evaluate, definition)))
    counts = [{"fold": fold, **support} for fold, _, _, support in inner]
    if not all(item[3]["sufficient"] for item in inner):
        return 1.0, {
            "status": "unavailable_fixed_c_1",
            "inner_folds": counts,
            "reason": "An inner geographic fold lacks support; no inner folds were dropped",
            "trials": [],
        }
    trials = []
    for c_value in sorted(definition.models["logistic_l2"]["C"]):
        scores = []
        for _, train, evaluate, _ in inner:
            model = _fit(features, train, predictors, c_value, definition)
            scores.append(_evaluate(model, features, evaluate)[0]["average_precision"])
        trials.append(
            {
                "C": c_value,
                "inner_average_precision": scores,
                "mean_average_precision": math.fsum(scores) / len(scores),
            }
        )
    selected = max(trials, key=lambda trial: (trial["mean_average_precision"], -trial["C"]))
    return selected["C"], {
        "status": "nested_geographic_selection",
        "inner_folds": counts,
        "trials": trials,
        "selection_metric": "mean_inner_average_precision",
        "tie_break": "smaller_C",
    }


def fit_and_evaluate_development(
    feature_rows_by_id: dict[str, dict], memberships: list[dict], definition: ExperimentDefinition
) -> tuple[dict, dict, dict]:
    pools = validate_memberships(memberships)
    report = {
        "version": EVALUATION_VERSION,
        "phase": "development",
        "scenarios": {},
        "final_holdout_evaluated": False,
        "catboost_trained": False,
        "prediction_rows": [],
    }
    models, locked_scenarios = {}, {}
    used_ids = set()
    for pool, rows in pools.items():
        for fold in definition.split.development_folds:
            train = [row for row in rows if fold in row["training_folds"]]
            evaluate = [row for row in rows if row["validation_fold"] == fold]
            if not _support(train, evaluate, definition)["sufficient"]:
                raise ValueError(f"Insufficient outer-fold support: {pool}, fold {fold}")
        final_train = [row for row in rows if 3 in row["training_folds"]]
        counts = _counts(final_train)
        if (
            counts["positives"] < definition.support.minimum_train_positives
            or counts["background"] < definition.support.minimum_background
        ):
            raise ValueError(f"Insufficient final-training development support: {pool}")
    for scenario, specification in scenario_definitions(definition).items():
        rows, predictors = pools[specification["pool"]], specification["predictors"]
        folds, fixed_c_results = [], {c: [] for c in sorted(definition.models["logistic_l2"]["C"])}
        for fold in definition.split.development_folds:
            train = [row for row in rows if fold in row["training_folds"]]
            evaluate = [row for row in rows if row["validation_fold"] == fold]
            used_ids.update(row["building_source_id"] for row in (*train, *evaluate))
            selected_c, tuning = _choose_c(feature_rows_by_id, train, fold, predictors, definition)
            candidate = _fit(feature_rows_by_id, train, predictors, selected_c, definition)
            candidate_metrics, candidate_scores = _evaluate(candidate, feature_rows_by_id, evaluate)
            metrics = {"candidate": candidate_metrics}
            prediction_scores = {"candidate": candidate_scores}
            for c_value in fixed_c_results:
                if c_value == selected_c:
                    result = candidate_metrics
                else:
                    fixed = _fit(feature_rows_by_id, train, predictors, c_value, definition)
                    result = _evaluate(fixed, feature_rows_by_id, evaluate)[0]
                fixed_c_results[c_value].append(result["average_precision"])
            for name, baseline_predictors in definition.baseline_predictors.items():
                baseline = _fit(feature_rows_by_id, train, baseline_predictors, 1.0, definition)
                metrics[name], prediction_scores[name] = _evaluate(
                    baseline, feature_rows_by_id, evaluate
                )
            for name, scores in prediction_scores.items():
                report["prediction_rows"].extend(
                    {
                        "scenario": scenario,
                        "pool": specification["pool"],
                        "model": name,
                        "fold": fold,
                        "building_source_id": row["building_source_id"],
                        "label": row["label"],
                        "decision_score": score,
                    }
                    for row, score in zip(evaluate, scores, strict=True)
                )
            _, labels = _feature_rows(feature_rows_by_id, evaluate)
            metrics["prevalence_reference"] = ranking_metrics(labels, [0.0] * len(labels))
            metrics["random_reference"] = random_reference_metrics(labels)
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
                    "selected_C": selected_c,
                    "tuning": tuning,
                    "metrics": metrics,
                }
            )
        final_c = max(fixed_c_results, key=lambda c: (math.fsum(fixed_c_results[c]) / 3, -c))
        final_train = [row for row in rows if 3 in row["training_folds"]]
        used_ids.update(row["building_source_id"] for row in final_train)
        scenario_models = {
            "candidate": _fit(feature_rows_by_id, final_train, predictors, final_c, definition)
        }
        for name, baseline_predictors in definition.baseline_predictors.items():
            scenario_models[name] = _fit(
                feature_rows_by_id, final_train, baseline_predictors, 1.0, definition
            )
        descriptions = {name: describe_baseline(model) for name, model in scenario_models.items()}
        models[scenario] = scenario_models
        summary = summarize_folds(folds)
        report["scenarios"][scenario] = {
            "pool": specification["pool"],
            "predictors": list(predictors),
            "folds": folds,
            "summary": summary,
            "final_training_support": _counts(final_train),
            "final_C": final_c,
            "final_C_selection": {
                "method": "fixed_C_development_mean_AP",
                "folds": [0, 1, 2],
                "tie_break": "smaller_C",
                "trials": [
                    {
                        "C": c,
                        "average_precision_by_fold": scores,
                        "mean_average_precision": math.fsum(scores) / 3,
                    }
                    for c, scores in fixed_c_results.items()
                ],
            },
            "final_models": descriptions,
        }
        locked_scenarios[scenario] = {
            "pool": specification["pool"],
            "predictors": list(predictors),
            "final_C": final_c,
            "training_ids": [row["building_source_id"] for row in final_train],
            "models": {
                name: {"description_sha256": content_hash(description), "description": description}
                for name, description in descriptions.items()
            },
        }
    report["development_gate"] = development_gate(
        report["scenarios"]["primary"]["summary"], definition
    )
    engine_lock = {
        "version": EVALUATION_VERSION,
        "state": "models_locked_before_final",
        "config_sha256": configuration_hash(definition),
        "scenarios": locked_scenarios,
        "development_memberships_sha256": content_hash(pools),
        "development_features_sha256": content_hash(
            [feature_rows_by_id[key] for key in sorted(used_ids)]
        ),
        "primary_development_summary": report["scenarios"]["primary"]["summary"],
        "final_holdout_evaluated": False,
    }
    return report, models, {"engine": engine_lock, "engine_sha256": content_hash(engine_lock)}


def evaluate_final(
    locked_models: dict,
    final_feature_rows_by_id: dict[str, dict],
    memberships: list[dict],
    definition: ExperimentDefinition,
    lock_payload: dict,
) -> tuple[dict, list[dict]]:
    engine_lock = lock_payload["engine"]
    if content_hash(engine_lock) != lock_payload["engine_sha256"]:
        raise ValueError("Locked evaluation metadata has changed")
    if (
        engine_lock["state"] != "models_locked_before_final"
        or engine_lock["final_holdout_evaluated"]
        or engine_lock["config_sha256"] != configuration_hash(definition)
    ):
        raise ValueError("Final evaluation requires matching unevaluated locked models")
    pools = validate_memberships(memberships, final=True)
    specifications = scenario_definitions(definition)
    if set(locked_models) != set(specifications) or set(engine_lock["scenarios"]) != set(
        specifications
    ):
        raise ValueError("Every predeclared scenario must be locked before final evaluation")
    for scenario, specification in specifications.items():
        locked = engine_lock["scenarios"][scenario]
        if locked["pool"] != specification["pool"] or locked["predictors"] != list(
            specification["predictors"]
        ):
            raise ValueError(
                "Locked scenario differs from the predeclared predictor/pool definition"
            )
        if set(locked_models[scenario]) != {"candidate", "population_only", "access_only"}:
            raise ValueError("Candidate and both baselines must be locked together")
        for name, model in locked_models[scenario].items():
            if (
                content_hash(describe_baseline(model))
                != locked["models"][name]["description_sha256"]
            ):
                raise ValueError("Fitted model changed after development lock")
        rows = pools[specification["pool"]]
        if set(locked["training_ids"]) & {row["building_source_id"] for row in rows}:
            raise ValueError("Final evaluation overlaps locked training buildings")
        counts = _counts(rows)
        if (
            counts["positives"] < definition.support.minimum_evaluation_positives
            or counts["background"] < definition.support.minimum_background
        ):
            raise ValueError(f"Insufficient final-holdout support: {scenario}")
    report = {
        "version": EVALUATION_VERSION,
        "phase": "sealed_final",
        "scenarios": {},
        "final_holdout_evaluated": True,
        "sensitivity_models_can_replace_primary": False,
    }
    predictions = []
    for scenario, specification in specifications.items():
        memberships_for_pool = pools[specification["pool"]]
        features, labels = _feature_rows(final_feature_rows_by_id, memberships_for_pool)
        metrics = {}
        for name, model in locked_models[scenario].items():
            scores = score_baseline(model, features)
            metrics[name] = ranking_metrics(labels, scores)
            predictions.extend(
                {
                    "scenario": scenario,
                    "pool": specification["pool"],
                    "model": name,
                    "fold": 3,
                    "building_source_id": row["building_source_id"],
                    "label": row["label"],
                    "decision_score": score,
                }
                for row, score in zip(memberships_for_pool, scores, strict=True)
            )
        metrics["prevalence_reference"] = ranking_metrics(labels, [0.0] * len(labels))
        metrics["random_reference"] = random_reference_metrics(labels)
        report["scenarios"][scenario] = {
            "support": _counts(memberships_for_pool),
            "metrics": metrics,
            "evaluation_ids_sha256": content_hash(
                [row["building_source_id"] for row in memberships_for_pool]
            ),
        }
    dev_gate = development_gate(engine_lock["primary_development_summary"], definition)
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
    passed = dev_gate["status"] == "passed_development_only" and all(
        check["passed"] for check in checks
    )
    report["promotion_gate"] = {
        "status": "passed" if passed else "failed",
        "primary_scenario_only": True,
        "development": dev_gate,
        "final_checks": checks,
        "interpretation": definition.promotion_gate["interpretation"],
    }
    return report, predictions
