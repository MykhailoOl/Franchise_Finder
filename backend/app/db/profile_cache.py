from psycopg import Connection


def invalidate_profiles(connection: Connection) -> None:
    if connection.execute(
        "SELECT to_regprocedure('invalidate_building_profiles()') IS NOT NULL"
    ).fetchone()[0]:
        connection.execute("SELECT invalidate_building_profiles()")
