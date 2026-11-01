import argparse
import json
import re
import time
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MCDONALDS_URL = "https://mcdonalds.pl/data/places"
SUBWAY_COUNTRY_URL = "https://restaurants.subway.com/pl/polska"
SUBWAY_REGION_URL = SUBWAY_COUNTRY_URL + "/mz"
CATEGORY = {"id": "quick-service", "name": "Quick-service restaurants"}


class LocatorParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.scripts = []
        self.current_script = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "a":
            self.links.append(attributes)
        if tag == "script" and attributes.get("type") == "text/data":
            self.current_script = {"attributes": attributes, "text": ""}

    def handle_data(self, data):
        if self.current_script is not None:
            self.current_script["text"] += data

    def handle_endtag(self, tag):
        if tag == "script" and self.current_script is not None:
            self.scripts.append(self.current_script)
            self.current_script = None


def download(url, cache_path):
    request = Request(
        url,
        headers={
            "User-Agent": "MasterFranchises/0.1 public-locator-research",
            "Accept": "application/json,text/html",
        },
    )
    with urlopen(request, timeout=60) as response:
        content = response.read()
        encoding = response.headers.get_content_charset() or "utf-8"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(content)
    return content.decode(encoding)


def parse_html(content):
    parser = LocatorParser()
    parser.feed(content)
    return parser


def split_address(raw):
    normalized = re.sub(r"^(?:ul\.|al\.)\s*", "", raw, flags=re.IGNORECASE).strip()
    match = re.fullmatch(r"(.+?)\s+(\d+[a-zA-Z]?(?:/\d+[a-zA-Z]?)?(?:\s+[A-Z])?)", normalized)
    if match is None:
        return None, None
    return match[1], re.sub(r"\s+", "", match[2])


def make_snapshot(brand_id, name, website, source_url, retrieved_at, note, outlets):
    identifiers = [item["source_id"] for item in outlets]
    if not outlets or len(identifiers) != len(set(identifiers)):
        raise ValueError(f"{brand_id}: missing outlets or duplicate source identifiers")
    for item in outlets:
        lon, lat = item["coordinates"]
        if not 20.75 < lon < 21.35 or not 52.0 < lat < 52.45:
            raise ValueError(f"Unexpected Warsaw coordinate: {brand_id}/{item['source_id']}")
    return {
        "schema_version": 1,
        "categories": [CATEGORY],
        "brands": [
            {
                "id": brand_id,
                "name": name,
                "category_id": CATEGORY["id"],
                "website": website,
                "source_url": source_url,
                "retrieved_at": retrieved_at,
                "coverage_status": "complete",
                "coverage_note": note,
            }
        ],
        "outlets": outlets,
    }


def extract_mcdonalds(destination, retrieved_at):
    raw = json.loads(download(MCDONALDS_URL, destination / "raw" / "mcdonalds-places.json"))
    records = raw["places"]
    if len({item["id"] for item in records}) != len(records):
        raise ValueError("McDonald's national feed contains duplicate IDs")
    outlets = []
    for item in records:
        if item["city"].strip().casefold() != "warszawa":
            continue
        street, number = split_address(item["address"])
        outlets.append(
            {
                "source_id": str(item["id"]),
                "brand_id": "mcdonalds",
                "name": "McDonald’s " + item["address"],
                "address": {
                    "raw": item["address"],
                    "street": street,
                    "house_number": number,
                    "postal_code": item.get("postCode") or None,
                    "city": "Warszawa",
                },
                "coordinates": [float(item["lng"]), float(item["lat"])],
                "source_url": "https://mcdonalds.pl/restauracje/" + item["slug"] + "/",
                "retrieved_at": retrieved_at,
                "status": "temporarily_closed" if item["temporarilyClosed"] else "listed",
                "operating_model": "unknown",
                "operating_model_source_url": None,
                "attributes": {
                    "locator_id": item["id"],
                    "locator_city": item["city"],
                    "locator_slug": item["slug"],
                    "locator_tip": item.get("tip"),
                    "temporarily_closed": bool(item["temporarilyClosed"]),
                    "coordinate_source": MCDONALDS_URL,
                },
            }
        )
    outlets.sort(key=lambda item: int(item["source_id"]))
    note = (
        f"All {len(outlets)} records labelled Warszawa in the complete public national "
        f"locator feed ({len(records)} Polish listings) retrieved {retrieved_at[:10]}. "
        "Complete means complete relative to this locator snapshot, not independent "
        "verification that every outlet is trading. No per-outlet franchise status is supplied."
    )
    snapshot = make_snapshot(
        "mcdonalds",
        "McDonald’s",
        "https://mcdonalds.pl/",
        "https://mcdonalds.pl/restauracje/",
        retrieved_at,
        note,
        outlets,
    )
    return snapshot, {
        "national_listings": len(records),
        "warsaw_listings": len(outlets),
    }


