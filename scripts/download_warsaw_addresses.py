import argparse
import hashlib
import json
import math
import re
import time
from collections import Counter
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
            "typeNames": "wfs:punkty_adresowe",
            "outputFormat": "application/json",
            "srsName": "EPSG:4326",
            "count": "500000",
            "sortBy": "ID_IIP",
        }
    )
)
IDENTIFIER_PATTERN = re.compile(
    r"^(PL\.ZIPIN\.1469\.EMUiA\."
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.(.+)$"
)


def download(destination: Path) -> None:
    for attempt in range(3):
        try:
            request = Request(
                DOWNLOAD_URL,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "MasterFranchises/0.1 local-Warsaw-address-import",
                },
            )
            with urlopen(request, timeout=180) as response, destination.open("wb") as output:
                for chunk in iter(lambda: response.read(1024 * 1024), b""):
                    output.write(chunk)
            return
        except (HTTPError, URLError, TimeoutError, OSError):
            if attempt == 2:
                raise
            time.sleep(2**attempt)
    raise RuntimeError("Download did not complete")


def validate(path: Path) -> dict:
    identifiers = set()
    stable_identifiers = set()
    street_numbers = Counter()
    full_addresses = Counter()
    null_fields = Counter()
    street_prefixes = Counter()
    bounds = [180.0, 90.0, -180.0, -90.0]
    count = 0
    with path.open("rb") as stream:
        for feature in ijson.items(stream, "features.item", use_float=True):
            properties = feature.get("properties") or {}
            identifier = properties.get("ID_IIP")
            match = IDENTIFIER_PATTERN.fullmatch(identifier or "")
            if not match:
                raise ValueError("Missing or unexpected Warsaw address identifier")
            stable_identifier = match.group(1)
            if identifier in identifiers or stable_identifier in stable_identifiers:
                raise ValueError(f"Duplicate address identifier: {identifier}")
            identifiers.add(identifier)
            stable_identifiers.add(stable_identifier)
            if (
                properties.get("ID_GMINY") != "1465011"
                or properties.get("NAZWA_MIEJSCOWOSCI") != "Warszawa"
            ):
                raise ValueError(f"Unexpected municipality or locality: {identifier}")
            for field in ("NAZWA_ULICY", "NUMER_PORZADKOWY", "KOD_POCZTOWY"):
                value = properties.get(field)
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"Missing address component {field}: {identifier}")
            geometry = feature.get("geometry") or {}
            position = geometry.get("coordinates")
            if geometry.get("type") != "Point" or not isinstance(position, list):
                raise ValueError(f"Missing Point geometry: {identifier}")
            if len(position) != 2:
                raise ValueError(f"Expected a two-dimensional point: {identifier}")
            if not all(
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
                for value in position
            ):
                raise ValueError(f"Invalid coordinate: {identifier}")
            longitude, latitude = position
            if not (20.7 < longitude < 21.4 and 52.0 < latitude < 52.5):
                raise ValueError(f"Coordinate outside Warsaw envelope: {identifier}")
            bounds[0] = min(bounds[0], longitude)
            bounds[1] = min(bounds[1], latitude)
            bounds[2] = max(bounds[2], longitude)
            bounds[3] = max(bounds[3], latitude)
            street_number = (properties["NAZWA_ULICY"], properties["NUMER_PORZADKOWY"])
            street_numbers[street_number] += 1
            full_addresses[(*street_number, properties["KOD_POCZTOWY"])] += 1
            street_prefixes[properties["NAZWA_ULICY"].split()[0]] += 1
            null_fields.update(key for key, value in properties.items() if value is None)
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
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return {
        "feature_count": count,
        "unique_stable_identifiers": len(stable_identifiers),
        "number_matched": int(summary["numberMatched"]),
        "number_returned": int(summary["numberReturned"]),
        "bounds": bounds,
        "null_fields": dict(null_fields),
        "street_prefix_counts": dict(street_prefixes),
        "duplicate_street_number_groups": sum(value > 1 for value in street_numbers.values()),
        "duplicate_street_number_postcode_groups": sum(
            value > 1 for value in full_addresses.values()
        ),
        "source_response_timestamp": summary.get("timeStamp"),
        "sha256": digest,
        "bytes": path.stat().st_size,
    }


def metadata(summary: dict, retrieved_at: str) -> dict:
    return {
        "name": "Warsaw municipal address points (EMUiA)",
        "url": SOURCE_URL,
        "download_url": DOWNLOAD_URL,
        "layer": "wfs:punkty_adresowe",
        "attribution": "Address points: City of Warsaw, EMUiA",
        "retrieved_at": retrieved_at,
        "source_updated_at": None,
        "stable_identifier": "ID_IIP namespace and local UUID, excluding the version suffix",
        "date_notes": (
            "DATA has no confirmed documented meaning and is preserved as a raw source field. "
            "The ID_IIP version suffix and WFS response time are not dataset update dates."
        ),
        "matching_notes": (
            "The source exposes no building identifier or address status. "
            "A spatial building association is an inference."
        ),
        "access_notes": (
            "Municipal WFS capabilities declare AccessConstraints NONE and identify no named "
            "CC or ODbL license. PRG's separate official publication states unrestricted reuse; "
            "this download is the municipal layer, not a PRG export."
        ),
        "references": [
            SOURCE_URL + "?service=WFS&version=2.0.0&request=GetCapabilities",
            SOURCE_URL
            + "?service=WFS&version=2.0.0&request=DescribeFeatureType"
            + "&typeNames=wfs:punkty_adresowe",
            "https://eli.gov.pl/api/acts/DU/2021/1368/text.html",
            "https://www.geoportal.gov.pl/pl/dane/panstwowy-rejestr-granic-prg/",
        ],
        **summary,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output", type=Path, default=PROJECT_ROOT / ".local/data/warsaw-addresses.geojson"
    )
    parser.add_argument("--verify-existing", action="store_true")
    arguments = parser.parse_args()
    output = arguments.output.resolve()
    if arguments.verify_existing:
        print(json.dumps(validate(output), ensure_ascii=False, indent=2))
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_suffix(output.suffix + ".part")
    print("Downloading Warsaw municipal address points from the city WFS...")
    download(partial)
    retrieved_at = datetime.now(UTC).isoformat()
    summary = validate(partial)
    provenance = metadata(summary, retrieved_at)
    partial.replace(output)
    output.with_suffix(".metadata.json").write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(provenance, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
