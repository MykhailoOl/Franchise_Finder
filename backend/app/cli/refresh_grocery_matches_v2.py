import argparse
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from app.cli.extract_grocery_features import load_json, write_json
from app.core.config import Settings
from app.core.grocery_matching_v2 import MATCHING_VERSION, POLICY, decide_match, stable_hash
from app.db.buildings import WARSAW_BOUNDS
from app.db.grocery_matching_v2 import collect_matching_evidence
from app.db.profile_cache import invalidate_profiles

BRANDS = ("zabka", "lewiatan")


def summarize(decisions: list[dict]) -> dict:
    before = Counter(row["previous_match_status"] for row in decisions)
    after = Counter(row["match_status"] for row in decisions)
    changed = [
        row
        for row in decisions
        if (row["previous_match_status"], row["previous_building_source_id"])
        != (row["match_status"], row["building_source_id"])
    ]
    return {
        "outlets": len(decisions),
        "before": dict(sorted(before.items())),
        "after": dict(sorted(after.items())),
        "new_associations": sum(
            row["previous_match_status"] != "matched" and row["match_status"] == "matched"
            for row in decisions
        ),
        "withdrawn_associations": sum(
            row["previous_match_status"] == "matched" and row["match_status"] != "matched"
            for row in decisions
        ),
        "changed_building_associations": sum(
            row["previous_match_status"] == row["match_status"] == "matched"
            and row["previous_building_source_id"] != row["building_source_id"]
            for row in decisions
        ),
        "matched_buildings_before": len(
            {
                row["previous_building_source_id"]
                for row in decisions
                if row["previous_match_status"] == "matched" and row["previous_building_source_id"]
            }
        ),
        "matched_buildings_after": len(
            {row["building_source_id"] for row in decisions if row["match_status"] == "matched"}
        ),
        "changes": [
            {
                key: row[key]
                for key in (
                    "brand_id",
                    "source_id",
                    "previous_match_status",
                    "previous_building_source_id",
                    "match_status",
                    "building_source_id",
                    "match_method",
                    "reason",
                    "postal_status",
                )
            }
            for row in changed
        ],
    }


def collect(connection) -> list[dict]:
    return collect_matching_evidence(
        connection,
        building_dataset_id="warsaw-buildings",
        address_dataset_id="warsaw-addresses",
        bounds=list(WARSAW_BOUNDS),
        brand_ids=BRANDS,
    )


def make_report(connection) -> dict:
    evidence = collect(connection)
    decisions = [decide_match(row) for row in evidence]
    report = {
        "version": MATCHING_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "geography": "Warsaw",
        "policy": POLICY,
        "policy_sha256": stable_hash(POLICY),
        "evidence_sha256": stable_hash(evidence),
        "decisions_sha256": stable_hash(decisions),
        "evidence": evidence,
        "decisions": decisions,
        "summary": summarize(decisions),
        "rankings_enabled": False,
        "training_performed": False,
        "old_experiment_outcomes_changed": False,
    }
    report["report_sha256"] = stable_hash(report)
    return report


def validate_report(report: dict) -> None:
    body = {key: value for key, value in report.items() if key != "report_sha256"}
    if (
        report.get("version") != MATCHING_VERSION
        or report.get("geography") != "Warsaw"
        or report.get("report_sha256") != stable_hash(body)
        or report.get("policy_sha256") != stable_hash(POLICY)
        or report.get("policy") != POLICY
        or report.get("evidence_sha256") != stable_hash(report.get("evidence"))
        or report.get("decisions_sha256") != stable_hash(report.get("decisions"))
        or report.get("rankings_enabled") is not False
        or report.get("training_performed") is not False
    ):
        raise ValueError("Matching review report changed or belongs to a different policy")
    decisions = [decide_match(row) for row in report["evidence"]]
    identities = [(row["brand_id"], row["source_id"]) for row in decisions]
    if (
        decisions != report["decisions"]
        or len(set(identities)) != len(identities)
        or any(brand not in BRANDS for brand, _ in identities)
        or report["summary"] != summarize(decisions)
    ):
        raise ValueError("Matching decisions cannot be reproduced from the reviewed evidence")


def association_note(decision: dict) -> str:
    if decision["match_status"] == "conflict":
        return "The official listing and municipal address do not identify a building consistently."
    if decision["match_status"] != "matched":
        return "The available address and map evidence does not identify one building reliably."
    pieces = ["Building association inferred from the official listing and municipal geometry."]
    if decision["street_resolution"]["method"] == "unique_name_expansion":
        pieces.append("A unique local street-name expansion is corroborated by the locator point.")
    if decision["postal_status"] == "disagreement":
        pieces.append("Postal codes differ, but the address and point identify the same footprint.")
    if decision["match_method"] and decision["match_method"].endswith("_near_point"):
        pieces.append("The locator point is within 10 m and covers no alternative building.")
    return " ".join(pieces)


