import json

import pytest

from app.core.data_contract import data_contract
from app.core.experiment import canonical_bytes, content_hash
from app.core.grocery_exposure import (
    FEATURE_FILES,
    READINESS_FILES,
    SCENARIOS,
    _model_files,
    file_sha256,
    inventory_exposure,
    record_identifiers,
)


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_bytes(value))


def records(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(canonical_bytes(row) + b"\n" for row in rows))


def hashes(directory, names):
    return {name: file_sha256(directory / name) for name in sorted(names)}


@pytest.fixture
def artifacts(tmp_path):
    experiment = {"version": "fixture"}
    config = content_hash({"experiment": experiment, "data_contract": data_contract()})
    development, final = [], []
    for identifier, fold, label in (
        ("A", 0, "observed_presence"),
        ("B", 1, "unlabelled_background"),
        ("C", 3, "observed_presence"),
        ("D", 3, "unlabelled_background"),
        ("E", 0, "unlabelled_background"),
    ):
        for pool in ("valid_addressed", "all_valid"):
            row = {
                "building_source_id": identifier,
                "category": "grocery",
                "pool": pool,
                "label": label,
                "fold": fold,
                "validation_fold": None if identifier == "E" else fold,
                "training_folds": [3, 1 - fold] if identifier in {"A", "B"} else [],
            }
            (final if fold == 3 else development).append(row)
    readiness = tmp_path / "readiness"
    records(readiness / "candidates.jsonl", [{"building_source_id": "geometry-only"}])
    records(
        readiness / "memberships.jsonl",
        development
        + final
        + [
            {
                "building_source_id": "F",
                "category": "pizza",
                "pool": "all_valid",
                "label": "observed_presence",
            }
        ],
    )
    dump(
        readiness / "readiness.json",
        {
            "config_sha256": config,
            "dataset_sha256": "a" * 64,
            "experiment": experiment,
            "candidate_sha256": file_sha256(readiness / "candidates.jsonl"),
            "memberships_sha256": file_sha256(readiness / "memberships.jsonl"),
        },
    )
    ready_manifest = {
        "config_sha256": config,
        "dataset_sha256": "a" * 64,
        "artifacts": hashes(readiness, READINESS_FILES),
    }
    dump(readiness / "manifest.json", ready_manifest)
    features = tmp_path / "features"
    for partition, members in (("development", development), ("final-holdout", final)):
        records(features / f"memberships-{partition}.jsonl", members)
        records(
            features / f"features-{partition}.jsonl",
            [
                {"building_source_id": key, "values": {"private": "never interpreted"}}
                for key in sorted({row["building_source_id"] for row in members})
            ],
        )
    for filename in ("quality.json", "sources.json"):
        dump(features / filename, {})
    feature_identity = {
        "config_sha256": config,
        "dataset_sha256": "a" * 64,
        "memberships_sha256": ready_manifest["artifacts"]["memberships.jsonl"],
    }
    feature_manifest = {
        "identity": feature_identity,
        "readiness_manifest": ready_manifest,
        "experiment": experiment,
        "artifacts": hashes(features, FEATURE_FILES),
    }
    dump(features / "manifest.json", feature_manifest)
    models = {}
    for family in ("logistic", "catboost"):
        directory = tmp_path / family
        directory.mkdir()
        binaries, names = _model_files(family)
        for name in binaries:
            (directory / name).write_bytes(b"not deserialized")
        for filename in (
            "development-evaluation.json",
            "model-details.json",
            "feature-sources.json",
        ):
            dump(directory / filename, {})
        records(
            directory / "development-predictions.jsonl",
            [{"building_source_id": key, "decision_score": "never interpreted"} for key in "AB"],
        )
        recipe = {"version": family}
        dump(directory / "recipe.json", recipe)
        identity = {
            "version": family,
            "config_sha256": config,
            "feature_identity": feature_identity,
            "feature_manifest_sha256": file_sha256(features / "manifest.json"),
            "recipe_sha256": content_hash(recipe),
        }
        if family == "catboost":
            lineage = {
                "training_manifest_sha256": file_sha256(models["logistic"] / "manifest.json"),
                "evaluation_manifest_sha256": file_sha256(
                    models["logistic"] / "evaluation" / "manifest.json"
                ),
            }
            dump(directory / "logistic-lineage.json", lineage)
            identity["logistic_lineage_sha256"] = content_hash(lineage)
        engine = {
            "config_sha256": config,
            "scenarios": {
                scenario: {
                    "pool": "all_valid" if scenario == "all_valid" else "valid_addressed",
                    "training_ids": ["A", "B"],
                }
                for scenario in SCENARIOS
            },
        }
        dump(
            directory / "lock.json",
            {
                "identity": identity,
                "engine": engine,
                "engine_sha256": content_hash(engine),
                "model_files": hashes(directory, binaries),
            },
        )
        dump(
            directory / "manifest.json",
            {
                "state": "locked_before_final",
                "identity": identity,
                "feature_artifact_hashes": feature_manifest["artifacts"],
                "artifacts": hashes(directory, names),
            },
        )
        evaluation = directory / "evaluation"
        dump(evaluation / "evaluation.json", {})
        records(
            evaluation / "predictions.jsonl",
            [{"building_source_id": key, "decision_score": "never interpreted"} for key in "CD"],
        )
        files = {"evaluation.json", "predictions.jsonl"}
        if family == "catboost":
            dump(evaluation / "shap-review.json", {})
            files.add("shap-review.json")
        dump(
            evaluation / "manifest.json",
            {
                "training_manifest_sha256": file_sha256(directory / "manifest.json"),
                "artifacts": hashes(evaluation, files),
            },
        )
        models[family] = directory
    return readiness, features, models


