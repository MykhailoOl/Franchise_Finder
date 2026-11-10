import hashlib
import json
import math
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.data_contract import DATA_CONTRACT_VERSION, PREDICTORS, data_contract

CONFIG_PATH = Path(__file__).resolve().parents[1] / "experiments" / "warsaw-presence-v1.json"
POOLS = ("valid_addressed", "all_valid")


class FrozenDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SpatialDefinition(FrozenDefinition):
    center_longitude: float = Field(ge=20.8, le=21.3)
    center_latitude: float = Field(ge=52.0, le=52.5)
    crs: Literal[2180]
    final_holdout_fold: Literal[3]
    development_folds: tuple[Literal[0, 1, 2], ...]
    separation_m: float = Field(ge=2000, allow_inf_nan=False)
    maximum_catchment_m: Literal[1000]
    fold_names: tuple[str, str, str, str]
    policy: str


class SupportDefinition(FrozenDefinition):
    minimum_train_positives: int = Field(ge=1)
    minimum_evaluation_positives: int = Field(ge=1)
    minimum_background: int = Field(ge=1)


class ExperimentDefinition(FrozenDefinition):
    version: Literal["warsaw-presence-v1"]
    defined_at: date
    contract_version: Literal["warsaw-data-v1"]
    seed: str = Field(min_length=1)
    target: str
    category_roles: dict[str, Literal["primary", "exploratory", "evidence_only"]]
    primary_pool: Literal["valid_addressed"]
    sensitivity_pool: Literal["all_valid"]
    candidate_policy: str
    background_per_positive: int = Field(ge=1, le=10)
    background_policy: str
    split: SpatialDefinition
    primary_predictors: tuple[str, ...]
    deferred_predictors: tuple[str, ...]
    competition_policy: str
    preprocessing_policy: str
    tuning_policy: str
    baseline_predictors: dict[str, tuple[str, ...]]
    baseline_algorithm: dict[str, Any]
    models: dict[str, dict[str, Any]]
    support: SupportDefinition
    promotion_gate: dict[str, Any]
    references: tuple[str, ...]

    @model_validator(mode="after")
    def validate_scope(self) -> "ExperimentDefinition":
        expected_roles = {
            "grocery": "primary",
            "quick-service": "exploratory",
            "pizza": "exploratory",
            "beauty": "evidence_only",
        }
        if self.category_roles != expected_roles:
            raise ValueError("Category roles must preserve the predeclared experiment scope")
        if self.contract_version != DATA_CONTRACT_VERSION:
            raise ValueError("Data contract version has changed")
        primary, deferred = set(self.primary_predictors), set(self.deferred_predictors)
        competition = {
            name
            for name, item in PREDICTORS.items()
            if "official-franchise-locators" in item.sources
        }
        if len(self.primary_predictors) != 17 or len(self.deferred_predictors) != 4:
            raise ValueError("This version declares 17 primary and 4 deferred predictors")
        if primary & deferred or primary | deferred != set(PREDICTORS) or deferred != competition:
            raise ValueError("Competition predictors must remain deferred without target masking")
        if self.split.development_folds != (0, 1, 2):
            raise ValueError("Northeast final holdout cannot participate in development")
        if self.split.separation_m < 2 * self.split.maximum_catchment_m:
            raise ValueError("Split separation must cover two maximum catchment radii")
        if self.promotion_gate.get("status") != "unevaluated":
            raise ValueError("Experiment definition cannot claim an evaluated promotion gate")
        if set(self.models) != {"logistic_l2", "catboost_cpu"}:
            raise ValueError("Only the two declared bounded classifier families are permitted")
        catboost = self.models["catboost_cpu"]

        def bounded_integer(value: Any, upper: int) -> bool:
            return type(value) is int and 1 <= value <= upper

        def positive_finite(value: Any) -> bool:
            return type(value) in (int, float) and math.isfinite(value) and value > 0

        if (
            catboost.get("task_type") != "CPU"
            or catboost.get("family") != "CatBoostClassifier"
            or not bounded_integer(catboost.get("iterations"), 300)
            or not bounded_integer(catboost.get("thread_count"), 4)
            or not bounded_integer(catboost.get("max_trials"), 2)
            or not positive_finite(catboost.get("learning_rate"))
            or catboost["learning_rate"] > 1
            or not positive_finite(catboost.get("l2_leaf_reg"))
            or not isinstance(catboost.get("depth"), list)
            or not 1 <= len(catboost["depth"]) <= catboost["max_trials"]
            or any(not bounded_integer(depth, 6) for depth in catboost["depth"])
        ):
            raise ValueError("CatBoost declaration exceeds the bounded CPU experiment")
        logistic = self.models["logistic_l2"]
        if (
            logistic.get("family") != "LogisticRegression"
            or logistic.get("penalty") != "l2"
            or not bounded_integer(logistic.get("max_iter"), 1000)
            or not isinstance(logistic.get("C"), list)
            or not 1 <= len(logistic["C"]) <= 3
            or any(not positive_finite(value) for value in logistic["C"])
        ):
            raise ValueError("Logistic declaration requires positive bounded tuning values")
        baseline = self.baseline_algorithm
        if (
            baseline.get("family") != "LogisticRegression"
            or baseline.get("penalty") != "l2"
            or baseline.get("C") != 1.0
            or baseline.get("tuning") != "none"
            or not bounded_integer(baseline.get("max_iter"), 1000)
        ):
            raise ValueError("Baselines must be fixed restricted-feature L2 C=1 classifiers")
        if any(set(names) - primary for names in self.baseline_predictors.values()):
            raise ValueError("Baselines must use the declared environmental predictors")
        return self


