from typing import Any, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    field_validator,
    model_validator,
)

from app.core.address_search import fold_polish


class SnapshotModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CategoryRecord(SnapshotModel):
    id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=64)
    name: str = Field(min_length=1, max_length=120)


class BrandRecord(CategoryRecord):
    category_id: str
    website: HttpUrl
    source_url: HttpUrl
    retrieved_at: AwareDatetime
    coverage_status: Literal["complete", "partial", "unavailable"]
    coverage_note: str = Field(min_length=1, max_length=5000)


class OutletAddress(SnapshotModel):
    raw: str = Field(min_length=1, max_length=2000)
    street: str | None = Field(default=None, max_length=300)
    house_number: str | None = Field(default=None, max_length=100)
    postal_code: str | None = Field(default=None, max_length=30)
    city: str

    @field_validator("city")
    @classmethod
    def require_warsaw(cls, value: str) -> str:
        if fold_polish(value.strip()) not in ("warszawa", "warsaw"):
            raise ValueError("Only explicitly identified Warsaw outlets belong in this snapshot")
        return "Warszawa"


class OutletRecord(SnapshotModel):
    source_id: str = Field(min_length=1, max_length=300)
    brand_id: str
    name: str | None = Field(default=None, max_length=500)
    address: OutletAddress
    coordinates: tuple[float, float] | None = None
    source_url: HttpUrl
    retrieved_at: AwareDatetime
    status: Literal["listed", "open", "temporarily_closed", "unknown"] = "listed"
    operating_model: Literal["unknown", "franchise", "company_owned"] = "unknown"
    operating_model_source_url: HttpUrl | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)

    @field_validator("coordinates")
    @classmethod
    def validate_coordinates(cls, value: tuple[float, float] | None) -> tuple[float, float] | None:
        if value is not None and not (-180 <= value[0] <= 180 and -85 <= value[1] <= 85):
            raise ValueError("Coordinates must be finite longitude, latitude in EPSG:4326")
        return value

    @model_validator(mode="after")
    def require_operating_model_evidence(self) -> "OutletRecord":
        if self.operating_model != "unknown" and self.operating_model_source_url is None:
            raise ValueError("A known outlet operating model requires outlet-specific evidence")
        return self


class FranchiseSnapshot(SnapshotModel):
    schema_version: Literal[1]
    categories: list[CategoryRecord] = Field(min_length=1, max_length=20)
    brands: list[BrandRecord] = Field(min_length=1, max_length=50)
    outlets: list[OutletRecord] = Field(max_length=20_000)
    provenance: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_references(self) -> "FranchiseSnapshot":
        categories = {category.id for category in self.categories}
        brands = {brand.id for brand in self.brands}
        if len(categories) != len(self.categories) or len(brands) != len(self.brands):
            raise ValueError("Category and brand IDs must be unique within the snapshot")
        if any(brand.category_id not in categories for brand in self.brands):
            raise ValueError("Every brand must reference a category included in the snapshot")
        if any(outlet.brand_id not in brands for outlet in self.outlets):
            raise ValueError("Every outlet must reference a brand included in the snapshot")
        identities = {(outlet.brand_id, outlet.source_id) for outlet in self.outlets}
        if len(identities) != len(self.outlets):
            raise ValueError("Outlet source IDs must be unique within their brand")
        for brand in self.brands:
            if brand.coverage_status == "unavailable" and any(
                outlet.brand_id == brand.id for outlet in self.outlets
            ):
                raise ValueError("An unavailable inventory cannot contain listed outlets")
        return self
