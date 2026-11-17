import argparse
import hashlib
import json
import math
import re
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

import psycopg
from psycopg.types.json import Jsonb

from app.core.config import Settings
from app.db.population_context_v2 import SCHEMA, VERSION

GRID_PATTERN = re.compile(r"PL_CRS3035RES500mN(\d+)E(\d+)\Z")


def load_snapshot(directory):
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    path = directory / "population-context.json"
    with path.open("rb") as stream:
        actual_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    if manifest.get("version") != VERSION or manifest.get("artifacts") != {
        "population-context.json": actual_hash
    }:
        raise ValueError("Population context manifest or content changed")
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    source = snapshot["source"]
    city = source.get("city")
    if (
        snapshot.get("version") != VERSION
        or city not in {"warsaw", "radom"}
        or source.get("id") != f"{city}-gus-national-population-500m-2021-v2"
        or source.get("resolution_m") != 500
        or source.get("population_definition") != "national_definition"
        or source.get("coordinate_reference_system") != "EPSG:3035"
        or source.get("observed_at") != "2021-03-31"
        or source.get("absence_means_zero") is not False
        or manifest.get("source") != source
        or urlparse(source.get("url", "")).hostname != "geo.stat.gov.pl"
    ):
        raise ValueError("Population context source differs from the versioned contract")
    retrieved = datetime.fromisoformat(source["retrieved_at"])
    if retrieved.tzinfo is None or date.fromisoformat(source["observed_at"]) > retrieved.date():
        raise ValueError("Population source requires valid observation and retrieval dates")
    bounds = source.get("grid_support_bounds_epsg3035")
    if (
        not isinstance(bounds, list)
        or len(bounds) != 4
        or any(
            type(value) is not int or not math.isfinite(value) or value % 500 for value in bounds
        )
        or bounds[0] >= bounds[2]
        or bounds[1] >= bounds[3]
    ):
        raise ValueError("Population context requires a valid native grid envelope")
    identities, positions = set(), set()
    for cell in snapshot["cells"]:
        match = GRID_PATTERN.fullmatch(cell["cell_id"])
        x, y, value = cell["easting"], cell["northing"], cell["population"]
        if (
            not match
            or type(x) is not int
            or type(y) is not int
            or x % 500
            or y % 500
            or (int(match[2]), int(match[1])) != (x, y)
            or not (bounds[0] <= x < bounds[2] and bounds[1] <= y < bounds[3])
            or value is not None
            and (type(value) is not int or value < 0)
            or cell["status"] != ("observed" if value is not None else "missing")
            or cell["cell_id"] in identities
            or (x, y) in positions
        ):
            raise ValueError("Population context contains invalid or duplicate cells")
        identities.add(cell["cell_id"])
        positions.add((x, y))
    if not identities or len(identities) != manifest.get("cells"):
        raise ValueError("Population context cell count is inconsistent")
    return snapshot, actual_hash


def import_context(directory: Path, settings: Settings | None = None):
    snapshot, digest = load_snapshot(directory)
    source = {**snapshot["source"], "snapshot_sha256": digest, "version": VERSION}
    settings = settings or Settings()
    with psycopg.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        dbname=settings.postgres_db,
        user=settings.postgres_user,
        password=settings.postgres_password.get_secret_value(),
        connect_timeout=5,
    ) as connection:
        connection.execute(SCHEMA)
        connection.execute(
            "INSERT INTO population_context_sources(id,metadata) VALUES(%s,%s) "
            "ON CONFLICT(id) DO UPDATE SET metadata=excluded.metadata,imported_at=now()",
            (source["id"], Jsonb(source)),
        )
        connection.execute(
            "DELETE FROM population_context_cells WHERE source_id=%s", (source["id"],)
        )
        with connection.cursor().copy(
            "COPY population_context_cells(source_id,cell_id,easting,northing,population,status) "
            "FROM STDIN"
        ) as stream:
            for cell in snapshot["cells"]:
                stream.write_row(
                    (
                        source["id"],
                        cell["cell_id"],
                        cell["easting"],
                        cell["northing"],
                        cell["population"],
                        cell["status"],
                    )
                )
        if connection.execute(
            "SELECT to_regprocedure('invalidate_building_profiles()') IS NOT NULL"
        ).fetchone()[0]:
            connection.execute("SELECT invalidate_building_profiles()")
    return {"source_id": source["id"], "cells": len(snapshot["cells"]), "sha256": digest}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", type=Path, required=True)
    arguments = parser.parse_args()
    print(json.dumps(import_context(arguments.input_dir)))


if __name__ == "__main__":
    main()
