import math
from copy import deepcopy
from itertools import product
from numbers import Real

from app.core.experiment import configuration_hash, content_hash, load_experiment
from app.core.grocery_baseline import _raw_matrices, describe_baseline, score_baseline
from app.core.grocery_catboost import describe_catboost, score_catboost
from app.core.grocery_evaluation import (
    METRICS,
    REFERENCE_NAMES,
    random_reference_metrics,
    ranking_metrics,
)
from app.core.grocery_features import EXTRACTION_VERSION, FEATURE_IDS

EVALUATION_VERSION = "grocery-fresh-evaluation-v1"
MODEL_NAMES = ("catboost", "logistic", "population_only", "access_only")
LABELS = {"observed_presence": 1, "unlabelled_background": 0}


def _reported_metrics(metrics: dict) -> dict:
    positives, rows, k = metrics["positives"], metrics["rows"], metrics["effective_k"]
    if (
        any(type(value) is not int for value in (positives, rows, k))
        or not 0 < positives <= rows
        or k != min(20, rows)
        or metrics.get("requested_k") != 20
    ):
        raise ValueError("Historical metrics require consistent positive counts and top-20 budget")
    for name in METRICS:
        value = metrics[name]
        if isinstance(value, bool) or not isinstance(value, Real) or not 0 <= value <= 1:
            raise ValueError("Historical metrics must be finite values between zero and one")
    precision, recall = (metrics[name] for name in METRICS[1:])
    expected = precision * k / positives
    if not math.isclose(recall, expected, rel_tol=1e-10, abs_tol=1e-12):
        raise ValueError("Historical precision and recall do not reconstruct the same top-20 hits")
    return {
        "positive_count": positives,
        "row_count": rows,
        "effective_k": k,
        "known_hits_at_k": precision * k,
        "precision_at_k": precision,
        "recall_at_k": recall,
        "recall_from_precision": expected,
        "maximum_recall_at_k": min(k, positives) / positives,
        "identity_verified": True,
    }


def _minimum_hit_increments(folds: list[dict], references: tuple[str, ...], definition) -> dict:
    threshold = definition.promotion_gate["minimum_mean_recall_gain"]
    positives = [fold["candidate"]["positive_count"] for fold in folds]
    current = [fold["candidate"]["known_hits_at_k"] for fold in folds]
    capacities = [
        math.floor(min(fold["candidate"]["effective_k"], positive) - hits + 1e-10)
        for fold, positive, hits in zip(folds, positives, current, strict=True)
    ]
    required = [[fold[name]["recall_at_k"] for fold in folds] for name in references]
    best = None
    for increments in product(*(range(capacity + 1) for capacity in capacities)):
        if best is not None and sum(increments) >= sum(best):
            continue
        recalls = [
            (hits + extra) / positive
            for hits, extra, positive in zip(current, increments, positives, strict=True)
        ]
        if all(
            math.fsum(actual - baseline for actual, baseline in zip(recalls, values, strict=True))
            / len(folds)
            + 1e-12
            >= threshold
            and (
                not definition.promotion_gate["require_nonnegative_gain_every_development_fold"]
                or all(
                    actual + 1e-12 >= baseline
                    for actual, baseline in zip(recalls, values, strict=True)
                )
            )
            for values in required
        ):
            best = increments
    return {
        "reference_names": list(references),
        "minimum_additional_full_known_hits": sum(best) if best is not None else None,
        "one_feasible_allocation_by_fold": list(best) if best is not None else None,
        "scope": "Arithmetic recall-gate diagnostic, not a proposed ranking or model adjustment",
        "assumption": "Whole additional known hits with other retrieved hits unchanged",
        "fractional_boundary_ties_can_change_feasible_allocations": True,
        "other_metric_gates_reassessed": False,
    }


