import argparse
import hashlib
import importlib.util
import json
import math
import re
import shutil
import time
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
VERSION = "radom-public-sources-v1"
CITY_BOUNDS = [21.0531545892973, 51.3484959754452, 21.2593121086332, 51.485976468327]
CONTEXT_BOUNDS = [21.03, 51.33, 21.28, 51.50]
BUILDING_ENDPOINT = "https://ikerg.modgik.radom.pl/radom-egib"
ADDRESS_ENDPOINT = "https://mapy.geoportal.gov.pl/wss/ext/wfs/KrajowaIntegracjaNumeracjiAdresowej"
OVERPASS_ENDPOINT = "https://overpass-api.de/api/interpreter"
GROCERY_ENDPOINTS = {
    "zabka": "https://www.zabka.pl/app/uploads/locator-store-data.json",
    "lewiatan": "https://lewiatan.pl/api/stores",
}
GUS_INPUTS = {
    "archive": {
        "path": ".local/data/profile/sources/Grid_125_XLS.zip",
        "sha256": "de81cf7c3150f0120dbfcb486cd993f25d5be15e53ce20a548b584dbb3f36dbb",
    },
    "feed": {
        "path": ".local/data/profile/sources/population-feed.xml",
        "sha256": "e9b06cdd3b9c27d1ab0be5deb87359476f2cfcf57fe4b3c4eb43f4b934888c63",
    },
    "archive_metadata": {
        "path": ".local/data/profile/sources/Grid_125_XLS.zip.metadata.json",
        "sha256": "bb64eba034056bdd8f8e9030c577b73a464bc518a87b0968e4b814c8ef30d2af",
    },
    "feed_metadata": {
        "path": ".local/data/profile/sources/population-feed.xml.metadata.json",
        "sha256": "7304d09d163def4f25a8ca244617677bd9ec73424eb4e4137341e8d236dee354",
    },
}


