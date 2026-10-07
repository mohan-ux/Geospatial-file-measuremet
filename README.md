# Geospatial File Measurement API

A production-quality **FastAPI** backend service that accepts geospatial files (Shapefile `.zip` or KML `.kml`), extracts features, and returns accurate area/length measurements using automatic CRS reprojection.

Built as part of the **Aereo Software Development Intern** engineering assignment.

---

## Table of Contents

- [Features](#features)
- [Setup & Run](#setup--run)
- [API Reference](#api-reference)
- [Architecture](#architecture)
- [File Processing Flow](#file-processing-flow)
- [Measurement & CRS Handling](#measurement--crs-handling)
- [Design Decisions](#design-decisions)
- [Learning & Future Scope](#learning--future-scope)

---

## Features

- ✅ Upload `.zip` (Shapefile) or `.kml` files via REST API
- ✅ Per-feature extraction: geometry type, WKT, properties, CRS
- ✅ Area (m²) for Polygons, Length (m) for LineStrings
- ✅ Automatic UTM zone selection from centroid — no manual CRS config
- ✅ Graceful handling of unsupported geometry types (no crashes)
- ✅ SQLite persistence — no external DB setup required
- ✅ Auto-generated Swagger UI at `/docs`
- ✅ Full async stack (FastAPI + SQLAlchemy async + aiosqlite)
- ✅ Integration test suite

---

## Setup & Run

### Prerequisites

- Python 3.11+
- `pip`

### 1. Clone the repo

```bash
git clone https://github.com/your-username/geospatial-file-measurement-api.git
cd geospatial-file-measurement-api
```

### 2. Create a virtual environment

```bash
python -m venv venv

# Windows
venv\Scripts\activate

# macOS / Linux
source venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

> **Note for Windows users**: If GeoPandas/Fiona install fails, use the pre-built wheels:
> ```bash
> pip install pipwin
> pipwin install gdal fiona
> pip install geopandas
> ```

### 4. Configure environment (optional)

```bash
cp .env.example .env
# Edit .env to override defaults (upload dir, DB URL, etc.)
```

### 5. Run the server

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

The API is now live at **http://localhost:8000**  
Swagger docs: **http://localhost:8000/docs**  
ReDoc: **http://localhost:8000/redoc**

### 6. Run tests

```bash
pytest -v
```

---

## API Reference

### Base URL: `http://localhost:8000`

---

### `POST /api/files/`

Upload and process a geospatial file.

**Request:** `multipart/form-data`

| Field | Type | Description |
|-------|------|-------------|
| `file` | File | `.zip` (Shapefile) or `.kml` |

**Example (cURL):**
```bash
# Upload a KML
curl -X POST http://localhost:8000/api/files/ \
  -F "file=@/path/to/survey.kml"

# Upload a Shapefile
curl -X POST http://localhost:8000/api/files/ \
  -F "file=@/path/to/survey.zip"
```

**Response `201 Created`:**
```json
{
  "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "filename": "survey.kml",
  "file_type": "kml",
  "feature_count": 120,
  "crs": "EPSG:4326",
  "status": "COMPLETED",
  "error_message": null,
  "created_at": "2024-10-07T10:00:00Z"
}
```

**Error responses:**
- `422` — Unsupported file type
- `413` — File exceeds size limit

---

### `GET /api/files/`

List all uploaded files.

**Example:**
```bash
curl http://localhost:8000/api/files/
```

**Response `200 OK`:**
```json
[
  {
    "id": "a1b2c3d4-...",
    "filename": "survey.kml",
    "feature_count": 120,
    "crs": "EPSG:4326",
    "status": "COMPLETED",
    "created_at": "2024-10-07T10:00:00Z"
  }
]
```

---

### `GET /api/files/{id}/`

Get metadata for a specific file.

**Example:**
```bash
curl http://localhost:8000/api/files/a1b2c3d4-e5f6-7890-abcd-ef1234567890/
```

**Response `200 OK`:**
```json
{
  "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "filename": "survey.kml",
  "file_type": "kml",
  "feature_count": 120,
  "crs": "EPSG:4326",
  "status": "COMPLETED",
  "error_message": null,
  "created_at": "2024-10-07T10:00:00Z",
  "updated_at": "2024-10-07T10:00:05Z"
}
```

**Error:** `404 Not Found` if id does not exist.

---

### `GET /api/files/{id}/measurements/`

Get per-feature measurements for a processed file.

**Example:**
```bash
curl http://localhost:8000/api/files/a1b2c3d4-e5f6-7890-abcd-ef1234567890/measurements/
```

**Response `200 OK`:**
```json
{
  "file_id": "a1b2c3d4-...",
  "filename": "survey.kml",
  "crs": "EPSG:4326",
  "feature_count": 3,
  "features": [
    {
      "feature_index": 0,
      "geometry_type": "Polygon",
      "geometry_wkt": "POLYGON ((77.5 12.9, 77.6 12.9, ...))",
      "properties": { "name": "Plot A" },
      "area_m2": 123456.78,
      "length_m": null,
      "projected_crs": "+proj=utm +zone=43 +datum=WGS84 +units=m",
      "note": null
    },
    {
      "feature_index": 1,
      "geometry_type": "LineString",
      "geometry_wkt": "LINESTRING (77.5 12.9, 77.6 12.95)",
      "properties": { "name": "Road A" },
      "area_m2": null,
      "length_m": 12450.33,
      "projected_crs": "+proj=utm +zone=43 +datum=WGS84 +units=m",
      "note": null
    },
    {
      "feature_index": 2,
      "geometry_type": "Point",
      "geometry_wkt": "POINT (77.55 12.95)",
      "properties": { "name": "Marker" },
      "area_m2": null,
      "length_m": null,
      "projected_crs": null,
      "note": "Point geometry — no measurement required."
    }
  ]
}
```

---

### `GET /health`

```bash
curl http://localhost:8000/health
# {"status": "ok", "version": "1.0.0"}
```

---

## Architecture

```
geospatial-file-measurement-api/
├── app/
│   ├── main.py                  # FastAPI app, lifespan, CORS, routers
│   ├── database.py              # Async SQLAlchemy engine + session factory
│   ├── models/
│   │   └── file_record.py       # ORM: FileRecord, FeatureMeasurement
│   ├── schemas/
│   │   ├── file.py              # Pydantic: upload/info/list responses
│   │   └── measurement.py       # Pydantic: per-feature measurement response
│   ├── api/
│   │   └── routes/
│   │       └── files.py         # Route handlers (POST + 3× GET)
│   ├── services/
│   │   ├── file_processor.py    # Upload pipeline orchestrator
│   │   ├── measurements.py      # CRS detection + area/length calculation
│   │   └── parsers/
│   │       ├── base.py          # Abstract BaseParser
│   │       ├── shapefile.py     # Shapefile parser (GeoPandas/Fiona)
│   │       └── kml.py           # KML parser (lxml)
│   └── core/
│       ├── config.py            # Pydantic BaseSettings
│       └── exceptions.py        # Typed HTTP exceptions
├── uploads/                     # Uploaded files (gitignored)
├── tests/
│   └── test_files_api.py        # Integration tests
├── requirements.txt
├── pytest.ini
└── .env.example
```

---

## File Processing Flow

```
Client
  │
  ▼
POST /api/files/
  │
  ├─ Validate extension (.zip → shapefile | .kml → kml)
  ├─ Size check (default: 100 MB max)
  ├─ Save file to uploads/{uuid}_{filename}
  ├─ Create FileRecord in DB (status=PROCESSING)
  │
  └─ process_file()
       │
       ├─ Select parser (ShapefileParser | KMLParser)
       ├─ Parse → ParseResult { features[], crs_str }
       │
       ├─ For each feature:
       │    ├─ Polygon        → calculate_measurement() → area_m²
       │    ├─ LineString     → calculate_measurement() → length_m
       │    ├─ Point          → note: "no measurement required"
       │    └─ Other types   → note: "unsupported geometry type"
       │
       ├─ Bulk-insert FeatureMeasurement rows
       └─ Update FileRecord (status=COMPLETED, feature_count, crs)
```

---

## Measurement & CRS Handling

### The Problem

KML and many Shapefiles use **EPSG:4326** (geographic coordinates — degrees of latitude/longitude). Computing area or length directly in degrees produces meaningless results.

### The Solution — Auto UTM Reprojection

For every geometry requiring measurement:

1. **Detect native CRS** from the file (Fiona for Shapefile, hardcoded EPSG:4326 for KML per spec).
2. **If already projected** (metres, feet, etc.) — use it directly.
3. **If geographic (degrees)** — compute the dataset centroid in lon/lat and select the correct **UTM zone**:
   - Zone number = `floor((longitude + 180) / 6) + 1`
   - Hemisphere derived from latitude sign
4. **Reproject** the geometry using PyProj `Transformer` (`always_xy=True` to avoid axis-order issues).
5. Call Shapely's `.area` / `.length` on the projected geometry — units are **metres**.

### Why UTM?

UTM (Universal Transverse Mercator) is a conformal projection that preserves local shapes. Each 6° zone minimises distortion within its region. For drone/survey data over a city or district, a single UTM zone is always appropriate. This approach requires zero manual CRS configuration by the API caller.

---

## Design Decisions

### FastAPI over Django REST Framework
FastAPI's native async support makes it a better fit for I/O-heavy file processing. Its auto-generated OpenAPI docs reduce documentation overhead. Pydantic v2 models give type-safe validation at zero cost.

### SQLite + SQLAlchemy async
Zero external setup — any evaluator can clone and run. For production, swapping to PostgreSQL is a single `DATABASE_URL` change (SQLAlchemy is DB-agnostic).

### Abstract `BaseParser`
Adding GeoJSON or GeoTIFF support later requires only a new class implementing `parse()`. The orchestrator (`file_processor.py`) never needs to change.

### lxml for KML (not fastkml)
`fastkml` has a heavy dependency tree. Direct lxml parsing is ~5× faster for large KML files, handles both KML 2.1 and 2.2 namespaces, and gives full control over geometry extraction.

### Sync processing within the request
The upload endpoint processes the file synchronously before returning. This keeps the API simple (no background task polling) while remaining appropriate for intern-project scale. For production, a Celery/Redis task queue would handle this asynchronously.

### Per-feature WKT storage
Storing geometry as WKT in SQLite allows future features (e.g., spatial queries via Shapely) without adding PostGIS.

---

## Learning & Future Scope

### What I learned building this

- Deep-dive into PyProj's `Transformer` and the `always_xy` pitfall (PyProj 6+ changed axis order).
- Shapely 2.0's new `transform()` API vs. the deprecated `shapely.ops.transform`.
- KML namespace handling — many real-world KML files mix 2.1 and 2.2 namespaces.
- SQLAlchemy 2.0 mapped_column style and async session management.

### Future Scope

| Feature | Description |
|---------|-------------|
| **Async background processing** | Move `process_file()` to Celery + Redis; use WebSocket or polling for status |
| **GeoJSON support** | Add `GeoJSONParser` — trivial with the `BaseParser` abstraction |
| **PostgreSQL + PostGIS** | Enable spatial indexing and geometric queries |
| **Docker + Docker Compose** | One-command setup with `docker-compose up` |
| **File streaming** | Stream large uploads to disk instead of reading all bytes into memory |
| **Multi-CRS output** | Let clients request measurements in custom CRS |
| **Visualisation endpoint** | `GET /api/files/{id}/preview/` returns GeoJSON for map rendering |
| **Auth** | JWT-based auth with per-user file isolation |
| **Batch upload** | Accept multiple files in one request |
