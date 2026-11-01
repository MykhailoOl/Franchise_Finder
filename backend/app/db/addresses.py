from typing import Any

from sqlalchemy import Connection, Engine, text

from app.core.address_search import AddressQuery, escape_like, parse_address_query

DATASET_ID = "warsaw-addresses"

SOURCE_QUERY = text(
    "SELECT name, url, attribution, updated_at, imported_at, feature_count"
    " FROM map_sources WHERE id = :dataset_id"
)

SEARCH_QUERY = text(
    "SELECT a.id, a.address, a.street, a.house_number, a.postal_code,"
    " ARRAY[ST_X(ST_Transform(a.geometry, 4326)), ST_Y(ST_Transform(a.geometry, 4326))]"
    " AS coordinates, b.id AS building_id, a.match_status, a.match_method, a.distance_m,"
    " (a.street_normalized = :street AND a.house_number_normalized = :house) AS exact_match"
    " FROM addresses a LEFT JOIN buildings b ON b.source_id = a.building_source_id"
    " WHERE a.dataset_id = :dataset_id"
    " AND (CAST(:house AS text) IS NULL OR a.house_number_normalized = :house)"
    " AND (CAST(:postal AS text) IS NULL OR a.postal_code = :postal)"
    " AND (a.street_normalized LIKE :contains OR a.street_normalized % :street)"
    " ORDER BY (a.street_normalized = :street) DESC,"
    " (a.street_normalized LIKE :prefix) DESC, similarity(a.street_normalized, :street) DESC,"
    " a.street_normalized, length(a.house_number_normalized), a.house_number_normalized,"
    " a.postal_code, a.id LIMIT :limit"
)


def address_source(row: Any) -> dict[str, Any]:
    return {key: row[key] for key in ("name", "url", "attribution", "updated_at")}


def get_source(connection: Connection) -> Any:
    exists = connection.execute(text("SELECT to_regclass('addresses') IS NOT NULL")).scalar_one()
    if not exists:
        return None
    return connection.execute(SOURCE_QUERY, {"dataset_id": DATASET_ID}).mappings().first()


def get_building_addresses(connection: Connection, building_source_id: str) -> tuple[list, str]:
    source = get_source(connection)
    if source is None:
        return [], "not_imported"
    rows = connection.execute(
        text(
            "SELECT id, source_id, address, street, house_number, postal_code, match_method,"
            " distance_m FROM addresses WHERE building_source_id = :building_source_id"
            " AND match_status = 'matched' ORDER BY street_normalized,"
            " length(house_number_normalized), house_number_normalized, id"
        ),
        {"building_source_id": building_source_id},
    ).mappings()
    results = [dict(row, source=address_source(source), is_inferred=True) for row in rows]
    return results, "matched" if results else "unmatched"


def search_addresses(engine: Engine, query: str, limit: int) -> dict[str, Any]:
    parsed = parse_address_query(query)
    with engine.begin() as connection:
        connection.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
        connection.execute(text("SET LOCAL statement_timeout = '5s'"))
        source = get_source(connection)
        response = {
            "query": query,
            "status": "ready" if source and source["feature_count"] else "empty",
            "source": address_source(source) if source else None,
            "results": [],
            "best_match_id": None,
            "exact_match_count": 0,
        }
        if source is None or not source["feature_count"]:
            return response
        if (
            parsed.house_number
            and connection.execute(
                text(
                    "SELECT EXISTS(SELECT 1 FROM addresses WHERE dataset_id = :dataset_id"
                    " AND street_normalized = :street)"
                ),
                {"dataset_id": DATASET_ID, "street": parsed.full_street},
            ).scalar_one()
        ):
            parsed = AddressQuery(parsed.full_street, None, parsed.postal_code, parsed.full_street)
        if len(parsed.street) < 2:
            return response
        parameters = {
            "dataset_id": DATASET_ID,
            "street": parsed.street,
            "house": parsed.house_number,
            "postal": parsed.postal_code,
            "prefix": escape_like(parsed.street) + "%",
            "contains": "%" + "%".join(escape_like(parsed.street).split()) + "%",
            "limit": limit,
        }
        if parsed.house_number:
            exact = connection.execute(
                text(
                    "SELECT COUNT(*) AS exact_count, MIN(id) AS id FROM addresses"
                    " WHERE dataset_id = :dataset_id AND street_normalized = :street"
                    " AND house_number_normalized = :house"
                    " AND (CAST(:postal AS text) IS NULL OR postal_code = :postal)"
                ),
                parameters,
            ).one()
            response["exact_match_count"] = exact.exact_count
            response["best_match_id"] = exact.id if exact.exact_count == 1 else None
        rows = connection.execute(SEARCH_QUERY, parameters).mappings()
        response["results"] = [dict(row, exact_match=bool(row["exact_match"])) for row in rows]
    return response