def collector_recipe():
    return {
        "version": VERSION,
        "municipality_id": "1463011",
        "building_id_prefix": "146301_1.",
        "candidate_frame": "all_buildings_in_official_radom_city_wfs",
        "municipality_id_source_url": "https://bip.radom.pl/",
        "city_service_bounds": CITY_BOUNDS,
        "context_bounds": CONTEXT_BOUNDS,
        "context_buildings": "city_only_with_declared_extent_partial_edge_flags",
        "buildings": {
            "endpoint": BUILDING_ENDPOINT,
            "type_name": "ms:budynki",
            "crs": "urn:ogc:def:crs:EPSG::2178",
        },
        "addresses": {
            "endpoint": ADDRESS_ENDPOINT,
            "type_name": "adresyUlice:A07_Punkty_adresowe",
            "crs": "urn:ogc:def:crs:EPSG::2180",
            "filter": {"field": "ID_GMINY", "equals": "1463011"},
        },
        "wfs_page_size": 5000,
        "wfs_format": "application/gml+xml; version=3.2",
        "osm_endpoint": OVERPASS_ENDPOINT,
        "osm_normalization": "existing_frozen_access_and_activity_classification",
        "grocery_endpoints": GROCERY_ENDPOINTS,
        "grocery_snapshot": "fresh_national_responses_after_protocol_freeze",
        "grocery_scope": "Radom_city_names_then_explicit_coordinate_and_address_checks",
        "gus_inputs": GUS_INPUTS,
        "population_observed_at": "2021-03-31",
        "missing_population": "null_never_zero",
        "purpose": "offline_external_evaluation_only",
    }


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def utc_now():
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def write_json(path, payload):
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def load_helper(relative_path):
    path = ROOT / relative_path
    spec = importlib.util.spec_from_file_location("radom_" + path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_protocol(path, expected_sha256):
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("An explicit frozen protocol SHA256 is required")
    if sha256(path) != expected_sha256:
        raise ValueError("Frozen protocol SHA256 mismatch")
    protocol = json.loads(Path(path).read_text(encoding="utf-8"))
    if protocol.get("collector") != collector_recipe():
        raise ValueError("Collector recipe differs from the frozen protocol")
    for source in GUS_INPUTS.values():
        if sha256(ROOT / source["path"]) != source["sha256"]:
            raise ValueError("Pinned GUS input SHA256 mismatch")
    return protocol


def fetch(url, destination, body=None):
    metadata_path = destination.with_suffix(destination.suffix + ".metadata.json")
    body_sha256 = hashlib.sha256(body).hexdigest() if body is not None else None
    if destination.exists():
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if (
            metadata.get("sha256") != sha256(destination)
            or metadata.get("url") != url
            or metadata.get("request_body_sha256") != body_sha256
            or metadata.get("bytes") != destination.stat().st_size
        ):
            raise ValueError("Existing acquisition does not match its immutable request or hash")
        return metadata
    headers = {"User-Agent": "MasterFranchises/0.1 offline-evaluation"}
    if body is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = Request(url, data=body, headers=headers)
    temporary = destination.with_suffix(destination.suffix + ".part")
    response = None
    for attempt in range(3):
        try:
            response = urlopen(request, timeout=240)
            break
        except (HTTPError, URLError, TimeoutError) as error:
            if isinstance(error, HTTPError) and error.code not in {429, 502, 503, 504}:
                raise
            if attempt == 2:
                raise
            time.sleep(5 * (attempt + 1))
    with response, temporary.open("wb") as target:
        shutil.copyfileobj(response, target)
        metadata = {
            "url": url,
            "retrieved_at": utc_now(),
            "http_last_modified": response.headers.get("Last-Modified"),
            "content_type": response.headers.get("Content-Type"),
            "request_body_sha256": body_sha256,
        }
    temporary.replace(destination)
    metadata.update({"sha256": sha256(destination), "bytes": destination.stat().st_size})
    write_json(metadata_path, metadata)
    return metadata


def local_name(tag):
    return tag.rsplit("}", 1)[-1]


def parse_xml(content):
    if b"<!DOCTYPE" in content.upper() or b"<!ENTITY" in content.upper():
        raise ValueError("XML declarations with entities are not accepted")
    root = ET.fromstring(content)
    if local_name(root.tag) != "FeatureCollection":
        raise ValueError("WFS returned an error or an unexpected document")
    return root


def wfs_count(root, name):
    value = root.attrib.get(name)
    if value is None or not value.isdecimal():
        raise ValueError(f"WFS must publish a finite {name}")
    return int(value)


def points_from_ring(ring, transformer):
    positions = [node for node in ring.iter() if local_name(node.tag) == "posList"]
    if len(positions) == 1:
        node = positions[0]
        dimension = int(node.attrib.get("srsDimension", "2"))
        values = [float(value) for value in (node.text or "").split()]
        if dimension not in (2, 3) or len(values) % dimension:
            raise ValueError("Invalid GML coordinate dimensions")
        raw = [values[index : index + dimension] for index in range(0, len(values), dimension)]
    elif not positions:
        raw = [
            [float(value) for value in (node.text or "").split()]
            for node in ring.iter()
            if local_name(node.tag) == "pos"
        ]
        if any(len(position) not in (2, 3) for position in raw):
            raise ValueError("Invalid GML position dimensions")
    else:
        raise ValueError("Multiple GML coordinate lists in one ring")
    if not raw or any(not math.isfinite(value) for position in raw for value in position):
        raise ValueError("Missing or nonfinite GML coordinates")
    coordinates = [list(transformer.transform(*position[:2])) for position in raw]
    for lon, lat in coordinates:
        if not (20.5 < lon < 21.8 and 51.0 < lat < 51.9):
            raise ValueError("Transformed geometry is outside the Radom region")
    return coordinates


@lru_cache(maxsize=4)
def source_transformer(declared, expected_crs):
    from pyproj import CRS, Transformer

    if not declared.startswith("urn:ogc:def:crs:EPSG::"):
        raise ValueError("GML must declare unambiguous EPSG authority axis order")
    if CRS.from_user_input(declared) != CRS.from_user_input(expected_crs):
        raise ValueError("GML source CRS differs from the request")
    return Transformer.from_crs(declared, "OGC:CRS84")


def parse_geometry(container, expected_crs):
    geometries = [
        node
        for node in container.iter()
        if local_name(node.tag) in {"Point", "Polygon", "MultiSurface", "MultiPolygon"}
    ]
    if not geometries:
        raise ValueError("Unsupported or missing GML geometry")
    geometry = geometries[0]
    declared = geometry.attrib.get("srsName", expected_crs)
    transformer = source_transformer(declared, expected_crs)
    if local_name(geometry.tag) == "Point":
        points = points_from_ring(geometry, transformer)
        if len(points) != 1:
            raise ValueError("A GML point requires exactly one coordinate pair")
        return {"type": "Point", "coordinates": points[0]}
    polygons = [node for node in geometry.iter() if local_name(node.tag) == "Polygon"]
    result = []
    for polygon in polygons:
        rings = []
        for kind in ("exterior", "interior"):
            boundaries = [node for node in polygon if local_name(node.tag) == kind]
            if kind == "exterior" and len(boundaries) != 1:
                raise ValueError("Every polygon requires one exterior ring")
            for boundary in boundaries:
                ring = points_from_ring(boundary, transformer)
                if len(ring) < 4 or ring[0] != ring[-1]:
                    raise ValueError("Unclosed or incomplete polygon ring")
                rings.append(ring)
        result.append(rings)
    if not result:
        raise ValueError("No polygon surfaces found")
    return {"type": "MultiPolygon", "coordinates": result}


def normalize_gml_feature(node, kind, crs):
    properties = {}
    geometry = None
    for child in node:
        name = local_name(child.tag)
        if name == "boundedBy" and child.tag.startswith("{http://www.opengis.net/gml"):
            continue
        if list(child):
            if geometry is not None:
                raise ValueError("Multiple complex GML feature fields")
            geometry = parse_geometry(child, crs)
        else:
            properties[name] = child.text
    if geometry is None:
        raise ValueError("Feature has no geometry")
    if kind == "buildings":
        source_id = properties.get("ID_BUDYNKU")
        if not isinstance(source_id, str) or not source_id.startswith("146301_1."):
            raise ValueError("Building is outside the frozen municipal source namespace")
        if geometry["type"] != "MultiPolygon":
            raise ValueError("Building requires a polygon footprint")
    else:
        source_id = properties.get("ID_IIP")
        if not source_id or properties.get("ID_GMINY") != "1463011":
            raise ValueError("Address is outside the frozen municipality")
        if geometry["type"] != "Point":
            raise ValueError("Address requires a point")
        properties.update(
            street=properties.get("NAZWA_ULICY"),
            house_number=properties.get("NUMER_PORZADKOWY"),
            postal_code=properties.get("KOD_POCZTOWY"),
            city=properties.get("NAZWA_MIEJSCOWOSCI"),
        )
    properties["source_id"] = source_id
    return {"type": "Feature", "id": source_id, "geometry": geometry, "properties": properties}


def collect_wfs(output, kind):
    config = collector_recipe()[kind]
    parameters = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeNames": config["type_name"],
        "srsName": config["crs"],
        "outputFormat": collector_recipe()["wfs_format"],
    }
    if kind == "addresses":
        parameters["filter"] = (
            '<fes:Filter xmlns:fes="http://www.opengis.net/fes/2.0">'
            "<fes:PropertyIsEqualTo><fes:ValueReference>ID_GMINY</fes:ValueReference>"
            "<fes:Literal>1463011</fes:Literal></fes:PropertyIsEqualTo></fes:Filter>"
        )
    raw_dir = output / "raw" / kind
    raw_dir.mkdir(exist_ok=True)
    hits_path = raw_dir / "hits.xml"
    fetch(config["endpoint"] + "?" + urlencode({**parameters, "resultType": "hits"}), hits_path)
    expected = wfs_count(parse_xml(hits_path.read_bytes()), "numberMatched")
    if expected <= 0:
        raise ValueError("The municipal WFS snapshot is empty")
    features = []
    identifiers = set()
    pages = []
    page_size = collector_recipe()["wfs_page_size"]
    while len(features) < expected:
        start = len(features)
        page_path = raw_dir / f"{start:08d}.gml"
        query = {**parameters, "startIndex": start, "count": page_size}
        metadata = fetch(config["endpoint"] + "?" + urlencode(query), page_path)
        root = parse_xml(page_path.read_bytes())
        if (
            root.attrib.get("numberMatched") != "unknown"
            and wfs_count(root, "numberMatched") != expected
        ):
            raise ValueError("WFS snapshot count changed during pagination")
        members = [node for node in root if local_name(node.tag) in {"member", "featureMember"}]
        if wfs_count(root, "numberReturned") != len(members) or not members:
            raise ValueError("WFS page is incomplete")
        for member in members:
            if len(member) != 1:
                raise ValueError("Invalid WFS feature member")
            feature = normalize_gml_feature(member[0], kind, config["crs"])
            if feature["id"] in identifiers:
                raise ValueError("Duplicate WFS identity or unsupported pagination")
            identifiers.add(feature["id"])
            features.append(feature)
        pages.append(metadata)
        if len(features) > expected:
            raise ValueError("WFS returned more features than the declared snapshot")
    final_hits = raw_dir / "hits-after.xml"
    fetch(config["endpoint"] + "?" + urlencode({**parameters, "resultType": "hits"}), final_hits)
    if wfs_count(parse_xml(final_hits.read_bytes()), "numberMatched") != expected:
        raise ValueError("WFS snapshot count changed during acquisition")
    features.sort(key=lambda row: row["id"])
    path = output / f"{kind}.geojson"
    write_json(
        path, {"type": "FeatureCollection", "numberReturned": expected, "features": features}
    )
    provenance = {
        "id": f"radom-{kind}",
        "name": "Radom official cadastral buildings"
        if kind == "buildings"
        else "Radom KINA addresses",
        "url": config["endpoint"],
        "attribution": "Prezydent Miasta Radomia" if kind == "buildings" else "GUGiK / EMUiA",
        "retrieved_at": pages[-1]["retrieved_at"],
        "observed_at": None,
        "snapshot_at": None,
        "coverage_bounds": CITY_BOUNDS,
        "coverage_bounds_semantics": (
            "declared municipal service envelope, not completeness guarantee"
        ),
        "source_coordinate_reference_system": config["crs"],
        "coordinate_reference_system": "EPSG:4326",
        "coordinate_order": "longitude,latitude",
        "feature_count": expected,
        "sha256": sha256(path),
        "geojson_sha256": sha256(path),
        "pages": pages,
        "notes": [
            "Source observation and publication times are unknown; retrieval is not a survey date.",
            "All original attribute values are retained, including missing or invalid use/storeys.",
            "City-only building context is incomplete near the municipal edge.",
            "Pages form a checked acquisition interval, not an atomic source snapshot.",
        ],
    }
    write_json(output / f"{kind}.metadata.json", provenance)
    return provenance


