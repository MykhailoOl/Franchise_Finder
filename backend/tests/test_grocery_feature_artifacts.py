import json

import pytest

from app.cli import extract_grocery_features as extraction
from app.core.experiment import canonical_bytes, configuration_hash, load_experiment


def write_frozen(directory, mutate=None):
    definition = load_experiment()
    candidates = [
        {
            "building_source_id": "development",
            "pools": ["valid_addressed", "all_valid"],
            "fold": 0,
            "validation_fold": 0,
            "training_folds": [1, 2, 3],
        },
        {
            "building_source_id": "final",
            "pools": ["valid_addressed", "all_valid"],
            "fold": 3,
            "validation_fold": 3,
            "training_folds": [],
        },
    ]
    members = [
        {
            **{key: value for key, value in candidate.items() if key != "pools"},
            "category": "grocery",
            "pool": pool,
            "label": "observed_presence",
        }
        for candidate in candidates
        for pool in candidate["pools"]
    ]
    if mutate:
        mutate(candidates, members)
    for name, rows in (("candidates.jsonl", candidates), ("memberships.jsonl", members)):
        (directory / name).write_bytes(b"".join(canonical_bytes(row) + b"\n" for row in rows))
    report = {
        "status": "readiness_only",
        "empty_required_tables": [],
        "config_sha256": configuration_hash(definition),
        "dataset_sha256": "a" * 64,
        "candidate_sha256": extraction.file_hash(directory / "candidates.jsonl"),
        "memberships_sha256": extraction.file_hash(directory / "memberships.jsonl"),
        "summary": {
            "category_support": [
                {"category": "grocery", "pool": pool, "status": "ready_for_feature_work"}
                for pool in ("valid_addressed", "all_valid")
            ]
        },
    }
    extraction.write_json(directory / "readiness.json", report)
    manifest = {
        "config_sha256": report["config_sha256"],
        "dataset_sha256": report["dataset_sha256"],
        "artifacts": {
            name: extraction.file_hash(directory / name) for name in extraction.READINESS_FILES
        },
    }
    extraction.write_json(directory / "manifest.json", manifest)
    return definition


def test_frozen_grocery_preserves_shared_pool_memberships_and_final_isolation(tmp_path):
    definition = write_frozen(tmp_path)
    bundle = extraction.load_frozen_grocery(tmp_path, definition)
    assert len(bundle["candidates"]) == 2
    assert len(bundle["members"]) == 4
    assert bundle["candidates"]["final"]["training_folds"] == []
    (tmp_path / "memberships.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="content has changed"):
        extraction.load_frozen_grocery(tmp_path, definition)


def test_membership_cannot_change_the_frozen_building_split(tmp_path):
    definition = write_frozen(tmp_path, lambda candidates, members: members[0].update(fold=3))
    with pytest.raises(ValueError, match="geographic assignment"):
        extraction.load_frozen_grocery(tmp_path, definition)


def test_rejects_source_refresh_before_extraction(monkeypatch):
    snapshots = {name: {"row_count": 1, "sha256": name} for name in extraction.TABLE_KEYS}
    monkeypatch.setattr(
        extraction,
        "fingerprint_table",
        lambda connection, name: (
            {**snapshots[name], "sha256": "changed"}
            if name == "profile_population"
            else snapshots[name]
        ),
    )
    with pytest.raises(ValueError, match="Source changed since readiness: profile_population"):
        extraction.validate_source_snapshot(None, {"source_snapshots": snapshots})


def test_feature_artifact_reuse_and_tamper_detection(tmp_path):
    identity = {"runtime": {"database": ["PostgreSQL", "PostGIS"]}, "as_of_date": "2026-11-10"}
    for name in extraction.OUTPUT_FILES:
        (tmp_path / name).write_bytes(b"{}\n")
    manifest = {
        "identity": identity,
        "artifacts": {
            name: extraction.file_hash(tmp_path / name) for name in extraction.OUTPUT_FILES
        },
    }
    extraction.write_json(tmp_path / "manifest.json", manifest)
    extraction.validate_existing(tmp_path, identity)
    (tmp_path / "features-development.jsonl").write_bytes(b"changed")
    with pytest.raises(ValueError, match="content has changed"):
        extraction.validate_existing(tmp_path, identity)


def test_snapshot_outputs_keep_holdout_out_of_quality_and_labels_out_of_features(
    tmp_path, monkeypatch
):
    definition = write_frozen(tmp_path)
    bundle = extraction.load_frozen_grocery(tmp_path, definition)
    monkeypatch.setattr(extraction, "LOCAL_DIRECTORY", tmp_path)
    monkeypatch.setattr(extraction, "validate_source_snapshot", lambda *args: None)
    monkeypatch.setattr(extraction, "build_source_context", lambda *args: {"sources": {}})
    monkeypatch.setattr(
        extraction, "producer_identity", lambda *args: {"code_sha256": "b" * 64, "runtime": {}}
    )
    calls = []
    monkeypatch.setattr(
        extraction,
        "extract_raw_features",
        lambda connection, ids: (
            calls.append(ids) or [{"building_source_id": value} for value in ids]
        ),
    )
    monkeypatch.setattr(
        extraction,
        "build_feature_row",
        lambda raw, *args: {**raw, "values": dict.fromkeys(definition.primary_predictors)},
    )
    inspected = []
    monkeypatch.setattr(
        extraction,
        "summarize_feature_rows",
        lambda rows: inspected.extend(row["building_source_id"] for row in rows) or {},
    )

    class Connection:
        def execute(self, query):
            return self

        def fetchone(self):
            return ("on", "repeatable read")

    destination = extraction.extract_snapshot(Connection(), bundle, definition, tmp_path / "out", 1)
    assert inspected == ["development"]
    assert calls == [["development"], ["final"]]
    rows = list(extraction.read_records(destination / "features-final-holdout.jsonl"))
    assert rows[0]["building_source_id"] == "final"
    assert not {"label", "fold", "pool"} & set(rows[0])
    quality = json.loads((destination / "quality.json").read_text(encoding="utf-8"))
    assert quality["training_performed"] is False
    assert quality["final_holdout_distributions_evaluated"] is False
    assert (
        extraction.extract_snapshot(Connection(), bundle, definition, tmp_path / "out", 1)
        == destination
    )
    assert calls == [["development"], ["final"]]
