import argparse
import hashlib
import importlib.util
import json
import math
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from pyproj import Transformer

ROOT = Path(__file__).resolve().parents[2]
VERSION = "osm-activity-evidence-v2"
ENDPOINT = "https://overpass-api.de/api/interpreter"
REGIONS = {
    "warsaw": {
        "candidate_bounds": [20.85, 52.09, 21.29, 52.38],
        "previous_bounds": [20.82, 52.07, 21.32, 52.40],
    },
    "radom": {
        "candidate_bounds": [21.0531545892973, 51.3484959754452, 21.2593121086332, 51.485976468327],
        "previous_bounds": [21.03, 51.33, 21.28, 51.50],
    },
}
spec = importlib.util.spec_from_file_location(
    "frozen_activity_v1", Path(__file__).with_name("download_activity.py")
)
legacy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(legacy)


def acquisition_bounds(city):
    region = REGIONS[city]
    projected = Transformer.from_crs(4326, 2180, always_xy=True).transform_bounds(
        *region["candidate_bounds"], densify_pts=101
    )
    buffered = [projected[0] - 1000, projected[1] - 1000, projected[2] + 1000, projected[3] + 1000]
    west, south, east, north = Transformer.from_crs(2180, 4326, always_xy=True).transform_bounds(
        *buffered, densify_pts=101
    )
    previous = region["previous_bounds"]
    return [
        min(west, previous[0]),
        min(south, previous[1]),
        max(east, previous[2]),
        max(north, previous[3]),
    ]


def query_for(bounds):
    west, south, east, north = bounds
    return f"""[out:json][timeout:180][bbox:{south},{west},{north},{east}];
(
  nwr[amenity~"^(school|university|marketplace)$"];
  nwr[education~"^(school|university)$"];
  nwr[office][office!~"^(no|vacant|closed)$"];
  nwr[shop~"^(mall|department_store)$"];
);
out meta center;"""


def categories_for(tags):
    inactive = {"yes", "true", "1"}
    prefixes = (
        "closed",
        "disused",
        "abandoned",
        "demolished",
        "razed",
        "removed",
        "construction",
        "proposed",
    )
    if any(str(tags.get(key, "")).lower() in inactive for key in prefixes):
        return [], "inactive"
    if any(
        str(value).lower() not in {"", "no", "false", "0"}
        for key, value in tags.items()
        if any(
            key == f"{prefix}:{kind}"
            for prefix in prefixes
            for kind in ("amenity", "education", "office", "shop")
        )
    ):
        return [], "inactive_or_conflicting_lifecycle"
    categories = set(legacy.categories_for(tags))
    amenity, education = tags.get("amenity"), tags.get("education")
    if (
        amenity in {"school", "university"}
        and education in {"school", "university"}
        and amenity != education
    ):
        return sorted(categories - {"school", "university"}), "conflicting_education_tags"
    if education in {"school", "university"}:
        categories.add(education)
    return sorted(categories), "education_and_amenity" if education else "legacy_activity_tags"


def normalize(raw, acquisition, city):
    legacy.validate_raw(raw)
    bounds = acquisition["coverage_bounds"]
    seen, features, skipped, added = {}, [], Counter(), Counter()
    for element in raw["elements"]:
        kind, identifier = element.get("type"), element.get("id")
        if kind not in {"node", "way", "relation"} or type(identifier) is not int:
            raise ValueError("Unexpected OpenStreetMap identity")
        source_id = f"{kind}/{identifier}"
        if source_id in seen:
            if seen[source_id] != element:
                raise ValueError("Conflicting duplicate OpenStreetMap identity")
            skipped["repeated_source_id"] += 1
            continue
        seen[source_id] = element
        tags = element.get("tags", {})
        categories, classification = categories_for(tags)
        if not categories:
            skipped[classification] += 1
            continue
        point = element if kind == "node" else element.get("center", {})
        lon, lat = point.get("lon"), point.get("lat")
        if any(type(value) not in {float, int} or not math.isfinite(value) for value in (lon, lat)):
            skipped["invalid_coordinates"] += 1
            continue
        if not (bounds[0] <= lon <= bounds[2] and bounds[1] <= lat <= bounds[3]):
            skipped["representative_point_outside_bounds"] += 1
            continue
        added.update(set(categories) - set(legacy.categories_for(tags)))
        features.append(
            {
                "type": "Feature",
                "id": source_id,
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": {
                    "source_id": source_id,
                    "name": tags.get("name") or None,
                    "kind": "activity",
                    "categories": categories,
                    "brand_id": None,
                    "source_url": f"https://www.openstreetmap.org/{source_id}",
                    "updated_at": element.get("timestamp"),
                    "location_method": "node" if kind == "node" else f"{kind}_center",
                    "tags": tags,
                    "lifecycle_conflict": bool(tags.get("end_date")),
                    "classification_basis": classification,
                },
            }
        )
    if not features:
        raise ValueError("No valid activity records remain")
    features.sort(key=lambda row: row["id"])
    counts = Counter(
        category for feature in features for category in feature["properties"]["categories"]
    )
    metadata = {
        "id": f"{city}-osm-activity",
        "version": VERSION,
        "normalization_version": VERSION,
        "name": f"OpenStreetMap {city.title()} activity places",
        "url": "https://www.openstreetmap.org/copyright",
        "attribution": "© OpenStreetMap contributors",
        "license": "ODbL 1.0",
        "retrieved_at": acquisition["retrieved_at"],
        "observed_at": None,
        "snapshot_at": acquisition["snapshot_at"],
        "coverage_bounds": bounds,
        "candidate_bounds": REGIONS[city]["candidate_bounds"],
        "minimum_acquisition_halo_m": 1000,
        "query": acquisition["query"],
        "query_endpoint": acquisition["endpoint"],
        "raw_sha256": acquisition["sha256"],
        "feature_count": len(features),
        "category_counts": dict(sorted(counts.items())),
        "additional_category_memberships_from_education_tags": dict(sorted(added.items())),
        "skipped_counts": dict(sorted(skipped.items())),
        "legacy_normalizer_sha256": hashlib.sha256(Path(legacy.__file__).read_bytes()).hexdigest(),
        "coordinate_reference_system": "EPSG:4326",
        "coordinate_order": "longitude,latitude",
        "definition_sources": [
            "https://wiki.openstreetmap.org/wiki/Key:education",
            "https://wiki.openstreetmap.org/wiki/Key:office",
            "https://wiki.openstreetmap.org/wiki/Lifecycle_prefix",
        ],
        "notes": [
            "Mapped activity indicators do not measure footfall or inventory completeness.",
            "Education and amenity tags are supported; conflicting education tags are withheld.",
            "Each OSM identity contributes once per category; entity deduplication is separate.",
            "Area centres are not verified entrances; source bounds do not imply completeness.",
            "The acquisition envelope includes a minimum 1 km halo around candidate bounds.",
            "An edit timestamp does not verify current operation; end_date remains flagged.",
        ],
    }
    return {"type": "FeatureCollection", "features": features}, metadata


