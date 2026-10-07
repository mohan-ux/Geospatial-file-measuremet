"""
Comprehensive edge-case and robustness tests for Geospatial File Measurement API.

Covers:
  - Empty files (0 bytes) -> 400 Bad Request
  - Oversized uploads -> 413 Payload Too Large
  - Malformed & corrupt KML (unclosed tags, broken coordinates, missing geometries)
  - Self-intersecting polygon (bowtie) with auto-repair via shapely.make_valid
  - Extreme polar latitudes (-89°, +89°) & antimeridian coordinates
  - Non-standard & multi-namespace KML
  - Corrupt zip archives & zip missing .shp files
  - Empty geometries & Point geometries (non-measurable)
  - MultiPolygon and MultiLineString geometries
"""

import io
import zipfile
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base, get_session
from app.main import app
from app.services.measurements import calculate_measurement
from pyproj import CRS
from shapely.geometry import (
    Point,
    Polygon,
    MultiPolygon,
    LineString,
    MultiLineString,
    GeometryCollection,
)

# In-memory DB setup
TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
test_engine = create_async_engine(TEST_DB_URL, echo=False, future=True)
TestAsyncSession = async_sessionmaker(
    bind=test_engine, class_=AsyncSession, expire_on_commit=False
)


async def override_get_session():
    async with TestAsyncSession() as session:
        yield session


@pytest.fixture(scope="module", autouse=True)
async def setup_test_db():
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setattr("app.core.config.settings.upload_dir", str(tmp_path))
    app.dependency_overrides[get_session] = override_get_session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 1. Empty & Oversized Files
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_upload_empty_kml(client):
    """Uploading a 0-byte file must return 400 Bad Request."""
    files = {"file": ("empty.kml", b"", "application/vnd.google-earth.kml+xml")}
    resp = await client.post("/api/files/", files=files)
    assert resp.status_code == 400
    assert "empty" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_upload_empty_zip(client):
    """Uploading a 0-byte zip must return 400 Bad Request."""
    files = {"file": ("empty.zip", b"", "application/zip")}
    resp = await client.post("/api/files/", files=files)
    assert resp.status_code == 400
    assert "empty" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_upload_exceeds_max_size(client, monkeypatch):
    """Uploading file exceeding max_upload_size_mb must return 413."""
    # Temporarily set max upload to 1 byte (via the base setting)
    # The property max_upload_size_bytes will compute 1 * 1024 * 1024 = 1MB
    # So we need to make the file larger than that
    from app.core.config import settings
    original_max = settings.max_upload_size_mb
    settings.max_upload_size_mb = 0  # Set to 0 MB (0 bytes)
    
    try:
        # Create file larger than 0 bytes
        oversized_data = b"A" * 1024
        files = {"file": ("large.kml", oversized_data, "application/vnd.google-earth.kml+xml")}
        resp = await client.post("/api/files/", files=files)
        assert resp.status_code == 413
        assert "exceeds" in resp.json()["detail"].lower()
    finally:
        settings.max_upload_size_mb = original_max


# ---------------------------------------------------------------------------
# 2. Corrupt / Malicious Zip Handling
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_upload_corrupt_zip(client):
    """Uploading a corrupted zip file should be marked FAILED without server crash."""
    corrupt_bytes = b"PK\x03\x04ThisIsNotAValidZipArchiveContent"
    files = {"file": ("corrupt.zip", corrupt_bytes, "application/zip")}
    resp = await client.post("/api/files/", files=files)
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "FAILED"
    assert body["error_message"] is not None


@pytest.mark.asyncio
async def test_upload_zip_missing_shp(client):
    """Zip archive containing files but NO .shp file should fail gracefully."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("readme.txt", "This zip has no shapefile inside.")
    
    files = {"file": ("no_shp.zip", buf.getvalue(), "application/zip")}
    resp = await client.post("/api/files/", files=files)
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "FAILED"
    assert "no .shp" in body["error_message"].lower()


# ---------------------------------------------------------------------------
# 3. Malformed / Degenerate KML Files
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_upload_malformed_xml_kml(client):
    """KML with unclosed tags should be recovered gracefully by lxml parser."""
    malformed_kml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <kml xmlns="http://www.opengis.net/kml/2.2">
      <Document>
        <Placemark>
          <name>Recoverable Polygon</name>
          <Polygon>
            <outerBoundaryIs>
              <LinearRing>
                <coordinates>
                  77.5,12.9,0 77.6,12.9,0 77.6,13.0,0 77.5,13.0,0 77.5,12.9,0
                </coordinates>
              </LinearRing>
            </outerBoundaryIs>
          </Polygon>
        </Placemark>
    """  # Missing closing </Document> and </kml>
    
    files = {"file": ("recovered.kml", malformed_kml, "application/vnd.google-earth.kml+xml")}
    resp = await client.post("/api/files/", files=files)
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "COMPLETED"
    assert body["feature_count"] == 1


