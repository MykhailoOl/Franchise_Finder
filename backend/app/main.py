from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from app.api.routes.health import router as health_router
from app.api.routes.map import router as map_router
from app.core.config import Settings
from app.core.grocery_model_status import GroceryModelStatus
from app.db.session import create_database_engine


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        engine = create_database_engine(settings)
        application.state.engine = engine
        application.state.grocery_model_status = GroceryModelStatus(
            settings.grocery_assessment_artifact_directory
        )
        try:
            yield
        finally:
            engine.dispose()

    application = FastAPI(
        title="MasterFranchises API",
        description="Franchise recommendations and premises comparison in Poland.",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.add_middleware(
        GZipMiddleware,
        minimum_size=1000,
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.api_cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization", "If-None-Match"],
        expose_headers=["ETag"],
    )
    application.include_router(health_router, prefix="/api/v1")
    application.include_router(map_router, prefix="/api/v1")
    return application


app = create_app()
