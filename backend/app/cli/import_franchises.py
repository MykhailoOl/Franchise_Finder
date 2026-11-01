import argparse
import json
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from app.cli.import_addresses import ADDRESS_SCHEMA_PATH
from app.cli.import_buildings import SCHEMA_PATH
from app.core.address_search import normalize_house_number, normalize_street
from app.core.config import Settings
from app.core.franchise_snapshot import FranchiseSnapshot
from app.db.profile_cache import invalidate_profiles

FRANCHISE_SCHEMA_PATH = SCHEMA_PATH.with_name("franchise_schema.sql")

UPSERT_OUTLETS = """
INSERT INTO franchise_outlets (
    brand_id,source_id,name,address_raw,street,street_normalized,house_number,
    house_number_normalized,postal_code,city,geometry,source_url,retrieved_at,
    status,operating_model,operating_model_source_url,attributes
) VALUES (
    %(brand_id)s,%(source_id)s,%(name)s,%(address_raw)s,%(street)s,%(street_normalized)s,
    %(house_number)s,%(house_number_normalized)s,%(postal_code)s,%(city)s,
    CASE WHEN %(longitude)s::float8 IS NOT NULL THEN ST_Transform(
        ST_SetSRID(ST_Point(%(longitude)s,%(latitude)s),4326),3857) END,
    %(source_url)s,%(retrieved_at)s,%(status)s,%(operating_model)s,
    %(operating_model_source_url)s,%(attributes)s
) ON CONFLICT (brand_id,source_id) DO UPDATE SET
    name=EXCLUDED.name,address_raw=EXCLUDED.address_raw,street=EXCLUDED.street,
    street_normalized=EXCLUDED.street_normalized,house_number=EXCLUDED.house_number,
    house_number_normalized=EXCLUDED.house_number_normalized,postal_code=EXCLUDED.postal_code,
    city=EXCLUDED.city,geometry=EXCLUDED.geometry,source_url=EXCLUDED.source_url,
    retrieved_at=EXCLUDED.retrieved_at,status=EXCLUDED.status,
    operating_model=EXCLUDED.operating_model,
    operating_model_source_url=EXCLUDED.operating_model_source_url,attributes=EXCLUDED.attributes
"""


def load_snapshots(paths: list[Path]) -> list[FranchiseSnapshot]:
    snapshots = [FranchiseSnapshot.model_validate_json(path.read_bytes()) for path in paths]
    brands = [brand.id for snapshot in snapshots for brand in snapshot.brands]
    if len(brands) != len(set(brands)):
        raise ValueError("A brand can appear in only one input snapshot per import")
    category_names: dict[str, str] = {}
    for snapshot in snapshots:
        for category in snapshot.categories:
            if category.id in category_names and category_names[category.id] != category.name:
                raise ValueError(f"Conflicting category names for {category.id}")
            category_names[category.id] = category.name
    return snapshots


def import_franchises(paths: list[Path], settings: Settings) -> dict[str, Any]:
    snapshots = load_snapshots(paths)
    with psycopg.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        dbname=settings.postgres_db,
        user=settings.postgres_user,
        password=settings.postgres_password.get_secret_value(),
        connect_timeout=5,
    ) as connection:
        connection.execute("SELECT pg_advisory_xact_lock(hashtext('masterfranchises-warsaw-map'))")
        connection.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
        connection.execute(ADDRESS_SCHEMA_PATH.read_text(encoding="utf-8"))
        connection.execute(FRANCHISE_SCHEMA_PATH.read_text(encoding="utf-8"))
        for snapshot in snapshots:
            for category in snapshot.categories:
                connection.execute(
                    "INSERT INTO franchise_categories VALUES (%s,%s)"
                    " ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name",
                    (category.id, category.name),
                )
            for brand in snapshot.brands:
                if (
                    brand.coverage_status == "unavailable"
                    and connection.execute(
                        "SELECT EXISTS(SELECT 1 FROM franchise_outlets WHERE brand_id=%s)",
                        (brand.id,),
                    ).fetchone()[0]
                ):
                    raise ValueError(
                        f"Unavailable snapshot cannot replace existing {brand.id} inventory"
                    )
                connection.execute(
                    "INSERT INTO franchise_brands VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                    " ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name,"
                    " category_id=EXCLUDED.category_id,website=EXCLUDED.website,"
                    " source_url=EXCLUDED.source_url,retrieved_at=EXCLUDED.retrieved_at,"
                    " coverage_status=EXCLUDED.coverage_status,"
                    " coverage_note=EXCLUDED.coverage_note,"
                    " provenance=EXCLUDED.provenance",
                    (
                        brand.id,
                        brand.name,
                        brand.category_id,
                        str(brand.website),
                        str(brand.source_url),
                        brand.retrieved_at,
                        brand.coverage_status,
                        brand.coverage_note,
                        Jsonb(snapshot.provenance),
                    ),
                )
            parameters = []
            for outlet in snapshot.outlets:
                address = outlet.address
                parameters.append(
                    {
                        "brand_id": outlet.brand_id,
                        "source_id": outlet.source_id,
                        "name": outlet.name,
                        "address_raw": address.raw,
                        "street": address.street,
                        "street_normalized": normalize_street(address.street)
                        if address.street
                        else None,
                        "house_number": address.house_number,
                        "house_number_normalized": normalize_house_number(address.house_number)
                        if address.house_number
                        else None,
                        "postal_code": address.postal_code.strip() if address.postal_code else None,
                        "city": address.city,
                        "longitude": outlet.coordinates[0] if outlet.coordinates else None,
                        "latitude": outlet.coordinates[1] if outlet.coordinates else None,
                        "source_url": str(outlet.source_url),
                        "retrieved_at": outlet.retrieved_at,
                        "status": outlet.status,
                        "operating_model": outlet.operating_model,
                        "operating_model_source_url": str(outlet.operating_model_source_url)
                        if outlet.operating_model_source_url
                        else None,
                        "attributes": Jsonb(outlet.attributes),
                    }
                )
            with connection.cursor() as cursor:
                cursor.executemany(UPSERT_OUTLETS, parameters)
            for brand in snapshot.brands:
                if brand.coverage_status != "complete":
                    continue
                source_ids = [o.source_id for o in snapshot.outlets if o.brand_id == brand.id]
                connection.execute(
                    "DELETE FROM franchise_outlets WHERE brand_id=%s AND NOT(source_id=ANY(%s))",
                    (brand.id, source_ids),
                )
        connection.execute("SELECT refresh_franchise_links()")
        invalidate_profiles(connection)
        connection.execute("ANALYZE franchise_outlets")
        connection.execute("ANALYZE building_franchise_summary")
        rows = connection.execute(
            "SELECT brand_id,match_status,count(*) FROM franchise_outlets GROUP BY 1,2 ORDER BY 1,2"
        ).fetchall()
        mapped, matched, total = connection.execute(
            "SELECT count(display_geometry),count(building_source_id),count(*)"
            " FROM franchise_outlets"
        ).fetchone()
    return {
        "total_outlets": total,
        "mapped_outlets": mapped,
        "matched_outlets": matched,
        "matches": [
            dict(zip(("brand_id", "match_status", "count"), row, strict=True)) for row in rows
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import official Warsaw franchise locator snapshots"
    )
    parser.add_argument("paths", nargs="+", type=Path)
    arguments = parser.parse_args()
    try:
        result = import_franchises(arguments.paths, Settings())
    except (OSError, ValueError, psycopg.Error) as error:
        parser.exit(1, f"Import failed; previous franchise inventory retained: {error}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
