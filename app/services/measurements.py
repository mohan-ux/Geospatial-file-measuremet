"""
Measurement calculation service.

Handles CRS detection, automatic UTM reprojection, and geometry measurement.

Supported geometry types:
  - Polygon / MultiPolygon   → area (m²)
  - LineString / MultiLineString / LinearRing → length (m)
  - Point / MultiPoint       → no measurement (not applicable)
  - GeometryCollection       → decomposed into individual parts

Production hardening:
  - NaN / Inf coordinate guards before reprojection
  - Antimeridian crossing detection & user warning in feature note
  - Clamping latitude to [-80, 84] to prevent UTM math explosion at poles
  - Zero-area / degenerate geometry detection
  - Automatic geometry repair via shapely.make_valid
  - Full exception isolation: an unmeasurable geometry returns None and a note
    instead of throwing and crashing the batch process
"""

import logging
import math
from typing import Optional, Tuple

import shapely
from pyproj import CRS, Transformer
from shapely.ops import transform
from shapely.geometry.base import BaseGeometry
from shapely.validation import explain_validity

logger = logging.getLogger(__name__)

_POLYGON_TYPES = {"Polygon", "MultiPolygon"}
_LINE_TYPES = {"LineString", "MultiLineString", "LinearRing"}
_POINT_TYPES = {"Point", "MultiPoint"}


def _has_nan_or_inf(geometry: BaseGeometry) -> bool:
    """Return True if any coordinate in the geometry contains NaN or Inf."""
    try:
        coords = list(geometry.coords) if hasattr(geometry, "coords") else []
        for coord in coords:
            for val in coord:
                if math.isnan(val) or math.isinf(val):
                    return True
        return False
    except Exception:
        return False


def _repair_geometry(geometry: BaseGeometry) -> Optional[BaseGeometry]:
    """
    Attempt to repair an invalid geometry using shapely.make_valid.

    Returns the repaired geometry, or None if it cannot be repaired.
    """
    if geometry.is_valid:
        return geometry

    reason = explain_validity(geometry)
    logger.debug("Repairing invalid geometry (%s).", reason)

    try:
        repaired = shapely.make_valid(geometry)
        if repaired is None or repaired.is_empty:
            logger.warning("Geometry could not be repaired (result is empty).")
            return None
        return repaired
    except Exception as exc:
        logger.warning("make_valid failed: %s", exc)
        return None


def _get_utm_crs(geometry: BaseGeometry, source_crs: CRS) -> Tuple[CRS, Optional[str]]:
    """
    Determine the best UTM CRS for a geometry and any notices (e.g. antimeridian crossing).

    Returns:
        (utm_crs, note)
    """
    if not source_crs.is_geographic:
        return source_crs, None

    # Reproject centroid to EPSG:4326 if needed
    epsg_4326 = CRS.from_epsg(4326)
    if source_crs != epsg_4326:
        try:
            to_4326 = Transformer.from_crs(source_crs, epsg_4326, always_xy=True)
            centroid_geom = transform(to_4326.transform, geometry.centroid)
        except Exception as exc:
            logger.warning("Centroid reprojection to 4326 failed: %s. Trying raw centroid.", exc)
            centroid_geom = geometry.centroid
    else:
        centroid_geom = geometry.centroid

    lon = centroid_geom.x
    lat = centroid_geom.y

    # Guard against NaN/Inf centroid
    if math.isnan(lon) or math.isnan(lat) or math.isinf(lon) or math.isinf(lat):
        raise ValueError(f"Centroid coordinates are invalid (lon={lon}, lat={lat}). Cannot select UTM zone.")

    warning_note = None
    # Antimeridian crossing warning
    bounds = geometry.bounds  # (minx, miny, maxx, maxy)
    if bounds[0] < -170 and bounds[2] > 170:
        warning_note = "Geometry appears to cross the antimeridian; single UTM zone measurement may have distortion."
        logger.warning(
            "Geometry appears to cross the antimeridian (bounds: %s). Measurements may be inaccurate.",
            bounds,
        )

    # Clamp latitude for UTM validity (UTM standard bounds [-80, 84])
    clamped_lat = max(-80.0, min(84.0, lat))
    if clamped_lat != lat:
        logger.warning(
            "Latitude %.4f is outside UTM range [-80, 84]; clamped to %.4f for zone selection.",
            lat,
            clamped_lat,
        )

    # UTM zone number (1–60)
    zone_number = int((lon + 180) / 6) % 60 + 1
    hemisphere = "north" if clamped_lat >= 0 else "south"

    utm_crs = CRS.from_dict(
        {
            "proj": "utm",
            "zone": zone_number,
            "south": hemisphere == "south",
            "datum": "WGS84",
            "units": "m",
        }
    )

    logger.debug(
        "UTM zone %d%s selected (centroid lon=%.4f lat=%.4f)",
        zone_number,
        "N" if hemisphere == "north" else "S",
        lon,
        lat,
    )
    return utm_crs, warning_note


