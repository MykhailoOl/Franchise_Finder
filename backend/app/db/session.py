from fastapi import Request
from sqlalchemy import Engine, create_engine

from app.core.config import Settings


def create_database_engine(settings: Settings) -> Engine:
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_timeout=5,
        connect_args={"connect_timeout": 3},
    )


def get_engine(request: Request) -> Engine:
    return request.app.state.engine
