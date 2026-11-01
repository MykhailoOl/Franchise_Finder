from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from app.db.session import get_engine

router = APIRouter(prefix="/health", tags=["health"])


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    service: str = "MasterFranchises API"


class ReadinessResponse(BaseModel):
    status: Literal["ok"] = "ok"
    database: Literal["ok"] = "ok"
    postgis_version: str


@router.get("", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse()


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    responses={503: {"description": "Database or PostGIS is unavailable"}},
)
def readiness(engine: Annotated[Engine, Depends(get_engine)]) -> ReadinessResponse:
    try:
        with engine.connect() as connection:
            postgis_version = connection.execute(text("SELECT PostGIS_Version()")).scalar_one()
    except SQLAlchemyError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database or PostGIS is unavailable",
        ) from error
    return ReadinessResponse(postgis_version=postgis_version)
