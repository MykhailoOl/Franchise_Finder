import math
import re
from collections import defaultdict
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, HttpUrl, model_validator

from app.core.address_search import fold_polish

COMPETITION_CATEGORIES = {
    "grocery": "Grocery",
    "quick-service": "Quick-service restaurants",
    "pizza": "Pizza",
    "beauty": "Beauty",
}
ACCESS_CATEGORIES = {
    "bus_stop": "Bus stops",
    "tram_stop": "Tram stops",
    "rail_station": "Rail stations",
    "metro_entrance": "Metro entrances",
    "parking": "Parking",
}


class ProfileSource(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1)
    url: HttpUrl
    attribution: str
    license: str | None = None
    retrieved_at: AwareDatetime
    observed_at: str | None = None
    snapshot_at: str | None = None
    coverage_bounds: tuple[float, float, float, float]
    notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_bounds(self) -> "ProfileSource":
        west, south, east, north = self.coverage_bounds
        if not (-180 <= west < east <= 180 and -85 <= south < north <= 85):
            raise ValueError("Source coverage bounds must be finite west,south,east,north")
        return self


class PopulationCell(BaseModel):
    source_id: str = Field(min_length=1)
    population: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    observation_year: int = Field(ge=1900, le=2100)
    resolution_m: int = Field(gt=0)
    status: Literal["observed", "suppressed", "missing"]

    @model_validator(mode="after")
    def validate_population(self) -> "PopulationCell":
        if (self.status == "observed") != (self.population is not None):
            raise ValueError("Only observed cells may have a numeric population")
        return self


class ProfilePoi(BaseModel):
    source_id: str = Field(min_length=1)
    name: str | None = None
    kind: Literal["competitor", "access"]
    categories: list[str] = Field(min_length=1)
    brand_id: str | None = None
    source_url: HttpUrl
    updated_at: AwareDatetime | None = None
    location_method: str = "node"
    tags: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_categories(self) -> "ProfilePoi":
        allowed = COMPETITION_CATEGORIES if self.kind == "competitor" else ACCESS_CATEGORIES
        if any(category not in allowed for category in self.categories):
            raise ValueError("POI category does not belong to its declared kind")
        self.categories = sorted(set(self.categories))
        return self


def normalize_poi_name(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", fold_polish(value or "")).strip()


def duplicate_pois(rows: list[dict[str, Any]]) -> dict[str, str]:
    def transit(row: dict) -> bool:
        return row["kind"] == "access" and bool(set(row["categories"]) & {"bus_stop", "tram_stop"})

    def role(row: dict) -> str:
        tags = row.get("tags", {})
        if tags.get("public_transport") == "platform" or tags.get("highway") == "bus_stop":
            return "platform"
        if tags.get("public_transport") == "stop_position":
            return "stop_position"
        return "unknown"

    def priority(row: dict) -> tuple:
        return (
            transit(row) and role(row) != "platform",
            row["location_method"] != "node",
            row["source_id"],
        )

    def same_place(first: dict, second: dict) -> bool:
        distance = math.hypot(first["x"] - second["x"], first["y"] - second["y"])
        if transit(first):
            first_tags, second_tags = first.get("tags", {}), second.get("tags", {})
            for key in ("ref", "local_ref"):
                if first_tags.get(key) and second_tags.get(key):
                    if first_tags[key] != second_tags[key]:
                        return False
            same_reference = any(
                first_tags.get(key) and first_tags.get(key) == second_tags.get(key)
                for key in ("ref", "local_ref")
            )
            if same_reference:
                return distance <= 100
            return distance <= 20 and {role(first), role(second)} == {"platform", "stop_position"}
        return distance <= 20

    buckets: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    duplicates = {}
    ordered = sorted(rows, key=priority)
    for row in ordered:
        if not row["normalized_name"]:
            continue
        identity = (row["kind"], tuple(row["categories"]), row["normalized_name"])
        gx, gy = math.floor(row["x"] / 100), math.floor(row["y"] / 100)
        candidates = [
            prior
            for dx in (-1, 0, 1)
            for dy in (-1, 0, 1)
            for prior in buckets[(*identity, gx + dx, gy + dy)]
            if same_place(prior, row)
        ]
        if candidates:
            duplicates[row["source_id"]] = min(candidates, key=priority)["source_id"]
        else:
            buckets[(*identity, gx, gy)].append(row)
    return duplicates
