import argparse
import hashlib
import html
import json
import math
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from municipal_address_aliases import apply_municipal_aliases

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_ROOT = PROJECT_ROOT / ".local/data/franchises"
SOURCES = {
    "zabka": {
        "name": "Żabka",
        "category_id": "grocery",
        "website": "https://www.zabka.pl/",
        "source_url": "https://www.zabka.pl/app/uploads/locator-store-data.json",
        "cache": "zabka-stores.raw.json",
    },
    "lewiatan": {
        "name": "Lewiatan",
        "category_id": "grocery",
        "website": "https://lewiatan.pl/",
        "source_url": "https://lewiatan.pl/api/stores",
        "cache": "lewiatan-stores.raw.json",
    },
    "da-grasso": {
        "name": "Da Grasso",
        "category_id": "pizza",
        "website": "https://www.dagrasso.pl/",
        "source_url": "https://www.dagrasso.pl/stores",
        "cache": "dagrasso-stores.html",
    },
}


def address_parts(value: str) -> tuple[str | None, str | None, str | None]:
    parts = re.split(r"\s+(?:lok\.?|lokal|lokalu)\s*", value, maxsplit=1, flags=re.I)
    base = parts[0].strip().rstrip(",")
    match = re.fullmatch(r"(.+?)\s+(\d+[a-zA-Z]?(?:[/\-][\da-zA-Z]+)*)", base)
    if not match:
        return None, None, parts[1] if len(parts) == 2 else None
    return match.group(1), match.group(2), parts[1] if len(parts) == 2 else None


def coordinates(longitude: object, latitude: object) -> list[float] | None:
    if longitude in (None, "") or latitude in (None, ""):
        return None
    position = [float(longitude), float(latitude)]
    if not all(math.isfinite(value) for value in position):
        raise ValueError("Non-finite outlet coordinates")
    if not (20.7 < position[0] < 21.4 and 52.0 < position[1] < 52.5):
        return None
    return position


def outlet(
    brand_id: str,
    source_id: str,
    name: str,
    street_address: str,
    postal_code: str | None,
    position: list[float] | None,
    source_url: str,
    retrieved_at: str,
    attributes: dict,
) -> dict:
    street, house_number, unit = address_parts(street_address)
    if unit is not None:
        attributes["source_unit"] = unit
    return {
        "source_id": str(source_id),
        "brand_id": brand_id,
        "name": name,
        "address": {
            "raw": f"{street_address}, {postal_code + ' ' if postal_code else ''}Warszawa",
            "street": street,
            "house_number": house_number,
            "postal_code": postal_code,
            "city": "Warszawa",
        },
        "coordinates": position,
        "source_url": source_url,
        "retrieved_at": retrieved_at,
        "status": "listed",
        "operating_model": "unknown",
        "operating_model_source_url": None,
        "attributes": attributes,
    }


def extract_zabka(body: bytes, retrieved_at: str) -> tuple[list[dict], dict]:
    records = json.loads(body)
    if not isinstance(records, list) or len(records) < 1000:
        raise ValueError("Expected the complete national Żabka locator array")
    warsaw = [row for row in records if row.get("town", "").casefold() == "warszawa"]
    excluded = Counter()
    results = []
    for row in warsaw:
        reason = next(
            (key for key in ("locatorPlanned", "isNano", "isVending") if row.get(key) is True),
            None,
        )
        if reason:
            excluded[reason] += 1
            continue
        if row.get("isVisible") is False or str(row.get("active")).lower() != "true":
            excluded["not_visible_or_active"] += 1
            continue
        results.append(
            outlet(
                "zabka",
                row["locationId"],
                "Żabka",
                row["street"],
                None,
                coordinates(row.get("lon"), row.get("lat")),
                row["storeUrl"],
                retrieved_at,
                {
                    key: row.get(key)
                    for key in ("storeId", "townId", "isNano", "isVending", "isVisible")
                },
            )
        )
    return results, {
        "national_source_count": len(records),
        "warsaw_source_count": len(warsaw),
        "excluded_warsaw_records": dict(excluded),
        "coverage_note": (
            "All Warsaw-labelled ordinary outlets in the complete official national locator "
            "response; planned, Nano, vending, invisible and inactive entries are excluded. "
            "This is the published locator inventory, not a guarantee of every physical store."
        ),
    }


def extract_lewiatan(body: bytes, retrieved_at: str) -> tuple[list[dict], dict]:
    payload = json.loads(body)
    records = payload["data"]
    if set(payload) != {"data"} or not isinstance(records, list) or len(records) < 1000:
        raise ValueError("Unexpected Lewiatan national locator response")
    warsaw = [row for row in records if re.match(r"^Warszawa(?:\s*\(|$)", row["city"], re.I)]
    results = [
        outlet(
            "lewiatan",
            row["id"],
            "Lewiatan",
            row["address"],
            row["post_code"] or None,
            coordinates(row.get("longitude"), row.get("latitude")),
            urljoin("https://lewiatan.pl", row["url"]),
            retrieved_at,
            {
                "source_city": row["city"],
                "source_longitude": row.get("longitude"),
                "source_latitude": row.get("latitude"),
                "coordinate_note": (
                    "Source coordinates rejected as outside the broad Warsaw envelope"
                    if row.get("longitude")
                    and row.get("latitude")
                    and coordinates(row["longitude"], row["latitude"]) is None
                    else "Official locator coordinates; accuracy not independently established"
                ),
            },
        )
        for row in warsaw
    ]
    return results, {
        "national_source_count": len(records),
        "warsaw_source_count": len(warsaw),
        "coverage_note": (
            "All Warsaw and Warsaw-district records in the complete official /api/stores "
            "response used by the public locator, including records without coordinates. "
            "The locator has fewer national records than the brand's approximately 3,200-store "
            "network claim; completeness is limited to this published inventory."
        ),
    }


