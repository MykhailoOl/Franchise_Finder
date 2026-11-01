from datetime import datetime
from statistics import median
from typing import Any

AUDIT_VERSION = "data-audit-v1"


def integrity_check(identifier: str, violations: int, detail: str) -> dict[str, Any]:
    return {
        "id": identifier,
        "status": "pass" if violations == 0 else "fail",
        "violations": violations,
        "detail": detail,
    }


def summarize_population_sample(rows: list[dict]) -> list[dict]:
    summaries = []
    for radius in (300, 500, 1000):
        selected = [row for row in rows if row["radius_m"] == radius]
        if not selected:
            continue
        coverage = [row["published_cell_area_fraction"] for row in selected]
        summaries.append(
            {
                "radius_m": radius,
                "sampled_buildings": len(selected),
                "published_cell_area_fraction_min": min(coverage),
                "published_cell_area_fraction_median": median(coverage),
                "published_cell_area_fraction_max": max(coverage),
                "no_published_cells": sum(row["population_subtotal"] is None for row in selected),
                "explicit_zero_subtotals": sum(row["population_subtotal"] == 0 for row in selected),
                "interpretation": "Published-row area fraction, not resident completeness",
            }
        )
    return summaries


def source_date_issues(metadata: dict) -> list[str]:
    issues = []
    parsed = {}
    for field in ("retrieved_at", "snapshot_at", "observed_at"):
        value = metadata.get(field)
        if value is None:
            continue
        try:
            parsed[field] = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            issues.append(f"{field} is not an ISO date or timestamp")
    if not metadata.get("retrieved_at"):
        issues.append("Retrieval date is unknown; import time is not a substitute")
    retrieved = parsed.get("retrieved_at")
    for field in ("snapshot_at", "observed_at"):
        value = parsed.get(field)
        if value and retrieved:
            if value.date() > retrieved.date():
                issues.append(f"{field} is later than retrieval")
            elif value.tzinfo and retrieved.tzinfo and value > retrieved:
                issues.append(f"{field} is later than retrieval")
    return issues
