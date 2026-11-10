import json
from copy import deepcopy
from pathlib import Path

import pytest

from app.cli import evaluate_grocery_fresh as checkpoint
from app.core.experiment import content_hash


def write_records(path, rows):
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8"
    )


@pytest.fixture
def prepared_bundle(tmp_path):
    root = tmp_path
    storage = root / ".local" / "evaluations"
    prepared, sources = storage / "prepared", storage / "sources"
    prepared.mkdir(parents=True)
    sources.mkdir()
    model_root = root / ".local" / "models"
    model_root.mkdir()
    models = {}
    for name in sorted(checkpoint.MODEL_ROLES):
        path = model_root / f"{name}.joblib"
        path.write_bytes(f"fixture-{name}".encode())
        models[name] = {
            "path": path.relative_to(root).as_posix(),
            "sha256": checkpoint.file_hash(path),
        }
    protocol = {
        "version": "grocery-fresh-evaluation-v1",
        "geography": "Radom",
        "collector": {"municipality_id": "1463011"},
        "candidate": {"namespace": "146301_1."},
        "freshness": {"minimum_separation_m": 2000},
        "promotion": {"automatic_promotion": False, "rankings_enabled": False},
        "models": models,
        "lineage": {"manifest_sha256": {}},
    }
    protocol_path = root / "protocol.json"
    checkpoint.write_json(protocol_path, protocol)
    protocol_hash = checkpoint.file_hash(protocol_path)
    (sources / "protocol.json").write_bytes(protocol_path.read_bytes())
    sources_manifest = {
        "protocol_sha256": protocol_hash,
        "collected_at": "2026-11-10T12:00:00+00:00",
        "sources": {},
        "artifacts": {"protocol.json": protocol_hash},
    }
    checkpoint.write_json(sources / "manifest.json", sources_manifest)
    memberships = [
        {
            "building_source_id": "146301_1.0001",
            "label": "observed_presence",
            "x": 637000,
            "y": 410000,
        },
        {
            "building_source_id": "146301_1.0002",
            "label": "unlabelled_background",
            "x": 638000,
            "y": 411000,
        },
    ]
    write_records(prepared / "cohort.jsonl", memberships)
    write_records(
        prepared / "features.jsonl",
        [{"building_source_id": row["building_source_id"]} for row in memberships],
    )
    checkpoint.write_json(prepared / "sources.json", {})
    checkpoint.write_json(prepared / "quality.json", {})
    manifest = {
        "protocol_sha256": protocol_hash,
        "sources_manifest_sha256": checkpoint.file_hash(sources / "manifest.json"),
        "candidate_frame": {"complete": True},
        "freshness": {
            "passed": True,
            "overlapping_ids": [],
            "old_study_geography_overlap": False,
            "minimum_distance_m": 80000.0,
        },
        "counts": {"rows": 2, "positives": 1, "background": 1},
        "artifacts": {
            name: checkpoint.file_hash(prepared / name) for name in checkpoint.PREPARED_FILES
        },
    }
    checkpoint.write_json(prepared / "manifest.json", manifest)
    return {
        "root": root,
        "prepared": prepared,
        "sources": sources,
        "protocol": protocol,
        "protocol_path": protocol_path,
        "protocol_hash": protocol_hash,
        "manifest": manifest,
        "sources_manifest": sources_manifest,
        "memberships": memberships,
        "audit": {"exposure": {"private_ids": {"excluded": ["old-Warsaw-building"]}}},
        "output": storage / "result",
    }


def verify(bundle):
    return checkpoint.verify_prepared(
        bundle["prepared"],
        bundle["sources"],
        bundle["protocol"],
        bundle["protocol_hash"],
        bundle["audit"],
    )