def apply_report(connection, report: dict) -> dict:
    validate_report(report)
    connection.execute("SELECT pg_advisory_xact_lock(hashtext('masterfranchises-warsaw-map'))")
    current = collect(connection)
    if stable_hash(current) != report["evidence_sha256"]:
        raise ValueError("Source or association evidence changed after review; create a new audit")
    connection.execute("""
        CREATE TABLE IF NOT EXISTS grocery_match_history (
            report_sha256 text NOT NULL,
            brand_id text NOT NULL,
            source_id text NOT NULL,
            before_row jsonb NOT NULL,
            evidence jsonb NOT NULL,
            decision jsonb NOT NULL,
            applied_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY(report_sha256,brand_id,source_id)
        )
    """)
    for evidence, decision in zip(current, report["decisions"], strict=True):
        parameters = {
            **decision,
            "report_sha256": report["report_sha256"],
            "evidence": Jsonb(evidence),
            "decision": Jsonb(decision),
            "match_note": association_note(decision),
        }
        connection.execute(
            """
            INSERT INTO grocery_match_history(report_sha256,brand_id,source_id,before_row,
                evidence,decision)
            SELECT %(report_sha256)s,o.brand_id,o.source_id,to_jsonb(o),%(evidence)s,%(decision)s
            FROM franchise_outlets o WHERE o.brand_id=%(brand_id)s AND o.source_id=%(source_id)s
        """,
            parameters,
        )
        cursor = connection.execute(
            """
            UPDATE franchise_outlets o SET
                building_source_id=%(building_source_id)s,address_source_id=%(address_source_id)s,
                match_status=%(match_status)s,match_method=%(match_method)s,
                match_distance_m=%(match_distance_m)s,match_note=%(match_note)s,
                candidate_building_ids=%(candidate_building_ids)s,
                display_geometry=coalesce(o.geometry,
                    (SELECT a.geometry FROM addresses a WHERE a.source_id=%(address_source_id)s),
                    (SELECT ST_PointOnSurface(b.geometry) FROM buildings b
                        WHERE b.source_id=%(building_source_id)s)),
                point_source=CASE WHEN o.geometry IS NOT NULL THEN 'official_locator'
                    WHEN %(address_source_id)s IS NOT NULL THEN 'municipal_address'
                    WHEN %(building_source_id)s IS NOT NULL THEN 'building_representative_point'
                    ELSE NULL END
            WHERE o.brand_id=%(brand_id)s AND o.source_id=%(source_id)s
        """,
            parameters,
        )
        if cursor.rowcount != 1:
            raise ValueError("A reviewed grocery outlet is no longer uniquely identifiable")
    connection.execute("DELETE FROM building_franchise_summary")
    connection.execute("""
        INSERT INTO building_franchise_summary(
            building_source_id,outlet_count,brand_ids,brand_flags)
        SELECT building_source_id,count(*)::integer,
            array_agg(DISTINCT brand_id ORDER BY brand_id),
            jsonb_object_agg('brand_' || brand_id,true)
        FROM franchise_outlets WHERE match_status='matched' AND building_source_id IS NOT NULL
        GROUP BY building_source_id
    """)
    invalidate_profiles(connection)
    connection.execute("ANALYZE franchise_outlets")
    connection.execute("ANALYZE building_franchise_summary")
    observed = dict(
        connection.execute(
            "SELECT match_status,count(*) FROM franchise_outlets WHERE brand_id=ANY(%s) GROUP BY 1",
            (list(BRANDS),),
        ).fetchall()
    )
    if observed != report["summary"]["after"]:
        raise ValueError("Applied matching counts differ from the reviewed decisions")
    return {
        "report_sha256": report["report_sha256"],
        "version": MATCHING_VERSION,
        "summary": report["summary"],
        "rankings_enabled": False,
        "training_performed": False,
        "applied_at": datetime.now(UTC).isoformat(),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    audit = commands.add_parser("audit")
    audit.add_argument("--output", type=Path, required=True)
    apply = commands.add_parser("apply")
    apply.add_argument("--report", type=Path, required=True)
    apply.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    if arguments.output.exists():
        parser.error("Output must be a new review or application receipt")
    settings = Settings()
    with psycopg.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        dbname=settings.postgres_db,
        user=settings.postgres_user,
        password=settings.postgres_password.get_secret_value(),
        connect_timeout=5,
        options="-csearch_path=public -ctimezone=UTC",
    ) as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        connection.execute("SET LOCAL statement_timeout='60s'")
        if arguments.command == "audit":
            connection.execute("SET TRANSACTION READ ONLY")
            result = make_report(connection)
        else:
            result = apply_report(connection, load_json(arguments.report))
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    write_json(arguments.output, result)
    print(
        json.dumps(
            {
                "command": arguments.command,
                "version": MATCHING_VERSION,
                "summary": {
                    key: value for key, value in result["summary"].items() if key != "changes"
                },
                "rankings_enabled": False,
            }
        )
    )


if __name__ == "__main__":
    main()
