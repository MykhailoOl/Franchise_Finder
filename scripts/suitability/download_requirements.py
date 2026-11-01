import argparse
import hashlib
import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "brand_requirements", ROOT / "backend/app/core/brand_requirements.py"
)
catalog = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(catalog)


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def acquire_source(source: dict, output: Path, refresh: bool) -> dict:
    cached = output / f"{source['id']}.html"
    if cached.exists() and not refresh:
        content = cached.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if digest != source["reviewed_sha256"]:
            raise ValueError(f"Reviewed cached source checksum differs: {source['id']}")
        return {
            **source,
            "path": str(cached.relative_to(output.parent)),
            "sha256": digest,
            "matches_reviewed_snapshot": True,
        }
    request = Request(source["url"], headers={"User-Agent": "MasterFranchises/0.1 research"})
    with urlopen(request, timeout=45) as response:
        content = response.read()
        final_url = response.url
        content_type = response.headers.get_content_type()
    if content_type != "text/html" or not content:
        raise ValueError(f"Unexpected evidence response for {source['id']}: {content_type}")
    digest = hashlib.sha256(content).hexdigest()
    destination = output / f"{source['id']}.{digest}.html"
    destination.write_bytes(content)
    return {
        **source,
        "retrieved_at": datetime.now(UTC).isoformat(),
        "reviewed_retrieved_at": source["retrieved_at"],
        "final_url": final_url,
        "path": str(destination.relative_to(output.parent)),
        "sha256": digest,
        "matches_reviewed_snapshot": digest == source["reviewed_sha256"],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=ROOT / ".local/data/suitability")
    parser.add_argument("--refresh", action="store_true")
    arguments = parser.parse_args()
    output = arguments.output_dir / "requirements-sources"
    output.mkdir(parents=True, exist_ok=True)
    sources = {
        source["id"]: source
        for brand in catalog.REQUIREMENTS_BY_BRAND.values()
        for source in brand["sources"]
    }
    snapshots = []
    for source in sources.values():
        try:
            snapshots.append(acquire_source(source, output, arguments.refresh))
        except (OSError, ValueError) as error:
            snapshots.append({**source, "error": str(error), "matches_reviewed_snapshot": False})
    evidence = {
        "requirements_version": catalog.REQUIREMENTS_VERSION,
        "checked_at": datetime.now(UTC).isoformat(),
        "sources": snapshots,
        "notes": [
            "The tracked catalog contains manually reviewed public requirements, "
            "not an eligibility engine.",
            "A changed HTML hash requires review; it can reflect layout changes "
            "rather than policy changes.",
            "Fetching a newer page does not silently change the curated claims "
            "or their review dates.",
            "Raw HTML snapshots are retained locally as evidence; "
            "no republication license is asserted.",
        ],
    }
    write_json(arguments.output_dir / "requirements-evidence.json", evidence)
    write_json(
        arguments.output_dir / "brand-requirements.json",
        {
            "schema_version": 1,
            "requirements_version": catalog.REQUIREMENTS_VERSION,
            "brands": list(catalog.REQUIREMENTS_BY_BRAND.values()),
        },
    )
    print(
        json.dumps(
            {
                "brands": len(catalog.REQUIREMENTS_BY_BRAND),
                "reviewed_sources": len(sources),
                "matching_snapshots": sum(item["matches_reviewed_snapshot"] for item in snapshots),
                "acquisition_errors": sum("error" in item for item in snapshots),
                "changed_or_missing_sources": [
                    item["id"] for item in snapshots if not item["matches_reviewed_snapshot"]
                ],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
