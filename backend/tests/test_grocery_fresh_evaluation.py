from copy import deepcopy

import pytest

import app.core.grocery_baseline as baseline
import app.core.grocery_catboost as catboost
import app.core.grocery_fresh_evaluation as evaluation
from app.core.data_contract import PREDICTORS
from app.core.experiment import load_experiment
from app.core.grocery_features import EXTRACTION_VERSION, FEATURE_IDS, SOURCE_TABLES


def protocol():
    return {
        "version": "prospective-radom-fixture-v1",
        "support": {"minimum_positives": 30, "minimum_background": 30},
        "metrics": {"minimum_ap_gain": 0.02, "minimum_precision_at_20_gain": 0.05},
    }


def fresh_rows(positives=32, background=32):
    features, memberships = {}, []
    for prefix, count, label in (
        ("p", positives, "observed_presence"),
        ("n", background, "unlabelled_background"),
    ):
        for index in range(count):
            identifier = f"external-{prefix}-{index:03}"
            values = {
                name: "m" if PREDICTORS[name].unit == "category" else 1.0 for name in FEATURE_IDS
            }
            features[identifier] = {
                "building_source_id": identifier,
                "extraction_version": EXTRACTION_VERSION,
                "values": values,
                "observations": {
                    name: {
                        "unit": PREDICTORS[name].unit,
                        "status": "available",
                        "coverage_fraction": 1.0,
                        "reason": None,
                    }
                    for name in FEATURE_IDS
                },
                "quality": {
                    "missingness_by_predictor": dict.fromkeys(FEATURE_IDS, False),
                    "source_available_by_dataset": dict.fromkeys(SOURCE_TABLES, True),
                    "source_coverage_fraction_by_radius": {
                        source: {str(radius): 1.0 for radius in (300, 500, 1000)}
                        for source in SOURCE_TABLES
                    },
                },
            }
            memberships.append({"building_source_id": identifier, "label": label})
    return features, memberships


@pytest.fixture
def scoring_only(monkeypatch):
    definition = load_experiment()
    models = {
        name: {
            "name": name,
            "predictors": list(definition.primary_predictors)
            if name in {"catboost", "logistic"}
            else list(definition.baseline_predictors[name]),
        }
        for name in evaluation.MODEL_NAMES
    }
    calls = []

    def score(model, rows):
        calls.append((model["name"], [row["building_source_id"] for row in rows]))
        return [
            float("-p-" in row["building_source_id"]) if model["name"] == "catboost" else 0.0
            for row in rows
        ]

    monkeypatch.setattr(evaluation, "score_catboost", score)
    monkeypatch.setattr(evaluation, "score_baseline", score)
    monkeypatch.setattr(evaluation, "describe_catboost", deepcopy)
    monkeypatch.setattr(evaluation, "describe_baseline", deepcopy)
    monkeypatch.setattr(baseline, "fit_baseline", lambda *args, **kwargs: pytest.fail("Fitting"))
    monkeypatch.setattr(catboost, "fit_catboost", lambda *args, **kwargs: pytest.fail("Fitting"))
    return models, calls


def test_whole_cohort_scoring_can_pass_numeric_checks_but_never_promote(scoring_only):
    features, memberships = fresh_rows()
    models, calls = scoring_only
    report, predictions = evaluation.evaluate_fresh(features, memberships, models, protocol())
    assert report["status"] == "evaluated"
    assert report["protocol_canonical_sha256"] == evaluation.content_hash(protocol())
    assert "protocol_sha256" not in report
    assert report["numeric_gate"]["passed"] is True
    assert report["numeric_gate"]["recall_used_for_gate"] is False
    assert len(report["numeric_gate"]["checks"]) == 4
    assert {check["baseline"] for check in report["numeric_gate"]["checks"]} == {
        "population_only",
        "access_only",
    }
    assert report["metrics"]["catboost"]["known_presence_precision_at_20"] == 1
    assert report["metrics"]["catboost"]["known_presence_recall_at_20"] == 20 / 32
    assert report["paired_gains"]["logistic"]["average_precision"] == 0.5
    assert report["promotion_gate"]["status"] == "withheld_pending_independent_review"
    assert report["promotion_gate"]["eligible_for_promotion"] is False
    assert report["promotion_gate"]["website_integration"] is False
    assert report["original_v1_numeric_gate"] == "failed"
    assert report["original_v1_outcomes_rewritten"] is False
    assert report["background_subsampling_performed"] is False
    assert report["models_refitted"] is report["preprocessing_refitted"] is False
    assert report["hyperparameters_selected"] is False
    assert report["support"] == {"rows": 64, "positives": 32, "background": 32}
    assert len(predictions) == 4 * 64
    assert len(calls) == 4
    assert all(ids == sorted(features) for _, ids in calls)


