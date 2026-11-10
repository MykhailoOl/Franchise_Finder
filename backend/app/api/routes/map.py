import hashlib
import json
import math
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from psycopg import Error as PsycopgError
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from app.core.grocery_model_status import public_assessment
from app.db import addresses, buildings, franchises, grocery_assessments, profiles
from app.db.session import get_engine

router = APIRouter(prefix="/map", tags=["map"])
DatabaseEngine = Annotated[Engine, Depends(get_engine)]
MVT_MEDIA_TYPE = "application/vnd.mapbox-vector-tile"
EMPTY_DATA_MESSAGE = (
    "Warsaw building data has not been imported. Run the building import command in README.md."
)


def cached_response(
    request: Request, content: bytes, media_type: str, max_age: int = 60
) -> Response:
    etag = f'W/"{hashlib.sha256(content).hexdigest()}"'
    headers = {"ETag": etag, "Cache-Control": f"public, max-age={max_age}, must-revalidate"}
    candidates = request.headers.get("if-none-match", "").split(",")
    if any(candidate.strip().removeprefix("W/") in (etag[2:], "*") for candidate in candidates):
        return Response(status_code=304, headers=headers)
    return Response(content=content, media_type=media_type, headers=headers)


def json_response(request: Request, content: dict[str, Any], max_age: int = 60) -> Response:
    payload = json.dumps(
        jsonable_encoder(content), ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return cached_response(request, payload, "application/json", max_age)


def tile_intersects_warsaw(z: int, x: int, y: int) -> bool:
    count = 2**z
    margin = 64 / 4096
    west = (x - margin) / count * 360 - 180
    east = (x + 1 + margin) / count * 360 - 180
    north = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y - margin) / count))))
    south = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1 + margin) / count))))
    allowed_west, allowed_south, allowed_east, allowed_north = buildings.WARSAW_BOUNDS
    return (
        east >= allowed_west
        and west <= allowed_east
        and north >= allowed_south
        and south <= allowed_north
    )


@router.get("/metadata")
def metadata(request: Request, engine: DatabaseEngine) -> Response:
    try:
        result = buildings.get_metadata(engine)
    except SQLAlchemyError as error:
        raise HTTPException(status_code=503, detail="The map database is unavailable") from error
    return json_response(request, result, max_age=0)


@router.get("/tiles/{z}/{x}/{y}.pbf", response_class=Response)
def tile(
    request: Request,
    engine: DatabaseEngine,
    z: Annotated[int, Path(ge=buildings.MIN_ZOOM, le=buildings.MAX_ZOOM)],
    x: Annotated[int, Path(ge=0)],
    y: Annotated[int, Path(ge=0)],
) -> Response:
    if x >= 2**z or y >= 2**z:
        raise HTTPException(status_code=422, detail="Tile coordinates exceed the zoom level")
    if not tile_intersects_warsaw(z, x, y):
        return cached_response(request, b"", MVT_MEDIA_TYPE)
    try:
        content = buildings.get_tile(engine, z, x, y)
    except buildings.MapDataUnavailableError as error:
        raise HTTPException(status_code=503, detail=EMPTY_DATA_MESSAGE) from error
    except SQLAlchemyError as error:
        raise HTTPException(status_code=503, detail="The map database is unavailable") from error
    return cached_response(request, content, MVT_MEDIA_TYPE)


@router.get("/buildings/{building_id}")
def building_details(
    request: Request,
    engine: DatabaseEngine,
    building_id: Annotated[int, Path(ge=1, le=9_007_199_254_740_991)],
) -> Response:
    try:
        result = buildings.get_building(engine, building_id)
    except buildings.MapDataUnavailableError as error:
        raise HTTPException(status_code=503, detail=EMPTY_DATA_MESSAGE) from error
    except SQLAlchemyError as error:
        raise HTTPException(status_code=503, detail="The map database is unavailable") from error
    if result is None:
        raise HTTPException(status_code=404, detail="Building not found")
    return json_response(request, result, max_age=0)


@router.get("/addresses/search")
def search_addresses(
    request: Request,
    engine: DatabaseEngine,
    q: Annotated[str, Query(min_length=2, max_length=120)],
    limit: Annotated[int, Query(ge=1, le=20)] = 8,
) -> Response:
    try:
        result = addresses.search_addresses(engine, q.strip(), limit)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except SQLAlchemyError as error:
        raise HTTPException(
            status_code=503, detail="The address database is unavailable"
        ) from error
    return json_response(request, result, max_age=0)


@router.get("/franchises")
def franchise_catalog(request: Request, engine: DatabaseEngine) -> Response:
    try:
        result = franchises.get_catalog(engine)
    except SQLAlchemyError as error:
        raise HTTPException(
            status_code=503, detail="The franchise database is unavailable"
        ) from error
    return json_response(request, result, max_age=0)


@router.get("/outlets")
def franchise_outlets(
    request: Request,
    engine: DatabaseEngine,
    brand_ids: Annotated[str | None, Query(max_length=3250)] = None,
) -> Response:
    try:
        result = franchises.get_outlets(engine, franchises.parse_brand_ids(brand_ids))
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except SQLAlchemyError as error:
        raise HTTPException(
            status_code=503, detail="The franchise database is unavailable"
        ) from error
    return json_response(request, result, max_age=0)


@router.get("/buildings/{building_id}/profile")
def building_profile(
    request: Request,
    engine: DatabaseEngine,
    building_id: Annotated[int, Path(ge=1, le=9_007_199_254_740_991)],
    radius_m: Annotated[int, Query(ge=300, le=1000)] = 500,
) -> Response:
    if radius_m not in profiles.RADII:
        raise HTTPException(status_code=422, detail="Radius must be 300, 500 or 1000 metres")
    try:
        result = profiles.get_profile(engine, building_id, radius_m)
    except buildings.MapDataUnavailableError as error:
        raise HTTPException(status_code=503, detail=EMPTY_DATA_MESSAGE) from error
    except SQLAlchemyError as error:
        raise HTTPException(
            status_code=503, detail="The profile database is unavailable"
        ) from error
    if result is None:
        raise HTTPException(status_code=404, detail="Building not found")
    return json_response(request, result, max_age=0)


@router.get("/buildings/{building_id}/grocery-assessment")
def grocery_assessment(
    request: Request,
    engine: DatabaseEngine,
    building_id: Annotated[int, Path(ge=1, le=9_007_199_254_740_991)],
) -> Response:
    try:
        result = grocery_assessments.get_assessment(engine, building_id)
    except buildings.MapDataUnavailableError as error:
        raise HTTPException(
            status_code=503, detail="Building information is unavailable"
        ) from error
    except (SQLAlchemyError, PsycopgError, ValueError) as error:
        raise HTTPException(
            status_code=503, detail="The grocery assessment is unavailable"
        ) from error
    if result is None:
        raise HTTPException(status_code=404, detail="Building not found")
    result = public_assessment(result, request.app.state.grocery_model_status)
    return json_response(request, result, max_age=0)