def test_inventory_is_deterministic_conservative_and_never_interprets_value_fields(artifacts):
    readiness, features, models = artifacts
    result = inventory_exposure([readiness], features, models, ["online-only", "A"])
    assert result == inventory_exposure(
        [readiness, readiness], features, models, ["A", "online-only"]
    )
    assert result["private_ids"]["feature_extracted"] == list("ABCDE")
    assert result["private_ids"]["sampled"] == list("ABCDEF")
    assert result["private_ids"]["model_training"] == list("AB")
    assert result["private_ids"]["predicted"] == list("ABCD")
    assert result["private_ids"]["excluded"] == list("ABCDEF") + ["online-only"]
    assert "geometry-only" not in result["private_ids"]["excluded"]
    assert result["summary"]["known_grocery_positive_overlap_count"] == 2
    assert result["summary"]["feature_extracted_grocery_positive_count"] == 2
    assert result["summary"]["untouched_known_grocery_positive_count"] == 0
    assert result["summary"]["historical_online_exposure"] == "incomplete"
    assert result["summary"]["old_study_geography_eligible"] is False
    assert result["policy"]["minimum_separation_m"] == 2000
    assert "never interpreted" not in json.dumps(result)


@pytest.mark.parametrize("which", ["features", "predictions", "binary", "readiness"])
def test_inventory_rejects_modified_artifacts_before_claiming_unexposed_ids(artifacts, which):
    readiness, features, models = artifacts
    path = {
        "features": features / "features-development.jsonl",
        "predictions": models["logistic"] / "evaluation" / "predictions.jsonl",
        "binary": models["catboost"] / "primary.joblib",
        "readiness": readiness / "memberships.jsonl",
    }[which]
    with path.open("ab") as stream:
        stream.write(b"changed")
    with pytest.raises(ValueError, match="content has changed"):
        inventory_exposure([readiness], features, models)


def test_identity_header_parser_never_deserializes_rest_of_feature_or_score_record(tmp_path):
    path = tmp_path / "identities.jsonl"
    path.write_text(
        '{"building_source_id":"A","values":DO_NOT_PARSE}\n'
        '{"building_source_id":"B","decision_score":DO_NOT_PARSE}\n',
        encoding="utf-8",
    )
    assert record_identifiers(path) == ({"A", "B"}, 2)
    path.write_text('{"decision_score":1,"building_source_id":"A"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="canonical building identity"):
        record_identifiers(path)


def test_duplicate_feature_ids_and_missing_model_family_are_rejected(tmp_path, artifacts):
    path = tmp_path / "duplicates.jsonl"
    records(path, [{"building_source_id": "A"}, {"building_source_id": "A"}])
    with pytest.raises(ValueError, match="repeats"):
        record_identifiers(path, unique=True)
    readiness, features, models = artifacts
    with pytest.raises(ValueError, match="both existing"):
        inventory_exposure([readiness], features, {"logistic": models["logistic"]})


def test_inconsistent_model_identity_is_rejected_even_after_manifest_rehash(artifacts):
    readiness, features, models = artifacts
    path = models["catboost"] / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["identity"]["feature_manifest_sha256"] = "b" * 64
    dump(path, manifest)
    with pytest.raises(ValueError, match="not bound"):
        inventory_exposure([readiness], features, models)


def test_another_readiness_inventory_cannot_replace_frozen_feature_lineage(artifacts):
    readiness, features, models = artifacts
    path = features / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["readiness_manifest"]["dataset_sha256"] = "c" * 64
    dump(path, manifest)
    with pytest.raises(ValueError, match="supplied readiness"):
        inventory_exposure([readiness], features, models)
