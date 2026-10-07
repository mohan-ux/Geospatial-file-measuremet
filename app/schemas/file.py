"""
Pydantic schemas for file-related API requests and responses.
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class FileUploadResponse(BaseModel):
    """Returned immediately after a successful file upload and processing."""
    id: str = Field(..., description="Unique file record ID (UUID)")
    filename: str = Field(..., description="Original uploaded filename")
    file_type: str = Field(..., description="Detected file type: 'shapefile' or 'kml'")
    feature_count: Optional[int] = Field(None, description="Number of features extracted")
    crs: Optional[str] = Field(None, description="Coordinate Reference System of the file")
    status: str = Field(..., description="Processing status: PENDING | PROCESSING | COMPLETED | FAILED")
    error_message: Optional[str] = Field(None, description="Error detail if status is FAILED")
    created_at: datetime = Field(..., description="UTC timestamp of upload")

    model_config = {"from_attributes": True}


class FileInfoResponse(BaseModel):
    """Returned by GET /api/files/{id}/"""
    id: str
    filename: str
    file_type: str
    feature_count: Optional[int]
    crs: Optional[str]
    status: str
    error_message: Optional[str]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class FileListResponse(BaseModel):
    """Returned by GET /api/files/ — summary list."""
    id: str
    filename: str
    feature_count: Optional[int]
    crs: Optional[str]
    status: str
    created_at: datetime

    model_config = {"from_attributes": True}