def republish_prepared(bundle, rewrite_memberships=False):
    if rewrite_memberships:
        write_records(bundle["prepared"] / "cohort.jsonl", bundle["memberships"])
    bundle["manifest"]["artifacts"] = {
        name: checkpoint.file_hash(bundle["prepared"] / name) for name in checkpoint.PREPARED_FILES
    }
    checkpoint.write_json(bundle["prepared"] / "manifest.json", bundle["manifest"])


@pytest.mark.parametrize("relative", ["..", "../outside.json", "nested/../../outside.json", "."])
def test_safe_child_rejects_directory_escape(tmp_path, relative):
    with pytest.raises(ValueError, match="inside"):
        checkpoint.safe_child(tmp_path, relative)


def test_safe_child_rejects_absolute_external_path_and_allows_nested_artifacts(tmp_path):
    with pytest.raises(ValueError, match="inside"):
        checkpoint.safe_child(tmp_path / "inside", str(tmp_path / "outside"))
    assert checkpoint.safe_child(tmp_path, "raw/source.json") == tmp_path / "raw" / "source.json"


def test_prepared_bundle_validates_identity_counts_and_spatial_proof(prepared_bundle):
    bundle = prepared_bundle
    manifest, sources_manifest, memberships = verify(bundle)
    assert manifest == bundle["manifest"]
    assert sources_manifest == bundle["sources_manifest"]
    assert memberships == bundle["memberships"]


@pytest.mark.parametrize("name", sorted(checkpoint.PREPARED_FILES))
def test_prepared_file_changes_are_detected(prepared_bundle, name):
    path = prepared_bundle["prepared"] / name
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="content changed"):
        verify(prepared_bundle)


@pytest.mark.parametrize(
    "damage",
    [
        "protocol",
        "acquisition_link",
        "distance",
        "nonfinite_distance",
        "boolean_distance",
        "incomplete_frame",
        "old_geography",
        "overlap_proof",
        "counts",
        "duplicate",
        "wrong_namespace",
        "exposed_id",
        "label",
        "origin",
    ],
)
def test_self_consistent_invalid_preparation_is_rejected(prepared_bundle, damage):
    bundle = prepared_bundle
    manifest = bundle["manifest"]
    rewrite = False
    if damage == "protocol":
        manifest["protocol_sha256"] = "0" * 64
    elif damage == "acquisition_link":
        manifest["sources_manifest_sha256"] = "0" * 64
    elif damage == "distance":
        manifest["freshness"]["minimum_distance_m"] = 2000.0
    elif damage == "nonfinite_distance":
        manifest["freshness"]["minimum_distance_m"] = "Infinity"
    elif damage == "boolean_distance":
        manifest["freshness"]["minimum_distance_m"] = True
    elif damage == "incomplete_frame":
        manifest["candidate_frame"]["complete"] = False
    elif damage == "old_geography":
        manifest["freshness"]["old_study_geography_overlap"] = True
    elif damage == "overlap_proof":
        manifest["freshness"]["overlapping_ids"] = ["old-Warsaw-building"]
    elif damage == "counts":
        manifest["counts"]["positives"] = 2
    elif damage == "duplicate":
        bundle["memberships"].append(deepcopy(bundle["memberships"][0]))
        rewrite = True
    elif damage == "wrong_namespace":
        bundle["memberships"][0]["building_source_id"] = "Warsaw-building"
        rewrite = True
    elif damage == "exposed_id":
        bundle["audit"]["exposure"]["private_ids"]["excluded"].append(
            bundle["memberships"][0]["building_source_id"]
        )
    elif damage == "label":
        bundle["memberships"][0]["label"] = "failed_business"
        rewrite = True
    else:
        bundle["memberships"][0]["x"] = True
        rewrite = True
    republish_prepared(bundle, rewrite)
    with pytest.raises(ValueError):
        verify(bundle)