def osm_query(kind):
    helper_path = (
        "scripts/profile/download_pois.py"
        if kind == "pois"
        else "scripts/suitability/download_activity.py"
    )
    module = load_helper(helper_path)
    west, south, east, north = CONTEXT_BOUNDS
    query = re.sub(
        r"\[bbox:[^]]+\]", f"[bbox:{south},{west},{north},{east}]", module.QUERY, count=1
    )
    return module, query


def collect_osm(output, kind):
    module, query = osm_query(kind)
    raw_path = output / "raw" / f"{kind}.json"
    metadata = fetch(OVERPASS_ENDPOINT, raw_path, urlencode({"data": query}).encode("utf-8"))
    raw = json.loads(raw_path.read_bytes())
    if raw.get("remark") or not isinstance(raw.get("elements"), list):
        raise ValueError("Incomplete Overpass response")
    snapshot_at = raw.get("osm3s", {}).get("timestamp_osm_base")
    if not isinstance(snapshot_at, str):
        raise ValueError("Overpass response has no source snapshot date")
    metadata.update(snapshot_at=snapshot_at, endpoint=OVERPASS_ENDPOINT, query=query)
    module.BOUNDS = list(CONTEXT_BOUNDS)
    module.QUERY = query
    collection, provenance = module.normalize(raw, metadata)
    if kind == "pois":
        collection["features"] = [
            row for row in collection["features"] if row["properties"]["kind"] == "access"
        ]
        collection["features"].sort(key=lambda row: row["properties"]["source_id"])
        provenance["category_counts"] = dict(
            Counter(
                category
                for row in collection["features"]
                for category in row["properties"]["categories"]
            )
        )
    collection["numberReturned"] = len(collection["features"])
    path = output / f"{kind}.geojson"
    write_json(path, collection)
    provenance.update(
        id=f"radom-osm-{kind}",
        name="OpenStreetMap Radom access points"
        if kind == "pois"
        else "OpenStreetMap Radom activity",
        feature_count=len(collection["features"]),
        sha256=sha256(path),
        geojson_sha256=sha256(path),
    )
    write_json(output / f"{kind}.metadata.json", provenance)
    return provenance