@pytest.mark.asyncio
async def test_upload_kml_invalid_coordinates(client):
    """KML with Placemarks containing invalid/corrupt coordinate strings."""
    corrupt_coord_kml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <kml xmlns="http://www.opengis.net/kml/2.2">
      <Document>
        <Placemark>
          <name>Corrupt Coords Placemark</name>
          <Point>
            <coordinates>not_a_number,also_invalid,0</coordinates>
          </Point>
        </Placemark>
        <Placemark>
          <name>Valid LineString</name>
          <LineString>
            <coordinates>77.1,12.1,0 77.2,12.2,0</coordinates>
          </LineString>
        </Placemark>
      </Document>
    </kml>"""

    files = {"file": ("corrupt_coords.kml", corrupt_coord_kml, "application/vnd.google-earth.kml+xml")}
    resp = await client.post("/api/files/", files=files)
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "COMPLETED"
    assert body["feature_count"] == 2

    file_id = body["id"]
    m_resp = await client.get(f"/api/files/{file_id}/measurements/")
    assert m_resp.status_code == 200
    features = m_resp.json()["features"]
    assert len(features) == 2
    # The first feature should record a parsing note rather than crashing
    assert features[0]["geometry_type"] in ("Null", "Unknown") or features[0]["note"] is not None
    # The second feature should be measured accurately
    assert features[1]["geometry_type"] == "LineString"
    assert features[1]["length_m"] is not None
    assert features[1]["length_m"] > 0


@pytest.mark.asyncio
async def test_upload_kml_self_intersecting_polygon(client):
    """Bowtie self-intersecting polygon must be repaired by shapely.make_valid and measured."""
    # Self-intersecting hourglass / bowtie coordinates
    bowtie_kml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <kml xmlns="http://www.opengis.net/kml/2.2">
      <Document>
        <Placemark>
          <name>Bowtie Polygon</name>
          <Polygon>
            <outerBoundaryIs>
              <LinearRing>
                <coordinates>
                  0,0,0 1,1,0 0,1,0 1,0,0 0,0,0
                </coordinates>
              </LinearRing>
            </outerBoundaryIs>
          </Polygon>
        </Placemark>
      </Document>
    </kml>"""

    files = {"file": ("bowtie.kml", bowtie_kml, "application/vnd.google-earth.kml+xml")}
    resp = await client.post("/api/files/", files=files)
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "COMPLETED"

    file_id = body["id"]
    m_resp = await client.get(f"/api/files/{file_id}/measurements/")
    features = m_resp.json()["features"]
    assert len(features) == 1
    assert features[0]["area_m2"] is not None
    assert features[0]["area_m2"] > 0
    assert features[0]["note"] is not None
    assert "repaired" in features[0]["note"].lower()


# ---------------------------------------------------------------------------
# 4. Geometry and Projection Unit Tests
# ---------------------------------------------------------------------------

def test_measurement_polar_regions():
    """Geometries near the North Pole (89° N) or South Pole (-89° S) should not crash."""
    source_crs = CRS.from_epsg(4326)
    # Polygon near North Pole
    poly_north = Polygon([(0, 89.0), (1, 89.0), (1, 89.5), (0, 89.5), (0, 89.0)])
    area_m2, length_m, proj_crs, note = calculate_measurement(poly_north, source_crs)
    assert area_m2 is not None
    assert area_m2 > 0
    assert proj_crs is not None

    # Polygon near South Pole
    poly_south = Polygon([(0, -89.0), (1, -89.0), (1, -89.5), (0, -89.5), (0, -89.0)])
    area_m2_s, length_m_s, proj_crs_s, note_s = calculate_measurement(poly_south, source_crs)
    assert area_m2_s is not None
    assert area_m2_s > 0


def test_measurement_antimeridian_crossing():
    """Line crossing the 180° / -180° antimeridian produces measurement and notice."""
    source_crs = CRS.from_epsg(4326)
    line = LineString([(179.5, 0.0), (-179.5, 0.0)])
    area_m2, length_m, proj_crs, note = calculate_measurement(line, source_crs)
    assert length_m is not None
    assert length_m > 0
    assert note is not None
    assert "antimeridian" in note.lower()