def test_acquisition_manifest_cannot_escape_its_directory(prepared_bundle):
    bundle = prepared_bundle
    external = bundle["root"] / "external.json"
    external.write_bytes(b"{}")
    bundle["sources_manifest"]["artifacts"][str(external)] = checkpoint.file_hash(external)
    checkpoint.write_json(bundle["sources"] / "manifest.json", bundle["sources_manifest"])
    bundle["manifest"]["sources_manifest_sha256"] = checkpoint.file_hash(
        bundle["sources"] / "manifest.json"
    )
    republish_prepared(bundle)
    with pytest.raises(ValueError, match="inside"):
        verify(bundle)


@pytest.mark.parametrize("key", ["../escape", "Radom", "0" * 63, "G" * 64])
def test_checkpoint_ledger_rejects_noncanonical_city_key(tmp_path, key):
    with pytest.raises(ValueError):
        checkpoint.claim_checkpoint(tmp_path / "ledger", key, {"test": True})


def test_failed_consumed_checkpoint_cannot_be_retried_or_reidentified(tmp_path):
    key = content_hash({"municipality_id": "1463011"})
    ledger = tmp_path / "ledger"
    claim = checkpoint.claim_checkpoint(ledger, key, {"protocol_sha256": "first"})
    assert claim["state"] == "consumed"
    for identity in ({"protocol_sha256": "first"}, {"protocol_sha256": "changed"}):
        with pytest.raises(ValueError, match="already been consumed"):
            checkpoint.claim_checkpoint(ledger, key, identity)


def completed_fixture(tmp_path):
    key = content_hash({"municipality_id": "1463011"})
    ledger, output = tmp_path / "ledger", tmp_path / "result"
    identity = {"protocol_sha256": "frozen"}
    claim = checkpoint.claim_checkpoint(ledger, key, identity)
    output.mkdir()
    report = {"status": "evaluated", "promotion_gate": {"eligible_for_promotion": False}}
    for name in checkpoint.RESULT_FILES:
        checkpoint.write_json(output / name, report if name == "evaluation.json" else {})
    checkpoint.write_json(
        output / "manifest.json",
        {
            "identity": identity,
            "artifacts": {
                name: checkpoint.file_hash(output / name) for name in checkpoint.RESULT_FILES
            },
        },
    )
    checkpoint.complete_checkpoint(ledger, key, claim, output)
    completed = checkpoint.claim_checkpoint(ledger, key, identity)
    return key, ledger, output, completed, report


def test_completed_checkpoint_reuses_only_same_identity_and_output(tmp_path):
    key, ledger, output, completed, report = completed_fixture(tmp_path)
    assert checkpoint.reuse_completed(completed, output) == report
    with pytest.raises(ValueError, match="already been consumed"):
        checkpoint.claim_checkpoint(ledger, key, {"protocol_sha256": "changed"})
    with pytest.raises(ValueError, match="identity or result"):
        checkpoint.reuse_completed(completed, tmp_path / "other")


@pytest.mark.parametrize("name", ["manifest.json", *sorted(checkpoint.RESULT_FILES)])
def test_completed_checkpoint_tampering_is_rejected(tmp_path, name):
    _, _, output, completed, _ = completed_fixture(tmp_path)
    path = output / name
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="changed"):
        checkpoint.reuse_completed(completed, output)