def load_experiment(path: Path = CONFIG_PATH) -> ExperimentDefinition:
    return ExperimentDefinition.model_validate_json(path.read_bytes())


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=lambda item: item.isoformat() if isinstance(item, (date, datetime)) else str(item),
    ).encode("utf-8")


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def configuration_hash(definition: ExperimentDefinition) -> str:
    return content_hash(
        {"experiment": definition.model_dump(mode="json"), "data_contract": data_contract()}
    )


def candidate_pools(row: dict) -> list[str]:
    if not row["valid_geometry"]:
        return []
    return ["valid_addressed", "all_valid"] if row["has_unique_address"] else ["all_valid"]


def quadrant(x: float, y: float, center: tuple[float, float]) -> int:
    return int(x >= center[0]) + 2 * int(y >= center[1])


def distance_to_quadrant(x: float, y: float, center: tuple[float, float], fold: int) -> float:
    dx = max(center[0] - x, 0) if fold % 2 else max(x - center[0], 0)
    dy = max(center[1] - y, 0) if fold >= 2 else max(y - center[1], 0)
    return math.hypot(dx, dy)


def split_membership(
    row: dict, center: tuple[float, float], definition: ExperimentDefinition
) -> dict:
    if not row["valid_geometry"]:
        return {"fold": None, "validation_fold": None, "training_folds": []}
    x, y = row["x"], row["y"]
    if not all(math.isfinite(value) for value in (x, y)):
        raise ValueError("Valid geometry must have finite projected origin coordinates")
    fold = quadrant(x, y, center)
    separation = definition.split.separation_m
    outside_final_buffer = distance_to_quadrant(x, y, center, 3) > separation
    training = [
        test
        for test in range(4)
        if fold != test
        and fold != 3
        and outside_final_buffer
        and distance_to_quadrant(x, y, center, test) > separation
    ]
    return {
        "fold": fold,
        "validation_fold": fold if fold == 3 or outside_final_buffer else None,
        "training_folds": training,
    }


def sample_background(
    candidate_ids: set[str],
    positive_ids: set[str],
    category: str,
    pool: str,
    definition: ExperimentDefinition,
) -> list[str]:
    limit = definition.background_per_positive * len(candidate_ids & positive_ids)
    return sorted(
        candidate_ids - positive_ids,
        key=lambda identifier: (
            content_hash([definition.seed, pool, category, identifier]),
            identifier,
        ),
    )[:limit]


