import argparse
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from app.core.config import Settings
from app.core.data_audit import (
    AUDIT_VERSION,
    integrity_check,
    source_date_issues,
    summarize_population_sample,
)
from app.core.data_contract import data_contract

TABLES = (
    "map_sources",
    "buildings",
    "addresses",
    "franchise_brands",
    "franchise_outlets",
    "profile_sources",
    "profile_population",
    "profile_pois",
    "profile_activity",
)


def json_value(value: Any) -> str | float:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"Unsupported report value: {type(value).__name__}")


def audit_database(connection: psycopg.Connection, sample_size: int = 60) -> dict:
    if not 1 <= sample_size <= 500:
        raise ValueError("Population sample size must be between 1 and 500 buildings")
    cursor = connection.cursor(row_factory=dict_row)

    def rows(query: str, parameters: tuple = ()) -> list[dict]:
        return cursor.execute(query, parameters).fetchall()

    def one(query: str, parameters: tuple = ()) -> dict:
        return rows(query, parameters)[0]

    tables = {
        name: one(
            "SELECT to_regclass(format('%%I.%%I',current_schema(),%s::text))"
            " IS NOT NULL AS present",
            (name,),
        )["present"]
        for name in TABLES
    }
    report = {
        "version": AUDIT_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "scope": "Data readiness only; no model validation or commercial outcome labels",
        "database_read_only": one("SHOW transaction_read_only")["transaction_read_only"] == "on",
        "transaction_isolation": one("SHOW transaction_isolation")["transaction_isolation"],
        "available_tables": tables,
        "checks": [],
        "datasets": {},
        "sources": [],
        "planned_data_contract": data_contract(),
        "limitations": [
            "Unlisted buildings are unknown outcomes, not negative business examples",
            "Outlet presence does not establish commercial success or franchise ownership",
            "Public locators and OpenStreetMap are not an exhaustive census of trading businesses",
            "Point/building associations are inferred; conflicts remain unresolved",
            "Building footprint area is not the usable area of an available commercial unit",
            "Rent, vacancy, utilities, premises permissions and sales outcomes remain unknown",
            "Missing population rows remain unknown; no zero filling or extrapolation",
            "Published-cell area fraction does not quantify the share of residents captured",
            "OSM edit/snapshot dates and download/import dates are not on-site observation dates",
            "Activity POIs represent mapped features; offices are not employee or company counts",
        ],
    }
    checks = report["checks"]
    datasets = report["datasets"]
    for table, present in tables.items():
        if not present:
            checks.append(
                {"id": f"{table}_availability", "status": "warning", "detail": "Not imported"}
            )
    if tables["map_sources"]:
        report["sources"].extend(
            {"kind": "municipal", **row, "retrieved_at": None}
            for row in rows("SELECT * FROM map_sources ORDER BY id")
        )
    if tables["buildings"]:
        dataset = one("""
            SELECT count(*) AS count,count(DISTINCT source_id) AS unique_source_ids,
                count(*) FILTER(WHERE source_id='') AS empty_ids,
                count(*) FILTER(WHERE NOT ST_IsValid(geometry)
                    OR ST_IsEmpty(geometry)) AS invalid_geometries,
                count(*) FILTER(WHERE properties->>'ID_BUDYNKU'
                    IS DISTINCT FROM source_id) AS source_identity_mismatches,
                count(*) FILTER(WHERE properties->>'KONDYGNACJE_NADZIEMNE'
                    IS NOT NULL) AS aboveground_storeys_present,
                count(*) FILTER(WHERE properties->>'KONDYGNACJE_NADZIEMNE'='0')
                    AS aboveground_storeys_zero,
                count(*) FILTER(WHERE properties->>'KONDYGNACJE_PODZIEMNE'
                    IS NOT NULL) AS underground_storeys_present
            FROM buildings
        """)
        datasets["buildings"] = dataset
        checks.append(
            integrity_check(
                "building_stable_identity",
                dataset["count"]
                - dataset["unique_source_ids"]
                + dataset["empty_ids"]
                + dataset["source_identity_mismatches"],
                "Numeric IDs are internal; official ID_BUDYNKU remains the refresh/link identity",
            )
        )
        checks.append(
            integrity_check(
                "building_geometry", dataset["invalid_geometries"], "Valid nonempty footprints"
            )
        )
    if tables["addresses"] and tables["buildings"]:
        dataset = one("""
            SELECT count(*) AS count,count(DISTINCT a.source_id) AS unique_source_ids,
                count(DISTINCT building_source_id) AS linked_buildings,
                count(*) FILTER(WHERE a.properties->>'ID_IIP' IS NULL OR
                    left(a.properties->>'ID_IIP',length(a.source_id)+1)<>a.source_id||'.')
                    AS source_identity_mismatches,
                count(*) FILTER(WHERE (match_status='matched')
                    IS DISTINCT FROM (building_source_id IS NOT NULL)) AS link_status_mismatches,
                count(*) FILTER(WHERE match_status='matched' AND
                    (candidate_count<>1 OR NOT ST_Covers(b.geometry,a.geometry)))
                    AS invalid_containment_links
            FROM addresses a LEFT JOIN buildings b ON b.source_id=a.building_source_id
        """)
        dataset["by_match_status"] = rows(
            "SELECT match_status,count(*) AS count FROM addresses GROUP BY 1 ORDER BY 1"
        )
        dataset["normalized_duplicate_street_house_groups"] = one("""
            SELECT count(*) AS count FROM (SELECT 1 FROM addresses
                GROUP BY street_normalized,house_number_normalized HAVING count(*)>1) groups
        """)["count"]
        dataset["normalized_duplicate_street_house_postcode_groups"] = one("""
            SELECT count(*) AS count FROM (SELECT 1 FROM addresses
                GROUP BY street_normalized,house_number_normalized,postal_code
                HAVING count(*)>1) groups
        """)["count"]
        dataset["official_duplicate_street_house_groups"] = one("""
            SELECT count(*) AS count FROM (SELECT 1 FROM addresses
                GROUP BY street,house_number HAVING count(*)>1) groups
        """)["count"]
        dataset["official_duplicate_street_house_postcode_groups"] = one("""
            SELECT count(*) AS count FROM (SELECT 1 FROM addresses
                GROUP BY street,house_number,postal_code HAVING count(*)>1) groups
        """)["count"]
        dataset["normalized_address_ambiguity_examples"] = rows("""
            SELECT street_normalized,house_number_normalized,postal_code,count(*) AS count,
                array_agg(DISTINCT address ORDER BY address) AS official_labels,
                array_agg(DISTINCT building_source_id ORDER BY building_source_id) AS buildings
            FROM addresses GROUP BY 1,2,3 HAVING count(*)>1 ORDER BY 1,2,3 LIMIT 10
        """)
        if dataset["normalized_duplicate_street_house_postcode_groups"]:
            checks.append(
                {
                    "id": "normalized_address_ambiguity",
                    "status": "warning",
                    "detail": "Postcode can leave normalized addresses ambiguous",
                    "groups": dataset["normalized_duplicate_street_house_postcode_groups"],
                }
            )
        datasets["addresses"] = dataset
        checks.append(
            integrity_check(
                "address_stable_identity",
                dataset["count"]
                - dataset["unique_source_ids"]
                + dataset["source_identity_mismatches"],
                "EMUiA namespace and UUID remain stable; raw ID_IIP retains its version suffix",
            )
        )
        checks.append(
            integrity_check(
                "address_associations",
                dataset["link_status_mismatches"] + dataset["invalid_containment_links"],
                "Only unique ST_Covers matches link addresses",
            )
        )
    if tables["franchise_brands"] and tables["franchise_outlets"]:
        dataset = one("""
            SELECT count(*) AS count,count(DISTINCT (brand_id,source_id)) AS unique_source_ids,
                count(display_geometry) AS located_outlets,
                count(*) FILTER(WHERE match_status='matched') AS matched_outlets,
                count(DISTINCT building_source_id) AS linked_buildings,
                count(*) FILTER(WHERE operating_model='unknown') AS ownership_unknown,
                count(*) FILTER(WHERE (match_status='matched')
                    IS DISTINCT FROM (building_source_id IS NOT NULL)) AS link_status_mismatches
            FROM franchise_outlets
        """)
        dataset["by_brand"] = rows("""
            SELECT b.id,b.name,b.category_id,b.coverage_status,b.coverage_note,b.retrieved_at,
                count(o.id) AS outlets,count(o.display_geometry) AS located,
                count(o.building_source_id) AS matched,
                count(DISTINCT o.building_source_id) AS matched_buildings
            FROM franchise_brands b LEFT JOIN franchise_outlets o ON b.id=o.brand_id
            GROUP BY b.id ORDER BY b.id
        """)
        dataset["by_match_method"] = rows("""
            SELECT match_status,match_method,count(*) AS count FROM franchise_outlets
            GROUP BY 1,2 ORDER BY 1,2
        """)
        dataset["building_category_presence"] = rows("""
            SELECT b.category_id,count(*) AS matched_outlet_records,
                count(DISTINCT o.building_source_id) AS distinct_building_category_pairs
            FROM franchise_outlets o JOIN franchise_brands b ON b.id=o.brand_id
            WHERE o.match_status='matched' AND o.building_source_id IS NOT NULL
            GROUP BY 1 ORDER BY 1
        """)
        dataset["presence_semantics"] = (
            "One presence per stable building/category pair; multiple outlets are retained. "
            "Buildings without matched outlets have unknown outcomes, not negative labels."
        )
        dataset["presence_observations"] = rows("""
            SELECT o.building_source_id,b.category_id,
                array_agg(DISTINCT o.brand_id ORDER BY o.brand_id) AS brand_ids,
                jsonb_agg(jsonb_build_object('brand_id',o.brand_id,'source_id',o.source_id,
                    'source_url',o.source_url,'match_method',o.match_method)
                    ORDER BY o.brand_id,o.source_id) AS source_outlets
            FROM franchise_outlets o JOIN franchise_brands b ON b.id=o.brand_id
            WHERE o.match_status='matched' AND o.building_source_id IS NOT NULL
            GROUP BY o.building_source_id,b.category_id ORDER BY 1,2
        """)
        dataset["excluded_unresolved_outlets"] = rows("""
            SELECT brand_id,source_id,address_raw,source_url,match_status,match_method,
                match_note,display_geometry IS NOT NULL AS has_display_location,
                candidate_building_ids
            FROM franchise_outlets WHERE match_status<>'matched' OR building_source_id IS NULL
            ORDER BY brand_id,source_id
        """)
        dataset["invalid_locator_geometries"] = one("""
            SELECT count(*) AS count FROM franchise_outlets WHERE geometry IS NOT NULL
                AND (NOT ST_IsValid(geometry) OR ST_IsEmpty(geometry))
        """)["count"]
        checks.append(
            integrity_check(
                "outlet_geometry",
                dataset["invalid_locator_geometries"],
                "Missing coordinates remain null; display points do not create footprint links",
            )
        )
        datasets["franchises"] = dataset
        checks.append(
            integrity_check(
                "outlet_identity_and_links",
                dataset["count"] - dataset["unique_source_ids"] + dataset["link_status_mismatches"],
                "Conflicting or unmatched outlets have no building link",
            )
        )
        if tables["buildings"] and tables["addresses"]:
            associations = one("""
                WITH evidence AS (
                    SELECT o.*,b.geometry AS building_geometry,
                        p.point_count,a.address_count,a.same_building_count
                    FROM franchise_outlets o JOIN buildings b ON b.source_id=o.building_source_id
                    CROSS JOIN LATERAL (
                        SELECT count(*) AS point_count FROM buildings other
                        WHERE other.geometry && o.geometry AND ST_Covers(other.geometry,o.geometry)
                    ) p
                    CROSS JOIN LATERAL (
                        SELECT count(*) AS address_count,
                            count(*) FILTER(WHERE a.building_source_id=o.building_source_id)
                                AS same_building_count
                        FROM addresses a WHERE a.street_normalized=o.street_normalized
                            AND a.house_number_normalized=o.house_number_normalized
                            AND (o.postal_code IS NULL OR a.postal_code=o.postal_code)
                    ) a WHERE o.match_status='matched'
                ) SELECT count(*) FILTER(WHERE NOT coalesce(CASE
                    WHEN match_method='point_inside_building' THEN
                        geometry IS NOT NULL AND point_count=1
                        AND ST_Covers(building_geometry,geometry) AND address_count=0
                    WHEN match_method='address_and_point' THEN
                        geometry IS NOT NULL AND point_count=1
                        AND ST_Covers(building_geometry,geometry) AND same_building_count>0
                    WHEN match_method='exact_address' THEN
                        geometry IS NULL AND address_count>0 AND same_building_count=address_count
                    WHEN match_method='exact_address_near_point' THEN
                        geometry IS NOT NULL AND point_count=0 AND address_count=1
                        AND same_building_count=1 AND ST_DWithin(ST_Transform(geometry,2180),
                            ST_Transform(building_geometry,2180),10)
                        AND match_distance_m BETWEEN 0 AND 10
                    ELSE false END,false)) AS violations FROM evidence
            """)
            dataset["association_evidence_violations"] = associations["violations"]
            checks.append(
                integrity_check(
                    "outlet_association_evidence",
                    associations["violations"],
                    "Linked footprints satisfy their declared point/address/10-metre method",
                )
            )
        report["sources"].extend(
            {"kind": "official_brand_locator", **row}
            for row in rows("""
                SELECT id,name,source_url,retrieved_at,coverage_status,coverage_note,provenance
                FROM franchise_brands ORDER BY id
            """)
        )
    if tables["profile_sources"]:
        report["sources"].extend(
            rows("""
            SELECT id,kind,metadata,imported_at,feature_count FROM profile_sources ORDER BY kind
        """)
        )
    if tables["profile_population"]:
        dataset = one("""
            SELECT count(*) AS count,count(DISTINCT source_id) AS unique_source_ids,
                count(*) FILTER(WHERE status='observed') AS published_cells,
                count(*) FILTER(WHERE status='missing') AS missing_cells,
                count(*) FILTER(WHERE status='suppressed') AS suppressed_cells,
                count(*) FILTER(WHERE status='observed' AND population=0) AS explicit_zero_cells,
                sum(population) FILTER(WHERE status='observed') AS regional_published_total,
                count(*) FILTER(WHERE (status='observed') IS DISTINCT FROM
                    (population IS NOT NULL) OR population<0) AS population_semantic_violations,
                array_agg(DISTINCT observation_year) AS observation_years,
                array_agg(DISTINCT resolution_m) AS resolutions_m FROM profile_population
        """)
        dataset["regional_total_scope"] = (
            "Imported regional grid rectangle, not Warsaw municipality"
        )
        dataset["absent_rows_mean_zero"] = False
        dataset["sample_method"] = (
            "First stable source IDs ordered by md5(source_id), then source_id"
        )
        if tables["buildings"]:
            sample = rows(
                """
                WITH selected AS MATERIALIZED (
                    SELECT source_id,ST_PointOnSurface(ST_Transform(geometry,2180)) AS origin
                    FROM buildings ORDER BY md5(source_id),source_id LIMIT %s
                ), areas AS (
                    SELECT source_id,radius_m,ST_Buffer(origin,radius_m,64) AS geometry
                    FROM selected CROSS JOIN unnest(ARRAY[300,500,1000]) AS radius_m
                ) SELECT a.source_id,a.radius_m,
                    coalesce(sum(ST_Area(ST_Intersection(p.geometry,a.geometry)))
                        FILTER(WHERE p.status='observed')/ST_Area(a.geometry),0)
                        AS published_cell_area_fraction,
                    sum(p.population*ST_Area(ST_Intersection(p.geometry,a.geometry))
                        /ST_Area(p.geometry)) FILTER(WHERE p.status='observed'
                            AND ST_Area(ST_Intersection(p.geometry,a.geometry))>0)
                        AS population_subtotal
                FROM areas a LEFT JOIN profile_population p
                    ON p.geometry && a.geometry AND ST_Intersects(p.geometry,a.geometry)
                GROUP BY a.source_id,a.radius_m,a.geometry ORDER BY a.source_id,a.radius_m
            """,
                (sample_size,),
            )
            dataset["catchment_sample"] = sample
            dataset["catchment_summary"] = summarize_population_sample(sample)
            checks.append(
                integrity_check(
                    "population_sample_overlap",
                    sum(row["published_cell_area_fraction"] > 1.000001 for row in sample),
                    "Sampled published-cell overlap does not exceed catchment area",
                )
            )
        datasets["population"] = dataset
        checks.append(
            integrity_check(
                "population_missingness",
                dataset["population_semantic_violations"],
                "Unknown cells retain null population; explicit observed zero remains distinct",
            )
        )
    for table, kind in (("profile_pois", "pois"), ("profile_activity", "activity")):
        if not tables[table]:
            continue
        dataset = one(f"""
            SELECT count(*) AS count,count(DISTINCT source_id) AS unique_source_ids,
                count(*) FILTER(WHERE duplicate_of IS NULL) AS retained_records,
                count(*) FILTER(WHERE duplicate_of IS NOT NULL) AS duplicate_records,
                count(*) FILTER(WHERE updated_at IS NULL) AS unknown_edit_dates,
                count(*) FILTER(WHERE NOT ST_IsValid(geometry)
                    OR ST_IsEmpty(geometry)) AS invalid_geometries FROM {table}
        """)
        dataset["category_memberships"] = rows(f"""
            SELECT category,count(*) AS source_records,
                count(*) FILTER(WHERE duplicate_of IS NULL) AS retained_records
            FROM {table} CROSS JOIN unnest(categories) AS category GROUP BY 1 ORDER BY 1
        """)
        dataset["location_methods"] = rows(f"""
            SELECT location_method,count(*) AS count FROM {table} GROUP BY 1 ORDER BY 1
        """)
        if kind == "activity":
            dataset["lifecycle_conflict_examples"] = rows("""
                SELECT source_id,name,tags->>'end_date' AS end_date,
                    tags->>'check_date' AS check_date,source_url
                FROM profile_activity WHERE coalesce(tags->>'end_date','')<>'' ORDER BY source_id
            """)
            dataset["lifecycle_conflict_count"] = len(dataset["lifecycle_conflict_examples"])
            dataset["lifecycle_note"] = (
                "An end_date alongside current activity tags needs source review; raw tags retained"
            )
        invalid_duplicates = one(f"""
            SELECT count(*) AS count FROM {table} p LEFT JOIN {table} retained
                ON retained.source_id=p.duplicate_of WHERE p.duplicate_of IS NOT NULL
                AND (retained.source_id IS NULL OR retained.duplicate_of IS NOT NULL
                    OR p.source_id=p.duplicate_of OR p.categories<>retained.categories
                    OR p.kind<>retained.kind)
        """)["count"]
        dataset["invalid_duplicate_links"] = invalid_duplicates
        dataset["completeness"] = (
            "Unknown; bounded source extract is not an exhaustive venue census"
        )
        datasets[kind] = dataset
        checks.append(
            integrity_check(
                f"{kind}_identity_geometry_duplicates",
                dataset["count"]
                - dataset["unique_source_ids"]
                + dataset["invalid_geometries"]
                + invalid_duplicates,
                "Duplicate links preserve categories and point directly to a retained record",
            )
        )
    for source in report["sources"]:
        metadata = source.get("metadata", source)
        source["date_issues"] = source_date_issues(metadata)
        kind = source.get("kind")
        if kind in {"population", "pois", "activity"} and kind in datasets:
            checks.append(
                integrity_check(
                    f"{kind}_manifest_count",
                    abs(source["feature_count"] - datasets[kind]["count"]),
                    "Imported source feature count agrees with stored raw records",
                )
            )
        municipal_kind = {"warsaw-buildings": "buildings", "warsaw-addresses": "addresses"}.get(
            source.get("id")
        )
        if kind == "municipal" and municipal_kind in datasets:
            checks.append(
                integrity_check(
                    f"{municipal_kind}_manifest_count",
                    abs(source["feature_count"] - datasets[municipal_kind]["count"]),
                    "Municipal source feature count agrees with stored records",
                )
            )
    report["status"] = (
        "integrity_failures" if any(c["status"] == "fail" for c in checks) else "audited"
    )
    return report