def collect_population(output):
    from pyproj import Transformer

    path = output / "population.geojson"
    metadata_path = output / "population.metadata.json"
    if path.exists() and metadata_path.exists():
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if (
            metadata.get("geojson_sha256") != sha256(path)
            or metadata.get("source_archive_sha256") != GUS_INPUTS["archive"]["sha256"]
            or metadata.get("requested_bounds") != CONTEXT_BOUNDS
        ):
            raise ValueError(
                "Existing population extraction differs from the pinned source or bounds"
            )
        return metadata
    module = load_helper("scripts/profile/download_population.py")
    archive = ROOT / GUS_INPUTS["archive"]["path"]
    archive_metadata = json.loads(
        (ROOT / GUS_INPUTS["archive_metadata"]["path"]).read_text(encoding="utf-8")
    )
    feed = ET.fromstring((ROOT / GUS_INPUTS["feed"]["path"]).read_bytes())
    entry = feed.find(module.ATOM + "entry")
    source_url = entry.find(module.ATOM + "link").attrib["href"]
    if archive_metadata["sha256"] != GUS_INPUTS["archive"]["sha256"]:
        raise ValueError("GUS provenance does not match the pinned archive")
    forward = Transformer.from_crs(4326, 3035, always_xy=True)
    reverse = Transformer.from_crs(3035, 4326, always_xy=True)
    projected = forward.transform_bounds(*CONTEXT_BOUNDS, densify_pts=101)
    bounds = [
        math.floor(projected[0] / 125) * 125,
        math.floor(projected[1] / 125) * 125,
        math.ceil(projected[2] / 125) * 125,
        math.ceil(projected[3] / 125) * 125,
    ]
    observations, statistics, workbooks, tokens = module.extract_observations(archive, bounds)
    features = []
    counts = Counter()
    west, south, east, north = bounds
    for y in range(south, north, 125):
        for x in range(west, east, 125):
            record = observations.get((x, y))
            population = record["population"] if record is not None else None
            status = "observed" if population is not None else "missing"
            counts[status] += 1
            grid_id = f"PL_CRS3035RES125mN{y}E{x}"
            features.append(
                {
                    "type": "Feature",
                    "id": grid_id,
                    "geometry": module.cell_geometry(x, y, reverse),
                    "properties": {
                        "source_id": grid_id,
                        "grid_id": grid_id,
                        "population": population,
                        "observation_year": 2021,
                        "resolution_m": 125,
                        "status": status,
                        "source_row_present": record is not None,
                        "source_total_raw": record["source_total_raw"] if record else None,
                    },
                }
            )
    path = output / "population.geojson"
    write_json(path, {"type": "FeatureCollection", "features": features})
    provenance = {
        "id": "radom-population-2021",
        "name": "GUS NSP 2021 population, 125m grid, Radom and surrounding area",
        "url": source_url,
        "feed_url": module.FEED_URL,
        "attribution": "Główny Urząd Statystyczny — Narodowy Spis Powszechny 2021",
        "license": feed.findtext(module.ATOM + "rights"),
        "retrieved_at": archive_metadata["retrieved_at"],
        "generated_at": utc_now(),
        "observed_at": "2021-03-31",
        "snapshot_at": None,
        "observation_year": 2021,
        "source_published_at": entry.findtext(module.ATOM + "updated"),
        "resolution_m": 125,
        "source_coordinate_reference_system": "EPSG:3035",
        "coordinate_reference_system": "EPSG:4326",
        "coordinate_order": "longitude,latitude",
        "coverage_bounds": list(reverse.transform_bounds(*bounds, densify_pts=101)),
        "coverage_bounds_semantics": "generated grid support, not observed population completeness",
        "requested_bounds": CONTEXT_BOUNDS,
        "grid_support_bounds_epsg3035": bounds,
        "cell_count": len(features),
        "observed_cell_count": counts["observed"],
        "missing_cell_count": counts["missing"],
        "source_statistics": statistics,
        "source_workbooks": workbooks,
        "source_non_numeric_tokens": tokens,
        "absent_cells_mean_zero": False,
        "source_archive_sha256": GUS_INPUTS["archive"]["sha256"],
        "source_archive_path": str(archive),
        "sha256": sha256(path),
        "geojson_sha256": sha256(path),
        "notes": [
            "2021 census population; retrieval year does not update the observation year.",
            "Missing cells remain null; generated empty cells are coverage diagnostics.",
            "Area-weighted catchment subtotals assume uniform population in observed cells.",
        ],
    }
    write_json(output / "population.metadata.json", provenance)
    return provenance