def diagnose_previous(development_report: dict, final_report: dict, definition) -> dict:
    primary = development_report["scenarios"]["primary"]
    raw_folds = primary["folds"]
    if [fold["fold"] for fold in raw_folds] != list(definition.split.development_folds):
        raise ValueError("Historical diagnosis requires all original development folds in order")
    development_gate = development_report.get(
        "development_gate", development_report.get("reference_gate_diagnostic")
    )
    final_gate = final_report.get("reference_gate_diagnostic", final_report.get("promotion_gate"))
    if not isinstance(development_gate, dict) or not isinstance(final_gate, dict):
        raise ValueError("Historical reports require their original numeric validation gates")
    folds = []
    for fold in raw_folds:
        metrics = fold["metrics"]
        if {"candidate", *REFERENCE_NAMES} - metrics.keys():
            raise ValueError("Historical diagnosis requires every original reference model")
        checked = {name: _reported_metrics(value) for name, value in metrics.items()}
        counts = {
            (item["positive_count"], item["row_count"], item["effective_k"])
            for item in checked.values()
        }
        if len(counts) != 1:
            raise ValueError("Historical candidate and references must share evaluation counts")
        folds.append(checked)
    final_metrics = {
        name: _reported_metrics(value)
        for name, value in final_report["scenarios"]["primary"]["metrics"].items()
    }
    ceiling = math.fsum(fold["candidate"]["maximum_recall_at_k"] for fold in folds) / len(folds)
    threshold = definition.promotion_gate["minimum_mean_recall_gain"]
    reference_diagnostics = {}
    for name in REFERENCE_NAMES:
        maximum_gain = math.fsum(
            fold["candidate"]["maximum_recall_at_k"] - fold[name]["recall_at_k"] for fold in folds
        ) / len(folds)
        actual_gain = math.fsum(
            fold["candidate"]["recall_at_k"] - fold[name]["recall_at_k"] for fold in folds
        ) / len(folds)
        reference_diagnostics[name] = {
            "observed_mean_recall_gain": actual_gain,
            "maximum_mean_recall_gain": maximum_gain,
            "original_required_mean_recall_gain": threshold,
            "threshold_arithmetically_attainable": maximum_gain + 1e-12 >= threshold,
            "additional_mean_recall_needed": max(0.0, threshold - actual_gain),
            "increment_diagnostic": _minimum_hit_increments(folds, (name,), definition),
        }
    return {
        "version": EVALUATION_VERSION,
        "status": "historical_diagnosis_only",
        "config_sha256": configuration_hash(definition),
        "original_outcomes_rewritten": False,
        "original_development_gate": deepcopy(development_gate),
        "original_final_gate": deepcopy(final_gate),
        "original_promotion_gate": deepcopy(final_report.get("promotion_gate")),
        "development_failures": [
            deepcopy(check) for check in development_gate["checks"] if check["passed"] is False
        ],
        "final_failures": [
            deepcopy(check)
            for check in final_gate.get("final_checks", [])
            if check["passed"] is False
        ],
        "precision_recall_coupling": {
            "identity": "recall_at_k = precision_at_k * effective_k / observed_positive_count",
            "independent_metrics": False,
            "boundary_ties": "The identity also holds for fractional boundary-tie allocation",
            "fold_order": [fold["fold"] for fold in raw_folds],
            "development": folds,
            "exposed_final": final_metrics,
        },
        "maximum_mean_recall_at_20": ceiling,
        "reference_diagnostics": reference_diagnostics,
        "combined_recall_increment_diagnostic": _minimum_hit_increments(
            folds, REFERENCE_NAMES, definition
        ),
        "fresh_evaluation_required": True,
        "interpretation": "Changing metrics cannot retroactively pass the original experiment",
    }


def _validate_protocol(protocol: dict) -> None:
    if not isinstance(protocol.get("version"), str) or not protocol["version"].strip():
        raise ValueError("Fresh evaluation requires a versioned frozen protocol")
    if protocol.get("support") != {"minimum_positives": 30, "minimum_background": 30}:
        raise ValueError("Fresh evaluation requires the frozen support minima")
    if protocol.get("metrics") != {
        "minimum_ap_gain": 0.02,
        "minimum_precision_at_20_gain": 0.05,
    }:
        raise ValueError("Fresh evaluation requires the frozen prospective metric thresholds")


