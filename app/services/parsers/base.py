"""
Abstract base parser that all format-specific parsers must implement.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from shapely.geometry.base import BaseGeometry


@dataclass
class ParsedFeature:
    """Represents a single geospatial feature extracted from a file."""
    index: int
    geometry_type: str
    geometry: Optional[BaseGeometry]
    properties: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ParseResult:
    """Result of parsing a geospatial file."""
    features: List[ParsedFeature]
    crs_str: str  # e.g. "EPSG:4326"


class BaseParser(ABC):
    """
    Abstract base class for geospatial file parsers.

    Subclasses must implement `parse(file_path: str) -> ParseResult`.
    """

    @abstractmethod
    def parse(self, file_path: str) -> ParseResult:
        """
        Parse the geospatial file at `file_path` and return a ParseResult.

        Args:
            file_path: Absolute or relative path to the file on disk.

        Returns:
            ParseResult containing all extracted features and the file CRS.
        """
        ...
