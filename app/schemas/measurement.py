"""
Pydantic schemas for measurement responses.
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class FeatureMeasurementResponse(BaseModel):
    """Measurement result for a single geospatial feature."""
    feature_index: int = Field(..., description="Zero-based index of the feature in the file")
    geometry_type: str = Field(..., description="Geometry type, e.g. Polygon, LineString, Point")
    geometry_wkt: Optional[str] = Field(None, description="Well-Known Text representation of the geometry")
    properties: Optional[Dict[str, Any]] = Field(None, description="Feature attributes/properties")
    area_m2: Optional[float] = Field(None, description="Area in square metres (Polygon only)")
    length_m: Optional[float] = Field(None, description="Length in metres (LineString only)")
    projected_crs: Optional[str] = Field(None, description="Projected CRS used for measurement")
    note: Optional[str] = Field(None, description="Informational note, e.g. unsupported geometry")

    model_config = {"from_attributes": True}


class MeasurementsResponse(BaseModel):
    """Full measurements response for a file."""
    file_id: str
    filename: str
    crs: Optional[str]
    feature_count: int
    features: List[FeatureMeasurementResponse]