def test_fresh_evaluation_is_input_order_invariant(scoring_only):
    features, memberships = fresh_rows()
    models, _ = scoring_only
    first = evaluation.evaluate_fresh(features, memberships, models, protocol())
    second = evaluation.evaluate_fresh(
        dict(reversed(list(features.items()))), list(reversed(memberships)), models, protocol()
    )
    assert first == second


@pytest.mark.parametrize("positives,background", [(29, 30), (30, 29), (0, 60), (60, 0), (0, 0)])
def test_insufficient_cohort_has_no_metrics_or_scoring(scoring_only, positives, background):
    features, memberships = fresh_rows(positives, background)
    models, calls = scoring_only
    report, predictions = evaluation.evaluate_fresh(features, memberships, models, protocol())
    assert report["status"] == "insufficient_evidence"
    assert report["metrics"] is None
    assert report["numeric_gate"]["status"] == "unevaluated"
    assert report["cohort_changed"] is False
    assert predictions == calls == []


@pytest.mark.parametrize(
    "damage",
    [
        "duplicate",
        "bad_label",
        "missing_feature",
        "extra_feature",
        "identity",
        "version",
        "missing_predictor",
        "missing_observation",
        "missingness",
        "wrong_model_role",
        "missing_model",
    ],
)
def test_invalid_inputs_fail_before_any_scoring(scoring_only, damage):
    features, memberships = fresh_rows()
    models, calls = scoring_only
    first = memberships[0]["building_source_id"]
    if damage == "duplicate":
        memberships.append(deepcopy(memberships[0]))
    elif damage == "bad_label":
        memberships[0]["label"] = "failed_business"
    elif damage == "missing_feature":
        del features[first]
    elif damage == "extra_feature":
        features["unexpected"] = deepcopy(features[first])
    elif damage == "identity":
        features[first]["building_source_id"] = "other"
    elif damage == "version":
        features[first]["extraction_version"] = "new-unapproved-features"
    elif damage == "missing_predictor":
        del features[first]["values"][FEATURE_IDS[0]]
    elif damage == "missing_observation":
        del features[first]["observations"][FEATURE_IDS[0]]
    elif damage == "missingness":
        features[first]["quality"]["missingness_by_predictor"][FEATURE_IDS[0]] = True
    elif damage == "wrong_model_role":
        models["logistic"]["predictors"] = models["population_only"]["predictors"]
    else:
        del models["access_only"]
    with pytest.raises(ValueError):
        evaluation.evaluate_fresh(features, memberships, models, protocol())
    assert calls == []


@pytest.mark.parametrize("damage", ["support", "threshold", "version"])
def test_mutated_protocol_is_rejected_before_scoring(scoring_only, damage):
    features, memberships = fresh_rows()
    models, calls = scoring_only
    definition = protocol()
    if damage == "support":
        definition["support"]["minimum_positives"] = 5
    elif damage == "threshold":
        definition["metrics"]["minimum_ap_gain"] = 0.0
    else:
        definition["version"] = ""
    with pytest.raises(ValueError, match="protocol|frozen"):
        evaluation.evaluate_fresh(features, memberships, models, definition)
    assert calls == []


@pytest.mark.parametrize("scores", [[float("nan")] * 64, [False] * 64, [0.0] * 63])
def test_invalid_scores_never_produce_a_completed_report(scoring_only, monkeypatch, scores):
    features, memberships = fresh_rows()
    models, _ = scoring_only
    monkeypatch.setattr(evaluation, "score_catboost", lambda *args: scores)
    with pytest.raises(ValueError, match="finite and aligned"):
        evaluation.evaluate_fresh(features, memberships, models, protocol())


def test_fractional_ties_preserve_known_hits_without_id_tiebreaking(scoring_only, monkeypatch):
    features, memberships = fresh_rows()
    models, _ = scoring_only
    monkeypatch.setattr(evaluation, "score_catboost", lambda model, rows: [0.0] * len(rows))
    report, _ = evaluation.evaluate_fresh(features, memberships, models, protocol())
    metrics = report["metrics"]["catboost"]
    assert metrics["known_presence_precision_at_20"] == 0.5
    assert metrics["known_presence_recall_at_20"] == 10 / 32
    assert metrics["cutoff_tie"]["selected_fraction"] == 20 / 64
    assert metrics["tie_policy"] == "Grouped AP; fractional allocation of top-k boundary ties"
    assert report["numeric_gate"]["passed"] is False
    assert report["promotion_gate"]["eligible_for_promotion"] is False


