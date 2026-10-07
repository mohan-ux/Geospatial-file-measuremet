"""
FastAPI application entry point.

Startup:
    - Initialises the database (creates tables if needed).
    - Creates the uploads directory.

Lifespan:
    Uses the modern async lifespan context manager.
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes.files import router as files_router
from app.core.config import settings
from app.database import init_db

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: setup on startup, teardown on shutdown."""
    logger.info("Starting %s v%s", settings.app_name, settings.app_version)

    # Ensure uploads directory exists
    Path(settings.upload_dir).mkdir(parents=True, exist_ok=True)

    # Create DB tables
    await init_db()
    logger.info("Database initialised successfully.")

    yield  # Application runs here

    logger.info("Shutting down %s.", settings.app_name)


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description=(
        "Production-ready FastAPI service that accepts geospatial files (Shapefile .zip or .kml), "
        "extracts geometry features, reprojects to optimal local UTM coordinate reference systems, "
        "and computes accurate planar measurements (area in m², length in m)."
    ),
    contact={
        "name": "Geospatial API Team",
    },
    license_info={"name": "MIT"},
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# CORS Middleware
# ---------------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # TODO: Configure specific origins in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Security Headers Middleware
# ---------------------------------------------------------------------------
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    """Add security headers to all responses."""
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


# ---------------------------------------------------------------------------
# Global Exception Handlers
# ---------------------------------------------------------------------------
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Clean JSON response for request validation failures."""
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "error": "Validation Error",
            "detail": exc.errors(),
        },
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Catch-all handler for unexpected internal errors."""
    logger.exception("Unhandled server exception: %s", exc)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": "Internal Server Error",
            "detail": "An unexpected error occurred while processing the request.",
        },
    )


# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------
app.include_router(files_router)


# ---------------------------------------------------------------------------
# System Endpoints
# ---------------------------------------------------------------------------
@app.get("/health", tags=["Health"], summary="Health check")
async def health() -> JSONResponse:
    """Service health check endpoint."""
    return JSONResponse(
        {
            "status": "ok",
            "version": settings.app_version,
            "app_name": settings.app_name,
            "max_upload_size_mb": settings.max_upload_size_mb,
            "debug": settings.debug,
        }
    )


@app.get("/", tags=["Root"], summary="API root")
async def root() -> JSONResponse:
    """Service information and links."""
    return JSONResponse(
        {
            "message": f"Welcome to {settings.app_name}",
            "version": settings.app_version,
            "docs": "/docs",
            "redoc": "/redoc",
            "health": "/health",
        }
    )
