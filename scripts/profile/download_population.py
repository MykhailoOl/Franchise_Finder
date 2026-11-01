import argparse
import hashlib
import io
import json
import math
import re
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from pyproj import Transformer

FEED_URL = (
    "https://geo.stat.gov.pl/atom-web/atom/GRID?"
    "spatial_dataset_identifier_code=GRID_2021_125m_xls&"
    "spatial_dataset_identifier_namespace=GRID"
)
GRID_SPEC_URL = (
    "https://inspire-mif.github.io/technical-guidelines/data/su/dataspecification_su.pdf"
)
CENSUS_DATE_URL = "https://spis.gov.pl/lista-pytan-w-nsp-2021/"
ATOM = "{http://www.w3.org/2005/Atom}"
SHEET = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
GRID_PATTERN = re.compile(r"PL_CRS3035RES125mN(\d+)E(\d+)\Z")
RESOLUTION = 125


def utc_now():
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    temporary.replace(path)


def sha256(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def acquire(url, destination, refresh=False):
    manifest_path = destination.with_suffix(destination.suffix + ".metadata.json")
    if destination.exists() and not refresh:
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest["url"] != url:
                raise ValueError("Source download URL changed; rerun with --refresh")
            if manifest["sha256"] != sha256(destination):
                raise ValueError(f"Cached source checksum changed: {destination}")
            return manifest
        manifest = {
            "url": url,
            "retrieved_at": datetime.fromtimestamp(destination.stat().st_mtime, UTC).isoformat(),
            "retrieval_timestamp_basis": "Local file modification time from initial download",
            "bytes": destination.stat().st_size,
            "sha256": sha256(destination),
        }
        write_json(manifest_path, manifest)
        return manifest
    temporary = destination.with_suffix(destination.suffix + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "MasterFranchises/0.1"})
    with urllib.request.urlopen(request, timeout=120) as response, temporary.open("wb") as target:
        headers = dict(response.headers)
        while block := response.read(1024 * 1024):
            target.write(block)
    advertised = headers.get("Content-Length") or headers.get("content-length")
    if advertised and temporary.stat().st_size != int(advertised):
        raise ValueError("Downloaded source length differs from Content-Length")
    temporary.replace(destination)
    manifest = {
        "url": url,
        "retrieved_at": utc_now(),
        "bytes": destination.stat().st_size,
        "sha256": sha256(destination),
        "response_headers": headers,
    }
    write_json(manifest_path, manifest)
    return manifest


def shared_strings(archive):
    values = []
    with archive.open("xl/sharedStrings.xml") as source:
        context = ET.iterparse(source, events=("start", "end"))
        _, root = next(context)
        for event, element in context:
            if event == "end" and element.tag == SHEET + "si":
                values.append("".join(element.itertext()))
                root.clear()
    return values


def worksheet_rows(archive, strings):
    with archive.open("xl/worksheets/sheet1.xml") as source:
        data_element = None
        for event, element in ET.iterparse(source, events=("start", "end")):
            if event == "start" and element.tag == SHEET + "sheetData":
                data_element = element
            if event != "end" or element.tag != SHEET + "row":
                continue
            row = {}
            for cell in element.findall(SHEET + "c"):
                column = re.sub(r"\d", "", cell.attrib["r"])
                value = cell.findtext(SHEET + "v")
                if cell.attrib.get("t") == "s" and value is not None:
                    value = strings[int(value)]
                elif cell.attrib.get("t") == "inlineStr":
                    value = "".join(cell.find(SHEET + "is").itertext())
                row[column] = value
            yield row
            if data_element is not None:
                data_element.clear()


def grid_position(grid_id):
    match = GRID_PATTERN.fullmatch(grid_id)
    if not match:
        raise ValueError(f"Unexpected GUS grid identifier: {grid_id}")
    northing, easting = (int(value) for value in match.groups())
    if northing % RESOLUTION or easting % RESOLUTION:
        raise ValueError(f"Grid origin is not aligned to 125m: {grid_id}")
    return easting, northing


def parse_population(raw):
    if raw is None or raw.strip() == "":
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    if not math.isfinite(value) or value < 0 or not value.is_integer():
        raise ValueError(f"Unexpected population total: {raw}")
    return int(value)


