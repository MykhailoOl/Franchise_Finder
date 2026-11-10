import hashlib
import json
from pathlib import Path

from app.core.config import BACKEND_DIRECTORY
from app.core.experiment import configuration_hash, content_hash, load_experiment
from app.core.grocery_features import EXTRACTION_VERSION, SOURCE_TABLES

SELECTION_PATH = BACKEND_DIRECTORY / "app" / "experiments" / "grocery-assessment-v1.json"
WITHHELD_REASON = (
    "Grocery recommendations are still being checked. "
    "Review the location facts and premises requirements below."
)
PUBLIC_FIELDS = {
    "building_id",
    "building_source_id",
    "category",
    "status",
    "reason",
    "computed_at",
    "origin",
    "available_radii_m",
    "evidence",
    "evidence_highlights",
    "evidence_note",
    "quality",
    "sources",
    "requirements",
    "missing_details",
    "versions",
}


def read_verified_json(path: Path, expected_hash: str) -> dict:
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != expected_hash:
        raise ValueError("Grocery assessment metadata integrity mismatch")
    value = json.loads(content)
    if not isinstance(value, dict):
        raise ValueError("Grocery assessment metadata must be an object")
    return value


class GroceryModelStatus:
    def __init__(self, directory: Path | None = None, selection_path: Path = SELECTION_PATH):
        self.selection_path = selection_path
        self.directory = directory

    def _load(self) -> tuple[dict, dict, dict]:
        selection = json.loads(self.selection_path.read_bytes())
        if (
            selection["version"] != "grocery-assessment-policy-v1"
            or selection["public_mode"] != "evidence_only"
            or selection["ranking_enabled"] is not False
        ):
            raise ValueError("Unsupported public grocery assessment policy")
        name = selection["artifact_directory_name"]
        if Path(name).name != name or name in {"", ".", ".."}:
            raise ValueError("Invalid grocery artifact directory name")
        directory = self.directory or BACKEND_DIRECTORY.parent / ".local" / "models" / name
        training = read_verified_json(
            directory / "manifest.json", selection["training_manifest_sha256"]
        )
        identity = training["identity"]
        definition = load_experiment()
        if (
            training["state"] != "locked_before_final"
            or identity["version"] != selection["model_version"]
            or identity["config_sha256"] != configuration_hash(definition)
            or identity["feature_identity"]["extraction_version"] != EXTRACTION_VERSION
        ):
            raise ValueError("Model lineage differs from the public feature contract")
        lock = read_verified_json(directory / "lock.json", training["artifacts"]["lock.json"])
        if (
            lock["identity"] != identity
            or content_hash(lock["engine"]) != lock["engine_sha256"]
            or lock["engine"]["config_sha256"] != identity["config_sha256"]
            or lock["engine"]["state"] != "models_locked_before_reused_final"
            or lock["engine"]["promotion_status"] != "withheld_reused_holdout"
            or lock["final_holdout_records_read"] is not False
            or lock["final_holdout_previously_evaluated_by_logistic"] is not True
            or any(
                training["artifacts"].get(name) != digest
                for name, digest in lock["model_files"].items()
            )
        ):
            raise ValueError("The grocery training lock is inconsistent")
        evaluation_manifest = read_verified_json(
            directory / "evaluation" / "manifest.json", selection["evaluation_manifest_sha256"]
        )
        if (
            evaluation_manifest["training_manifest_sha256"] != selection["training_manifest_sha256"]
            or evaluation_manifest["state"] != "evaluated_locked_catboost_exploratory"
        ):
            raise ValueError("The grocery evaluation belongs to a different model")
        evaluation = read_verified_json(
            directory / "evaluation" / "evaluation.json",
            evaluation_manifest["artifacts"]["evaluation.json"],
        )
        gate = evaluation["promotion_gate"]
        if (
            evaluation["training_manifest_sha256"] != selection["training_manifest_sha256"]
            or evaluation["lock_sha256"] != training["artifacts"]["lock.json"]
            or evaluation["phase"] != "exploratory_reused_final"
            or evaluation["confirmatory_for_this_lock"] is not False
            or evaluation["final_holdout_previously_used"] is not True
            or evaluation["models_refitted"] is not False
            or gate["status"] != "withheld_reused_holdout"
            or gate["eligible_for_promotion"] is not False
            or gate["fresh_confirmation"] is not False
            or evaluation["reference_gate_diagnostic"]["thresholds_met"] is not False
        ):
            raise ValueError("Grocery validation does not match the reviewed public policy")
        sources = read_verified_json(
            directory / "feature-sources.json", training["artifacts"]["feature-sources.json"]
        )
        if set(sources["sources"]) != set(SOURCE_TABLES):
            raise ValueError("The model source provenance is incomplete")
        return selection, evaluation, sources

    def describe(self, runtime: dict) -> dict:
        result = {
            "status": "unavailable",
            "version": None,
            "evaluated_at": None,
            "source_status": "unverified",
            "reason": (
                "Grocery recommendations are unavailable. "
                "You can still review location facts and premises requirements."
            ),
        }
        try:
            selection, evaluation, sources = self._load()
            metadata = runtime.get("source_metadata", {})
            changed, unknown = [], []
            for identifier, (table, _) in SOURCE_TABLES.items():
                matches = [row for row in metadata.get(table, []) if row["id"] == identifier]
                trained = sources["sources"][identifier]
                live = runtime.get("source_context", {}).get("sources", {}).get(identifier)
                if len(matches) != 1 or not live or not live.get("available"):
                    unknown.append(identifier)
                    continue
                signature = content_hash(
                    {"source_metadata": matches[0], "data_sha256": trained["data_sha256"]}
                )
                if signature != trained["content_sha256"]:
                    changed.append(identifier)
            result.update(
                {
                    "status": "stale" if changed or unknown else "validation_withheld",
                    "version": selection["model_version"],
                    "evaluated_at": evaluation["evaluated_at"],
                    "source_status": "changed"
                    if changed
                    else "unavailable"
                    if unknown
                    else "metadata_match_content_unverified",
                    "reason": (
                        "Public data has changed or is incomplete since the grocery model was "
                        "checked. Recommendations are paused; available facts appear below."
                    )
                    if changed or unknown
                    else WITHHELD_REASON,
                }
            )
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            result["status"] = "integrity_error"
            result["reason"] = (
                "Grocery recommendations are unavailable while their supporting data is checked. "
                "Location facts and premises requirements are still shown."
            )
        return result


def public_assessment(assessment: dict, model: GroceryModelStatus) -> dict:
    result = {name: value for name, value in assessment.items() if name in PUBLIC_FIELDS}
    runtime = assessment.get("_runtime", {})
    state = model.describe(runtime)
    result["status"] = "evidence_only"
    result["model"] = state
    result["ranking"] = {
        "status": "withheld",
        "score": None,
        "rank": None,
        "reason": state["reason"],
    }
    result["versions"] = {**result["versions"], "model": state["version"]}
    return result