def run_audit(settings: Settings, sample_size: int = 60) -> dict:
    with psycopg.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        dbname=settings.postgres_db,
        user=settings.postgres_user,
        password=settings.postgres_password.get_secret_value(),
        connect_timeout=5,
    ) as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        connection.execute("SET LOCAL statement_timeout='120s'")
        return audit_database(connection, sample_size)


def attach_local_manifests(report: dict, directory: Path) -> None:
    manifests = []
    for identifier in ("warsaw-buildings", "warsaw-addresses"):
        path = directory / f"{identifier}.metadata.json"
        if not path.is_file():
            continue
        metadata = json.loads(path.read_text(encoding="utf-8"))
        manifests.append(
            {
                "source_id": identifier,
                "path": str(path.resolve()),
                "metadata": metadata,
                "verification_scope": (
                    "Local acquisition manifest; database import has no stored file hash"
                ),
                "date_issues": source_date_issues(metadata),
            }
        )
    report["local_acquisition_manifests"] = manifests


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only audit of imported Warsaw public datasets"
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--population-sample-size", type=int, default=60)
    parser.add_argument("--manifest-directory", type=Path)
    arguments = parser.parse_args()
    try:
        report = run_audit(Settings(), arguments.population_sample_size)
        if arguments.manifest_directory:
            attach_local_manifests(report, arguments.manifest_directory)
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=json_value) + "\n",
            encoding="utf-8",
        )
    except (OSError, ValueError, psycopg.Error) as error:
        parser.exit(1, f"Data audit failed: {error}\n")
    print(json.dumps({"status": report["status"], "output": str(arguments.output.resolve())}))
    if report["status"] == "integrity_failures":
        parser.exit(2)


if __name__ == "__main__":
    main()
