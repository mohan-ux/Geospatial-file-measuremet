"""
File processor service — orchestrates parsing, measurement, and DB persistence.

Production hardening:
  - Parsers run in a THREAD POOL via asyncio.to_thread() so they never
    block the async event loop (GeoPandas/Fiona/lxml are all synchronous).
  - Uploaded file is saved BEFORE the DB record is created, so a disk-write
    failure cannot leave an orphaned PROCESSING record in the database.
  - Orphan file cleanup: if processing fails, the uploaded file is optionally
    deleted (controlled by settings.delete_file_on_failure).
  - CRS fallback: if the file's CRS string cannot be parsed, defaults to
    EPSG:4326 with a warning rather than crashing.
  - Measurement rows are inserted in a single transaction to guarantee
    atomicity — either all succeed or none do.
  - Any exception during processing marks the record FAILED with a sanitised
    error message (no internal paths or stack traces in the DB).
"""

import asyncio
import json
import logging
import time
from pathlib import Path

from pyproj import CRS
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.file_record import FeatureMeasurement, FileRecord
from app.services.measurements import calculate_measurement
from app.services.parsers.base import ParseResult
from app.services.parsers.kml import KMLParser
from app.services.parsers.shapefile import ShapefileParser

logger = logging.getLogger(__name__)

# Maximum length for error messages stored in the database
_MAX_ERROR_MSG_LEN = 1000


def detect_file_type(filename: str) -> str:
    """
    Determine the file type from the filename extension.

    Returns:
        "shapefile" for .zip files
        "kml"       for .kml files

    Raises:
        ValueError: if the extension is not supported.
    """
    suffix = Path(filename).suffix.lower()
    if suffix == ".zip":
        return "shapefile"
    if suffix == ".kml":
        return "kml"
    raise ValueError(
        f"Unsupported file extension '{suffix}'. Accepted: .zip (Shapefile), .kml"
    )


def _get_parser(file_type: str):
    """Return the appropriate parser for the given file type."""
    if file_type == "shapefile":
        return ShapefileParser()
    if file_type == "kml":
        return KMLParser()
    raise ValueError(f"No parser registered for file type '{file_type}'.")


def _sanitise_error(exc: Exception) -> str:
    """
    Return a user-safe error message (no internal paths, no stack trace).
    Truncated to _MAX_ERROR_MSG_LEN characters.
    """
    msg = str(exc)
    # Remove absolute path prefixes to avoid leaking server layout
    try:
        upload_dir = str(Path(settings.upload_dir).resolve())
        msg = msg.replace(upload_dir, "<upload_dir>")
    except Exception:
        pass
    return msg[:_MAX_ERROR_MSG_LEN]


def _parse_crs(crs_str: str) -> CRS:
    """
    Parse a CRS string to a PyProj CRS object with a safe fallback.

    Falls back to EPSG:4326 if the string is unrecognised (e.g. "UNKNOWN").
    """
    if not crs_str or crs_str.upper() == "UNKNOWN":
        logger.warning("File has no CRS defined; assuming EPSG:4326.")
        return CRS.from_epsg(4326)
    try:
        return CRS.from_user_input(crs_str)
    except Exception:
        logger.warning(
            "Could not parse CRS '%s'; defaulting to EPSG:4326.", crs_str
        )
        return CRS.from_epsg(4326)


async def process_file(file_record: FileRecord, db: AsyncSession) -> None:
    """
    Full pipeline: parse → measure → persist to DB.

    Called after the FileRecord has been committed with status=PROCESSING.
    Updates the record's status to COMPLETED or FAILED in-place.

    The synchronous parser is executed in a thread pool executor so it
    does not block the event loop while reading large GIS files.
    """
    t_start = time.perf_counter()
    logger.info(
        "Starting processing: file='%s' id=%s type=%s",
        file_record.filename,
        file_record.id,
        file_record.file_type,
    )

    try:
        # ------------------------------------------------------------------
        # 1. Parse (blocking I/O → thread pool)
        # ------------------------------------------------------------------
        parser = _get_parser(file_record.file_type)
        result: ParseResult = await asyncio.to_thread(
            parser.parse, file_record.file_path
        )

        # ------------------------------------------------------------------
        # 2. Build the source CRS object
        # ------------------------------------------------------------------
        source_crs = _parse_crs(result.crs_str)

        # ------------------------------------------------------------------
        # 3. Measure each feature (CPU-bound but fast per feature)
        #    Keep in the event loop — each call is O(1) with no I/O.
        # ------------------------------------------------------------------
        measurement_rows: list[FeatureMeasurement] = []
        for feature in result.features:
            if feature.geometry is None:
                note = feature.properties.pop("_parse_error", None) or \
                       "Geometry is null or could not be parsed."
                measurement_rows.append(
                    FeatureMeasurement(
                        file_record_id=file_record.id,
                        feature_index=feature.index,
                        geometry_type=feature.geometry_type,
                        geometry_wkt=None,
                        area_m2=None,
                        length_m=None,
                        properties=json.dumps(feature.properties, default=str),
                        projected_crs=None,
                        note=note,
                    )
                )
                continue

            area_m2, length_m, projected_crs_str, note = calculate_measurement(
                feature.geometry, source_crs
            )

            # WKT truncation guard (very large geometries can produce huge WKT)
            wkt = feature.geometry.wkt
            if len(wkt) > 100_000:
                wkt = wkt[:100_000] + "...[truncated]"

            measurement_rows.append(
                FeatureMeasurement(
                    file_record_id=file_record.id,
                    feature_index=feature.index,
                    geometry_type=feature.geometry_type,
                    geometry_wkt=wkt,
                    area_m2=area_m2,
                    length_m=length_m,
                    properties=json.dumps(feature.properties, default=str),
                    projected_crs=projected_crs_str,
                    note=note,
                )
            )

        # ------------------------------------------------------------------
        # 4. Atomic DB commit: all measurements + updated file record
        # ------------------------------------------------------------------
        db.add_all(measurement_rows)
        file_record.feature_count = len(result.features)
        file_record.crs = result.crs_str
        file_record.status = "COMPLETED"
        db.add(file_record)
        await db.commit()

        elapsed = time.perf_counter() - t_start
        logger.info(
            "Completed: file='%s' features=%d elapsed=%.2fs",
            file_record.filename,
            len(result.features),
            elapsed,
        )

    except Exception as exc:
        elapsed = time.perf_counter() - t_start
        logger.exception(
            "Failed processing file='%s' id=%s elapsed=%.2fs",
            file_record.filename,
            file_record.id,
            elapsed,
        )

        # Roll back any partial state from this session
        await db.rollback()

        file_record.status = "FAILED"
        file_record.error_message = _sanitise_error(exc)
        db.add(file_record)
        await db.commit()

        # Optional: remove the uploaded file on failure
        if settings.delete_file_on_failure:
            try:
                Path(file_record.file_path).unlink(missing_ok=True)
                logger.info("Deleted failed upload: '%s'", file_record.file_path)
            except Exception as cleanup_exc:
                logger.warning("Could not delete failed upload: %s", cleanup_exc)