def test_measurement_multipolygon_and_multilinestring():
    """MultiPolygon and MultiLineString support."""
    source_crs = CRS.from_epsg(4326)
    p1 = Polygon([(77.0, 12.0), (77.1, 12.0), (77.1, 12.1), (77.0, 12.1), (77.0, 12.0)])
    p2 = Polygon([(77.2, 12.0), (77.3, 12.0), (77.3, 12.1), (77.2, 12.1), (77.2, 12.0)])
    mp = MultiPolygon([p1, p2])

    area_m2, length_m, proj_crs, note = calculate_measurement(mp, source_crs)
    assert area_m2 is not None
    assert area_m2 > 0
    assert length_m is None

    l1 = LineString([(77.0, 12.0), (77.1, 12.1)])
    l2 = LineString([(77.2, 12.0), (77.3, 12.1)])
    ml = MultiLineString([l1, l2])

    area_m2_l, length_m_l, proj_crs_l, note_l = calculate_measurement(ml, source_crs)
    assert length_m_l is not None
    assert length_m_l > 0
    assert area_m2_l is None


def test_measurement_empty_geometry():
    """Empty geometries should return None measurements gracefully."""
    source_crs = CRS.from_epsg(4326)
    empty_poly = Polygon()
    area, length, proj, note = calculate_measurement(empty_poly, source_crs)
    assert area is None
    assert length is None
    assert "empty" in note.lower()


@pytest.mark.asyncio
async def test_upload_zip_path_traversal(client):
    """Zip archives attempting path traversal (Zip Slip) must fail safely."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../../evil.txt", "attack content")
        zf.writestr("layer.shp", "shp content")
    
    files = {"file": ("traversal.zip", buf.getvalue(), "application/zip")}
    resp = await client.post("/api/files/", files=files)
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "FAILED"
    assert "traversal" in body["error_message"].lower()


@pytest.mark.asyncio
async def test_upload_kml_multigeometry_and_extended_data(client):
    """KML with MultiGeometry and ExtendedData elements."""
    kml_content = b"""<?xml version="1.0" encoding="UTF-8"?>
    <kml xmlns="http://www.opengis.net/kml/2.2">
      <Document>
        <Placemark>
          <name>\xe0\xa4\xac\xe0\xa5\x87\xe0\xa4\x82\xe0\xa4\x97\xe0\xa4\xb2\xe0\xa5\x82\xe0\xa4\xb0\xe0\xa5\x81 Multi</name>
          <description>Unicode text &amp; multi-geometry test</description>
          <ExtendedData>
            <Data name="SiteCode"><value>BLR-001</value></Data>
            <Data name="SurveyYear"><value>2026</value></Data>
          </ExtendedData>
          <MultiGeometry>
            <Polygon>
              <outerBoundaryIs>
                <LinearRing>
                  <coordinates>
                    77.5,12.9,0 77.6,12.9,0 77.6,13.0,0 77.5,13.0,0 77.5,12.9,0
                  </coordinates>
                </LinearRing>
              </outerBoundaryIs>
            </Polygon>
            <LineString>
              <coordinates>
                77.5,12.9,0 77.6,12.95,0 77.7,12.9,0
              </coordinates>
            </LineString>
          </MultiGeometry>
        </Placemark>
      </Document>
    </kml>"""

    files = {"file": ("multi_extended.kml", kml_content, "application/vnd.google-earth.kml+xml")}
    resp = await client.post("/api/files/", files=files)
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "COMPLETED"
    assert body["feature_count"] == 1

    file_id = body["id"]
    m_resp = await client.get(f"/api/files/{file_id}/measurements/")
    assert m_resp.status_code == 200
    features = m_resp.json()["features"]
    assert len(features) == 1
    # Check properties extracted from ExtendedData
    props = features[0]["properties"]
    assert props is not None
    assert props.get("SiteCode") == "BLR-001"
    assert props.get("SurveyYear") == "2026"


@pytest.mark.asyncio
async def test_upload_projected_shapefile(client, tmp_path):
    """Shapefile that is already in projected coordinates (e.g. UTM 43N / EPSG:32643)."""
    import geopandas as gpd

    shp_dir = tmp_path / "utm_shp"
    shp_dir.mkdir(parents=True, exist_ok=True)

    # In UTM 43N, coordinates are in meters
    poly = Polygon([(770000, 1420000), (771000, 1420000), (771000, 1421000), (770000, 1421000), (770000, 1420000)])
    gdf = gpd.GeoDataFrame(
        {"name": ["UTM 43N Box"]},
        geometry=[poly],
        crs="EPSG:32643",
    )
    gdf.to_file(str(shp_dir / "projected.shp"))

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for fp in shp_dir.iterdir():
            zf.write(fp, fp.name)

    files = {"file": ("utm43n.zip", buf.getvalue(), "application/zip")}
    resp = await client.post("/api/files/", files=files)
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "COMPLETED"

    file_id = body["id"]
    m_resp = await client.get(f"/api/files/{file_id}/measurements/")
    features = m_resp.json()["features"]
    assert len(features) == 1
    # 1000m x 1000m square = 1,000,000 m²
    assert pytest.approx(features[0]["area_m2"], rel=1e-2) == 1000000.0