def evaluate_fresh(features: dict, memberships: list[dict], models: dict, protocol: dict) -> tuple:
    _validate_protocol(protocol)
    if set(models) != set(MODEL_NAMES):
        raise ValueError("Fresh evaluation requires exactly the four frozen model roles")
    normalized = []
    seen = set()
    for membership in memberships:
        if not isinstance(membership, dict):
            raise ValueError("Fresh memberships must be records")
        identifier, label = membership.get("building_source_id"), membership.get("label")
        if (
            not isinstance(identifier, str)
            or not identifier.strip()
            or not isinstance(label, str)
            or label not in LABELS
        ):
            raise ValueError("Fresh memberships require stable IDs and positive-unlabelled labels")
        if identifier in seen:
            raise ValueError("Duplicate fresh building membership")
        seen.add(identifier)
        normalized.append({"building_source_id": identifier, "label": label})
    if set(features) != seen:
        raise ValueError("Fresh feature IDs must equal the complete frozen cohort membership")
    normalized.sort(key=lambda row: row["building_source_id"])
    rows = [features[row["building_source_id"]] for row in normalized]
    for identifier, row in zip(
        (item["building_source_id"] for item in normalized), rows, strict=True
    ):
        if (
            not isinstance(row, dict)
            or row.get("building_source_id") != identifier
            or row.get("extraction_version") != EXTRACTION_VERSION
            or set(row.get("values", {})) != set(FEATURE_IDS)
            or set(row.get("observations", {})) != set(FEATURE_IDS)
        ):
            raise ValueError("Fresh feature row identity or shared feature contract differs")
    labels = [LABELS[row["label"]] for row in normalized]
    support = {"rows": len(rows), "positives": sum(labels), "background": len(rows) - sum(labels)}
    sufficient = support["positives"] >= 30 and support["background"] >= 30
    report = {
        "version": EVALUATION_VERSION,
        "protocol_version": protocol["version"],
        "protocol_canonical_sha256": content_hash(protocol),
        "status": "evaluated" if sufficient else "insufficient_evidence",
        "support": support,
        "membership_sha256": content_hash(normalized),
        "feature_rows_sha256": content_hash(rows),
        "cohort_changed": False,
        "background_subsampling_performed": False,
        "models_refitted": False,
        "hyperparameters_selected": False,
        "preprocessing_refitted": False,
        "original_v1_numeric_gate": "failed",
        "original_v1_outcomes_rewritten": False,
        "metrics": None,
        "numeric_gate": {"status": "unevaluated", "passed": False, "checks": []},
        "promotion_gate": {
            "status": "withheld_pending_independent_review",
            "eligible_for_promotion": False,
            "website_integration": False,
        },
        "interpretation": {
            "target": "Observed selected-chain presence against unlabelled background",
            "scores": "Relative ranking scores, not business-success probabilities",
            "metrics": "New prospective criteria do not reinterpret the failed original gate",
            "recall": "Descriptive only; coupled to top-20 precision and positive count",
            "uncertainty": "One external city; no independence or confidence interval claim",
            "freshness": "Acquisition provenance and prior exposure are verified by the caller",
        },
    }
    if not sufficient:
        report["reason"] = (
            "The unchanged cohort requires at least 30 observed and 30 background buildings"
        )
        return report, []
    _raw_matrices(rows, tuple(FEATURE_IDS))
    definition = load_experiment()
    expected_predictors = {
        "catboost": list(FEATURE_IDS),
        "logistic": list(FEATURE_IDS),
        **{name: list(values) for name, values in definition.baseline_predictors.items()},
    }
    descriptions = {
        name: describe_catboost(models[name])
        if name == "catboost"
        else describe_baseline(models[name])
        for name in MODEL_NAMES
    }
    for name in MODEL_NAMES:
        if descriptions[name]["predictors"] != expected_predictors[name]:
            raise ValueError("Fresh evaluation model role has different frozen predictors")
    report["model_description_sha256"] = {
        name: content_hash(value) for name, value in descriptions.items()
    }
    metrics, predictions = {}, []
    for name in MODEL_NAMES:
        scorer = score_catboost if name == "catboost" else score_baseline
        scores = scorer(models[name], rows)
        if len(scores) != len(rows) or any(
            isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value)
            for value in scores
        ):
            raise ValueError("Fresh model scores must be finite and aligned to the whole cohort")
        metrics[name] = ranking_metrics(labels, scores)
        predictions.extend(
            {
                **membership,
                "model": name,
                "decision_score": float(score),
            }
            for membership, score in zip(normalized, scores, strict=True)
        )
    metrics["prevalence_reference"] = ranking_metrics(labels, [0.0] * len(labels))
    metrics["random_reference"] = random_reference_metrics(labels)
    gains = {
        name: {metric: metrics["catboost"][metric] - metrics[name][metric] for metric in METRICS}
        for name in (*MODEL_NAMES[1:], "prevalence_reference", "random_reference")
    }
    checks = [
        {
            "baseline": name,
            "metric": metric,
            "gain": gains[name][metric],
            "required_gain": threshold,
            "passed": gains[name][metric] + 1e-12 >= threshold,
        }
        for name in ("population_only", "access_only")
        for metric, threshold in (
            (METRICS[0], protocol["metrics"]["minimum_ap_gain"]),
            (METRICS[1], protocol["metrics"]["minimum_precision_at_20_gain"]),
        )
    ]
    report["metrics"] = metrics
    report["paired_gains"] = gains
    report["numeric_gate"] = {
        "status": "passed" if all(check["passed"] for check in checks) else "failed",
        "passed": all(check["passed"] for check in checks),
        "checks": checks,
        "recall_used_for_gate": False,
        "logistic_comparison": "Reported without selecting or replacing the locked candidate",
    }
    return report, predictions
