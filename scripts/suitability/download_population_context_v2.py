import argparse
import hashlib
import json
import math
import re
import sqlite3
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from pyproj import Transformer

VERSION = "gus-population-context-v2"
ROOT = Path(__file__).resolve().parents[2]
FEED_URL = (
    "https://geo.stat.gov.pl/atom-web/atom/GRID?"
    "spatial_dataset_identifier_code=NSP2021_500m&spatial_dataset_identifier_namespace=GRID"
)
REGIONS = {
    "warsaw": [20.82, 52.07, 21.32, 52.40],
    "radom": [21.03, 51.33, 21.28, 51.50],
}
GRID_PATTERN = re.compile(r"PL_CRS3035RES500mN(\d+)E(\d+)\Z")
ATOM = "{http://www.w3.org/2005/Atom}"


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(path.suffix + ".part")
    pending.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    pending.replace(path)


def acquire(url, path):
    metadata_path = path.with_suffix(path.suffix + ".metadata.json")
    if path.exists() or metadata_path.exists():
        if not path.exists() or not metadata_path.exists():
            raise ValueError("Incomplete cached public source")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if metadata["url"] != url or metadata["sha256"] != digest(path):
            raise ValueError("Cached public source identity changed")
        return metadata
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(path.suffix + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "MasterFranchises/coverage-v2"})
    with urllib.request.urlopen(request, timeout=120) as response, pending.open("wb") as target:
        while block := response.read(1024 * 1024):
            target.write(block)
    pending.replace(path)
    metadata = {
        "url": url,
        "retrieved_at": datetime.now(UTC).isoformat(),
        "sha256": digest(path),
        "bytes": path.stat().st_size,
    }
    write_json(metadata_path, metadata)
    return metadata


def parse_cell(identifier, value):
    match = GRID_PATTERN.fullmatch(identifier)
    if not match:
        raise ValueError("Unexpected official 500 m grid identity")
    northing, easting = map(int, match.groups())
    if northing % 500 or easting % 500:
        raise ValueError("Official grid identity is not aligned to 500 m")
    if value is not None and (type(value) is not int or value < 0):
        raise ValueError("Population must be an explicit nonnegative integer or unknown")
    return {
        "cell_id": identifier,
        "easting": easting,
        "northing": northing,
        "population": value,
        "status": "observed" if value is not None else "missing",
    }


def regional_cells(database, bounds):
    projected = Transformer.from_crs(4326, 3035, always_xy=True).transform_bounds(
        *bounds, densify_pts=101
    )
    west, south = (math.floor(value / 500) * 500 for value in projected[:2])
    east, north = (math.ceil(value / 500) * 500 for value in projected[2:])
    uri = database.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        contents = connection.execute("SELECT table_name,srs_id FROM gpkg_contents").fetchall()
        if contents != [("GRID_500", 3035)]:
            raise ValueError("Unexpected official population grid schema or projection")
        rows = connection.execute(
            "SELECT g.PL_code,g.TOT FROM GRID_500 g JOIN rtree_GRID_500_geom r ON r.id=g.fid"
            " WHERE r.maxx>? AND r.minx<? AND r.maxy>? AND r.miny<? ORDER BY g.PL_code",
            (west, east, south, north),
        )
        cells = [parse_cell(identifier, population) for identifier, population in rows]
    identities = {row["cell_id"] for row in cells}
    if not cells or len(identities) != len(cells):
        raise ValueError("Population context must contain unique official cells")
    return cells, [west, south, east, north]


def collect(city, output, sources):
    output = output.resolve()
    if not output.is_relative_to((ROOT / ".local/data/coverage-v2").resolve()):
        raise ValueError("New population context must stay in isolated coverage-v2 storage")
    if output.exists():
        raise ValueError("Population context output must not already exist")
    feed_path = sources / "population-500m-feed.xml"
    feed_metadata = acquire(FEED_URL, feed_path)
    feed = ET.fromstring(feed_path.read_bytes())
    matches = []
    for entry in feed.findall(ATOM + "entry"):
        for link in entry.findall(ATOM + "link"):
            if "NSP2021_TOT_GRID500m_GPKG.zip" in link.attrib.get("href", ""):
                matches.append((entry, link))
    if len(matches) != 1:
        raise ValueError("Official feed must identify one 500 m GeoPackage")
    entry, link = matches[0]
    archive = sources / "NSP2021_TOT_GRID500m_GPKG.zip"
    acquisition = acquire(link.attrib["href"], archive)
    if archive.stat().st_size != int(link.attrib["length"]):
        raise ValueError("Population archive differs from its official feed length")
    database = sources / "grid500m.gpkg"
    with zipfile.ZipFile(archive) as bundle:
        if bundle.namelist() != ["NSP2021_TOT_GRID500m.gpkg"]:
            raise ValueError("Unexpected population archive members")
        pending = database.with_suffix(".gpkg.part")
        with bundle.open(bundle.namelist()[0]) as source, pending.open("wb") as target:
            while block := source.read(1024 * 1024):
                target.write(block)
        pending.replace(database)
    cells, grid_bounds = regional_cells(database, REGIONS[city])
    source = {
        "id": f"{city}-gus-national-population-500m-2021-v2",
        "name": "Statistics Poland 2021 national population, 500 m census grid",
        "url": acquisition["url"],
        "attribution": "Statistics Poland (GUS), National Population and Housing Census 2021",
        "retrieved_at": acquisition["retrieved_at"],
        "observed_at": "2021-03-31",
        "source_published_at": entry.findtext(ATOM + "updated"),
        "feed_url": FEED_URL,
        "archive_sha256": acquisition["sha256"],
        "feed_sha256": feed_metadata["sha256"],
        "population_definition": "national_definition",
        "resolution_m": 500,
        "coordinate_reference_system": "EPSG:3035",
        "city": city,
        "requested_bounds": REGIONS[city],
        "grid_support_bounds_epsg3035": grid_bounds,
        "absence_means_zero": False,
        "zero_semantics": "Only explicit published zero values are observed zeros",
        "notes": [
            "The population of the containing census square is separate from a radial catchment.",
            "Coarser context does not fill gaps or replace values in the 125 m population grid.",
            "The population observation remains 2021 regardless of retrieval date.",
            "Different source releases may contain revisions; their totals are not merged.",
        ],
    }
    snapshot = {"version": VERSION, "source": source, "cells": cells}
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "population-context.json", snapshot)
    report = {
        "version": VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "source": source,
        "cells": len(cells),
        "explicit_zero_cells": sum(row["population"] == 0 for row in cells),
        "missing_cells": sum(row["population"] is None for row in cells),
        "expected_support_cells": (grid_bounds[2] - grid_bounds[0])
        * (grid_bounds[3] - grid_bounds[1])
        // 250000,
        "artifacts": {"population-context.json": digest(output / "population-context.json")},
        "fine_grid_modified": False,
        "model_features_modified": False,
    }
    write_json(output / "manifest.json", report)
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--city", choices=sorted(REGIONS), required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--sources-dir", type=Path, default=ROOT / ".local/data/coverage-v2/sources"
    )
    arguments = parser.parse_args()
    report = collect(arguments.city, arguments.output_dir, arguments.sources_dir)
    print(
        json.dumps({key: report[key] for key in ("cells", "explicit_zero_cells", "missing_cells")})
    )


if __name__ == "__main__":
    main()