def extract_observations(path, bounds):
    west, south, east, north = bounds
    observations = {}
    statistics = Counter()
    workbook_statistics = []
    missing_tokens = Counter()
    with zipfile.ZipFile(path) as bundle:
        workbook_names = sorted(bundle.namelist())
        if workbook_names != ["Grid_125_1.xlsx", "Grid_125_2.xlsx"]:
            raise ValueError(f"Unexpected source workbooks: {workbook_names}")
        for name in workbook_names:
            print(f"Reading {name}", flush=True)
            rows_read = 0
            with zipfile.ZipFile(io.BytesIO(bundle.read(name))) as workbook:
                strings = shared_strings(workbook)
                rows = worksheet_rows(workbook, strings)
                header = next(rows)
                if header != {"A": "PL_CODE", "B": "CODE", "C": "TOT"}:
                    raise ValueError(f"Unexpected XLSX columns: {header}")
                for row in rows:
                    grid_id = row["A"]
                    if grid_id != "PL_" + row["B"]:
                        raise ValueError(f"Inconsistent source grid codes: {row}")
                    x, y = grid_position(grid_id)
                    population = parse_population(row.get("C"))
                    rows_read += 1
                    statistics["source_rows"] += 1
                    if population is None:
                        statistics["source_null_rows"] += 1
                        missing_tokens[str(row.get("C"))] += 1
                    else:
                        statistics["source_population_sum"] += population
                        statistics["source_zero_rows"] += population == 0
                        statistics["source_positive_rows"] += population > 0
                    if not (west <= x < east and south <= y < north):
                        continue
                    if (x, y) in observations:
                        raise ValueError(f"Duplicate extracted grid cell: {grid_id}")
                    observations[x, y] = {
                        "population": population,
                        "source_total_raw": row.get("C"),
                    }
                del strings
            workbook_statistics.append({"name": name, "data_rows": rows_read})
    return observations, dict(statistics), workbook_statistics, dict(missing_tokens)