def extract_subway(destination, retrieved_at):
    country = parse_html(
        download(
            SUBWAY_COUNTRY_URL,
            destination / "raw" / "subway-country.html",
        )
    )
    national_counts = [
        int(match[1])
        for link in country.links
        if "Directory-listLink" in link.get("class", "").split()
        and (match := re.fullmatch(r"\((\d+)\)", link.get("data-count", "")))
    ]
    if len(national_counts) != 16:
        raise ValueError("Expected all 16 Polish regional directory counts")
    region = parse_html(
        download(
            SUBWAY_REGION_URL,
            destination / "raw" / "subway-mazowieckie.html",
        )
    )
    directories = {}
    for link in region.links:
        url = urljoin(SUBWAY_REGION_URL, link.get("href", ""))
        if url.rsplit("/", 1)[-1] not in {"warszawa", "warsaw"}:
            continue
        count = re.fullmatch(r"\((\d+)\)", link.get("data-count", ""))
        if count is not None:
            directories[url] = int(count[1])
    if not directories:
        raise ValueError("No Warsaw branches found in the official regional directory")
    detail_urls = set()
    for url, expected_count in directories.items():
        parser = parse_html(
            download(
                url,
                destination / "raw" / f"subway-{url.rsplit('/', 1)[-1]}.html",
            )
        )
        links = {
            urljoin(url, link["href"])
            for link in parser.links
            if "Teaser-title" in link.get("class", "").split()
        }
        if len(links) != expected_count:
            raise ValueError(f"Directory count changed or incomplete pagination: {url}")
        detail_urls.update(links)
    outlets_by_id = {}
    for index, url in enumerate(sorted(detail_urls)):
        time.sleep(0.2)
        page = parse_html(
            download(
                url,
                destination / "raw" / "subway-details" / f"{index:03d}.html",
            )
        )
        scripts = [
            script
            for script in page.scripts
            if script["attributes"].get("id") == "js-map-config-dir-map"
        ]
        if len(scripts) != 1:
            raise ValueError(f"Expected one public map configuration on {url}")
        entities = json.loads(scripts[0]["text"])["entities"]
        if len(entities) != 1:
            raise ValueError(f"Expected one restaurant entity on {url}")
        profile = entities[0]["profile"]
        address = profile["address"]
        if address["city"].casefold() not in {"warszawa", "warsaw"}:
            raise ValueError(f"Non-Warsaw restaurant in Warsaw directory: {url}")
        identifier = str(profile["meta"]["id"])
        coordinates = profile["yextDisplayCoordinate"]
        street, number = split_address(address["line1"])
        source_address = ", ".join(
            address[key] for key in ("line1", "line2", "line3") if address.get(key)
        )
        outlets_by_id[identifier] = {
            "source_id": identifier,
            "brand_id": "subway",
            "name": address.get("extraDescription") or "Subway",
            "address": {
                "raw": source_address,
                "street": street,
                "house_number": number,
                "postal_code": address.get("postalCode") or None,
                "city": "Warszawa",
            },
            "coordinates": [float(coordinates["long"]), float(coordinates["lat"])],
            "source_url": url,
            "retrieved_at": retrieved_at,
            "status": "unknown" if profile.get("closed") else "listed",
            "operating_model": "unknown",
            "operating_model_source_url": None,
            "attributes": {
                "locator_id": identifier,
                "yext_id": profile["meta"]["yextId"],
                "locator_city": address["city"],
                "locator_updated_at": profile["meta"].get("timestamp"),
                "locator_closed": profile.get("closed"),
                "coordinate_source": url,
                "source_street_line": address["line1"],
            },
        }
    outlets = sorted(outlets_by_id.values(), key=lambda item: item["source_id"])
    if len(outlets) != len(detail_urls):
        raise ValueError("Subway directory has duplicated entities; review coverage before import")
    note = (
        f"All {len(outlets)} distinct listings across both official Warsaw city directory "
        f"branches retrieved {retrieved_at[:10]}; their counts agree with the regional "
        f"directory. Poland regional counts total {sum(national_counts)} listings. "
        "Complete means complete relative to this locator snapshot, not independent "
        "verification that every outlet is trading. Source address spelling is preserved."
    )
    snapshot = make_snapshot(
        "subway",
        "Subway",
        "https://mysubway.pl/",
        SUBWAY_REGION_URL,
        retrieved_at,
        note,
        outlets,
    )
    return snapshot, {
        "national_listings": sum(national_counts),
        "warsaw_listings": len(outlets),
        "city_directories": directories,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT / ".local" / "data" / "franchises",
    )
    parser.add_argument("--brand", choices=["all", "mcdonalds", "subway"], default="all")
    arguments = parser.parse_args()
    arguments.output_dir.mkdir(parents=True, exist_ok=True)
    retrieved_at = datetime.now(UTC).isoformat()
    extractors = {"mcdonalds": extract_mcdonalds, "subway": extract_subway}
    for brand_id, extractor in extractors.items():
        if arguments.brand not in {"all", brand_id}:
            continue
        snapshot, statistics = extractor(arguments.output_dir, retrieved_at)
        output = arguments.output_dir / f"{brand_id}.json"
        output.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps({"brand": brand_id, "output": str(output), **statistics}))


if __name__ == "__main__":
    main()
