"""
API route handlers for file upload, retrieval, and measurement endpoints.

Endpoints:
    POST   /api/files/                   - Upload and process a geospatial file (streaming)
    GET    /api/files/                   - List all uploaded files
    GET    /api/files/{file_id}/         - Get file metadata
    GET    /api/files/{file_id}/measurements/ - Get per-feature measurements
"""

import json
import logging
import uuid
from pathlib import Path
from typing import List

import aiofiles
from fastapi import APIRouter, Depends, File, UploadFile, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.exceptions import (
    EmptyFileError,
    FileNotFoundError,
    FileTooLargeError,
    ProcessingError,
    UnsupportedFileTypeError,
)
from app.database import get_session
from app.models.file_record import FeatureMeasurement, FileRecord
from app.schemas.file import FileInfoResponse, FileListResponse, FileUploadResponse
from app.schemas.measurement import FeatureMeasurementResponse, MeasurementsResponse
from app.services.file_processor import detect_file_type, process_file

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/files", tags=["Files"])

# 64 KB streaming chunk size
_UPLOAD_CHUNK_SIZE = 64 * 1024


# ---------------------------------------------------------------------------
# POST /api/files/
# ---------------------------------------------------------------------------

@router.post(
    "/",
    response_model=FileUploadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a geospatial file",
    description=(
        "Upload a `.zip` containing a Shapefile or a `.kml` file. "
        "The service streams the upload, extracts features, calculates measurements "
        "via appropriate UTM CRS projections, and returns the processing result."
    ),
)
async def upload_file(
    file: UploadFile = File(..., description="Geospatial file (.zip or .kml)"),
    db: AsyncSession = Depends(get_session),
) -> FileUploadResponse:
    # 1. Sanitize filename (prevent directory traversal attacks)
    raw_filename = file.filename or "uploaded_file"
    sanitized_filename = Path(raw_filename).name.strip()
    if not sanitized_filename:
        sanitized_filename = "uploaded_file"

    # 2. Validate file type from extension
    try:
        file_type = detect_file_type(sanitized_filename)
    except ValueError:
        raise UnsupportedFileTypeError(sanitized_filename)

    # 3. Stream upload directly to disk with chunked size limits (DoS protection)
    upload_dir = Path(settings.upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)

    file_id = str(uuid.uuid4())
    saved_path = upload_dir / f"{file_id}_{sanitized_filename}"
    total_bytes = 0

    try:
        async with aiofiles.open(saved_path, "wb") as out_file:
            while True:
                chunk = await file.read(_UPLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > settings.max_upload_size_bytes:
                    # Exceeded maximum allowed size; abort and clean up
                    raise FileTooLargeError(settings.max_upload_size_mb)
                await out_file.write(chunk)
    except FileTooLargeError:
        saved_path.unlink(missing_ok=True)
        raise
    except Exception as exc:
        saved_path.unlink(missing_ok=True)
        logger.exception("Error saving uploaded file '%s': %s", sanitized_filename, exc)
        raise ProcessingError(f"Failed to save upload to disk: {str(exc)}")

    # 4. Check for empty files
    if total_bytes == 0:
        saved_path.unlink(missing_ok=True)
        raise EmptyFileError(sanitized_filename)

    # 5. Create initial FileRecord in DB with status=PROCESSING
    record = FileRecord(
        id=file_id,
        filename=sanitized_filename,
        file_path=str(saved_path),
        file_type=file_type,
        status="PROCESSING",
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)

    # 6. Process file (parse in worker thread, compute reprojections & measurements)
    await process_file(record, db)
    await db.refresh(record)

    return FileUploadResponse.model_validate(record)


# ---------------------------------------------------------------------------
# GET /api/files/
# ---------------------------------------------------------------------------

@router.get(
    "/",
    response_model=List[FileListResponse],
    summary="List all uploaded files",
    description="Returns a list of all uploaded files ordered by creation date descending.",
)
async def list_files(
    db: AsyncSession = Depends(get_session),
) -> List[FileListResponse]:
    result = await db.execute(
        select(FileRecord).order_by(FileRecord.created_at.desc())
    )
    records = result.scalars().all()
    return [FileListResponse.model_validate(r) for r in records]


# ---------------------------------------------------------------------------
# GET /api/files/{file_id}/
# ---------------------------------------------------------------------------

@router.get(
    "/{file_id}/",
    response_model=FileInfoResponse,
    summary="Get file metadata",
    description="Returns metadata and current processing status for a specific file by ID.",
)
async def get_file(
    file_id: str,
    db: AsyncSession = Depends(get_session),
) -> FileInfoResponse:
    record = await db.get(FileRecord, file_id)
    if record is None:
        raise FileNotFoundError(file_id)
    return FileInfoResponse.model_validate(record)


# ---------------------------------------------------------------------------
# GET /api/files/{file_id}/measurements/
# ---------------------------------------------------------------------------

@router.get(
    "/{file_id}/measurements/",
    response_model=MeasurementsResponse,
    summary="Get feature measurements",
    description=(
        "Returns per-feature measurement data (area in m² / length in m) "
        "and projected CRS info for a processed geospatial file."
    ),
)
async def get_measurements(
    file_id: str,
    db: AsyncSession = Depends(get_session),
) -> MeasurementsResponse:
    result = await db.execute(
        select(FileRecord)
        .where(FileRecord.id == file_id)
        .options(selectinload(FileRecord.measurements))
    )
    record = result.scalar_one_or_none()

    if record is None:
        raise FileNotFoundError(file_id)

    feature_responses: List[FeatureMeasurementResponse] = []
    for m in sorted(record.measurements, key=lambda x: x.feature_index):
        props = None
        if m.properties:
            try:
                props = json.loads(m.properties)
            except (json.JSONDecodeError, TypeError):
                props = {"raw": m.properties}

        feature_responses.append(
            FeatureMeasurementResponse(
                feature_index=m.feature_index,
                geometry_type=m.geometry_type,
                geometry_wkt=m.geometry_wkt,
                properties=props,
                area_m2=m.area_m2,
                length_m=m.length_m,
                projected_crs=m.projected_crs,
                note=m.note,
            )
        )

    return MeasurementsResponse(
        file_id=record.id,
        filename=record.filename,
        crs=record.crs,
        feature_count=record.feature_count or 0,
        features=feature_responses,
    )
