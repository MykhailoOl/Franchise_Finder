import random
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.cli.prepare_experiment import save_artifacts
from app.core.experiment import (
    ExperimentDefinition,
    build_memberships,
    candidate_pools,
    configuration_hash,
    content_hash,
    distance_to_quadrant,
    load_experiment,
    sample_background,
    split_membership,
)


def buildings():
    return [
        {
            "source_id": f"b-{fold}-{i:03}",
            "valid_geometry": True,
            "has_unique_address": i % 5 != 0,
            "building_type": "m",
            "x": (1 if fold % 2 else -1) * (3000 + i * 50),
            "y": (1 if fold >= 2 else -1) * (3000 + i * 50),
        }
        for fold in range(4)
        for i in range(60)
    ]


def observations(rows, categories=("grocery", "pizza", "beauty")):
    return [
        {"building_source_id": row["source_id"], "category_id": category}
        for row in rows
        if int(row["source_id"].rsplit("-", 1)[1]) < 8
        for category in categories
    ]


def test_definition_preserves_no_training_scope_and_leakage_constraints():
    definition = load_experiment()
    assert len(definition.primary_predictors) == 17
    assert definition.promotion_gate["status"] == "unevaluated"
    altered = definition.model_dump(mode="json")
    altered["split"]["development_folds"] = [0, 1, 3]
    with pytest.raises(ValidationError):
        ExperimentDefinition.model_validate(altered)
    altered = definition.model_dump(mode="json")
    altered["primary_predictors"][0] = "grocery_count_500m"
    with pytest.raises(ValidationError, match="deferred"):
        ExperimentDefinition.model_validate(altered)
    assert configuration_hash(definition) != content_hash({"version": definition.version})


@pytest.mark.parametrize(
    "field,value",
    [
        ("iterations", -10),
        ("thread_count", -3),
        ("max_trials", 0),
        ("learning_rate", float("inf")),
        ("depth", [20]),
    ],
)
def test_definition_rejects_unbounded_or_nonpositive_training_declarations(field, value):
    altered = load_experiment().model_dump(mode="json")
    altered["models"]["catboost_cpu"][field] = value
    with pytest.raises(ValidationError, match="bounded"):
        ExperimentDefinition.model_validate(altered)


def test_definition_rejects_duplicate_development_folds():
    altered = load_experiment().model_dump(mode="json")
    altered["split"]["development_folds"] = [0, 1, 2, 2]
    with pytest.raises(ValidationError, match="holdout"):
        ExperimentDefinition.model_validate(altered)


def test_memberships_deterministic_and_candidates_independent_of_positive_labels():
    definition = load_experiment()
    rows = buildings()
    labels = observations(rows)
    expected = build_memberships(rows, labels, (0, 0), definition)
    random.Random(123).shuffle(rows)
    random.Random(456).shuffle(labels)
    assert build_memberships(rows, labels, (0, 0), definition) == expected
    _, without_labels, _ = build_memberships(rows, [], (0, 0), definition)
    assert without_labels == expected[1]
    assert candidate_pools({"valid_geometry": False, "has_unique_address": True}) == []
    assert candidate_pools({"valid_geometry": True, "has_unique_address": False}) == ["all_valid"]
    by_building = {}
    for row in expected[2]:
        by_building.setdefault(row["building_source_id"], set()).add(row["fold"])
    assert all(len(folds) == 1 for folds in by_building.values())
    assert all(
        row["status"] == "evidence_only"
        for row in expected[0]["category_support"]
        if row["category"] == "beauty"
    )
    assert all(
        row["status"] == "insufficient_support"
        for row in expected[0]["category_support"]
        if row["category"] != "beauty"
    )


def test_background_stays_unknown_excludes_positives_and_final_cannot_change_development():
    definition = load_experiment()
    ids = {str(i) for i in range(100)}
    chosen = sample_background(ids, {"1", "2"}, "grocery", "test", definition)
    assert len(chosen) == 20
    assert not set(chosen) & {"1", "2"}
    assert sample_background(ids, set(), "grocery", "test", definition) == []
    rows = buildings()
    initial = observations(rows, ("grocery",))
    changed = [row for row in initial if row["building_source_id"] != "b-3-001"]
    first = build_memberships(rows, initial, (0, 0), definition)[2]
    second = build_memberships(rows, changed, (0, 0), definition)[2]
    assert [row for row in first if row["fold"] != 3] == [row for row in second if row["fold"] != 3]
    assert {row["label"] for row in first} == {"observed_presence", "unlabelled_background"}


def test_split_purges_both_final_and_validation_extents_without_shared_buildings():
    definition = load_experiment()
    near_final = {"valid_geometry": True, "x": -1999, "y": 5000}
    result = split_membership(near_final, (0, 0), definition)
    assert result == {"fold": 2, "validation_fold": None, "training_folds": []}
    boundary = {"valid_geometry": True, "x": -2000, "y": 5000}
    assert split_membership(boundary, (0, 0), definition)["training_folds"] == []
    rows = buildings() + [near_final, boundary]
    assigned = [(row, split_membership(row, (0, 0), definition)) for row in rows]
    for fold in range(4):
        train = [
            (row, membership)
            for row, membership in assigned
            if fold in membership["training_folds"]
        ]
        evaluate = [
            (row, membership)
            for row, membership in assigned
            if membership["validation_fold"] == fold
        ]
        assert all(membership["fold"] != 3 for _, membership in train)
        for row, _ in train:
            assert distance_to_quadrant(row["x"], row["y"], (0, 0), fold) > 2000
            assert distance_to_quadrant(row["x"], row["y"], (0, 0), 3) > 2000
            assert all(
                (row["x"] - test["x"]) ** 2 + (row["y"] - test["y"]) ** 2 > 2000**2
                for test, _ in evaluate
            )


def test_artifact_publish_is_immutable_and_interrupted_write_does_not_block_retry(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("app.cli.prepare_experiment.LOCAL_DIRECTORY", tmp_path)
    report = {"config_sha256": "a" * 64, "dataset_sha256": "b" * 64}
    original_write = Path.write_bytes

    def interrupted_write(path, data):
        if path.name == "manifest.json":
            raise OSError("simulated interrupted write")
        return original_write(path, data)

    monkeypatch.setattr(Path, "write_bytes", interrupted_write)
    with pytest.raises(OSError, match="interrupted"):
        save_artifacts(tmp_path, report, [{"building_source_id": "a"}], [])
    assert all(path.name.startswith(".pending-") for path in tmp_path.iterdir())
    monkeypatch.setattr(Path, "write_bytes", original_write)
    destination = save_artifacts(tmp_path, report, [{"building_source_id": "a"}], [])
    first = (destination / "readiness.json").read_bytes()
    assert save_artifacts(tmp_path, report, [{"building_source_id": "a"}], []) == destination
    assert (destination / "readiness.json").read_bytes() == first
    (destination / "candidates.jsonl").write_bytes(b"changed")
    with pytest.raises(ValueError, match="has changed"):
        save_artifacts(tmp_path, report, [{"building_source_id": "a"}], [])
