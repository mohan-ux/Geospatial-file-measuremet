"""
KML parser — reads .kml files using lxml for maximum robustness.

KML files are always EPSG:4326 (WGS 84) per the OGC KML specification.

Production hardening:
  - Tolerant XML parsing (recover=True handles malformed KML)
  - Supports KML 2.2 (opengis.net) and KML 2.1 (earth.google.com) namespaces
  - Supports no-namespace KML (some exporters omit the xmlns)
  - Coordinate parsing is fully guarded: bad tokens are skipped, not crashed
  - Handles CDATA sections in <description>
  - Handles empty <coordinates> elements
  - Handles LinearRing as a top-level geometry type
  - MultiGeometry recursion depth limited to 10 to prevent stack overflow
  - Each Placemark failure is isolated — one bad feature never kills the batch
  - Empty-file / zero-Placemark files return an empty feature list, not an error
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from lxml import etree
from shapely.geometry import (
    GeometryCollection,
    LinearRing,
    LineString,
    MultiLineString,
    MultiPoint,
    MultiPolygon,
    Point,
    Polygon,
)
from shapely.geometry.base import BaseGeometry

from app.services.parsers.base import BaseParser, ParsedFeature, ParseResult

logger = logging.getLogger(__name__)

# KML always uses WGS 84
KML_CRS = "EPSG:4326"

# All known KML namespaces
_KML_NAMESPACES = [
    "http://www.opengis.net/kml/2.2",
    "http://earth.google.com/kml/2.2",
    "http://earth.google.com/kml/2.1",
    "",  # no-namespace KML
]

_MAX_MULTIGEOMETRY_DEPTH = 10


# ---------------------------------------------------------------------------
# Coordinate parsing
# ---------------------------------------------------------------------------

def _parse_coordinates(coord_text: str) -> List[Tuple[float, float]]:
    """
    Parse a KML <coordinates> text block into (lon, lat) pairs.

    KML coordinate format: "lon,lat[,alt] lon,lat[,alt] ..."
    Whitespace (spaces, tabs, newlines) separates tuples.
    Altitude is optional and discarded — we work in 2D.

    Malformed tokens are logged and skipped rather than crashing.
    """
    coords: List[Tuple[float, float]] = []
    if not coord_text:
        return coords

    for token in coord_text.split():
        token = token.strip()
        if not token:
            continue
        parts = token.split(",")
        if len(parts) < 2:
            logger.debug("Skipping malformed coordinate token: %r", token)
            continue
        try:
            lon = float(parts[0])
            lat = float(parts[1])
        except ValueError:
            logger.debug("Could not parse float from coordinate token: %r", token)
            continue

        import math
        if math.isnan(lon) or math.isnan(lat) or math.isinf(lon) or math.isinf(lat):
            logger.debug("Skipping NaN/Inf coordinate: lon=%s lat=%s", lon, lat)
            continue

        coords.append((lon, lat))

    return coords


# ---------------------------------------------------------------------------
# Namespace-aware element lookup helpers
# ---------------------------------------------------------------------------

def _find(parent: etree._Element, tag: str) -> Optional[etree._Element]:
    """Find a child element by tag across all known KML namespaces."""
    for ns in _KML_NAMESPACES:
        key = f"{{{ns}}}{tag}" if ns else tag
        el = parent.find(key)
        if el is not None:
            return el
    return None


def _findall(parent: etree._Element, tag: str) -> List[etree._Element]:
    """Find all child elements by tag across all known KML namespaces."""
    for ns in _KML_NAMESPACES:
        key = f"{{{ns}}}{tag}" if ns else tag
        els = parent.findall(key)
        if els:
            return els
    return []


def _find_deep(root: etree._Element, tag: str) -> List[etree._Element]:
    """Find all elements with a given local name anywhere in the tree."""
    results = []
    for ns in _KML_NAMESPACES:
        key = f".//{{{ns}}}{tag}" if ns else f".//{tag}"
        results.extend(root.findall(key))
    # Deduplicate by element id
    seen = set()
    deduped = []
    for el in results:
        eid = id(el)
        if eid not in seen:
            seen.add(eid)
            deduped.append(el)
    return deduped


def _coords_of(element: etree._Element) -> List[Tuple[float, float]]:
    """Extract parsed coordinates from a child <coordinates> element."""
    coord_el = _find(element, "coordinates")
    if coord_el is None or not (coord_el.text or "").strip():
        return []
    return _parse_coordinates(coord_el.text)


# ---------------------------------------------------------------------------
# Geometry extraction
# ---------------------------------------------------------------------------

def _element_to_geometry(
    element: etree._Element,
    depth: int = 0,
) -> Optional[BaseGeometry]:
    """
    Recursively extract a Shapely geometry from a KML element.

    Supports: Point, LineString, LinearRing, Polygon, MultiGeometry.
    Returns None if no recognised geometry is found or parsing fails.

    Depth guard prevents unbounded recursion from maliciously crafted KML.
    """
    if depth > _MAX_MULTIGEOMETRY_DEPTH:
        logger.warning("MultiGeometry nesting depth exceeded limit (%d); truncating.", _MAX_MULTIGEOMETRY_DEPTH)
        return None

    # --- Point ---
    point_el = _find(element, "Point")
    if point_el is not None:
        coords = _coords_of(point_el)
        if len(coords) >= 1:
            return Point(coords[0])

    # --- LineString ---
    ls_el = _find(element, "LineString")
    if ls_el is not None:
        coords = _coords_of(ls_el)
        if len(coords) >= 2:
            return LineString(coords)
        elif len(coords) == 1:
            logger.debug("LineString has only 1 coordinate; treating as Point.")
            return Point(coords[0])

    # --- LinearRing (standalone, rare but valid) ---
    lr_el = _find(element, "LinearRing")
    if lr_el is not None:
        coords = _coords_of(lr_el)
        if len(coords) >= 3:
            try:
                return LinearRing(coords)
            except Exception:
                return LineString(coords)  # fallback

    # --- Polygon ---
    poly_el = _find(element, "Polygon")
    if poly_el is not None:
        outer_el = _find(poly_el, "outerBoundaryIs")
        if outer_el is not None:
            outer_ring_el = _find(outer_el, "LinearRing")
            if outer_ring_el is not None:
                exterior = _coords_of(outer_ring_el)
                if len(exterior) >= 3:
                    holes = []
                    for inner_el in _findall(poly_el, "innerBoundaryIs"):
                        inner_ring_el = _find(inner_el, "LinearRing")
                        if inner_ring_el is not None:
                            hole_coords = _coords_of(inner_ring_el)
                            if len(hole_coords) >= 3:
                                holes.append(hole_coords)
                    try:
                        return Polygon(exterior, holes)
                    except Exception as exc:
                        logger.debug("Polygon construction failed (%s); trying without holes.", exc)
                        try:
                            return Polygon(exterior)
                        except Exception:
                            pass

    # --- MultiGeometry ---
    multi_el = _find(element, "MultiGeometry")
    if multi_el is not None:
        geoms = []
        for child in multi_el:
            geom = _element_to_geometry(child, depth=depth + 1)
            if geom is not None and not geom.is_empty:
                geoms.append(geom)
        if geoms:
            if all(g.geom_type == "Point" for g in geoms):
                return MultiPoint(geoms)
            if all(g.geom_type in ("LineString", "LinearRing") for g in geoms):
                return MultiLineString(geoms)
            if all(g.geom_type == "Polygon" for g in geoms):
                return MultiPolygon(geoms)
            return GeometryCollection(geoms)

    return None


# ---------------------------------------------------------------------------
# Property extraction
# ---------------------------------------------------------------------------

def _extract_properties(placemark: etree._Element) -> Dict[str, Any]:
    """
    Extract metadata from a <Placemark>:
      - <name>
      - <description> (text content, CDATA stripped by lxml automatically)
      - <ExtendedData><SimpleData name="...">value</SimpleData>
    """
    props: Dict[str, Any] = {}

    for tag in ("name", "description", "visibility", "styleUrl"):
        el = _find(placemark, tag)
        if el is not None:
            text = (el.text or "").strip()
            if text:
                props[tag] = text

    # ExtendedData / SimpleData
    ext_el = _find(placemark, "ExtendedData")
    if ext_el is not None:
        for ns in _KML_NAMESPACES:
            sd_tag = f"{{{ns}}}SimpleData" if ns else "SimpleData"
            for sd in ext_el.iter(sd_tag):
                key = sd.get("name", "unknown")
                props[key] = (sd.text or "").strip() or None

        # Also handle <Data><value> pattern
        data_tag = f"{{{_KML_NAMESPACES[0]}}}Data" if _KML_NAMESPACES[0] else "Data"
        for data_el in ext_el.findall(data_tag):
            key = data_el.get("name", "unknown")
            val_el = _find(data_el, "value")
            if val_el is not None:
                props[key] = (val_el.text or "").strip() or None

    return props


# ---------------------------------------------------------------------------
# Parser class
# ---------------------------------------------------------------------------

class KMLParser(BaseParser):
    """
    Parses a .kml file and extracts Placemark features.

    KML is always EPSG:4326 by specification; this parser enforces that.
    """

    def parse(self, file_path: str) -> ParseResult:
        path = Path(file_path)
        logger.info("Reading KML from '%s'", path.name)

        if not path.exists():
            raise ValueError(f"KML file not found: '{path}'.")

        if path.stat().st_size == 0:
            logger.warning("KML file '%s' is empty.", path.name)
            return ParseResult(features=[], crs_str=KML_CRS)

        # --- Parse XML with recovery mode for tolerant parsing ---
        try:
            parser = etree.XMLParser(
                recover=True,           # tolerate malformed XML
                remove_blank_text=True,
                resolve_entities=False,  # security: no external entity expansion
                no_network=True,         # security: no network access during parse
            )
            tree = etree.parse(str(path), parser)
            root = tree.getroot()
        except etree.XMLSyntaxError as exc:
            raise ValueError(f"KML file '{path.name}' is not valid XML: {exc}") from exc
        except Exception as exc:
            raise ValueError(f"Failed to parse KML '{path.name}': {exc}") from exc

        if root is None:
            raise ValueError(f"KML file '{path.name}' produced an empty XML tree.")

        # --- Find all Placemark elements ---
        placemarks = _find_deep(root, "Placemark")

        if not placemarks:
            logger.info("KML file '%s' contains no Placemark elements.", path.name)
            return ParseResult(features=[], crs_str=KML_CRS)

        # --- Extract features, isolating per-Placemark errors ---
        features: List[ParsedFeature] = []
        for idx, pm in enumerate(placemarks):
            try:
                geom = _element_to_geometry(pm)
                props = _extract_properties(pm)
                features.append(
                    ParsedFeature(
                        index=idx,
                        geometry_type=geom.geom_type if geom is not None else "Unknown",
                        geometry=geom,
                        properties=props,
                    )
                )
            except Exception as exc:
                logger.warning(
                    "Failed to parse Placemark at index %d in '%s': %s. Skipping.",
                    idx,
                    path.name,
                    exc,
                )
                # Add a null-geometry entry so feature_count is still accurate
                features.append(
                    ParsedFeature(
                        index=idx,
                        geometry_type="Unknown",
                        geometry=None,
                        properties={"_parse_error": str(exc)},
                    )
                )

        logger.info(
            "KML parsed: %d placemarks, CRS=%s (file: %s)",
            len(features),
            KML_CRS,
            path.name,
        )
        return ParseResult(features=features, crs_str=KML_CRS)
