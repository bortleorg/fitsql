# fitsql

A database and query language for astrophotography FITS files. Index your FITS and XISF frames, then query them — by object, filter, telescope, quality metrics, or free-form selection expressions — and browse the results as thumbnails.

Formats: FITS (`.fit`, `.fits`, `.fts`) and XISF (`.xisf`, from PixInsight or N.I.N.A. — uncompressed, zlib, LZ4/LZ4HC or zstd, with or without byte shuffling). Raw one-shot-colour frames (a `BAYERPAT` header) are measured on 2×2 superpixels and reported in sensor pixels.

## Stack

- **Backend**: Python FastAPI + SQLAlchemy (SQLite) + astropy
- **Frontend**: React + Vite
- **Thumbnails**: PIL/Pillow with asinh stretch
- **Star detection**: SEP (optional)

---

## Setup

### Prerequisites

- Python 3.10+
- Node.js 18+

---

### Backend

```bash
cd backend
python -m venv venv

# Windows
venv\Scripts\activate
# macOS/Linux
source venv/bin/activate

pip install -r requirements.txt
```

> **Note on SEP**: `sep` may require a C compiler. On Windows, install Visual Studio Build Tools or use a pre-built wheel. Star counting is optional — the app works without it.

Start the backend:

```bash
uvicorn main:app --reload --port 8000
```

The API will be at `http://localhost:8000`. The SQLite database is created automatically at `backend/fitsql.db` (an existing `backend/catalog.db` from before the rename is picked up automatically).

---

### Frontend

```bash
cd frontend
npm install
npm run dev
```

The UI will be at `http://localhost:5173`.

---

## Usage

1. Open the app at `http://localhost:5173`
2. Go to **Settings** → add one or more folder paths containing your FITS files
3. Click **Scan All Paths** — the backend walks each folder recursively, indexes headers, and generates thumbnails
4. Go to **Catalog** to browse and filter your files

---

## Public read-only API (`/api/v1`)

A versioned, read-only API for integrations, with its own OpenAPI schema:

- Interactive docs: `http://<host>/api/v1/docs` (Swagger) and `/api/v1/redoc`
- Schema: `/api/v1/openapi.json` — feed it to any OpenAPI client generator

| Path | What |
|------|------|
| `GET /api/v1/frames` | Search frames: filters, cone / FOV search, `where` expressions, sorting, pagination |
| `GET /api/v1/frames/{id}` | One frame |
| `GET /api/v1/frames/{id}/thumbnail` | Small JPEG thumbnail (≤512 px) |
| `GET /api/v1/frames/{id}/preview` | Large stretched JPEG for zooming (`size=`, rendered on first request) |
| `GET /api/v1/frames/{id}/diagnostics` | Per-star classification, screening grid and satellite trails from analysis |
| `GET /api/v1/fields` | Every selectable field path with type, description, sortability |
| `GET /api/v1/targets`, `/targets/{object}` | Integration per OBJECT, per night |
| `GET /api/v1/user-targets`, `/user-targets/{id}` | Integration per user-defined sky position |
| `GET /api/v1/sessions` | Integration per object + night |
| `GET /api/v1/calibration` | Which light setups have matching darks, flats and bias |
| `GET /api/v1/catalog` | Catalog totals and distinct filter / camera / telescope values |
| `GET /api/v1/queries`, `/queries/{id}/frames` | Saved Selector queries and the frames they approve |

Frames come back as nested groups (`file`, `capture`, `pointing`, `plate_solve`, `position`,
`quality`, `conditions`, `status`, `thumbnail`, `links`). Pick what you need with
`fields`, add the full FITS header with `expand=header`, and screen each frame against the
rest of its imaging session (clouds, veils, stray light) with `expand=sequence`:

```bash
curl -H "X-API-Key: $KEY" "http://localhost:8000/api/v1/frames?image_type=Light%20Frame&filter=Ha&path_not_like=reject&fields=capture,quality.hfr_px,thumbnail.url&sort=-quality.score"
curl -H "X-API-Key: $KEY" "http://localhost:8000/api/v1/frames/123?fields=id,header.EXPTIME,header.CCD-TEMP"
```