def historical_reports(catboost_format=True):
    def metrics(positives, rows, hits, ap):
        return {
            "positives": positives,
            "rows": rows,
            "effective_k": 20,
            "requested_k": 20,
            "average_precision": ap,
            "known_presence_precision_at_20": hits / 20,
            "known_presence_recall_at_20": hits / positives,
        }

    folds = []
    for fold, (positive, rows, hits, population, access) in enumerate(
        (
            (175, 1380, 12, 6, 3),
            (154, 2782, 13, 10, 9),
            (157, 1407, 18, 5, 8),
        )
    ):
        folds.append(
            {
                "fold": fold,
                "metrics": {
                    "candidate": metrics(positive, rows, hits, 0.6),
                    "population_only": metrics(positive, rows, population, 0.2),
                    "access_only": metrics(positive, rows, access, 0.3),
                    "prevalence_reference": metrics(
                        positive, rows, 20 * positive / rows, positive / rows
                    ),
                    "random_reference": metrics(
                        positive, rows, 20 * positive / rows, positive / rows
                    ),
                },
            }
        )
    gate = {
        "status": "failed_development",
        "checks": [
            {"baseline": name, "metric": "known_presence_recall_at_20", "passed": False}
            for name in ("population_only", "access_only")
        ],
    }
    final_gate = {"final_checks": [{"passed": True}], "thresholds_met": False}
    final = {"scenarios": {"primary": {"metrics": deepcopy(folds[0]["metrics"])}}}
    development = {"scenarios": {"primary": {"folds": folds}}}
    if catboost_format:
        development["reference_gate_diagnostic"] = gate
        final["reference_gate_diagnostic"] = final_gate
        final["promotion_gate"] = {"status": "withheld_reused_holdout"}
    else:
        development["development_gate"] = gate
        final["promotion_gate"] = {**final_gate, "status": "failed"}
    return development, final


@pytest.mark.parametrize("catboost_format", [True, False])
def test_history_preserves_failures_and_diagnoses_attainable_coupled_gate(catboost_format):
    development, final = historical_reports(catboost_format)
    original = deepcopy((development, final))
    report = evaluation.diagnose_previous(development, final, load_experiment())
    assert (development, final) == original
    assert report["original_outcomes_rewritten"] is False
    assert len(report["development_failures"]) == 2
    assert report["final_failures"] == []
    assert report["precision_recall_coupling"]["independent_metrics"] is False
    assert report["maximum_mean_recall_at_20"] == pytest.approx(0.1238481263958971)
    population = report["reference_diagnostics"]["population_only"]
    access = report["reference_diagnostics"]["access_only"]
    assert population["maximum_mean_recall_gain"] == pytest.approx(0.0801588220696501)
    assert access["maximum_mean_recall_gain"] == pytest.approx(0.08166818319684561)
    assert population["observed_mean_recall_gain"] == pytest.approx(0.045522927178978136)
    assert population["threshold_arithmetically_attainable"] is True
    assert population["increment_diagnostic"]["minimum_additional_full_known_hits"] == 3
    assert access["increment_diagnostic"]["minimum_additional_full_known_hits"] == 2
    assert report["combined_recall_increment_diagnostic"]["minimum_additional_full_known_hits"] == 3


def test_historical_impossible_threshold_is_reported_without_reinterpreting_failure():
    development, final = historical_reports()
    definition = load_experiment()
    definition.promotion_gate["minimum_mean_recall_gain"] = 0.2
    report = evaluation.diagnose_previous(development, final, definition)
    assert (
        report["reference_diagnostics"]["population_only"]["threshold_arithmetically_attainable"]
        is False
    )
    assert (
        report["combined_recall_increment_diagnostic"]["minimum_additional_full_known_hits"] is None
    )
    assert report["original_development_gate"]["status"] == "failed_development"


@pytest.mark.parametrize("damage", ["missing_fold", "count_mismatch", "recall_mismatch"])
def test_inconsistent_historical_metrics_are_rejected(damage):
    development, final = historical_reports()
    folds = development["scenarios"]["primary"]["folds"]
    if damage == "missing_fold":
        folds.pop()
    elif damage == "count_mismatch":
        item = folds[0]["metrics"]["population_only"]
        item["rows"] += 1
    else:
        folds[0]["metrics"]["candidate"]["known_presence_recall_at_20"] += 0.1
    with pytest.raises(ValueError, match="folds|counts|reconstruct"):
        evaluation.diagnose_previous(development, final, load_experiment())
