"""
Shapefile parser — reads .zip archives containing Shapefiles via GeoPandas.

Production hardening:
  - Zip safety: path traversal check, zip-bomb guard (uncompressed size limit)
  - Content validation: ensures at least one .shp file exists in the archive
  - Extracts to a secure temp directory (never writes outside it)
  - Handles null geometries per row gracefully
  - Handles encoding errors in .dbf attribute tables
  - Cleans up the temp directory after parsing
  - Serialises all attribute types to JSON-safe values
"""

import logging
import os
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import geopandas as gpd
import numpy as np

from app.services.parsers.base import BaseParser, ParsedFeature, ParseResult

logger = logging.getLogger(__name__)

# Safety limits
_MAX_UNCOMPRESSED_BYTES = 500 * 1024 * 1024   # 500 MB
_MAX_ZIP_ENTRIES = 10_000


class ShapefileParser(BaseParser):
    """
    Parses a .zip archive that contains Shapefile layer(s).

    If the zip contains multiple .shp files (multiple layers), the first one
    found is parsed and a warning is logged. This mirrors GeoPandas behaviour
    and is the most common real-world pattern.
    """

    def parse(self, file_path: str) -> ParseResult:
        path = Path(file_path)

        # --- 1. Validate it is a real ZIP ---
        if not zipfile.is_zipfile(path):
            raise ValueError(f"'{path.name}' is not a valid ZIP archive.")

        # --- 2. Safety checks ---
        self._check_zip_safety(path)

        # --- 3. Extract to temp dir and parse ---
        with tempfile.TemporaryDirectory(prefix="geo_shp_") as tmpdir:
            self._safe_extract(path, tmpdir)
            return self._parse_from_dir(Path(tmpdir), file_path)

    # ------------------------------------------------------------------
    # Safety
    # ------------------------------------------------------------------

    @staticmethod
    def _check_zip_safety(path: Path) -> None:
        """
        Validate zip contents before extraction.

        Raises ValueError on:
          - Zip-bomb (uncompressed size > limit)
          - Path traversal attack (absolute paths or '..' components)
          - Too many entries (decompression bomb variant)
        """
        with zipfile.ZipFile(path) as zf:
            entries = zf.infolist()

            if len(entries) > _MAX_ZIP_ENTRIES:
                raise ValueError(
                    f"ZIP contains {len(entries)} entries, exceeding the limit of {_MAX_ZIP_ENTRIES}."
                )

            total_uncompressed = 0
            for info in entries:
                # Path traversal check
                member_path = info.filename
                if os.path.isabs(member_path) or ".." in Path(member_path).parts:
                    raise ValueError(
                        f"Path traversal detected in ZIP archive: '{member_path}'."
                    )
                total_uncompressed += info.file_size

            if total_uncompressed > _MAX_UNCOMPRESSED_BYTES:
                raise ValueError(
                    f"ZIP archive uncompressed size ({total_uncompressed / 1e6:.1f} MB) "
                    f"exceeds the maximum allowed ({_MAX_UNCOMPRESSED_BYTES / 1e6:.0f} MB). "
                    "Possible zip-bomb detected."
                )

    @staticmethod
    def _safe_extract(zip_path: Path, dest_dir: str) -> None:
        """Extract only safe members from the ZIP to dest_dir."""
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                # Flatten directory structure to dest_dir — strip any sub-dirs
                basename = Path(info.filename).name
                if not basename:
                    continue  # skip directory entries
                dest = Path(dest_dir) / basename
                with zf.open(info) as src, open(dest, "wb") as dst:
                    dst.write(src.read())

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_from_dir(self, tmpdir: Path, original_path: str) -> ParseResult:
        shp_files: List[Path] = sorted(tmpdir.glob("*.shp"))

        if not shp_files:
            raise ValueError(
                "No .shp file found in the ZIP archive. "
                "Ensure the archive contains at least one Shapefile (.shp, .dbf, .shx)."
            )

        if len(shp_files) > 1:
            logger.warning(
                "ZIP contains %d .shp files; reading the first: '%s'. "
                "Other layers: %s",
                len(shp_files),
                shp_files[0].name,
                [f.name for f in shp_files[1:]],
            )

        shp_path = shp_files[0]
        logger.info("Reading Shapefile layer '%s'", shp_path.name)

        try:
            gdf: gpd.GeoDataFrame = gpd.read_file(str(shp_path))
        except Exception as exc:
            raise ValueError(f"Failed to read Shapefile '{shp_path.name}': {exc}") from exc

        crs_str = self._extract_crs_str(gdf)

        features: List[ParsedFeature] = []
        attr_columns = [col for col in gdf.columns if col != "geometry"]

        for idx, row in gdf.iterrows():
            geom = row.geometry  # may be None for null-geometry rows

            props: Dict[str, Any] = {
                col: self._serialize_value(row[col]) for col in attr_columns
            }

            features.append(
                ParsedFeature(
                    index=int(idx),   # type: ignore[arg-type]
                    geometry_type=geom.geom_type if geom is not None else "Unknown",
                    geometry=geom,
                    properties=props,
                )
            )

        logger.info(
            "Shapefile parsed: %d features, CRS=%s (layer: %s)",
            len(features),
            crs_str,
            shp_path.name,
        )
        return ParseResult(features=features, crs_str=crs_str)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_crs_str(gdf: gpd.GeoDataFrame) -> str:
        """Return a human-readable CRS identifier from a GeoDataFrame."""
        if gdf.crs is None:
            logger.warning("Shapefile has no CRS defined (.prj missing or empty). Defaulting to EPSG:4326.")
            return "EPSG:4326"
        epsg = gdf.crs.to_epsg()
        if epsg:
            return f"EPSG:{epsg}"
        wkt = gdf.crs.to_wkt()
        return wkt[:200] if wkt else gdf.crs.to_string()

    @staticmethod
    def _serialize_value(val: Any) -> Any:
        """
        Convert attribute values to JSON-serialisable types.

        Handles:
          - numpy scalar types (int64, float64, bool_)
          - pandas NA / NaT / NaN
          - datetime objects
          - bytes → hex string
          - fallback → str()
        """
        if val is None:
            return None

        # numpy NA check must come before isinstance(val, float) since
        # numpy scalars also satisfy float checks
        try:
            if hasattr(val, "__class__") and val.__class__.__module__ == "numpy":
                if isinstance(val, (np.integer,)):
                    return int(val)
                if isinstance(val, (np.floating,)):
                    v = float(val)
                    return None if (np.isnan(v) or np.isinf(v)) else v
                if isinstance(val, np.bool_):
                    return bool(val)
                # numpy datetime or other — fall through to str
        except Exception:
            pass

        if isinstance(val, float):
            import math
            return None if (math.isnan(val) or math.isinf(val)) else val
        if isinstance(val, (int, bool, str)):
            return val
        if isinstance(val, bytes):
            return val.hex()

        # pandas NA / NaT
        try:
            import pandas as pd
            if pd.isna(val):
                return None
        except (TypeError, ValueError, ImportError):
            pass

        # datetime
        try:
            return val.isoformat()
        except AttributeError:
            pass

        return str(val)