def normalize_grocery(brand_id, body, retrieved_at):
    if brand_id not in GROCERY_ENDPOINTS:
        raise ValueError("Unsupported grocery brand")
    payload = json.loads(body)
    if brand_id == "lewiatan" and (not isinstance(payload, dict) or set(payload) != {"data"}):
        raise ValueError("Unexpected national Lewiatan locator response")
    records = payload if brand_id == "zabka" else payload.get("data")
    if not isinstance(records, list) or len(records) < 1000:
        raise ValueError("Expected a complete national official grocery locator")
    outlets = []
    excluded = Counter()
    seen = set()
    for row in records:
        if not isinstance(row, dict):
            raise ValueError("Unexpected national locator record")
        town = row.get("town" if brand_id == "zabka" else "city")
        if not isinstance(town, str) or town.strip().casefold() != "radom":
            continue
        if brand_id == "zabka":
            for flag in ("locatorPlanned", "isNano", "isVending", "isVisible"):
                if row.get(flag) is not None and not isinstance(row[flag], bool):
                    raise ValueError("Unexpected official Żabka flag type")
            reason = next(
                (key for key in ("locatorPlanned", "isNano", "isVending") if row.get(key) is True),
                None,
            )
            if reason is not None:
                excluded[reason] += 1
                continue
            if row.get("isVisible") is False or str(row.get("active")).lower() != "true":
                excluded["not_visible_or_active"] += 1
                continue
        source_id = row.get("locationId" if brand_id == "zabka" else "id")
        if source_id is None or str(source_id) in seen:
            raise ValueError("Missing or duplicate grocery outlet identity")
        seen.add(str(source_id))
        address = row.get("street" if brand_id == "zabka" else "address")
        if not isinstance(address, str) or not address.strip():
            raise ValueError("Official grocery record has no address")
        parts = re.split(r"\s+(?:lok\.?|lokal|lokalu)\s*", address, maxsplit=1, flags=re.I)
        match = re.fullmatch(
            r"(.+?)\s+(\d+[a-zA-Z]?(?:[/\-][\da-zA-Z]+)*)", parts[0].strip().rstrip(",")
        )
        lon = row.get("lon" if brand_id == "zabka" else "longitude")
        lat = row.get("lat" if brand_id == "zabka" else "latitude")
        position = None
        rejected = False
        if lon not in (None, "") and lat not in (None, ""):
            candidate = [float(lon), float(lat)]
            if not all(math.isfinite(value) for value in candidate):
                raise ValueError("Nonfinite official outlet coordinates")
            if (
                CONTEXT_BOUNDS[0] <= candidate[0] <= CONTEXT_BOUNDS[2]
                and CONTEXT_BOUNDS[1] <= candidate[1] <= CONTEXT_BOUNDS[3]
            ):
                position = candidate
            else:
                rejected = True
        postal_code = None if brand_id == "zabka" else row.get("post_code") or None
        base_url = "https://www.zabka.pl/" if brand_id == "zabka" else "https://lewiatan.pl/"
        source_url = row.get("storeUrl" if brand_id == "zabka" else "url")
        if not isinstance(source_url, str) or not source_url.strip():
            raise ValueError("Official outlet has no source URL")
        outlets.append(
            {
                "source_id": str(source_id),
                "brand_id": brand_id,
                "name": "Żabka" if brand_id == "zabka" else "Lewiatan",
                "address": {
                    "raw": f"{address}, {postal_code + ' ' if postal_code else ''}Radom",
                    "street": match.group(1) if match else None,
                    "house_number": match.group(2) if match else None,
                    "postal_code": postal_code,
                    "city": "Radom",
                },
                "coordinates": position,
                "source_url": urljoin(base_url, source_url),
                "retrieved_at": retrieved_at,
                "status": "listed",
                "operating_model": "unknown",
                "operating_model_source_url": None,
                "attributes": {
                    "source_city": town,
                    "source_longitude": lon,
                    "source_latitude": lat,
                    "source_unit": parts[1] if len(parts) == 2 else None,
                    "coordinates_outside_context_bounds": rejected,
                },
            }
        )
    name = "Żabka" if brand_id == "zabka" else "Lewiatan"
    note = (
        "All explicitly Radom-labelled published official locator records; ordinary active "
        "Żabka outlets only. Published inventory is not a guarantee of physical completeness. "
        "Individual ownership/franchise status and source coordinates remain unverified."
    )
    return {
        "schema_version": 1,
        "categories": [{"id": "grocery", "name": "Grocery"}],
        "brands": [
            {
                "id": brand_id,
                "name": name,
                "category_id": "grocery",
                "website": "https://www.zabka.pl/"
                if brand_id == "zabka"
                else "https://lewiatan.pl/",
                "source_url": GROCERY_ENDPOINTS[brand_id],
                "retrieved_at": retrieved_at,
                "coverage_status": "complete",
                "coverage_note": note,
            }
        ],
        "outlets": sorted(outlets, key=lambda row: row["source_id"]),
        "provenance": {
            "source_sha256": hashlib.sha256(body).hexdigest(),
            "source_bytes": len(body),
            "retrieved_at": retrieved_at,
            "observed_at": None,
            "source_updated_at": None,
            "national_source_count": len(records),
            "radom_outlet_count": len(outlets),
            "excluded_radom_records": dict(excluded),
            "coordinate_count": sum(row["coordinates"] is not None for row in outlets),
            "license": "No open-data license identified; public locator facts with attribution",
            "coverage_note": note,
        },
    }


