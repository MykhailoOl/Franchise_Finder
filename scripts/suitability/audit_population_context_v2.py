import argparse
import hashlib
import json
import math
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from pyproj import Transformer

VERSION = "population-context-coverage-audit-v2"
ROOT = Path(__file__).resolve().parents[2]


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def records(path):
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            yield json.loads(line)


def audit(population_path, cohort_path, features_path):
    snapshot = json.loads(population_path.read_text(encoding="utf-8"))
    if snapshot["version"] != "gus-population-context-v2":
        raise ValueError("Audit requires the separate versioned population context")
    cells = {(row["easting"], row["northing"]): row for row in snapshot["cells"]}
    if len(cells) != len(snapshot["cells"]):
        raise ValueError("Duplicate population cell identity")
    transform = Transformer.from_crs(2180, 3035, always_xy=True)
    memberships = {}
    for row in records(cohort_path):
        identifier = row["building_source_id"]
        if identifier in memberships:
            raise ValueError("Duplicate building in coverage cohort")
        x, y = transform.transform(row["x"], row["y"])
        memberships[identifier] = cells.get((math.floor(x / 500) * 500, math.floor(y / 500) * 500))
    before = {str(radius): Counter() for radius in (300, 500, 1000)}
    extra = Counter()
    rows_seen = set()
    for row in records(features_path):
        identifier = row["building_source_id"]
        if identifier not in memberships or identifier in rows_seen:
            raise ValueError("Feature and cohort building identities differ")
        rows_seen.add(identifier)
        for radius, counts in before.items():
            counts[row["observations"][f"resident_subtotal_{radius}m"]["status"]] += 1
        cell = memberships[identifier]
        if cell is None or cell["status"] != "observed":
            extra["unavailable"] += 1
        elif cell["population"] == 0:
            extra["observed_zero"] += 1
        else:
            extra["observed_positive"] += 1
    if rows_seen != set(memberships):
        raise ValueError("Coverage audit must use the complete supplied cohort")
    return {
        "version": VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "rows": len(rows_seen),
        "city": snapshot["source"]["city"],
        "existing_125m_radial_estimate_status_counts": {
            key: dict(value) for key, value in before.items()
        },
        "additional_500m_containing_cell_status_counts": dict(extra),
        "new_spatial_scope": "containing_census_cell",
        "original_125m_values_changed": False,
        "coarse_cells_used_to_fill_fine_gaps": False,
        "models_trained": False,
        "models_scored": False,
        "limitation": "Coarser context does not make a partial radial estimate complete.",
        "inputs": {
            "population_sha256": digest(population_path),
            "cohort_sha256": digest(cohort_path),
            "features_sha256": digest(features_path),
        },
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--population", type=Path, required=True)
    parser.add_argument("--cohort", type=Path, required=True)
    parser.add_argument("--features", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.output.resolve().is_relative_to((ROOT / ".local/data/coverage-v2").resolve()):
        raise ValueError("Development audit must remain in separate ignored coverage-v2 storage")
    result = audit(args.population, args.cohort, args.features)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