def build_memberships(
    buildings: list[dict],
    observations: list[dict],
    center: tuple[float, float],
    definition: ExperimentDefinition,
) -> tuple[dict, list[dict], list[dict]]:
    by_id = {row["source_id"]: row for row in buildings}
    if len(by_id) != len(buildings):
        raise ValueError("Building source identities must be unique")
    positives = {category: set() for category in definition.category_roles}
    for row in observations:
        if row["category_id"] not in positives:
            raise ValueError("Unconfigured label category requires a new experiment definition")
        positives[row["category_id"]].add(row["building_source_id"])
    candidates = [
        {
            "building_source_id": identifier,
            "pools": candidate_pools(row),
            **split_membership(row, center, definition),
        }
        for identifier, row in sorted(by_id.items())
    ]
    candidate_map = {row["building_source_id"]: row for row in candidates}
    summary = {
        "pools": {},
        "category_support": [],
        "positive_exclusions": [],
        "labels": "observed_presence or unlabelled_background; no verified negative outcomes",
    }
    memberships = []
    for pool in POOLS:
        ids = {row["building_source_id"] for row in candidates if pool in row["pools"]}
        summary["pools"][pool] = {
            "candidates": len(ids),
            "by_building_type": dict(
                sorted(Counter(by_id[key].get("building_type") or "unknown" for key in ids).items())
            ),
            "by_fold": dict(sorted(Counter(candidate_map[key]["fold"] for key in ids).items())),
        }
        for category, role in sorted(definition.category_roles.items()):
            included = positives[category] & ids
            excluded = positives[category] - ids
            reasons = Counter()
            excluded_types = Counter()
            for identifier in sorted(excluded):
                row = by_id.get(identifier)
                reason = (
                    "missing_building"
                    if row is None
                    else "invalid_geometry"
                    if not row["valid_geometry"]
                    else "no_uniquely_matched_address"
                )
                reasons[reason] += 1
                excluded_types[(row or {}).get("building_type") or "unknown"] += 1
            summary["positive_exclusions"].append(
                {
                    "pool": pool,
                    "category": category,
                    "count": len(excluded),
                    "by_reason": dict(reasons),
                    "by_building_type": dict(excluded_types),
                    "building_source_ids": sorted(excluded),
                }
            )
            background = []
            for partition in ("development", "final"):
                partition_ids = {
                    key
                    for key in ids
                    if (candidate_map[key]["fold"] == 3) == (partition == "final")
                }
                background.extend(
                    sample_background(
                        partition_ids,
                        positives[category],
                        category,
                        f"{pool}:{partition}",
                        definition,
                    )
                )
            rows = [
                {
                    "pool": pool,
                    "category": category,
                    "building_source_id": identifier,
                    "label": "observed_presence"
                    if identifier in included
                    else "unlabelled_background",
                    **{
                        key: candidate_map[identifier][key]
                        for key in ("fold", "validation_fold", "training_folds")
                    },
                }
                for identifier in sorted(included | set(background))
            ]
            memberships.extend(rows)
            folds = []
            for fold in range(4):
                train = Counter(row["label"] for row in rows if fold in row["training_folds"])
                evaluate = Counter(row["label"] for row in rows if row["validation_fold"] == fold)
                train_before_purge = sum(row["fold"] not in {fold, 3} for row in rows)
                evaluation_before_purge = sum(row["fold"] == fold for row in rows)
                support = (
                    train["observed_presence"] >= definition.support.minimum_train_positives
                    and evaluate["observed_presence"]
                    >= definition.support.minimum_evaluation_positives
                    and train["unlabelled_background"] >= definition.support.minimum_background
                    and evaluate["unlabelled_background"] >= definition.support.minimum_background
                )
                folds.append(
                    {
                        "fold": fold,
                        "role": "sealed_final" if fold == 3 else "development",
                        "train_positives": train["observed_presence"],
                        "train_background": train["unlabelled_background"],
                        "evaluation_positives": evaluate["observed_presence"],
                        "evaluation_background": evaluate["unlabelled_background"],
                        "train_background_per_positive": train["unlabelled_background"]
                        / train["observed_presence"]
                        if train["observed_presence"]
                        else None,
                        "evaluation_background_per_positive": evaluate["unlabelled_background"]
                        / evaluate["observed_presence"]
                        if evaluate["observed_presence"]
                        else None,
                        "excluded_from_train": len(rows) - sum(train.values()),
                        "train_before_purge": train_before_purge,
                        "train_purged_for_spatial_separation": train_before_purge
                        - sum(train.values()),
                        "evaluation_purged_for_final_separation": evaluation_before_purge
                        - sum(evaluate.values()),
                        "support_sufficient": support,
                    }
                )
            status = (
                "evidence_only"
                if role == "evidence_only"
                else "ready_for_feature_work"
                if all(fold["support_sufficient"] for fold in folds)
                else "insufficient_support"
            )
            summary["category_support"].append(
                {
                    "pool": pool,
                    "category": category,
                    "role": role,
                    "positives": len(included),
                    "background": len(background),
                    "status": status,
                    "folds": folds,
                }
            )
    return summary, candidates, memberships