def collect_grocery(output, brand_id):
    raw_path = output / "raw" / f"{brand_id}-stores.raw.json"
    metadata = fetch(GROCERY_ENDPOINTS[brand_id], raw_path)
    snapshot = normalize_grocery(brand_id, raw_path.read_bytes(), metadata["retrieved_at"])
    path = output / f"{brand_id}.json"
    write_json(path, snapshot)
    return {
        "id": f"radom-{brand_id}-official-locator",
        **metadata,
        "normalized_sha256": sha256(path),
        "normalized_path": path.name,
        "observed_at": None,
        "snapshot_at": None,
    }


def collect(protocol_path, protocol_sha256, output_dir):
    validate_protocol(protocol_path, protocol_sha256)
    output = Path(output_dir).resolve()
    allowed_root = (ROOT / ".local" / "evaluations").resolve()
    if output == allowed_root or not output.is_relative_to(allowed_root):
        raise ValueError(
            "Acquisition output must be a new isolated .local/evaluations subdirectory"
        )
    if output.exists():
        if (
            not (output / "protocol.json").is_file()
            or sha256(output / "protocol.json") != protocol_sha256
            or (output / "manifest.json").exists()
        ):
            raise ValueError("Existing output is not an incomplete matching protocol acquisition")
    else:
        output.mkdir(parents=True)
        shutil.copyfile(protocol_path, output / "protocol.json")
    (output / "raw").mkdir(exist_ok=True)
    sources = {}
    for kind in ("buildings", "addresses"):
        sources[kind] = collect_wfs(output, kind)
    sources["population"] = collect_population(output)
    for kind in ("pois", "activity"):
        sources[kind] = collect_osm(output, kind)
    for brand_id in GROCERY_ENDPOINTS:
        sources[brand_id] = collect_grocery(output, brand_id)
    manifest = {
        "collector_version": VERSION,
        "protocol_sha256": protocol_sha256,
        "collected_at": utc_now(),
        "sources": sources,
        "artifacts": {
            path.relative_to(output).as_posix(): sha256(path)
            for path in sorted(output.rglob("*"))
            if path.is_file()
        },
    }
    write_json(output / "manifest.json", manifest)
    return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--protocol-sha256", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    arguments = parser.parse_args()
    manifest = collect(arguments.protocol, arguments.protocol_sha256, arguments.output_dir)
    print(
        json.dumps({"version": manifest["collector_version"], "files": len(manifest["artifacts"])})
    )


if __name__ == "__main__":
    main()