def cell_geometry(x, y, transformer):
    corners = [
        (x, y),
        (x + RESOLUTION, y),
        (x + RESOLUTION, y + RESOLUTION),
        (x, y + RESOLUTION),
        (x, y),
    ]
    coordinates = [list(transformer.transform(*corner)) for corner in corners]
    if not all(19 < lon < 23 and 51 < lat < 54 for lon, lat in coordinates):
        raise ValueError("Transformed cell is outside the expected Warsaw region")
    return {"type": "Polygon", "coordinates": [coordinates]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=Path(".local/data/profile"))
    parser.add_argument("--bounds", nargs=4, type=float, default=[20.82, 52.07, 21.32, 52.40])
    parser.add_argument("--refresh", action="store_true")
    arguments = parser.parse_args()
    output_dir = arguments.output_dir
    sources = output_dir / "sources"
    sources.mkdir(parents=True, exist_ok=True)
    feed_path = sources / "population-feed.xml"
    acquire(FEED_URL, feed_path, arguments.refresh)
    feed = ET.fromstring(feed_path.read_bytes())
    entry = feed.find(ATOM + "entry")
    download = entry.find(ATOM + "link")
    source_url = download.attrib["href"]
    archive_path = sources / "Grid_125_XLS.zip"
    archive_metadata = acquire(source_url, archive_path, arguments.refresh)
    if archive_path.stat().st_size != int(download.attrib["length"]):
        raise ValueError("Source ZIP length differs from GUS feed")
    forward = Transformer.from_crs(4326, 3035, always_xy=True)
    reverse = Transformer.from_crs(3035, 4326, always_xy=True)
    projected = forward.transform_bounds(*arguments.bounds, densify_pts=101)
    projected_bounds = [
        math.floor(projected[0] / RESOLUTION) * RESOLUTION,
        math.floor(projected[1] / RESOLUTION) * RESOLUTION,
        math.ceil(projected[2] / RESOLUTION) * RESOLUTION,
        math.ceil(projected[3] / RESOLUTION) * RESOLUTION,
    ]
    observations, statistics, workbooks, missing_tokens = extract_observations(
        archive_path, projected_bounds
    )
    features = []
    counts = Counter()
    west, south, east, north = projected_bounds
    for y in range(south, north, RESOLUTION):
        for x in range(west, east, RESOLUTION):
            record = observations.get((x, y))
            population = record["population"] if record is not None else None
            status = "observed" if population is not None else "missing"
            counts[status] += 1
            grid_id = f"PL_CRS3035RES125mN{y}E{x}"
            properties = {
                "source_id": grid_id,
                "grid_id": grid_id,
                "population": population,
                "observation_year": 2021,
                "resolution_m": RESOLUTION,
                "status": status,
                "source_row_present": record is not None,
                "source_total_raw": record["source_total_raw"] if record else None,
            }
            features.append(
                {
                    "type": "Feature",
                    "id": grid_id,
                    "properties": properties,
                    "geometry": cell_geometry(x, y, reverse),
                }
            )
    coverage_bounds = list(reverse.transform_bounds(*projected_bounds, densify_pts=101))
    geojson_path = output_dir / "population.geojson"
    write_json(geojson_path, {"type": "FeatureCollection", "features": features})
    metadata = {
        "id": "warsaw-population-2021",
        "name": "GUS NSP 2021 population, 125m grid, Warsaw and surrounding area",
        "url": source_url,
        "feed_url": FEED_URL,
        "attribution": "Główny Urząd Statystyczny — Narodowy Spis Powszechny 2021",
        "license": feed.findtext(ATOM + "rights"),
        "license_note": "GUS ATOM rights statement; no named Creative Commons license asserted",
        "retrieved_at": archive_metadata["retrieved_at"],
        "generated_at": utc_now(),
        "observed_at": "2021-03-31",
        "observation_year": 2021,
        "observation_date_source_url": CENSUS_DATE_URL,
        "source_published_at": entry.findtext(ATOM + "updated"),
        "resolution_m": RESOLUTION,
        "coordinate_reference_system": "EPSG:4326",
        "coordinate_order": "longitude,latitude",
        "source_coordinate_reference_system": "EPSG:3035",
        "geometry_method": "125m squares reconstructed from INSPIRE lower-left cell codes",
        "geometry_specification_url": GRID_SPEC_URL,
        "requested_bounds": arguments.bounds,
        "coverage_bounds": coverage_bounds,
        "grid_support_bounds_epsg3035": projected_bounds,
        "coverage_bounds_semantics": (
            "Envelope of generated grid support, not complete observed coverage"
        ),
        "cell_count": len(features),
        "observed_cell_count": counts["observed"],
        "missing_cell_count": counts["missing"],
        "source_rows_in_extract": len(observations),
        "observed_population_sum": sum(row["population"] or 0 for row in observations.values()),
        "source_statistics": statistics,
        "source_workbooks": workbooks,
        "source_non_numeric_tokens": missing_tokens,
        "absent_cells_mean_zero": False,
        "absent_cell_semantics": (
            "Unknown; represented as missing, not zero or confirmed suppression"
        ),
        "source_archive_sha256": archive_metadata["sha256"],
        "geojson_sha256": sha256(geojson_path),
        "notes": [
            "Population is from the 2021 census, not the 2026 download year.",
            "This regional extract is not an administrative Warsaw population total.",
            (
                "Missing table rows remain null because zero-versus-suppression semantics "
                "were not documented in the retrieved feed or workbooks."
            ),
            "Missing polygons are generated coverage diagnostics, not GUS observations.",
            (
                "Area-weighted catchment subtotals assume uniform population within "
                "each observed 125m cell."
            ),
            (
                "Catchments intersecting null cells require an incomplete-coverage label; "
                "no observed cells means unavailable, not zero."
            ),
            (
                "Cell totals cannot establish the number of people in a particular building, "
                "current footfall, or income."
            ),
        ],
    }
    write_json(output_dir / "population.metadata.json", metadata)
    print(
        json.dumps(
            {
                key: metadata[key]
                for key in [
                    "cell_count",
                    "observed_cell_count",
                    "missing_cell_count",
                    "observed_population_sum",
                    "coverage_bounds",
                    "source_statistics",
                    "source_non_numeric_tokens",
                ]
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
