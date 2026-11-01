import argparse
import hashlib
import json
import math
import re
import unicodedata
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / ".local/data/profile"
BOUNDS = [20.82, 52.07, 21.32, 52.40]
ENDPOINT = "https://overpass-api.de/api/interpreter"
QUERY = """[out:json][timeout:180][bbox:52.07,20.82,52.40,21.32];
(
  nwr[shop~"^(convenience|supermarket|grocery|beauty)$"];
  nwr[amenity=fast_food];
  nwr[amenity=restaurant][cuisine~"(^|;)pizza(;|$)"];
  nwr[amenity=beauty_salon];
  nwr[highway=bus_stop];
  nwr[public_transport=platform];
  nwr[railway~"^(tram_stop|station|halt|subway_entrance)$"];
  nwr[amenity=parking][access!~"^(private|no)$"];
);
out meta center;"""


def get_raw(endpoint: str, refresh: bool, method: str = "GET") -> tuple[dict, dict]:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    raw_path = OUTPUT / "osm-pois.raw.json"
    metadata_path = OUTPUT / "osm-pois.raw.metadata.json"
    if not refresh and raw_path.exists() and metadata_path.exists():
        content = raw_path.read_bytes()
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if hashlib.sha256(content).hexdigest() != metadata["sha256"]:
            raise ValueError(
                "Cached OSM data does not match its checksum; use --refresh"
            )
        if metadata["query"] != QUERY or metadata["coverage_bounds"] != BOUNDS:
            raise ValueError("Cached OSM query differs from this script; use --refresh")
        return json.loads(content), metadata
    parameters = urlencode({"data": QUERY})
    request = Request(
        f"{endpoint}?{parameters}" if method == "GET" else endpoint,
        data=parameters.encode() if method == "POST" else None,
        headers={
            "User-Agent": "MasterFranchises/0.1 (Warsaw public-data research)",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with urlopen(request, timeout=240) as response:
        content = response.read()
    raw = json.loads(content)
    if raw.get("remark") or not isinstance(raw.get("elements"), list):
        raise ValueError(
            f"Incomplete Overpass result: {raw.get('remark', 'missing elements')}"
        )
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


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def normalize_name(value: str) -> str:
    return re.sub(
        r"[^a-z0-9]",
        "",
        unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower(),
    )


def brand_id(tags: dict) -> str | None:
    aliases = {
        "zabka": "zabka",
        "lewiatan": "lewiatan",
        "mcdonalds": "mcdonalds",
        "subway": "subway",
        "dagrasso": "da-grasso",
        "dominospizza": "dominos",
        "dominos": "dominos",
        "yasumi": "yasumi",
        "depilconcept": "depilconcept",
    }
    for field in ("brand", "name"):
        name = normalize_name(tags.get(field, ""))
        if name in aliases:
            return aliases[name]
    return None


def classify(tags: dict) -> tuple[str, list[str]] | None:
    if any(
        tags.get(key) == "yes"
        for key in ("disused", "abandoned", "demolished", "closed")
    ):
        return None
    categories = []
    if tags.get("shop") in {"convenience", "supermarket", "grocery"}:
        categories.append("grocery")
    if tags.get("amenity") == "fast_food":
        categories.append("quick-service")
    cuisine = {part.strip().lower() for part in tags.get("cuisine", "").split(";")}
    if "pizza" in cuisine and tags.get("amenity") in {"fast_food", "restaurant"}:
        categories.append("pizza")
    if tags.get("shop") == "beauty" or tags.get("amenity") == "beauty_salon":
        categories.append("beauty")
    if categories:
        return "competitor", categories
    if tags.get("highway") == "bus_stop" or (
        tags.get("public_transport") == "platform" and tags.get("bus") == "yes"
    ):
        categories.append("bus_stop")
    if tags.get("railway") == "tram_stop" or (
        tags.get("public_transport") == "platform" and tags.get("tram") == "yes"
    ):
        categories.append("tram_stop")
    if tags.get("railway") in {"station", "halt"}:
        categories.append("rail_station")
    if tags.get("railway") == "subway_entrance":
        categories.append("metro_entrance")
    if tags.get("amenity") == "parking" and tags.get("access") not in {"private", "no"}:
        categories.append("parking")
    return ("access", categories) if categories else None


def normalize(raw: dict, metadata: dict) -> tuple[dict, dict]:
    features = []
    counts = Counter()
    seen = set()
    for element in raw["elements"]:
        tags = element.get("tags", {})
        classification = classify(tags)
        if classification is None:
            continue
        point = element if element["type"] == "node" else element.get("center", {})
        lon, lat = point.get("lon"), point.get("lat")
        if not all(
            isinstance(value, (int, float)) and math.isfinite(value)
            for value in (lon, lat)
        ):
            continue
        if not (BOUNDS[0] <= lon <= BOUNDS[2] and BOUNDS[1] <= lat <= BOUNDS[3]):
            continue
        source_id = f"{element['type']}/{element['id']}"
        if source_id in seen:
            continue
        seen.add(source_id)
        kind, categories = classification
        counts.update(categories)
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": {
                    "source_id": source_id,
                    "name": tags.get("name") or tags.get("brand") or None,
                    "kind": kind,
                    "categories": categories,
                    "brand_id": brand_id(tags) if kind == "competitor" else None,
                    "source_url": f"https://www.openstreetmap.org/{source_id}",
                    "updated_at": element.get("timestamp"),
                    "tags": tags,
                    "location_method": "node"
                    if element["type"] == "node"
                    else f"{element['type']}_center",
                },
            }
        )
    normalized_metadata = {
        "id": "warsaw-osm-pois",
        "name": "OpenStreetMap Warsaw businesses and access points",
        "url": "https://www.openstreetmap.org/copyright",
        "attribution": "© OpenStreetMap contributors",
        "license": "ODbL 1.0",
        "retrieved_at": metadata["retrieved_at"],
        "observed_at": None,
        "snapshot_at": metadata["snapshot_at"],
        "coverage_bounds": BOUNDS,
        "query_endpoint": metadata["endpoint"],
        "raw_sha256": metadata["sha256"],
        "query": QUERY,
        "category_counts": dict(counts),
        "feature_count": len(features),
        "notes": [
            "A bounded public OSM extract, not a complete business or transport census.",
            "Snapshot time describes the OSM database; object updated_at is last edit, not an on-site survey date.",
            "Way and relation positions are bounding-box centres; they are not verified entrances.",
            "Grocery covers convenience, supermarket and grocery shops; beauty covers beauty salons, not hairdressers alone.",
            "Fast-food venues serving pizza can appear in both categories; category totals must not be added together.",
            "Access distances describe mapped points, not routes, walking time, service frequency or passenger volume.",
            "Parking excludes explicitly private/no access; other restrictions and available capacity are not verified.",
            "Same-entity and cross-source duplicate handling is applied by the profile importer/query and disclosed there.",
        ],
    }
    return {"type": "FeatureCollection", "features": features}, normalized_metadata


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", default=ENDPOINT)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--method", choices=["GET", "POST"], default="GET")
    args = parser.parse_args()
    raw, metadata = get_raw(args.endpoint, args.refresh, args.method)
    data, normalized_metadata = normalize(raw, metadata)
    write_json(OUTPUT / "pois.geojson", data)
    write_json(OUTPUT / "pois.metadata.json", normalized_metadata)
    print(
        json.dumps(
            {
                "elements": len(raw["elements"]),
                "snapshot_at": metadata["snapshot_at"],
                "features": len(data["features"]),
                "categories": normalized_metadata["category_counts"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
