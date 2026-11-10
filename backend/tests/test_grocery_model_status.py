import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.core.experiment import configuration_hash, content_hash, load_experiment
from app.core.grocery_features import EXTRACTION_VERSION, SOURCE_TABLES
from app.core.grocery_model_status import GroceryModelStatus, public_assessment


def write_fixture(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(value, sort_keys=True).encode()
    path.write_bytes(content)
    return hashlib.sha256(content).hexdigest()


@pytest.fixture
def metadata_bundle(tmp_path):
    directory = tmp_path / "model"
    selection_path = tmp_path / "selection.json"
    metadata = {"map_sources": [], "profile_sources": []}
    sources = {}
    for identifier, (table, _) in SOURCE_TABLES.items():
        record = {"id": identifier, "feature_count": 10, "imported_at": "2026-11-01T00:00:00Z"}
        metadata[table].append(record)
        digest = hashlib.sha256(identifier.encode()).hexdigest()
        sources[identifier] = {
            "data_sha256": digest,
            "content_sha256": content_hash({"source_metadata": record, "data_sha256": digest}),
        }
    identity = {
        "version": "warsaw-grocery-catboost-v1",
        "config_sha256": configuration_hash(load_experiment()),
        "feature_identity": {"extraction_version": EXTRACTION_VERSION},
    }
    engine = {
        "config_sha256": identity["config_sha256"],
        "state": "models_locked_before_reused_final",
        "promotion_status": "withheld_reused_holdout",
    }
    lock = {
        "identity": identity,
        "engine": engine,
        "engine_sha256": content_hash(engine),
        "final_holdout_records_read": False,
        "final_holdout_previously_evaluated_by_logistic": True,
        "model_files": {"primary.joblib": "0" * 64},
    }
    lock_hash = write_fixture(directory / "lock.json", lock)
    source_hash = write_fixture(directory / "feature-sources.json", {"sources": sources})
    training = {
        "identity": identity,
        "state": "locked_before_final",
        "artifacts": {
            "lock.json": lock_hash,
            "feature-sources.json": source_hash,
            **lock["model_files"],
        },
    }
    training_hash = write_fixture(directory / "manifest.json", training)
    evaluation = {
        "training_manifest_sha256": training_hash,
        "lock_sha256": lock_hash,
        "phase": "exploratory_reused_final",
        "confirmatory_for_this_lock": False,
        "final_holdout_previously_used": True,
        "models_refitted": False,
        "promotion_gate": {
            "status": "withheld_reused_holdout",
            "eligible_for_promotion": False,
            "fresh_confirmation": False,
        },
        "reference_gate_diagnostic": {"thresholds_met": False},
        "evaluated_at": "2026-11-09T12:00:00Z",
    }
    evaluation_hash = write_fixture(directory / "evaluation" / "evaluation.json", evaluation)
    evaluation_manifest = {
        "training_manifest_sha256": training_hash,
        "state": "evaluated_locked_catboost_exploratory",
        "artifacts": {"evaluation.json": evaluation_hash},
    }
    selection = {
        "version": "grocery-assessment-policy-v1",
        "artifact_directory_name": "model",
        "model_version": identity["version"],
        "training_manifest_sha256": training_hash,
        "evaluation_manifest_sha256": write_fixture(
            directory / "evaluation" / "manifest.json", evaluation_manifest
        ),
        "public_mode": "evidence_only",
        "ranking_enabled": False,
    }
    write_fixture(selection_path, selection)
    return {
        "directory": directory,
        "selection_path": selection_path,
        "selection": selection,
        "evaluation": evaluation,
        "evaluation_manifest": evaluation_manifest,
        "runtime": {
            "source_metadata": metadata,
            "source_context": {"sources": {name: {"available": True} for name in sources}},
        },
        "model": GroceryModelStatus(directory, selection_path),
    }


def republish_evaluation(bundle):
    manifest = bundle["evaluation_manifest"]
    manifest["artifacts"]["evaluation.json"] = write_fixture(
        bundle["directory"] / "evaluation" / "evaluation.json", bundle["evaluation"]
    )
    bundle["selection"]["evaluation_manifest_sha256"] = write_fixture(
        bundle["directory"] / "evaluation" / "manifest.json", manifest
    )
    write_fixture(bundle["selection_path"], bundle["selection"])


def test_verified_metadata_stays_withheld_without_opening_model_binaries(metadata_bundle):
    bundle = metadata_bundle
    assert not list(bundle["directory"].glob("*.joblib"))
    result = bundle["model"].describe(bundle["runtime"])
    assert result["status"] == "validation_withheld"
    assert result["source_status"] == "metadata_match_content_unverified"
    assert result["version"] == "warsaw-grocery-catboost-v1"
    assert result["evaluated_at"] == bundle["evaluation"]["evaluated_at"]
    assert set(result) == {"status", "source_status", "version", "evaluated_at", "reason"}


@pytest.mark.parametrize("missing", ["manifest.json", "lock.json", "evaluation/evaluation.json"])
def test_missing_artifact_stays_unavailable(metadata_bundle, missing):
    bundle = metadata_bundle
    (bundle["directory"] / missing).unlink()
    result = bundle["model"].describe(bundle["runtime"])
    assert result["status"] == "unavailable"
    assert result["version"] is None


@pytest.mark.parametrize(
    "changed",
    [
        "manifest.json",
        "lock.json",
        "feature-sources.json",
        "evaluation/manifest.json",
        "evaluation/evaluation.json",
    ],
)
def test_changed_artifact_cannot_reuse_verified_status(metadata_bundle, changed):
    bundle = metadata_bundle
    assert bundle["model"].describe(bundle["runtime"])["status"] == "validation_withheld"
    path = bundle["directory"] / changed
    path.write_bytes(path.read_bytes() + b" ")
    result = bundle["model"].describe(bundle["runtime"])
    assert result["status"] == "integrity_error"
    assert str(bundle["directory"]) not in json.dumps(result)


@pytest.mark.parametrize("change", ["passed_gate", "fresh_confirmation", "numeric_gate", "refit"])
def test_consistently_rehashed_approval_does_not_enable_recommendations(metadata_bundle, change):
    bundle = metadata_bundle
    evaluation = bundle["evaluation"]
    if change == "passed_gate":
        evaluation["promotion_gate"]["status"] = "approved"
        evaluation["promotion_gate"]["eligible_for_promotion"] = True
    elif change == "fresh_confirmation":
        evaluation["confirmatory_for_this_lock"] = True
        evaluation["promotion_gate"]["fresh_confirmation"] = True
    elif change == "numeric_gate":
        evaluation["reference_gate_diagnostic"]["thresholds_met"] = True
    else:
        evaluation["models_refitted"] = True
    republish_evaluation(bundle)
    result = public_assessment({"versions": {}, "_runtime": bundle["runtime"]}, bundle["model"])
    assert result["model"]["status"] == "integrity_error"
    assert result["status"] == "evidence_only"
    assert result["ranking"]["status"] == "withheld"
    assert result["ranking"]["score"] is result["ranking"]["rank"] is None


def test_changed_sources_are_stale_and_unavailable_sources_are_not_zero(metadata_bundle):
    bundle = metadata_bundle
    runtime = copy.deepcopy(bundle["runtime"])
    runtime["source_metadata"]["map_sources"][0]["feature_count"] += 1
    result = bundle["model"].describe(runtime)
    assert result["status"] == "stale"
    assert result["source_status"] == "changed"
    runtime = copy.deepcopy(bundle["runtime"])
    runtime["source_context"]["sources"]["warsaw-population-2021"]["available"] = False
    result = bundle["model"].describe(runtime)
    assert result["status"] == "stale"
    assert result["source_status"] == "unavailable"


def test_public_response_excludes_internal_model_outputs(metadata_bundle):
    bundle = metadata_bundle
    assessment = {
        "versions": {"features": EXTRACTION_VERSION},
        "_runtime": bundle["runtime"],
        "score": 0.97,
        "rank": 1,
        "shap": [{"contribution": 100}],
        "predictions": [{"score": 0.97}],
        "training_labels": [1],
        "artifact_path": str(bundle["directory"]),
        "ranking": {"status": "available", "score": 0.97, "rank": 1},
    }
    result = public_assessment(assessment, bundle["model"])
    for name in (
        "_runtime",
        "score",
        "rank",
        "shap",
        "predictions",
        "training_labels",
        "artifact_path",
    ):
        assert name not in result
    assert result["ranking"]["score"] is result["ranking"]["rank"] is None
    assert assessment["ranking"]["score"] == 0.97
    assert "_runtime" in assessment


def test_metadata_service_imports_without_model_libraries():
    source = """
import importlib.abc
import sys

class RejectModels(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'sklearn', 'catboost', 'joblib', 'numpy', 'scipy', 'pandas'}:
            raise AssertionError('Optional model dependency imported: ' + fullname)

sys.meta_path.insert(0, RejectModels())
import app.core.grocery_model_status
"""
    result = subprocess.run(
        [sys.executable, "-c", source],
        cwd=Path(__file__).resolve().parents[1],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
