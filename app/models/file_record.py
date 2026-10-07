"""
ORM models for persisting file records and per-feature measurements.
"""

import uuid
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, Float
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class FileRecord(Base):
    """
    Represents an uploaded geospatial file.
    Tracks metadata and processing status.
    """
    __tablename__ = "file_records"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    file_path: Mapped[str] = mapped_column(String(512), nullable=False)
    file_type: Mapped[str] = mapped_column(String(20), nullable=False)  # "shapefile" | "kml"
    feature_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    crs: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="PENDING"
    )  # PENDING | PROCESSING | COMPLETED | FAILED
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    # One file → many feature measurements
    measurements: Mapped[List["FeatureMeasurement"]] = relationship(
        "FeatureMeasurement", back_populates="file_record", cascade="all, delete-orphan"
    )


class FeatureMeasurement(Base):
    """
    Stores per-feature measurement data extracted from the uploaded file.
    """
    __tablename__ = "feature_measurements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    file_record_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("file_records.id", ondelete="CASCADE"), nullable=False
    )
    feature_index: Mapped[int] = mapped_column(Integer, nullable=False)
    geometry_type: Mapped[str] = mapped_column(String(50), nullable=False)
    # WKT representation of the geometry
    geometry_wkt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Area in square metres (Polygon only)
    area_m2: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # Length in metres (LineString only)
    length_m: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # JSON-serialised properties/attributes
    properties: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Projected CRS used for measurement
    projected_crs: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    # Human-readable note (e.g. "unsupported geometry type")
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    file_record: Mapped["FileRecord"] = relationship(
        "FileRecord", back_populates="measurements"
    )