def _reproject(geometry: BaseGeometry, source_crs: CRS, target_crs: CRS) -> BaseGeometry:
    """Reproject a Shapely geometry from source_crs to target_crs."""
    transformer = Transformer.from_crs(source_crs, target_crs, always_xy=True)
    reprojected = transform(transformer.transform, geometry)
    if reprojected is None or reprojected.is_empty:
        raise ValueError("Reprojection produced an empty geometry.")
    return reprojected


def calculate_measurement(
    geometry: BaseGeometry,
    source_crs: CRS,
) -> Tuple[Optional[float], Optional[float], Optional[str], Optional[str]]:
    """
    Calculate area and/or length for a geometry with full error isolation.

    Returns:
        (area_m2, length_m, projected_crs_str, note)
        Values are None when not applicable.
        This function NEVER raises — all errors are returned as ``note``.
    """
    notes: list[str] = []

    # --- Pre-flight checks ---
    if geometry is None:
        return None, None, None, "Geometry is null."

    if geometry.is_empty:
        return None, None, None, "Geometry is empty — no measurement possible."

    geom_type = geometry.geom_type

    if geom_type in _POINT_TYPES:
        return None, None, None, "Point geometry — no measurement required."

    if geom_type not in (_POLYGON_TYPES | _LINE_TYPES):
        return (
            None,
            None,
            None,
            f"Geometry type '{geom_type}' is not supported for measurement.",
        )

    # --- Geometry validity repair ---
    if not geometry.is_valid:
        repaired = _repair_geometry(geometry)
        if repaired is None:
            return None, None, None, "Invalid geometry could not be repaired."
        geometry = repaired
        notes.append("Geometry was invalid/self-intersecting and was auto-repaired before measurement.")
        # After repair, geom_type may change (e.g., self-touching Polygon → MultiPolygon)
        geom_type = geometry.geom_type
        if geom_type not in (_POLYGON_TYPES | _LINE_TYPES):
            return (
                None,
                None,
                None,
                f"After repair, geometry became '{geom_type}' which is not supported for measurement.",
            )

    # Re-check emptiness after repair
    if geometry.is_empty:
        return None, None, None, "Geometry is empty after repair — no measurement possible."

    try:
        utm_crs, utm_note = _get_utm_crs(geometry, source_crs)
        if utm_note:
            notes.append(utm_note)

        projected_geom = _reproject(geometry, source_crs, utm_crs)
        projected_crs_str = utm_crs.to_string()

        final_note = "; ".join(notes) if notes else None

        if geom_type in _POLYGON_TYPES:
            area = projected_geom.area
            if math.isnan(area) or math.isinf(area):
                return None, None, projected_crs_str, "Computed area is invalid (NaN/Inf)."
            return round(area, 4), None, projected_crs_str, final_note

        if geom_type in _LINE_TYPES:
            length = projected_geom.length
            if math.isnan(length) or math.isinf(length):
                return None, None, projected_crs_str, "Computed length is invalid (NaN/Inf)."
            return None, round(length, 4), projected_crs_str, final_note

    except Exception as exc:
        logger.warning(
            "Measurement failed for geometry type '%s': %s", geom_type, exc, exc_info=True
        )
        return None, None, None, f"Measurement error: {exc}"

    return None, None, None, None
