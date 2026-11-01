import argparse
import hashlib
import json
import math
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / ".local/data/suitability"
BOUNDS = [20.82, 52.07, 21.32, 52.40]
ENDPOINT = "https://overpass-api.de/api/interpreter"
QUERY = """[out:json][timeout:180][bbox:52.07,20.82,52.40,21.32];
(
  nwr[amenity~"^(school|university|marketplace)$"];
  nwr[office][office!~"^(no|vacant|closed)$"];
  nwr[shop~"^(mall|department_store)$"];
);
out meta center;"""
CATEGORY_DEFINITIONS = {
    "school": {"amenity": ["school"]},
    "university": {"amenity": ["university"]},
    "office": {"office": "present and not an inactive value"},
    "retail_destination": {
        "shop": ["mall", "department_store"],
        "amenity": ["marketplace"],
    },
}
INACTIVE_VALUES = {
    "no",
    "vacant",
    "closed",
    "disused",
    "abandoned",
    "construction",
    "proposed",
}


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def validate_raw(raw: dict) -> None:
    if raw.get("remark") or not isinstance(raw.get("elements"), list):
        raise ValueError(f"Incomplete Overpass response: {raw.get('remark', 'missing elements')}")
    if not raw["elements"]:
        raise ValueError("Refusing an empty activity snapshot")


def acquire(output: Path, endpoint: str, refresh: bool) -> tuple[dict, dict]:
    output.mkdir(parents=True, exist_ok=True)
    raw_path = output / "activity.raw.json"
    metadata_path = output / "activity.raw.metadata.json"
    if raw_path.exists() and metadata_path.exists() and not refresh:
        content = raw_path.read_bytes()
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if hashlib.sha256(content).hexdigest() != metadata["sha256"]:
            raise ValueError("Cached activity snapshot checksum differs; use --refresh")
        if metadata["query"] != QUERY or metadata["coverage_bounds"] != BOUNDS:
            raise ValueError("Cached activity selection differs; use --refresh")
        raw = json.loads(content)
        validate_raw(raw)
        return raw, metadata
    request = Request(
        endpoint + "?" + urlencode({"data": QUERY}),
        headers={"User-Agent": "MasterFranchises/0.1 (public Warsaw activity research)"},
    )
    with urlopen(request, timeout=240) as response:
        content = response.read()
    raw = json.loads(content)
    validate_raw(raw)
    metadata = {
        "endpoint": endpoint,
        "query": QUERY,
        "retrieved_at": datetime.now(UTC).isoformat(),
        "snapshot_at": raw.get("osm3s", {}).get("timestamp_osm_base"),
        "sha256": hashlib.sha256(content).hexdigest(),
        "coverage_bounds": BOUNDS,
    }
    temporary = raw_path.with_suffix(".json.part")
    temporary.write_bytes(content)
    temporary.replace(raw_path)
    write_json(metadata_path, metadata)
    return raw, metadata


def categories_for(tags: dict) -> list[str]:
    if any(
        tags.get(key) == "yes"
        for key in (
            "disused",
            "abandoned",
            "demolished",
            "closed",
            "construction",
            "proposed",
        )
    ):
        return []
    categories = []
    if tags.get("amenity") == "school":
        categories.append("school")
    if tags.get("amenity") == "university":
        categories.append("university")
    if tags.get("office") and tags["office"] not in INACTIVE_VALUES:
        categories.append("office")
    if tags.get("shop") in {"mall", "department_store"} or tags.get("amenity") == "marketplace":
        categories.append("retail_destination")
    return sorted(categories)


