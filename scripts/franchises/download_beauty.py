import argparse
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[2]
YASUMI_URL = "https://gabinety.yasumi.pl/"
DEPILCONCEPT_URL = "https://depilconcept.pl/wp-json/api/v1/agencies/"
CATEGORY = {"id": "beauty", "name": "Beauty services"}


def download(url, path):
    request = Request(url, headers={"User-Agent": "MasterFranchises/0.1 public-locator-research"})
    with urlopen(request, timeout=60) as response:
        content = response.read().decode("utf-8-sig")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return content


def split_address(raw):
    value = re.sub(r"^Warszawa[^,]*,\s*", "", raw.strip())
    value = re.sub(r"^(?:ul\.|al\.|aleja)\s*", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s*-\s*wejście.*|\s*\(.*", "", value).rstrip(" ,")
    value = re.sub(r"(?:/|\s)+(?:lok\.?\s*|U(?=\d)).*$", "", value, flags=re.IGNORECASE)
    match = re.fullmatch(r"(.+?)\s+(\d+[A-Za-z]?(?:/\d+[A-Za-z]?)?)", value)
    return (match[1], match[2]) if match else (None, None)


def coordinates(address):
    try:
        point = [float(address["lng"]), float(address["lat"])]
    except (TypeError, ValueError, KeyError):
        return None
    return point if 20.75 < point[0] < 21.35 and 52.0 < point[1] < 52.45 else None


def outlet(brand, identifier, name, raw, source, record, retrieved_at):
    street, number = split_address(raw)
    if record.get("street_name") and record.get("street_number"):
        street = re.sub(r"^(?:ul\.|al\.|aleja)\s*", "", record["street_name"], flags=re.IGNORECASE)
        number = record["street_number"]
    point = coordinates(record)
    return {
        "source_id": str(identifier),
        "brand_id": brand,
        "name": name,
        "address": {
            "raw": raw,
            "street": street,
            "house_number": number,
            "postal_code": record.get("post_code") or None,
            "city": "Warszawa",
        },
        "coordinates": point,
        "source_url": source,
        "retrieved_at": retrieved_at,
        "status": "listed",
        "operating_model": "unknown",
        "operating_model_source_url": None,
        "attributes": {
            "coordinate_note": "Official locator point; not independently surveyed."
            if point
            else "Invalid or missing coordinates; try an exact municipal address.",
        },
    }


def snapshot(identifier, name, website, source, national_count, outlets, retrieved_at):
    if not outlets or len({row["source_id"] for row in outlets}) != len(outlets):
        raise ValueError(f"{name}: missing or duplicate outlets")
    return {
        "schema_version": 1,
        "categories": [CATEGORY],
        "brands": [
            {
                "id": identifier,
                "name": name,
                "category_id": CATEGORY["id"],
                "website": website,
                "source_url": source,
                "retrieved_at": retrieved_at,
                "coverage_status": "complete",
                "coverage_note": (
                    f"All {len(outlets)} Warsaw entries from the complete public locator "
                    f"({national_count} national entries). Listed locations, "
                    "not an audit of trading status or outlet ownership."
                ),
            }
        ],
        "outlets": outlets,
    }


def extract_yasumi(content, retrieved_at):
    marker = re.search(r"var data\s*=\s*", content)
    if marker is None:
        raise ValueError("Yasumi public locator data was not found")
    data, _ = json.JSONDecoder().raw_decode(content[marker.end() :])
    records = [item for group in data.values() for item in group]
    outlets = [
        outlet(
            "yasumi",
            row["url"].rstrip("/"),
            "YASUMI " + row["address"].split(",")[0],
            row["address"],
            row["url"],
            row,
            retrieved_at,
        )
        for row in records
        if row["address"].startswith("Warszawa")
    ]
    return snapshot(
        "yasumi", "YASUMI", "https://yasumi.pl/", YASUMI_URL, len(records), outlets, retrieved_at
    )


def extract_depilconcept(content, retrieved_at):
    records = json.loads(content)["agencies"]
    outlets = [
        outlet(
            "depilconcept",
            row["ID"],
            row["post_title"],
            row["agency_address"]["address"],
            row["website"],
            row["agency_address"],
            retrieved_at,
        )
        for row in records
        if re.search(r"\bWarszawa\b", row["post_title"]) and row.get("post_status") == "publish"
    ]
    return snapshot(
        "depilconcept",
        "DepilConcept",
        "https://depilconcept.pl/",
        DEPILCONCEPT_URL,
        len(records),
        outlets,
        retrieved_at,
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / ".local/data/franchises")
    args = parser.parse_args()
    retrieved_at = datetime.now(UTC).isoformat()
    for identifier, url, filename, extractor in [
        ("yasumi", YASUMI_URL, "yasumi.html", extract_yasumi),
        ("depilconcept", DEPILCONCEPT_URL, "depilconcept-agencies.json", extract_depilconcept),
    ]:
        content = download(url, args.output_dir / "sources" / filename)
        result = extractor(content, retrieved_at)
        result["provenance"] = {
            "source_capture_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "source_updated_at": None,
            "retrieved_at": retrieved_at,
            "collection_method": "Complete official public locator response",
            "license": "No open-data license identified; public locator facts with attribution",
        }
        path = args.output_dir / f"{identifier}.json"
        temporary = path.with_suffix(".json.part")
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
        print(f"{identifier}: {len(result['outlets'])} Warsaw outlets -> {path}")


if __name__ == "__main__":
    main()