@pytest.fixture
def lifecycle(prepared_bundle, monkeypatch):
    bundle = prepared_bundle
    monkeypatch.setattr(checkpoint, "ROOT", bundle["root"])
    monkeypatch.setattr(
        checkpoint,
        "verify_protocol",
        lambda *args: (
            bundle["protocol"],
            {"git_commit": "protocol", "committed_at": "2026-11-10T11:00:00+00:00"},
        ),
    )
    monkeypatch.setattr(checkpoint, "historical_audit", lambda protocol: deepcopy(bundle["audit"]))
    monkeypatch.setattr(checkpoint, "verify_acquisition_dates", lambda *args: None)
    monkeypatch.setattr(
        checkpoint, "producer_identity", lambda: {"git_commit": "producer", "worktree_clean": True}
    )
    calls = []
    key = content_hash({"municipality_id": "1463011"})
    ledger_path = bundle["root"] / ".local/evaluations/.consumption" / f"{key}.json"

    def load_model(path):
        assert checkpoint.load_json(ledger_path)["state"] == "consumed"
        return Path(path).name

    def evaluate(features, memberships, models, protocol):
        assert checkpoint.load_json(ledger_path)["state"] == "consumed"
        calls.append(1)
        return {
            "status": "evaluated",
            "support": {"rows": 2},
            "numeric_gate": {"passed": True},
            "promotion_gate": {"eligible_for_promotion": False},
        }, []

    monkeypatch.setattr("joblib.load", load_model)
    monkeypatch.setattr(checkpoint, "evaluate_fresh", evaluate)
    return bundle, calls, ledger_path


def run_lifecycle(bundle):
    return checkpoint.evaluate_checkpoint(
        bundle["protocol_path"],
        bundle["protocol_hash"],
        bundle["prepared"],
        bundle["sources"],
        bundle["output"],
    )


def test_lifecycle_consumes_before_model_loading_and_reuses_without_scoring(lifecycle):
    bundle, calls, ledger_path = lifecycle
    first = run_lifecycle(bundle)
    assert first["promotion_gate"]["eligible_for_promotion"] is False
    assert checkpoint.load_json(ledger_path)["state"] == "complete"
    assert run_lifecycle(bundle) == first
    assert calls == [1]


def test_lifecycle_tampered_preparation_never_scores_or_consumes(lifecycle):
    bundle, calls, ledger_path = lifecycle
    (bundle["prepared"] / "features.jsonl").write_bytes(b"changed")
    with pytest.raises(ValueError, match="content changed"):
        run_lifecycle(bundle)
    assert calls == []
    assert not ledger_path.exists()


def test_lifecycle_failed_scoring_remains_consumed(lifecycle, monkeypatch):
    bundle, _, ledger_path = lifecycle

    def fail(*args):
        raise ValueError("scoring failed")

    monkeypatch.setattr(checkpoint, "evaluate_fresh", fail)
    with pytest.raises(ValueError, match="scoring failed"):
        run_lifecycle(bundle)
    assert checkpoint.load_json(ledger_path)["state"] == "consumed"
    with pytest.raises(ValueError, match="already been consumed"):
        run_lifecycle(bundle)


@pytest.fixture
def acquisition(tmp_path):
    current = "2026-11-10T12:00:00+00:00"
    sources = tmp_path / "sources"
    sources.mkdir()
    raw_names = [
        "raw/pois.json",
        "raw/activity.json",
        "raw/zabka-stores.raw.json",
        "raw/lewiatan-stores.raw.json",
        "raw/buildings/hits.xml",
        "raw/buildings/hits-after.xml",
        "raw/buildings/00000000.gml",
        "raw/addresses/hits.xml",
        "raw/addresses/hits-after.xml",
        "raw/addresses/00000000.gml",
    ]
    artifacts = {}
    for name in raw_names:
        payload = sources / name
        payload.parent.mkdir(parents=True, exist_ok=True)
        payload.write_bytes(b"synthetic-public-payload")
        artifacts[name] = checkpoint.file_hash(payload)
        metadata_name = name + ".metadata.json"
        checkpoint.write_json(
            sources / metadata_name,
            {"sha256": artifacts[name], "retrieved_at": current},
        )
        artifacts[metadata_name] = checkpoint.file_hash(sources / metadata_name)
    source_metadata = {
        role: {"retrieved_at": current}
        for role in ("buildings", "addresses", "pois", "activity", "zabka", "lewiatan")
    }
    source_metadata["population"] = {
        "retrieved_at": "2026-10-23T12:00:00+00:00",
        "generated_at": current,
    }
    return {
        "sources": sources,
        "manifest": {
            "collector_version": "radom-public-sources-v1",
            "collected_at": current,
            "sources": source_metadata,
            "artifacts": artifacts,
        },
        "protocol": {"collector": {"version": "radom-public-sources-v1"}},
        "freeze": {"committed_at": "2026-11-10T11:00:00+00:00"},
    }


