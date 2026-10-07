"""
Integration tests for the Geospatial File Measurement API.

Tests use:
  - httpx.AsyncClient as the test client
  - An in-memory SQLite DB to avoid touching production data
  - Synthetic sample files generated on-the-fly via fixtures
"""

import io
import json
import zipfile
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base, get_session
from app.main import app

# ---------------------------------------------------------------------------
# Test DB setup — in-memory SQLite, isolated per test session
# ---------------------------------------------------------------------------
TEST_DB_URL = "sqlite+aiosqlite:///:memory:"

test_engine = create_async_engine(TEST_DB_URL, echo=False, future=True)
TestAsyncSession = async_sessionmaker(
    bind=test_engine, class_=AsyncSession, expire_on_commit=False
)


async def override_get_session():
    async with TestAsyncSession() as session:
        yield session


@pytest_asyncio.fixture(scope="session", autouse=True)
async def setup_test_db():
    """Create all tables once for the test session."""
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    """
    HTTP test client with:
      - DB dependency overridden to in-memory SQLite
      - Upload dir pointed at a temp directory
    """
    monkeypatch.setattr("app.core.config.settings.upload_dir", str(tmp_path))
    app.dependency_overrides[get_session] = override_get_session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Sample file helpers
# ---------------------------------------------------------------------------

def _make_kml(name: str = "Test Place") -> bytes:
    """Generate a minimal KML with one Polygon and one Point placemark."""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Placemark>
      <name>{name} - Polygon</name>
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
    <Placemark>
      <name>{name} - Point</name>
      <Point>
        <coordinates>77.55,12.95,0</coordinates>
      </Point>
    </Placemark>
    <Placemark>
      <name>{name} - LineString</name>
      <LineString>
        <coordinates>
          77.5,12.9,0 77.6,12.95,0 77.7,12.9,0
        </coordinates>
      </LineString>
    </Placemark>
  </Document>
</kml>""".encode("utf-8")


def _make_shapefile_zip(tmp_dir: Path) -> bytes:
    """
    Build a ZIP containing THREE single-geometry-type Shapefiles.

    Shapefile format enforces one geometry type per .shp file, so we create:
      - polygons.shp  (1 Polygon feature)
      - points.shp    (1 Point feature)
      - lines.shp     (1 LineString feature)

    All packed into a single ZIP archive — a common real-world pattern
    for distributing related layers together.
    """
    import geopandas as gpd
    from shapely.geometry import LineString, Point, Polygon

    shp_dir = tmp_dir / "shp_sample"
    shp_dir.mkdir(parents=True, exist_ok=True)

    layers = [
        (
            "polygons",
            gpd.GeoDataFrame(
                {"name": ["Bangalore Polygon"], "value": [42]},
                geometry=[Polygon([(77.5, 12.9), (77.6, 12.9), (77.6, 13.0), (77.5, 13.0)])],
                crs="EPSG:4326",
            ),
        ),
        (
            "points",
            gpd.GeoDataFrame(
                {"name": ["Bangalore Point"], "value": [0]},
                geometry=[Point(77.55, 12.95)],
                crs="EPSG:4326",
            ),
        ),
        (
            "lines",
            gpd.GeoDataFrame(
                {"name": ["Bangalore Line"], "value": [7]},
                geometry=[LineString([(77.5, 12.9), (77.6, 12.95), (77.7, 12.9)])],
                crs="EPSG:4326",
            ),
        ),
    ]

    for layer_name, gdf in layers:
        gdf.to_file(str(shp_dir / f"{layer_name}.shp"))

    # Pack all shapefile companion files into one ZIP
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for fp in shp_dir.iterdir():
            zf.write(fp, fp.name)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_health(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_upload_kml(client):
    kml_bytes = _make_kml("BLR")
    files = {"file": ("bangalore.kml", kml_bytes, "application/vnd.google-earth.kml+xml")}
    resp = await client.post("/api/files/", files=files)

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["filename"] == "bangalore.kml"
    assert body["status"] == "COMPLETED"
    assert body["crs"] == "EPSG:4326"
    assert body["feature_count"] == 3
    return body["id"]


@pytest.mark.asyncio
async def test_upload_unsupported_type(client):
    files = {"file": ("data.csv", b"a,b,c", "text/csv")}
    resp = await client.post("/api/files/", files=files)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_get_file_info(client):
    # Upload first
    kml_bytes = _make_kml("Test")
    files = {"file": ("test.kml", kml_bytes, "application/vnd.google-earth.kml+xml")}
    upload_resp = await client.post("/api/files/", files=files)
    file_id = upload_resp.json()["id"]

    # Retrieve
    resp = await client.get(f"/api/files/{file_id}/")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == file_id
    assert body["feature_count"] == 3


@pytest.mark.asyncio
async def test_get_file_not_found(client):
    resp = await client.get("/api/files/nonexistent-id/")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_measurements_kml(client):
    kml_bytes = _make_kml("Measurement Test")
    files = {"file": ("mtest.kml", kml_bytes, "application/vnd.google-earth.kml+xml")}
    upload_resp = await client.post("/api/files/", files=files)
    file_id = upload_resp.json()["id"]

    resp = await client.get(f"/api/files/{file_id}/measurements/")
    assert resp.status_code == 200
    body = resp.json()

    features = body["features"]
    assert len(features) == 3

    polygon_feat = next(f for f in features if f["geometry_type"] == "Polygon")
    assert polygon_feat["area_m2"] is not None
    assert polygon_feat["area_m2"] > 0
    assert polygon_feat["length_m"] is None

    point_feat = next(f for f in features if f["geometry_type"] == "Point")
    assert point_feat["area_m2"] is None
    assert point_feat["length_m"] is None
    assert "no measurement" in point_feat["note"].lower()

    line_feat = next(f for f in features if f["geometry_type"] == "LineString")
    assert line_feat["length_m"] is not None
    assert line_feat["length_m"] > 0
    assert line_feat["area_m2"] is None


@pytest.mark.asyncio
async def test_list_files(client):
    # Upload two files
    for name in ("list_test_1.kml", "list_test_2.kml"):
        kml = _make_kml(name)
        await client.post(
            "/api/files/",
            files={"file": (name, kml, "application/vnd.google-earth.kml+xml")},
        )

    resp = await client.get("/api/files/")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
    assert len(resp.json()) >= 2


@pytest.mark.asyncio
async def test_upload_shapefile(client, tmp_path):
    zip_bytes = _make_shapefile_zip(tmp_path)
    files = {"file": ("sample.zip", zip_bytes, "application/zip")}
    resp = await client.post("/api/files/", files=files)

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "COMPLETED"
    # GeoPandas reads the first .shp layer in the zip — each layer has 1 feature
    assert body["feature_count"] >= 1
    assert body["crs"] is not None
    assert "EPSG" in body["crs"]