def plain_text(value: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]*>", "", value)).split())


def extract_da_grasso(body: bytes, retrieved_at: str) -> tuple[list[dict], dict]:
    document = body.decode("utf-8")
    chunks = document.split('<div class="s4d-store-result">')[1:]
    identifiers = re.findall(r'data-store-id="([^"]+)"', document)
    if len(chunks) != len(identifiers) or len(chunks) < 100:
        raise ValueError("Da Grasso store-list structure changed or the list is incomplete")
    results = []
    for chunk in chunks:
        title = re.search(r's4d-store-result-title">\s*(.*?)\s*</span>', chunk, re.S)
        address = re.search(r'<div class="small">\s*(.*?)<br\s*/?>\s*(.*?)<br', chunk, re.S)
        identifier = re.search(r'data-store-id="([^"]+)"', chunk)
        source = re.search(r'href="(https://www\.dagrasso\.pl/Store/Details\?[^"]+)"', chunk)
        if not all((title, address, identifier, source)):
            raise ValueError("Could not parse a Da Grasso list entry")
        name = plain_text(title.group(1))
        city_line = plain_text(address.group(2))
        if not re.search(r"\bWarszawa\b", city_line):
            continue
        postal_match = re.match(r"^(\d{2}-\d{3})\s+Warszawa(?:\s|$)", city_line)
        if not postal_match:
            raise ValueError(f"Unexpected Warsaw locality: {city_line}")
        raw_street = address.group(1)
        suffix_match = re.search(r"<sup>(.*?)</sup>", raw_street, re.S)
        suffix = plain_text(suffix_match.group(1)) if suffix_match else ""
        base = plain_text(re.sub(r"<sup>.*?</sup>", "", raw_street, flags=re.S))
        if re.fullmatch(r"[A-Za-z]|/\d+[A-Za-z]?", suffix):
            street_address = base + suffix
        elif suffix:
            unit_label = suffix if re.match(r"lok\b", suffix, re.I) else "lok. " + suffix
            street_address = base + " " + unit_label
        else:
            street_address = base
        results.append(
            outlet(
                "da-grasso",
                identifier.group(1),
                "Da Grasso " + name,
                street_address,
                postal_match.group(1),
                None,
                html.unescape(source.group(1)),
                retrieved_at,
                {
                    "source_display_name": name,
                    "source_city_line": city_line,
                    "source_house_suffix": suffix,
                    "source_street_base": base,
                    "coordinate_note": "No outlet coordinates in the accessible official list",
                },
            )
        )
    return results, {
        "national_source_count": len(chunks),
        "warsaw_source_count": len(results),
        "coverage_note": (
            "All Warsaw outlets in the complete national server-rendered /stores list; "
            "all listed stores have unique source IDs and no pagination is present. "
            "Coordinates are not published in the acquired list and remain null; any subsequent "
            "local address match must be identified as a separate inference."
        ),
    }


EXTRACTORS = {
    "zabka": extract_zabka,
    "lewiatan": extract_lewiatan,
    "da-grasso": extract_da_grasso,
}


def build_snapshot(brand_id: str, body: bytes, retrieved_at: str) -> dict:
    source = SOURCES[brand_id]
    records, report = EXTRACTORS[brand_id](body, retrieved_at)
    apply_municipal_aliases(records)
    if not records or len({row["source_id"] for row in records}) != len(records):
        raise ValueError("Expected nonempty Warsaw inventory with unique source IDs")
    category_id = source["category_id"]
    return {
        "schema_version": 1,
        "categories": [
            {"id": category_id, "name": "Grocery" if category_id == "grocery" else "Pizza"}
        ],
        "brands": [
            {
                "id": brand_id,
                **{key: source[key] for key in ("name", "category_id", "website", "source_url")},
                "retrieved_at": retrieved_at,
                "coverage_status": "complete",
                "coverage_note": report["coverage_note"],
            }
        ],
        "outlets": sorted(records, key=lambda row: row["source_id"]),
        "provenance": {
            "source_sha256": hashlib.sha256(body).hexdigest(),
            "source_bytes": len(body),
            "retrieved_at": retrieved_at,
            "source_updated_at": None,
            "warsaw_outlet_count": len(records),
            "coordinate_count": sum(row["coordinates"] is not None for row in records),
            "license": "No open-data license identified; public locator facts with attribution",
            **report,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--brand", choices=[*SOURCES, "all"], default="all")
    parser.add_argument("--from-cache", action="store_true")
    parser.add_argument("--output-directory", type=Path, default=OUTPUT_ROOT)
    arguments = parser.parse_args()
    arguments.output_directory.mkdir(parents=True, exist_ok=True)
    for brand_id in SOURCES if arguments.brand == "all" else [arguments.brand]:
        source = SOURCES[brand_id]
        if arguments.from_cache:
            path = OUTPUT_ROOT / source["cache"]
            body = path.read_bytes()
            retrieved_at = datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat()
        else:
            request = Request(source["source_url"], headers={"User-Agent": "MasterFranchises/0.1"})
            with urlopen(request, timeout=60) as response:
                body = response.read()
            retrieved_at = datetime.now(UTC).isoformat()
        snapshot = build_snapshot(brand_id, body, retrieved_at)
        output = arguments.output_directory / f"{brand_id}.json"
        temporary = output.with_suffix(".json.part")
        temporary.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(output)
        print(json.dumps({"brand_id": brand_id, **snapshot["provenance"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
