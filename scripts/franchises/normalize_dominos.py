import argparse
import hashlib
import json
import math
import re
from pathlib import Path
from urllib.parse import urlsplit

from download_grocery_pizza import OUTPUT_ROOT, address_parts, outlet
from municipal_address_aliases import apply_municipal_aliases


def normalize_snapshot(source: dict, source_bytes: bytes) -> dict:
    source_url = source["source_url"]
    if source_url != "https://www.dominospizza.pl/lokale/Warszawa":
        raise ValueError("Expected a capture of the official Warsaw Domino's directory")
    records = source["outlets"]
    pages = source["pages_verified"]
    if not records or math.ceil(len(records) / 8) != pages:
        raise ValueError("Captured outlet count is inconsistent with the verified eight-row pages")
    results = []
    for record in records:
        parsed_url = urlsplit(record["url"])
        if parsed_url.netloc != "www.dominospizza.pl" or not parsed_url.path.startswith(
            "/lokale/Warszawa/"
        ):
            raise ValueError("Unexpected Domino's outlet URL")
        raw_address = record["address"].strip()
        captions = re.findall(r"\(([^()]*)\)", raw_address)
        searchable_address = re.sub(r"\s*\([^()]*\)", "", raw_address).strip()
        street, house_number, _ = address_parts(searchable_address)
        if not street or not house_number:
            raise ValueError(f"Could not separate the published street and number: {raw_address}")
        result = outlet(
            "dominos",
            parsed_url.path.rsplit("/", 1)[-1],
            "Domino's Pizza",
            searchable_address,
            None,
            None,
            record["url"],
            source["retrieved_at"],
            {
                "source_display_address": raw_address,
                "source_location_captions": captions,
                "coordinate_note": "No coordinates in the captured official directory DOM",
                "house_number_note": "Published slash numbers are retained without unit inference",
            },
        )
        result["address"]["raw"] = raw_address + ", Warszawa"
        results.append(result)
    if len({row["source_id"] for row in results}) != len(results):
        raise ValueError("Duplicate Domino's outlet source URLs")
    apply_municipal_aliases(results)
    return {
        "schema_version": 1,
        "categories": [{"id": "pizza", "name": "Pizza"}],
        "brands": [
            {
                "id": "dominos",
                "name": "Domino's Pizza",
                "category_id": "pizza",
                "website": "https://www.dominospizza.pl/",
                "source_url": source_url,
                "retrieved_at": source["retrieved_at"],
                "coverage_status": "complete",
                "coverage_note": (
                    "All Warsaw directory pages verified in the normal public browser; "
                    "four pages contained 8+8+8+7 outlets in the initial capture. "
                    "Published inventory only; individual franchise ownership is not disclosed. "
                    "Coordinates are absent and subsequent municipal address matching is inferred."
                ),
            }
        ],
        "outlets": sorted(results, key=lambda row: row["source_id"]),
        "provenance": {
            "source_capture_sha256": hashlib.sha256(source_bytes).hexdigest(),
            "source_capture_bytes": len(source_bytes),
            "source_updated_at": None,
            "retrieved_at": source["retrieved_at"],
            "pages_verified": pages,
            "warsaw_outlet_count": len(results),
            "coordinate_count": 0,
            "collection_method": "Normal public browser DOM, with every pagination page verified",
            "license": "No open-data license identified; public locator facts with attribution",
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input", type=Path, default=OUTPUT_ROOT / "sources/dominos-warsaw-browser.json"
    )
    parser.add_argument("--output", type=Path, default=OUTPUT_ROOT / "dominos.json")
    arguments = parser.parse_args()
    source_bytes = arguments.input.read_bytes()
    result = normalize_snapshot(json.loads(source_bytes), source_bytes)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = arguments.output.with_suffix(".json.part")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(arguments.output)
    print(json.dumps(result["provenance"], indent=2))


if __name__ == "__main__":
    main()