def collect(city, output, previous=None):
    if output.exists():
        raise ValueError("Activity v2 output must not already exist")
    if not output.resolve().is_relative_to((ROOT / ".local/data/coverage-v2").resolve()):
        raise ValueError("Activity enrichment must remain in isolated coverage-v2 storage")
    bounds = acquisition_bounds(city)
    query = query_for(bounds)
    request = Request(
        ENDPOINT,
        data=urlencode({"data": query}).encode(),
        headers={
            "User-Agent": "MasterFranchises/coverage-v2",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with urlopen(request, timeout=240) as response:
        content = response.read()
    raw = json.loads(content)
    snapshot_at = raw.get("osm3s", {}).get("timestamp_osm_base")
    if not isinstance(snapshot_at, str) or datetime.fromisoformat(snapshot_at).tzinfo is None:
        raise ValueError("OpenStreetMap snapshot requires a timestamp")
    acquisition = {
        "version": VERSION,
        "endpoint": ENDPOINT,
        "query": query,
        "coverage_bounds": bounds,
        "retrieved_at": datetime.now(UTC).isoformat(),
        "snapshot_at": snapshot_at,
        "sha256": hashlib.sha256(content).hexdigest(),
    }
    collection, metadata = normalize(raw, acquisition, city)
    previous_rows = json.loads(previous.read_text(encoding="utf-8"))["features"] if previous else []
    previous_ids = {row["id"] for row in previous_rows}
    current_ids = {row["id"] for row in collection["features"]}
    audit = {
        "version": VERSION,
        "city": city,
        "previous_records": len(previous_ids) if previous else None,
        "current_records": len(current_ids),
        "added_source_ids": sorted(current_ids - previous_ids) if previous else [],
        "removed_source_ids": sorted(previous_ids - current_ids) if previous else [],
        "additional_category_memberships_from_education_tags": metadata[
            "additional_category_memberships_from_education_tags"
        ],
        "comparison_note": (
            "Snapshot changes include source edits, expanded tags and lifecycle filtering; "
            "no model-quality improvement is claimed."
        ),
        "previous_sha256": hashlib.sha256(previous.read_bytes()).hexdigest() if previous else None,
        "models_trained": False,
        "models_scored": False,
    }
    output.mkdir(parents=True, exist_ok=False)
    (output / "activity.raw.json").write_bytes(content)
    legacy.write_json(output / "activity.raw.metadata.json", acquisition)
    legacy.write_json(output / "activity.geojson", collection)
    metadata["geojson_sha256"] = hashlib.sha256(
        (output / "activity.geojson").read_bytes()
    ).hexdigest()
    legacy.write_json(output / "activity.metadata.json", metadata)
    legacy.write_json(output / "coverage-audit.json", audit)
    legacy.write_json(
        output / "manifest.json",
        {
            "version": VERSION,
            "artifacts": {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted(output.iterdir())
            },
        },
    )
    return audit


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--city", choices=sorted(REGIONS), required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--previous", type=Path)
    arguments = parser.parse_args()
    audit = collect(arguments.city, arguments.output_dir, arguments.previous)
    print(
        json.dumps({key: value for key, value in audit.items() if not key.endswith("source_ids")})
    )


if __name__ == "__main__":
    main()
