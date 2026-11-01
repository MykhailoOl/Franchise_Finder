import argparse
import hashlib
import json
import math
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import ijson

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_URL = "https://wms2.um.warszawa.pl/geoserver/wfs/wfs"
DOWNLOAD_URL = (
    SOURCE_URL
    + "?"
    + urlencode(
        {
            "service": "WFS",
            "version": "2.0.0",
            "request": "GetFeature",
            "typeNames": "wfs:budynki",
            "outputFormat": "application/json",
            "srsName": "EPSG:4326",
            "count": "500000",
            "sortBy": "ID_BUDYNKU",
        }
    )
)


def download(destination: Path) -> str:
    for attempt in range(3):
        digest = hashlib.sha256()
        try:
            request = Request(
                DOWNLOAD_URL,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "MasterFranchises/0.1 local-Warsaw-data-import",
                },
            )
            with urlopen(request, timeout=180) as response, destination.open("wb") as output:
                for chunk in iter(lambda: response.read(1024 * 1024), b""):
                    output.write(chunk)
                    digest.update(chunk)
            return digest.hexdigest()
        except (HTTPError, URLError, TimeoutError, OSError):
            if attempt == 2:
                raise
            time.sleep(2**attempt)
    raise RuntimeError("Download did not complete")


def validate(path: Path) -> dict:
    identifiers = set()
    bounds = [180.0, 90.0, -180.0, -90.0]
    count = 0
    with path.open("rb") as stream:
        for feature in ijson.items(stream, "features.item", use_float=True):
            properties = feature.get("properties") or {}
            identifier = properties.get("ID_BUDYNKU")
            if not isinstance(identifier, str) or not identifier.startswith("1465"):
                raise ValueError("Missing or unexpected Warsaw building identifier")
            if identifier in identifiers:
                raise ValueError(f"Duplicate building identifier: {identifier}")
            identifiers.add(identifier)
            geometry = feature.get("geometry") or {}
            if geometry.get("type") != "MultiPolygon" or not geometry.get("coordinates"):
                raise ValueError(f"Missing MultiPolygon geometry: {identifier}")
            for polygon in geometry["coordinates"]:
                if not polygon:
                    raise ValueError(f"Empty polygon: {identifier}")
                for ring in polygon:
                    if len(ring) < 4 or ring[0] != ring[-1]:
                        raise ValueError(f"Invalid polygon ring: {identifier}")
                    for position in ring:
                        if len(position) < 2:
                            raise ValueError(f"Invalid coordinate: {identifier}")
                        longitude, latitude = position[:2]
                        if not all(math.isfinite(value) for value in (longitude, latitude)):
                            raise ValueError(f"Non-finite coordinate: {identifier}")
                        if not (20.7 < longitude < 21.4 and 52.0 < latitude < 52.5):
                            raise ValueError(f"Coordinate outside Warsaw envelope: {identifier}")
                        bounds[0] = min(bounds[0], longitude)
                        bounds[1] = min(bounds[1], latitude)
                        bounds[2] = max(bounds[2], longitude)
                        bounds[3] = max(bounds[3], latitude)
            count += 1
    summary = {}
    with path.open("rb") as stream:
        for prefix, event, value in ijson.parse(stream, use_float=True):
            if prefix in {"type", "numberMatched", "numberReturned", "timeStamp"} and event in {
                "string",
                "number",
            }:
                summary[prefix] = value
    if summary.get("type") != "FeatureCollection" or not count:
        raise ValueError("The source did not return a nonempty FeatureCollection")
    for field in ("numberMatched", "numberReturned"):
        if field not in summary or int(summary[field]) != count:
            raise ValueError(f"Incomplete dataset: {count} features, {field}={summary.get(field)}")
    return {
        "feature_count": count,
        "bounds": bounds,
        "source_response_timestamp": summary.get("timeStamp"),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output", type=Path, default=PROJECT_ROOT / ".local/data/warsaw-buildings.geojson"
    )
    parser.add_argument("--verify-existing", action="store_true")
    arguments = parser.parse_args()
    output = arguments.output.resolve()
    if arguments.verify_existing:
        print(json.dumps(validate(output), ensure_ascii=False, indent=2))
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_suffix(output.suffix + ".part")
    print("Downloading Warsaw cadastral building footprints from the city WFS...")
    digest = download(partial)
    summary = validate(partial)
    metadata = {
        "name": "Warsaw city cadastral buildings (EGiB)",
        "url": SOURCE_URL,
        "download_url": DOWNLOAD_URL,
        "attribution": "Building footprints: City of Warsaw, EGiB",
        "retrieved_at": datetime.now(UTC).isoformat(),
        "source_updated_at": None,
        "sha256": digest,
        "bytes": partial.stat().st_size,
        **summary,
    }
    partial.replace(output)
    output.with_suffix(".metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(metadata, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