def test_fresh_acquisition_allows_locked_cached_population_retrieval(acquisition):
    checkpoint.verify_acquisition_dates(**acquisition)


@pytest.mark.parametrize(
    "name",
    [
        "raw/buildings/00000000.gml.metadata.json",
        "raw/addresses/00000000.gml.metadata.json",
        "raw/zabka-stores.raw.json.metadata.json",
        "raw/lewiatan-stores.raw.json.metadata.json",
    ],
)
def test_one_prefreeze_raw_request_invalidates_otherwise_fresh_bundle(acquisition, name):
    path = acquisition["sources"] / name
    metadata = checkpoint.load_json(path)
    metadata["retrieved_at"] = "2026-11-10T10:59:59+00:00"
    checkpoint.write_json(path, metadata)
    acquisition["manifest"]["artifacts"][name] = checkpoint.file_hash(path)
    with pytest.raises(ValueError, match="must follow protocol commit"):
        checkpoint.verify_acquisition_dates(**acquisition)


@pytest.mark.parametrize(
    "name",
    [
        "raw/addresses/hits.xml.metadata.json",
        "raw/zabka-stores.raw.json.metadata.json",
        "raw/buildings/00000000.gml.metadata.json",
        "raw/addresses/00000000.gml.metadata.json",
    ],
)
def test_acquisition_missing_required_raw_timestamp_is_rejected(acquisition, name):
    del acquisition["manifest"]["artifacts"][name]
    with pytest.raises(ValueError, match="raw request timestamp"):
        checkpoint.verify_acquisition_dates(**acquisition)


def test_acquisition_payload_metadata_must_match_manifest(acquisition):
    name = "raw/zabka-stores.raw.json.metadata.json"
    path = acquisition["sources"] / name
    metadata = checkpoint.load_json(path)
    metadata["sha256"] = "0" * 64
    checkpoint.write_json(path, metadata)
    acquisition["manifest"]["artifacts"][name] = checkpoint.file_hash(path)
    with pytest.raises(ValueError, match="does not match its frozen payload"):
        checkpoint.verify_acquisition_dates(**acquisition)


@pytest.mark.parametrize("kind", ["buildings", "addresses"])
def test_wfs_counts_without_any_preserved_page_are_insufficient(acquisition, kind):
    artifacts = acquisition["manifest"]["artifacts"]
    del artifacts[f"raw/{kind}/00000000.gml"]
    del artifacts[f"raw/{kind}/00000000.gml.metadata.json"]
    with pytest.raises(ValueError, match="Every WFS page"):
        checkpoint.verify_acquisition_dates(**acquisition)


@pytest.mark.parametrize("timestamp", ["2026-11-10T11:00:00+00:00", "2026-11-10T12:00:00"])
def test_regional_population_extraction_must_be_strictly_postfreeze(acquisition, timestamp):
    acquisition["manifest"]["sources"]["population"]["generated_at"] = timestamp
    with pytest.raises(ValueError, match="must follow protocol commit"):
        checkpoint.verify_acquisition_dates(**acquisition)


@pytest.mark.parametrize("damage", ["collector", "missing_role", "source_date"])
def test_acquisition_rejects_inconsistent_source_metadata(acquisition, damage):
    manifest = acquisition["manifest"]
    if damage == "collector":
        manifest["collector_version"] = "changed"
    elif damage == "missing_role":
        del manifest["sources"]["lewiatan"]
    else:
        manifest["sources"]["lewiatan"]["retrieved_at"] = "2026-11-10T10:59:59+00:00"
    with pytest.raises(ValueError):
        checkpoint.verify_acquisition_dates(**acquisition)