def normalize(raw: dict, acquisition: dict) -> tuple[dict, dict]:
    validate_raw(raw)
    features = []
    counts = Counter()
    geometry_methods = Counter()
    skipped = Counter()
    seen = {}
    lifecycle_conflicts = []
    for element in raw["elements"]:
        element_type = element.get("type")
        if element_type not in {"node", "way", "relation"} or not isinstance(
            element.get("id"), int
        ):
            raise ValueError("Unexpected OSM element identity")
        source_id = f"{element_type}/{element['id']}"
        if source_id in seen:
            if seen[source_id] != element:
                raise ValueError(f"Conflicting duplicate OSM record: {source_id}")
            skipped["repeated_source_id"] += 1
            continue
        seen[source_id] = element
        tags = element.get("tags", {})
        categories = categories_for(tags)
        if not categories:
            skipped["inactive_or_unclassified"] += 1
            continue
        point = element if element_type == "node" else element.get("center", {})
        lon, lat = point.get("lon"), point.get("lat")
        if not all(
            isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
            for value in (lon, lat)
        ):
            skipped["invalid_coordinates"] += 1
            continue
        if not (BOUNDS[0] <= lon <= BOUNDS[2] and BOUNDS[1] <= lat <= BOUNDS[3]):
            skipped["representative_point_outside_bounds"] += 1
            continue
        lifecycle_conflict = bool(tags.get("end_date"))
        if lifecycle_conflict:
            lifecycle_conflicts.append(source_id)
        location_method = "node" if element_type == "node" else f"{element_type}_center"
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
                    "location_method": location_method,
                    "tags": tags,
                    "lifecycle_conflict": lifecycle_conflict,
                },
            }
        )
        counts.update(categories)
        geometry_methods[location_method] += 1
    if not features:
        raise ValueError("No valid activity records remain after normalization")
    features.sort(key=lambda feature: feature["id"])
    metadata = {
        "id": "warsaw-osm-activity",
        "name": "OpenStreetMap Warsaw schools, universities, offices and retail destinations",
        "url": "https://www.openstreetmap.org/copyright",
        "attribution": "© OpenStreetMap contributors",
        "license": "ODbL 1.0",
        "retrieved_at": acquisition["retrieved_at"],
        "observed_at": None,
        "snapshot_at": acquisition["snapshot_at"],
        "coverage_bounds": BOUNDS,
        "query_endpoint": acquisition["endpoint"],
        "query": QUERY,
        "raw_sha256": acquisition["sha256"],
        "coordinate_reference_system": "EPSG:4326",
        "coordinate_order": "longitude,latitude",
        "raw_element_count": len(raw["elements"]),
        "feature_count": len(features),
        "category_counts": dict(counts),
        "geometry_method_counts": dict(geometry_methods),
        "skipped_counts": dict(skipped),
        "lifecycle_conflict_count": len(lifecycle_conflicts),
        "lifecycle_conflict_source_ids": lifecycle_conflicts,
        "category_definitions": CATEGORY_DEFINITIONS,
        "definition_sources": [
            "https://wiki.openstreetmap.org/wiki/Tag:amenity=school",
            "https://wiki.openstreetmap.org/wiki/Tag:amenity=university",
            "https://wiki.openstreetmap.org/wiki/Key:office",
            "https://wiki.openstreetmap.org/wiki/Tag:shop=mall",
            "https://wiki.openstreetmap.org/wiki/Tag:shop=department_store",
            "https://wiki.openstreetmap.org/wiki/Tag:amenity=marketplace",
            "https://wiki.openstreetmap.org/wiki/Key:end_date",
        ],
        "notes": [
            "This is a bounded public OSM extract, not a complete register of institutions.",
            "Counts describe mapped activity records, not students, employees, "
            "visitors or footfall.",
            "Schools exclude kindergartens and training centres; universities may map campuses.",
            "Office tags include professional, administrative, government and nonprofit offices.",
            (
                "Retail destinations mean malls, department stores and marketplaces; individual "
                "shops and broad retail land-use parcels are not added to this category."
            ),
            "A node location or area bounding-box centre is not a verified entrance "
            "or building link.",
            "An OSM edit timestamp does not establish that an institution is currently operating.",
            (
                "Different OSM objects may represent one institution or different campuses. "
                "Source IDs are deduplicated here; importer entity heuristics remain separate."
            ),
            "Query bounds constrain mapped objects; area centres outside the bounds are excluded.",
            "Unmapped activity is unknown; missing records must not be interpreted as no activity.",
            (
                "An end_date with an active activity tag is retained with lifecycle_conflict=true. "
                "The tag may describe an ended feature or another historical event; it is not "
                "sufficient on its own to remove a mapped institution."
            ),
        ],
    }
    collection = {
        "type": "FeatureCollection",
        "numberReturned": len(features),
        "features": features,
    }
    return collection, metadata


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--endpoint", default=ENDPOINT)
    parser.add_argument("--refresh", action="store_true")
    arguments = parser.parse_args()
    raw, acquisition = acquire(arguments.output_dir, arguments.endpoint, arguments.refresh)
    collection, metadata = normalize(raw, acquisition)
    destination = arguments.output_dir / "activity.geojson"
    write_json(destination, collection)
    metadata["geojson_sha256"] = hashlib.sha256(destination.read_bytes()).hexdigest()
    write_json(arguments.output_dir / "activity.metadata.json", metadata)
    print(
        json.dumps(
            {
                key: metadata[key]
                for key in (
                    "raw_element_count",
                    "feature_count",
                    "category_counts",
                    "skipped_counts",
                    "snapshot_at",
                )
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
