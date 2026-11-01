from datetime import date
from typing import Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    StrictFloat,
    StrictStr,
    model_validator,
)

DATA_CONTRACT_VERSION = "warsaw-data-v1"
RADII_M = (300, 500, 1000)
BUILDING_USE_CODES = {"m", "g", "t", "k", "z", "b", "h", "p", "s", "i"}
OFFICIAL_LOCATOR_SOURCES = {
    "brand-zabka",
    "brand-lewiatan",
    "brand-mcdonalds",
    "brand-subway",
    "brand-da-grasso",
    "brand-dominos",
    "brand-yasumi",
    "brand-depilconcept",
}


class PredictorDefinition(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str
    unit: Literal["m2", "category", "storeys", "people", "m", "count", "fraction"]
    sources: tuple[str, ...]
    method: str
    limitation: str
    radius_m: int | None = None
    exclude_selected_building: bool = False


def planned_predictors() -> tuple[PredictorDefinition, ...]:
    definitions = [
        PredictorDefinition(
            id="building_footprint_area_m2",
            unit="m2",
            sources=("warsaw-buildings",),
            method="Area of official footprint in EPSG:2180",
            limitation="Not usable commercial unit area, floor area or rentable area",
        ),
        PredictorDefinition(
            id="mapped_building_use",
            unit="category",
            sources=("warsaw-buildings",),
            method="Published RODZAJ classification",
            limitation="Mapped building use does not confirm permitted use of a unit",
        ),
        PredictorDefinition(
            id="mapped_storeys",
            unit="storeys",
            sources=("warsaw-buildings",),
            method="Published positive KONDYGNACJE_NADZIEMNE value, otherwise missing",
            limitation="Not a measure of accessible floors or commercial unit capacity",
        ),
    ]
    definitions.extend(
        PredictorDefinition(
            id=f"resident_subtotal_{radius}m",
            unit="people",
            sources=("warsaw-population-2021",),
            method="Area-weighted sum of observed 125m cells within a radial catchment",
            limitation="2021 subtotal with coverage flag; no extrapolation or missing-cell zeros",
            radius_m=radius,
        )
        for radius in RADII_M
    )
    definitions.extend(
        PredictorDefinition(
            id=f"nearest_{category}_distance_m",
            unit="m",
            sources=("warsaw-osm-pois",),
            method="Straight-line distance to nearest deduplicated mapped point within 1000m",
            limitation="No point within search radius is censored, not zero or a walking route",
            radius_m=1000,
        )
        for category in ("bus_stop", "tram_stop", "rail_station", "metro_entrance")
    )
    definitions.extend(
        PredictorDefinition(
            id=f"{category}_count_500m",
            unit="count",
            sources=("warsaw-osm-pois",),
            method="Count of deduplicated mapped points within the radial catchment",
            limitation="Mapped supply, not transit frequency, parking capacity or availability",
            radius_m=500,
        )
        for category in ("bus_stop", "parking")
    )
    definitions.extend(
        PredictorDefinition(
            id=f"{category.replace('-', '_')}_count_500m",
            unit="count",
            sources=("official-franchise-locators", "warsaw-osm-pois"),
            method="Union of category-matched official and OSM venues after deduplication",
            limitation="Incomplete mapped competition; categories may overlap",
            radius_m=500,
            exclude_selected_building=True,
        )
        for category in ("grocery", "quick-service", "pizza", "beauty")
    )
    definitions.extend(
        PredictorDefinition(
            id=f"{category}_count_500m",
            unit="count",
            sources=("warsaw-osm-activity",),
            method="Count of deduplicated mapped activity features within radial catchment",
            limitation="Activity indicator, not measured visitors, employment or customer traffic",
            radius_m=500,
            exclude_selected_building=True,
        )
        for category in ("school", "university", "office", "retail_destination")
    )
    definitions.append(
        PredictorDefinition(
            id="residential_building_fraction_500m",
            unit="fraction",
            sources=("warsaw-buildings",),
            method="Residential count divided by count with known use, using interior points",
            limitation="Unclassified buildings excluded with a separate use-coverage flag",
            radius_m=500,
            exclude_selected_building=True,
        )
    )
    return tuple(definitions)


PREDICTORS = {item.id: item for item in planned_predictors()}
QUALITY_FIELDS = (
    "published_cell_area_fraction_300m",
    "published_cell_area_fraction_500m",
    "published_cell_area_fraction_1000m",
    "mapped_building_use_coverage_fraction_500m",
    "source_coverage_fraction_by_radius",
    "source_available_by_dataset",
    "source_age_days_by_dataset",
    "missingness_by_predictor",
    "nearest_distance_censored_by_category",
    "zero_or_missing_raw_storeys",
)
MISSING_PREMISES = (
    "usable_unit_area_m2",
    "rent_and_service_charges",
    "vacancy_and_lease_availability",
    "frontage_and_customer_entrance",
    "step_free_access",
    "utilities_and_ventilation",
    "permitted_unit_use",
)
EXCLUDED_TARGETS = ("revenue", "profit", "business_success", "closures")


class SourceEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str = Field(min_length=1)
    source_url: HttpUrl
    snapshot_version: str = Field(min_length=1)
    observed_at: date | AwareDatetime | None
    retrieved_at: AwareDatetime | None
    snapshot_at: AwareDatetime | None
    date_limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def explain_unknown_dates(self) -> "SourceEvidence":
        if (self.observed_at is None or self.retrieved_at is None) and not self.date_limitations:
            raise ValueError("Unknown source dates require an explicit limitation")
        return self


class FeatureObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    feature_id: str
    value: StrictFloat | StrictStr | None
    unit: str
    status: Literal[
        "available", "partial", "missing", "unavailable", "outside_coverage", "censored"
    ]
    reason: str | None = None
    coverage_fraction: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    sources: list[SourceEvidence]
    calculated_at: AwareDatetime
    contract_version: Literal["warsaw-data-v1"] = DATA_CONTRACT_VERSION

    @model_validator(mode="after")
    def preserve_missingness(self) -> "FeatureObservation":
        definition = PREDICTORS.get(self.feature_id)
        if definition is None or self.unit != definition.unit:
            raise ValueError("Feature identifier and unit must match the data contract")
        measured = self.status in {"available", "partial"}
        if measured != (self.value is not None):
            raise ValueError("Only available or partial observations may have a value")
        if measured and not self.sources:
            raise ValueError("Measured values require source evidence")
        allowed_sources = set(definition.sources)
        if "official-franchise-locators" in allowed_sources:
            allowed_sources.update(OFFICIAL_LOCATOR_SOURCES)
        if any(source.source_id not in allowed_sources for source in self.sources):
            raise ValueError("Source does not belong to this predictor's declared datasets")
        if self.status != "available" and not (self.reason or "").strip():
            raise ValueError("Incomplete or missing observations require a reason")
        if self.value is not None:
            if definition.unit == "category":
                if not isinstance(self.value, str) or not self.value.strip():
                    raise ValueError("Categorical values must be nonempty strings")
            elif not isinstance(self.value, float) or not 0 <= self.value < float("inf"):
                raise ValueError("Numeric values must be finite and nonnegative")
            elif definition.unit == "fraction" and self.value > 1:
                raise ValueError("Fractions cannot exceed one")
            if definition.unit in {"count", "storeys"} and not self.value.is_integer():
                raise ValueError("Mapped counts and storeys must be integers")
            if definition.unit in {"storeys", "m2"} and self.value <= 0:
                raise ValueError("Storeys and footprint areas must be positive")
            if definition.unit == "category" and self.value not in BUILDING_USE_CODES:
                raise ValueError("Building use must be a published EGiB classification code")
        if self.feature_id.startswith("resident_subtotal_") and measured:
            if self.coverage_fraction is None or self.coverage_fraction <= 0:
                raise ValueError("A population subtotal requires positive published-cell coverage")
            if self.status == "available" and self.coverage_fraction < 0.999:
                raise ValueError("Incomplete census coverage requires partial status")
        return self


def data_contract() -> dict:
    return {
        "version": DATA_CONTRACT_VERSION,
        "status": "definitions_only_not_computed",
        "radii_m": RADII_M,
        "origin": "ST_PointOnSurface of the selected building in EPSG:2180",
        "predictors": [item.model_dump() for item in PREDICTORS.values()],
        "quality_fields": QUALITY_FIELDS,
        "missing_premises": MISSING_PREMISES,
        "excluded_targets": EXCLUDED_TARGETS,
    }