Set `PUBLIC_API_KEYS=key1,key2` to require a key (`X-API-Key` header or `api_key` query
parameter); unset means open. The key only guards `/api/v1` — the UI's internal `/api/*`
routes below are unauthenticated, so only expose `/api/v1/` publicly.

## Internal API Endpoints (used by the UI; may change)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/paths` | List watched paths |
| POST | `/api/paths` | Add a watched path `{"path": "..."}` |
| DELETE | `/api/paths/{id}` | Remove a watched path |
| POST | `/api/scan` | Scan all paths and index new files |
| GET | `/api/files` | List files (with filters, pagination) |
| GET | `/api/files/{id}` | Single file detail |
| GET | `/api/files/{id}/preview` | Stretched JPEG for zooming / comparing (≤ `PREVIEW_MAX_PX`) |
| GET | `/api/files/{id}/pixels` | Full-resolution size for the 100% viewer (reads + caches the stretched frame) |
| GET | `/api/files/{id}/tile?x=&y=&w=&h=` | Lossless PNG of native pixels, for pixel peeping |
| GET | `/api/stats` | Summary stats and distinct filter values |
| GET | `/thumbnails/{filename}` | Serve thumbnail image |

### `/api/files` query parameters

| Param | Description |
|-------|-------------|
| `object` | Object name (partial match) |
| `filter` | Filter name (exact) |
| `imagetyp` | Image type (exact) |
| `telescop` | Telescope name (exact) |
| `instrume` | Instrument name (exact) |
| `date_from` | Date range start `YYYY-MM-DD` |
| `date_to` | Date range end `YYYY-MM-DD` |
| `focal_min` | Min focal length (mm) |
| `focal_max` | Max focal length (mm) |
| `exptime_min` | Min exposure (seconds) |
| `exptime_max` | Max exposure (seconds) |
| `page` | Page number (default 1) |
| `per_page` | Items per page (default 50, max 200) |

---

## File Structure

```
fitsql/
├── backend/
│   ├── main.py          # FastAPI app + all routes
│   ├── database.py      # SQLAlchemy models
│   ├── scanner.py       # FITS indexing, thumbnail generation, star detection
│   ├── requirements.txt
│   ├── fitsql.db        # Created on first run
│   └── thumbnails/      # Generated JPEG thumbnails
└── frontend/
    ├── index.html
    ├── package.json
    ├── vite.config.js
    └── src/
        ├── main.jsx
        ├── App.jsx
        ├── App.css
        ├── pages/
        │   ├── CatalogPage.jsx
        │   └── SettingsPage.jsx
        └── components/
            ├── api.js
            ├── FileCard.jsx
            ├── FileDetail.jsx
            └── FilterPanel.jsx
```

---

## Supported FITS Header Fields

| Field | FITS Keys Tried |
|-------|----------------|
| Object | `OBJECT` |
| Image Type | `IMAGETYP`, `FRAME`, `FRAMETYPE` |
| Date-Obs | `DATE-OBS`, `DATE_OBS`, `DATE` |
| Exposure | `EXPTIME`, `EXPOSURE`, `EXP_TIME` |
| Focal Length | `FOCALLEN`, `FOCAL`, `FOCAL_LENGTH`, `TELFOCAL` |
| Telescope | `TELESCOP`, `TELESCOPE` |
| Instrument | `INSTRUME`, `INSTRUMENT`, `CAMERA` |
| Filter | `FILTER`, `FILTER1` |
| RA | `RA`, `RA_DEG`, `CRVAL1` |
| Dec | `DEC`, `DEC_DEG`, `CRVAL2` |
| Gain | `GAIN`, `EGAIN`, `GAIN1` |
| Offset | `OFFSET`, `PEDESTAL`, `BLKLEVEL` |
| CCD Temp | `CCD-TEMP`, `CCD_TEMP`, `CCDTEMP`, `SET-TEMP` |
| X Binning | `XBINNING`, `BINX`, `HBIN` |
| Y Binning | `YBINNING`, `BINY`, `VBIN` |
