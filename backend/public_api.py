"""
Public read-only API, mounted at /api/v1 with its own OpenAPI schema and docs
(/api/v1/docs, /api/v1/redoc, /api/v1/openapi.json).

Only GET routes live here, so nothing reachable through this app can change the
catalog. Shared query helpers are borrowed from `main` lazily (main mounts this
app, so importing it at module load would be circular).
"""
import hmac
import json
import math
import os
import typing
from datetime import datetime, timezone
from functools import lru_cache
from typing import Optional

import numpy as np
from fastapi import Depends, FastAPI, Path, Query, Request, Security
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import APIKeyHeader, APIKeyQuery
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, defer
from starlette.exceptions import HTTPException as StarletteHTTPException

import config
import expression as expr_engine
import public_schemas as S
from database import FitsFile, get_config, get_db


def _core():
    import main
    return main


DESCRIPTION = """
Read-only access to the astrophotography catalog: every indexed FITS or XISF frame with its
capture metadata, plate solution, star-quality and screening metrics, observing conditions,
full header, JPEG thumbnail / zoomable preview and analysis diagnostics, plus per-target /
per-night integration rollups and calibration coverage.

## Authentication
When the server sets `PUBLIC_API_KEYS`, every data endpoint requires a key, sent as
the `X-API-Key` header (preferred) or the `api_key` query parameter (handy for
`<img src>` thumbnails). Without `PUBLIC_API_KEYS` the API is open. These docs and
`openapi.json` never require a key.

## Choosing fields
Frames are grouped: `file`, `capture`, `pointing`, `plate_solve`, `position`, `quality`,
`conditions`, `status`, `thumbnail`, `links` — all returned by default — plus two opt-in
groups: `header` (every FITS card) and `sequence` (each frame screened against the rest of
its imaging session for clouds, veils and stray light).

* `fields=capture,quality.hfr_px,thumbnail.url` — return only these groups / leaves (`id` is always included).
* `expand=header,sequence` — add opt-in groups to whatever `fields` selected.
* `fields=id,header.EXPTIME,header.CCD-TEMP` — pick individual FITS keywords.
* `fields=file.filename,sequence.anomaly,sequence.transparency` — pick session-screening values.
* `GET /fields` lists every selectable path with its type, description and sortability.

A field that is returned as `null` is unknown / not measured for that frame.

## Images and diagnostics
`GET /frames/{id}/thumbnail` is a small auto-stretched JPEG (≤512 px). `GET /frames/{id}/preview`
renders a larger one (up to the server's preview limit) from the source file on first request
and caches it — slow the first time, and it needs the file to be online. `GET /frames/{id}/diagnostics`
returns what the analysis measured: every sampled star with its classification (clean,
saturated, edge, blended) plus the screening grid and satellite trails.

## Raw colour (OSC) frames
Frames with a Bayer pattern are measured on 2×2 CFA superpixels and reported in sensor pixels.
Their eccentricity and the moment-based FWHM of small stars read somewhat high compared with
mono sensors; compare OSC with mono on `quality.hfr_px` or `quality.psf_fwhm_px`.

## Filtering
Simple filters are query parameters on `GET /frames` (all combine with AND; list-valued
ones accept comma-separated values). For anything more, `where` takes the catalog's
selection expression language, e.g.

```
hfr < median(hfr) + 2*mad(hfr) && eccentricity < 0.6 && path not like '%reject%'
date between '2026-05-01' and '2026-05-31 06:00' && moonillum < 0.5
```

Variables: `hfr, fwhm, fwhm_arcsec, psf_fwhm, psf_fwhm_arcsec, psf_beta, eccentricity, trail_score,
empty_cells, bg_spread, bg_dip, star_flux, streaks, stars,
quality, background, exptime, focal, pixel_scale, ra, dec, acquisition_ra/dec, astap_ra/dec,
coord_sep, fov, fov_w, fov_h, width, height, solved, wcs_scale, wcs_rotation, altitude, azimuth,
airmass, moonsep, moonillum, moonalt, ccd_temp, set_temp, temp_delta, date, hour` · sequence (each frame
vs. the other frames of its target/filter/rig/exposure session): `star_drop, hfr_rise, empty_rise, spread_rise,
flux_drop, dip_rise, transparency` · strings: `object, filter, imagetyp, telescop, instrume, bayer, path, filename` ·
booleans: `light, dark, flat, bias, osc, mono, rejected, accepted, good_tracking, trailed, occluded,
seq_anomaly, seq_ok, satellite, shadow, off_setpoint` (metric-based booleans are false until a frame is analyzed).
Functions: `median, mean, std, mad, madstd, percentile(x,q), min, max, abs, sqrt, log,
log10, isnan/missing/present, like(x,pat), between(x,lo,hi), contains(ra,dec),
angsep(ra,dec)`. Operators: `&& || ! < <= > >= == != + - * / %`, `x [not] like '…'`,
`x [not] between a and b`. Aggregates and the sequence variables are computed over the frames
matching the other filters. Dates are UTC and compare at the precision written
(`date == '2026-05-13'` is the whole day).

## Pagination & sorting
`page` (1-based) and `per_page` (≤ 500). Responses carry `total`, `pages` and ready-made
`links.next` / `links.prev`. `sort` takes comma-separated field paths, `-` for descending
(`sort=-capture.date_obs,file.filename`); nulls sort last and `id` breaks ties, so
pagination is stable. For incremental sync, poll with `indexed_since=<last run>` and
`sort=file.indexed_at`.

## Errors
Errors share one shape — `{"error": {"code", "message", "details"}}` — with codes such as
`unauthorized` (401), `not_found`, `not_analyzed` and `source_unavailable` (404),
`invalid_parameter` (422, or 400 for invalid values), `invalid_field`, `invalid_sort` and
`invalid_expression` (400), and `render_failed` (500).

## Stability
`/api/v1` only ever gains fields and endpoints; nothing is renamed or removed within v1.
Ignore fields you do not recognise.
"""

TAGS = [
    {"name": "Frames", "description": "Indexed FITS frames, their metadata and thumbnails."},
    {"name": "Targets", "description": "Integration rollups per OBJECT header and per user-defined sky position."},
    {"name": "Sessions", "description": "Integration grouped by object and night."},
    {"name": "Calibration", "description": "Which light setups have matching darks, flats and bias."},
    {"name": "Catalog", "description": "Catalog-wide summary, saved Selector queries and the field catalog."},
]


# ── Errors ────────────────────────────────────────────────────────────────────
class ApiException(Exception):
    def __init__(self, status: int, code: str, message: str, details: Optional[dict] = None):
        self.status, self.code, self.message, self.details = status, code, message, details


def _error(status, code, message, details=None, headers=None):
    body = {"error": {"code": code, "message": message, "details": details}}
    return JSONResponse(body, status_code=status, headers=headers)


_ERR = {"model": S.ErrorResponse}
COMMON_RESPONSES = {401: {**_ERR, "description": "Missing or invalid API key"},
                    422: {**_ERR, "description": "Invalid query parameter"}}


# ── Auth ──────────────────────────────────────────────────────────────────────
_key_header = APIKeyHeader(name="X-API-Key", auto_error=False,
                           description="API key (when the server sets PUBLIC_API_KEYS).")
_key_query = APIKeyQuery(name="api_key", auto_error=False,
                         description="API key as a query parameter, for clients that cannot set headers (e.g. <img>).")


def require_api_key(header_key: Optional[str] = Security(_key_header),
                    query_key: Optional[str] = Security(_key_query)):
    keys = config.PUBLIC_API_KEYS
    if not keys:
        return
    supplied = header_key or query_key or ""
    if not any(hmac.compare_digest(supplied.encode(), k.encode()) for k in keys):
        raise ApiException(401, "unauthorized",
                           "Missing or invalid API key — send the X-API-Key header or the api_key query parameter.")


public_app = FastAPI(
    title="fitsql — Public API",
    version="1.1.0",
    description=DESCRIPTION,
    openapi_tags=TAGS,
    dependencies=[Depends(require_api_key)],
    responses=COMMON_RESPONSES,
)


@public_app.exception_handler(ApiException)
async def _api_exception(request, exc: ApiException):
    headers = {"WWW-Authenticate": "ApiKey"} if exc.status == 401 else None
    return _error(exc.status, exc.code, exc.message, exc.details, headers)


@public_app.exception_handler(RequestValidationError)
async def _validation_exception(request, exc: RequestValidationError):
    errors = [{"loc": list(e.get("loc", [])), "message": e.get("msg"), "type": e.get("type")} for e in exc.errors()]
    return _error(422, "invalid_parameter", "One or more parameters are invalid.", {"errors": errors})


@public_app.exception_handler(StarletteHTTPException)
async def _http_exception(request, exc: StarletteHTTPException):
    codes = {404: "not_found", 405: "method_not_allowed"}
    return _error(exc.status_code, codes.get(exc.status_code, "http_error"), str(exc.detail))


# ── Frame representation ──────────────────────────────────────────────────────
def _api_base(request: Request) -> str:
    """Absolute URL of this API's root (scheme://host/…/api/v1)."""
    return f"{request.url.scheme}://{request.url.netloc}{request.scope.get('root_path', '')}".rstrip("/")


def _iso_utc(dt: Optional[datetime]) -> Optional[str]:
    return dt.replace(microsecond=0).isoformat() + "Z" if dt else None   # stored naive UTC


def _epoch_iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _bool(v):
    return None if v is None else bool(v)


@lru_cache(maxsize=50_000)
def _image_size(path: str, mtime_ns: int):
    from PIL import Image
    with Image.open(path) as im:       # reads the JPEG header only
        return im.size


def _thumb_file(f) -> Optional[str]:
    if not f.thumbnail:
        return None
    p = os.path.join(str(config.THUMBNAILS_DIR), os.path.basename(f.thumbnail))
    return p if os.path.isfile(p) else None


_NO_SEQUENCE = {name: None for name in S.SequenceInfo.model_fields} | {"judged": False}


def _finite(v):
    return float(v) if v is not None and np.isfinite(v) else None


def _session_key(f):
    """Session identity for sequence screening — main._sequence_vars' key plus the image type."""
    return (f.imagetyp or "", (f.object or "").lower(), (f.filter or "").lower(), f.instrume, f.telescop,
            round(float(f.exptime), 1) if f.exptime is not None else None)


def sequence_for(db: Session, frames) -> dict:
    """frame id -> sequence group, each frame screened against every indexed frame of its session."""
    wanted = {_session_key(f) for f in frames}
    if not wanted:
        return {}
    core = _core()
    peers = (db.query(FitsFile.id, FitsFile.imagetyp, FitsFile.object, FitsFile.filter, FitsFile.instrume,
                      FitsFile.telescop, FitsFile.exptime, FitsFile.date_obs, FitsFile.star_count,
                      FitsFile.median_hfr, FitsFile.empty_cells, FitsFile.bg_spread, FitsFile.star_flux,
                      FitsFile.bg_dip)
             .filter(FitsFile.index_error.is_(None))
             .filter(func.coalesce(FitsFile.imagetyp, "").in_({k[0] for k in wanted}))
             .filter(func.lower(func.coalesce(FitsFile.object, "")).in_({k[1] for k in wanted}))
             .all())
    by_type = {}
    for r in peers:
        if _session_key(r) in wanted:
            by_type.setdefault(r.imagetyp or "", []).append(r)

    out = {}
    for rows in by_type.values():      # main's session key has no image type: screen each type apart
        def arr(attr):
            return np.array([np.nan if getattr(r, attr) is None else getattr(r, attr) for r in rows], dtype=float)
        variables = {"object": [(r.object or "").lower() for r in rows],
                     "filter": [(r.filter or "").lower() for r in rows],
                     "stars": arr("star_count"), "hfr": arr("median_hfr"), "empty_cells": arr("empty_cells"),
                     "bg_spread": arr("bg_spread"), "star_flux": arr("star_flux"), "bg_dip": arr("bg_dip")}
        dt = np.array([core._obs_datetime(r.date_obs) for r in rows], dtype="datetime64[ms]")
        seq = core._sequence_vars(rows, dt, variables)
        for i, r in enumerate(rows):
            judged = bool(seq["seq_ok"][i] or seq["seq_anomaly"][i])
            out[r.id] = {"judged": judged, "anomaly": bool(seq["seq_anomaly"][i]) if judged else None,
                         **{k: _finite(seq[k][i]) for k in ("star_drop", "hfr_rise", "empty_rise", "spread_rise",
                                                            "flux_drop", "dip_rise", "transparency")}}
    return out


class _Ctx:
    def __init__(self, request: Request, db: Session, selection: dict):
        self.base = _api_base(request)
        self.sel = selection
        self.map_src = get_config(db, "export_map_from") or config.EXPORT_MAP_FROM
        self.map_dst = get_config(db, "export_map_to") or config.EXPORT_MAP_TO
        self.sequence = {}

    def wants(self, group, leaf):
        leaves = self.sel.get(group, set())
        return leaves is None or leaf in leaves

    def load_sequence(self, db, frames):
        if "sequence" in self.sel:
            self.sequence = sequence_for(db, frames)
        return self


_FORMATS = {".fit": "fits", ".fits": "fits", ".fts": "fits", ".xisf": "xisf"}


def _g_file(f, ctx):
    return {"path": f.path, "host_path": _core()._rewrite_path(f.path, ctx.map_src, ctx.map_dst),
            "filename": f.filename, "format": _FORMATS.get(os.path.splitext(f.path or "")[1].lower()),
            "size_bytes": f.file_size, "modified_at": _epoch_iso(f.file_mtime),
            "indexed_at": _iso_utc(f.indexed_at), "watched_path_id": f.watched_path_id}


def _g_capture(f, ctx):
    return {"object": f.object, "image_type": f.imagetyp, "date_obs": f.date_obs, "exposure_s": f.exptime,
            "filter": f.filter, "telescope": f.telescop, "camera": f.instrume, "focal_length_mm": f.focal_length,
            "gain": f.gain, "offset": f.offset, "sensor_temp_c": f.ccd_temp, "sensor_setpoint_c": f.set_temp,
            "binning_x": f.xbinning,
            "binning_y": f.ybinning, "width_px": f.naxis1, "height_px": f.naxis2, "bayer_pattern": f.bayer,
            "color": "osc" if f.bayer else "mono"}


def _g_pointing(f, ctx):
    return {"ra_deg": f.ra, "dec_deg": f.dec}


def _g_plate_solve(f, ctx):
    return {"solved": _bool(f.solved), "ra_deg": f.wcs_ra, "dec_deg": f.wcs_dec, "pixel_scale_arcsec": f.wcs_scale,
            "rotation_deg": f.wcs_rotation, "fov_width_deg": f.fov_w, "fov_height_deg": f.fov_h,
            "offset_from_pointing_arcmin": f.coord_sep_arcmin}


def _g_position(f, ctx):
    solved = f.wcs_ra is not None and f.wcs_dec is not None
    header = f.ra is not None and f.dec is not None
    scale = f.wcs_scale if f.wcs_scale is not None else f.pixel_scale

    def fov(stored, npx):
        if stored:
            return stored
        return npx * scale / 3600.0 if npx and scale else None

    return {"ra_deg": f.wcs_ra if solved else f.ra, "dec_deg": f.wcs_dec if solved else f.dec,
            "source": "plate_solve" if solved else ("header" if header else None),
            "pixel_scale_arcsec": scale, "fov_width_deg": fov(f.fov_w, f.naxis1),
            "fov_height_deg": fov(f.fov_h, f.naxis2)}


def _g_quality(f, ctx):
    trailed = None if f.trail_score is None else f.trail_score > config.TRAIL_THRESHOLD
    occluded = None if f.empty_cells is None else f.empty_cells > config.OCCLUDED_THRESHOLD
    # Same effective scale as the selector's psf_fwhm_arcsec: plate solve first, then header
    scale = f.wcs_scale if f.wcs_scale is not None else f.pixel_scale
    return {"star_count": f.star_count, "hfr_px": f.median_hfr, "fwhm_px": f.median_fwhm,
            "fwhm_arcsec": f.fwhm_arcsec, "eccentricity": f.eccentricity, "trail_score": f.trail_score,
            "trailed": trailed, "empty_cells": f.empty_cells, "occluded": occluded,
            "background_spread_sigma": f.bg_spread, "background_dip": f.bg_dip,
            "shadow": None if f.bg_dip is None else f.bg_dip > config.SHADOW_THRESHOLD, "psf_fwhm_px": f.psf_fwhm,
            "psf_fwhm_arcsec": f.psf_fwhm * scale if f.psf_fwhm is not None and scale is not None else None,
            "psf_beta": f.psf_beta, "star_flux_adu": f.star_flux,
            "streak_count": f.streaks, "satellite": None if f.streaks is None else f.streaks > 0,
            "background_adu": f.background_median,
            "pixel_scale_arcsec": f.pixel_scale, "score": f.quality_score}


def _g_conditions(f, ctx):
    return {"altitude_deg": f.altitude, "azimuth_deg": f.azimuth, "airmass": f.airmass,
            "moon_separation_deg": f.moon_sep, "moon_illumination": f.moon_illum, "moon_altitude_deg": f.moon_alt}


def _g_status(f, ctx):
    return {"rejected": f.rejected == 1, "grade": {1: "rejected", 0: "accepted"}.get(f.rejected),
            "index_error": f.index_error}


def _g_thumbnail(f, ctx):
    path = _thumb_file(f)
    out = {"available": path is not None, "url": f"{ctx.base}/frames/{f.id}/thumbnail" if path else None,
           "content_type": "image/jpeg" if path else None, "width_px": None, "height_px": None}
    if path and (ctx.wants("thumbnail", "width_px") or ctx.wants("thumbnail", "height_px")):
        try:
            out["width_px"], out["height_px"] = _image_size(path, os.stat(path).st_mtime_ns)
        except OSError:
            pass
    return out


def _g_links(f, ctx):
    return {"self": f"{ctx.base}/frames/{f.id}", "preview": f"{ctx.base}/frames/{f.id}/preview",
            "diagnostics": f"{ctx.base}/frames/{f.id}/diagnostics"}


_BUILDERS = {"file": _g_file, "capture": _g_capture, "pointing": _g_pointing, "plate_solve": _g_plate_solve,
             "position": _g_position, "quality": _g_quality, "conditions": _g_conditions, "status": _g_status,
             "thumbnail": _g_thumbnail, "links": _g_links}


def frame_dict(f, ctx: _Ctx) -> dict:
    out = {"id": f.id}
    for group, build in _BUILDERS.items():
        if group in ctx.sel:
            full, leaves = build(f, ctx), ctx.sel[group]
            out[group] = full if leaves is None else {k: v for k, v in full.items() if k in leaves}
    if "sequence" in ctx.sel:
        full, leaves = ctx.sequence.get(f.id, _NO_SEQUENCE), ctx.sel["sequence"]
        out["sequence"] = full if leaves is None else {k: v for k, v in full.items() if k in leaves}
    if "header" in ctx.sel:
        try:
            hdr = json.loads(f.header_json) if f.header_json else None
        except ValueError:
            hdr = None
        keys = ctx.sel["header"]
        if hdr is not None:
            # NaN / Infinity cards would make the response invalid JSON
            hdr = {k: (None if isinstance(v, float) and not math.isfinite(v) else v)
                   for k, v in hdr.items() if keys is None or k.upper() in keys}
        out["header"] = hdr
    return out


# ── Field selection / sorting ─────────────────────────────────────────────────
def parse_selection(fields: Optional[str], expand: Optional[str]) -> dict:
    """group -> None (whole group) or set of leaf names (header: set of FITS keywords)."""
    bad = []
    if fields is None or not fields.strip():
        sel = {g: None for g in S.FRAME_GROUPS}
    else:
        sel = {}
        for tok in (t.strip() for t in fields.split(",")):
            if not tok or tok == "id":
                continue
            group, _, leaf = tok.partition(".")
            model = S.FRAME_GROUPS.get(group) or S.HEAVY_GROUPS.get(group)
            if group == "header":
                leaf = leaf.upper()
            elif model is None or (leaf and leaf not in model.model_fields):
                bad.append(tok)
                continue
            if not leaf:
                sel[group] = None
            elif sel.get(group, set()) is not None:
                sel.setdefault(group, set()).add(leaf)
    for tok in (t.strip() for t in (expand or "").split(",")):
        if not tok:
            continue
        if tok in S.HEAVY_GROUPS:
            sel[tok] = None
        else:
            bad.append(f"expand={tok}")
    if bad:
        raise ApiException(400, "invalid_field", f"Unknown field(s): {', '.join(bad)}. See GET /fields.",
                           {"unknown": bad, "groups": list(S.FRAME_GROUPS) + sorted(S.HEAVY_GROUPS)})
    return sel


SORTABLE = {
    "id": FitsFile.id,
    "file.path": FitsFile.path, "file.filename": FitsFile.filename, "file.size_bytes": FitsFile.file_size,
    "file.modified_at": FitsFile.file_mtime, "file.indexed_at": FitsFile.indexed_at,
    "capture.object": FitsFile.object, "capture.image_type": FitsFile.imagetyp, "capture.date_obs": FitsFile.date_obs,
    "capture.exposure_s": FitsFile.exptime, "capture.filter": FitsFile.filter, "capture.telescope": FitsFile.telescop,
    "capture.camera": FitsFile.instrume, "capture.focal_length_mm": FitsFile.focal_length, "capture.gain": FitsFile.gain,
    "capture.sensor_temp_c": FitsFile.ccd_temp, "capture.sensor_setpoint_c": FitsFile.set_temp,
    "pointing.ra_deg": FitsFile.ra, "pointing.dec_deg": FitsFile.dec,
    "plate_solve.ra_deg": FitsFile.wcs_ra, "plate_solve.dec_deg": FitsFile.wcs_dec,
    "plate_solve.pixel_scale_arcsec": FitsFile.wcs_scale, "plate_solve.rotation_deg": FitsFile.wcs_rotation,
    "plate_solve.fov_width_deg": FitsFile.fov_w, "plate_solve.fov_height_deg": FitsFile.fov_h,
    "plate_solve.offset_from_pointing_arcmin": FitsFile.coord_sep_arcmin,
    "quality.star_count": FitsFile.star_count, "quality.hfr_px": FitsFile.median_hfr,
    "quality.fwhm_px": FitsFile.median_fwhm, "quality.fwhm_arcsec": FitsFile.fwhm_arcsec,
    "quality.eccentricity": FitsFile.eccentricity, "quality.trail_score": FitsFile.trail_score,
    "quality.empty_cells": FitsFile.empty_cells, "quality.background_spread_sigma": FitsFile.bg_spread,
    "quality.background_dip": FitsFile.bg_dip,
    "quality.psf_fwhm_px": FitsFile.psf_fwhm, "quality.psf_beta": FitsFile.psf_beta,
    "quality.star_flux_adu": FitsFile.star_flux, "quality.streak_count": FitsFile.streaks,
    "quality.background_adu": FitsFile.background_median,
    "quality.pixel_scale_arcsec": FitsFile.pixel_scale, "quality.score": FitsFile.quality_score,
    "conditions.altitude_deg": FitsFile.altitude, "conditions.azimuth_deg": FitsFile.azimuth,
    "conditions.airmass": FitsFile.airmass, "conditions.moon_separation_deg": FitsFile.moon_sep,
    "conditions.moon_illumination": FitsFile.moon_illum, "conditions.moon_altitude_deg": FitsFile.moon_alt,
}


def parse_sort(sort: Optional[str]):
    order, bad = [], []
    for tok in (t.strip() for t in (sort or "-capture.date_obs").split(",")):
        if not tok:
            continue
        desc = tok.startswith("-")
        col = SORTABLE.get(tok.lstrip("-+"))
        if col is None:
            bad.append(tok)
            continue
        order += [col.is_(None).asc(), col.desc() if desc else col.asc()]   # nulls last
    if bad:
        raise ApiException(400, "invalid_sort", f"Unsortable field(s): {', '.join(bad)}.",
                           {"unknown": bad, "sortable": sorted(SORTABLE)})
    return order + [FitsFile.id.asc()]


def _type_name(ann) -> str:
    args = [a for a in typing.get_args(ann) if a is not type(None)]
    if typing.get_origin(ann) is typing.Literal:
        return "string (" + " | ".join(map(str, typing.get_args(ann))) + ")"
    if args and typing.get_origin(ann) is typing.Union:
        return _type_name(args[0])
    return {str: "string", int: "integer", float: "number", bool: "boolean"}.get(ann, "object")


def field_catalog() -> dict:
    fields = [{"path": "id", "type": "integer", "description": "Stable frame id. Always returned.",
               "sortable": True, "default": True}]
    for group, model in S.FRAME_GROUPS.items():
        for name, info in model.model_fields.items():
            path = f"{group}.{name}"
            fields.append({"path": path, "type": _type_name(info.annotation), "description": info.description,
                           "sortable": path in SORTABLE, "default": True})
    fields.append({"path": "sequence", "type": "object", "sortable": False, "default": False,
                   "description": S.Frame.model_fields["sequence"].description})
    for name, info in S.SequenceInfo.model_fields.items():
        fields.append({"path": f"sequence.{name}", "type": _type_name(info.annotation), "description": info.description,
                       "sortable": False, "default": False})
    fields.append({"path": "header", "type": "object", "sortable": False, "default": False,
                   "description": S.Frame.model_fields["header"].description})
    fields.append({"path": "header.<KEYWORD>", "type": "string | number | boolean", "sortable": False,
                   "default": False, "description": "A single FITS keyword, e.g. header.EXPTIME or header.CCD-TEMP."})
    return {"groups": list(S.FRAME_GROUPS) + sorted(S.HEAVY_GROUPS), "fields": fields}


# ── Routes: Frames ────────────────────────────────────────────────────────────
FIELDS_DOC = ("Comma-separated groups and/or `group.leaf` paths to return (default: every group except `header`). "
              "`id` is always returned. See `GET /fields`.")
EXPAND_DOC = ("Opt-in groups to add on top of `fields`, comma-separated: `header` (all FITS cards), "
              "`sequence` (session screening).")
GRADES = {"accepted", "rejected", "unmarked"}


def _csv(v: Optional[str]):
    return [x.strip() for x in v.split(",") if x.strip()] if v else []


def _in_or_eq(q, col, value):
    vals = _csv(value)
    return q.filter(col.in_(vals)) if len(vals) > 1 else (q.filter(col == vals[0]) if vals else q)


@public_app.get("/frames", response_model=S.FramePage, response_model_exclude_unset=True, tags=["Frames"],
                summary="Search frames", responses={400: {**_ERR, "description": "Invalid field, sort or expression"}})
def list_frames(
    request: Request,
    fields: Optional[str] = Query(None, description=FIELDS_DOC, examples=["capture,quality.hfr_px,thumbnail.url"]),
    expand: Optional[str] = Query(None, description=EXPAND_DOC, examples=["header"]),
    sort: Optional[str] = Query(None, description="Comma-separated sortable paths, `-` prefix = descending. "
                                "Default `-capture.date_obs`.", examples=["-quality.score,capture.date_obs"]),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=500),
    ids: Optional[str] = Query(None, description="Only these frame ids (comma-separated).", examples=["12,15,99"]),
    object: Optional[str] = Query(None, description="OBJECT contains this text (case-insensitive).", examples=["M 31"]),
    image_type: Optional[str] = Query(None, description="Exact normalized IMAGETYP; comma-separated for several.",
                                      examples=["Light Frame"]),
    filter: Optional[str] = Query(None, description="Exact filter name(s), comma-separated.", examples=["Ha,OIII"]),
    telescope: Optional[str] = Query(None, description="Exact TELESCOP value(s), comma-separated."),
    camera: Optional[str] = Query(None, description="Exact INSTRUME value(s), comma-separated."),
    color: Optional[typing.Literal["osc", "mono"]] = Query(None),
    date_from: Optional[str] = Query(None, description="Capture time ≥ this (UTC). `YYYY-MM-DD` or `YYYY-MM-DD HH:MM[:SS]`.",
                                     examples=["2026-05-01"]),
    date_to: Optional[str] = Query(None, description="Capture time ≤ this (UTC), inclusive at the precision given "
                                   "(a date includes that whole day).", examples=["2026-05-31"]),
    exposure_min: Optional[float] = Query(None, description="Seconds."),
    exposure_max: Optional[float] = Query(None, description="Seconds."),
    focal_length_min: Optional[float] = Query(None, description="Millimetres."),
    focal_length_max: Optional[float] = Query(None, description="Millimetres."),
    hfr_max: Optional[float] = Query(None, description="Pixels."),
    fwhm_max: Optional[float] = Query(None, description="Pixels."),
    eccentricity_max: Optional[float] = Query(None),
    trail_score_max: Optional[float] = Query(None, ge=0, le=1, description="0–1, see `quality.trail_score`."),
    good_tracking: Optional[bool] = Query(None, description="true = tracking judged good (trail score at or below the "
                                          "server threshold), false = trailed. Frames not yet analyzed match neither."),
    psf_fwhm_max: Optional[float] = Query(None, description="Moffat PSF FWHM, pixels."),
    empty_cells_max: Optional[float] = Query(None, ge=0, le=1, description="0–1, see `quality.empty_cells`."),
    occluded: Optional[bool] = Query(None, description="true = part of the field blocked (`quality.occluded`), false = "
                                     "clear. Frames whose grid wasn't judged match neither."),
    satellite: Optional[bool] = Query(None, description="true = at least one satellite / aircraft streak, false = none. "
                                      "Frames not yet analyzed match neither."),
    background_dip_max: Optional[float] = Query(None, ge=0, le=1, description="0–1, see `quality.background_dip`."),
    shadow: Optional[bool] = Query(None, description="true = a frost / dew shadow in the background (`quality.shadow`), "
                                   "false = none. Frames not yet analyzed match neither."),
    stars_min: Optional[int] = Query(None),
    quality_min: Optional[float] = Query(None, description="0–100."),
    solved: Optional[bool] = Query(None, description="Only plate-solved (true) or unsolved (false) frames."),
    rejected: Optional[bool] = Query(None, description="Only manually rejected (true) or not rejected (false) frames."),
    grade: Optional[str] = Query(None, description="User grade(s), comma-separated: `accepted`, `rejected`, `unmarked`.",
                                 examples=["accepted,unmarked"]),
    file_format: Optional[typing.Literal["fits", "xisf"]] = Query(None, alias="format",
                                                                 description="Only FITS or only XISF files."),
    path_like: Optional[str] = Query(None, description="Path matches this SQL LIKE pattern (`%` wildcard, case-insensitive, "
                                     "`\\` and `/` equivalent). Text without `%` means *contains*.", examples=["%/2026-05-%"]),
    path_not_like: Optional[str] = Query(None, description="Exclude paths matching this pattern (same rules).",
                                         examples=["reject"]),
    watched_path_id: Optional[int] = Query(None, description="Only frames under this library root."),
    indexed_since: Optional[datetime] = Query(None, description="Only frames (re)indexed at or after this time "
                                              "(ISO 8601; naive = UTC). For incremental sync.",
                                              examples=["2026-05-14T00:00:00Z"]),
    near_ra: Optional[float] = Query(None, ge=0, le=360, description="Cone search centre RA, degrees (with near_dec, radius_deg)."),
    near_dec: Optional[float] = Query(None, ge=-90, le=90, description="Cone search centre Dec, degrees."),
    radius_deg: Optional[float] = Query(None, gt=0, le=180, description="Cone search radius around the frame centre, degrees."),
    covers_ra: Optional[float] = Query(None, ge=0, le=360, description="Only frames whose field of view contains this RA/Dec "
                                       "(rotation-aware; with covers_dec)."),
    covers_dec: Optional[float] = Query(None, ge=-90, le=90),
    where: Optional[str] = Query(None, description="Selection expression evaluated after the other filters (see API "
                                 "description).", examples=["hfr < median(hfr) + 2*mad(hfr) && path not like '%reject%'"]),
    indexing: typing.Literal["ok", "failed", "all"] = Query("ok", description="`ok` = indexed frames, `failed` = files "
                                                            "that could not be read (see status.index_error)."),
    db: Session = Depends(get_db),
):
    core = _core()
    sel = parse_selection(fields, expand)
    order = parse_sort(sort)

    q = db.query(FitsFile)
    if "header" not in sel:
        q = q.options(defer(FitsFile.header_json))
    if indexing == "ok":
        q = q.filter(FitsFile.index_error.is_(None))
    elif indexing == "failed":
        q = q.filter(FitsFile.index_error.isnot(None))
    if ids:
        try:
            q = q.filter(FitsFile.id.in_([int(i) for i in _csv(ids)]))
        except ValueError:
            raise ApiException(400, "invalid_parameter", "`ids` must be comma-separated integers.")
    if object:
        q = q.filter(FitsFile.object.ilike(f"%{object}%"))
    q = _in_or_eq(q, FitsFile.imagetyp, image_type)
    q = _in_or_eq(q, FitsFile.filter, filter)
    q = _in_or_eq(q, FitsFile.telescop, telescope)
    q = _in_or_eq(q, FitsFile.instrume, camera)
    if color == "osc":
        q = q.filter(FitsFile.bayer.isnot(None), FitsFile.bayer != "")
    elif color == "mono":
        q = q.filter((FitsFile.bayer.is_(None)) | (FitsFile.bayer == ""))
    if date_from:
        q = q.filter(FitsFile.date_obs >= core._date_bound(date_from))
    if date_to:
        q = q.filter(FitsFile.date_obs <= core._date_bound(date_to, end=True))
    for col, lo, hi in ((FitsFile.exptime, exposure_min, exposure_max),
                        (FitsFile.focal_length, focal_length_min, focal_length_max),
                        (FitsFile.median_hfr, None, hfr_max), (FitsFile.median_fwhm, None, fwhm_max),
                        (FitsFile.eccentricity, None, eccentricity_max), (FitsFile.trail_score, None, trail_score_max),
                        (FitsFile.psf_fwhm, None, psf_fwhm_max), (FitsFile.empty_cells, None, empty_cells_max),
                        (FitsFile.bg_dip, None, background_dip_max),
                        (FitsFile.star_count, stars_min, None),
                        (FitsFile.quality_score, quality_min, None)):
        if lo is not None:
            q = q.filter(col >= lo)
        if hi is not None:
            q = q.filter(col <= hi)
    if good_tracking is not None:
        q = q.filter(FitsFile.trail_score <= config.TRAIL_THRESHOLD if good_tracking
                     else FitsFile.trail_score > config.TRAIL_THRESHOLD)
    # NULL metrics fail both comparisons, so unanalyzed frames match neither true nor false
    if occluded is not None:
        q = q.filter(FitsFile.empty_cells > config.OCCLUDED_THRESHOLD if occluded
                     else FitsFile.empty_cells <= config.OCCLUDED_THRESHOLD)
    if satellite is not None:
        q = q.filter(FitsFile.streaks > 0 if satellite else FitsFile.streaks == 0)
    if shadow is not None:
        q = q.filter(FitsFile.bg_dip > config.SHADOW_THRESHOLD if shadow
                     else FitsFile.bg_dip <= config.SHADOW_THRESHOLD)
    if grade:
        grades = set(_csv(grade))
        if not grades or grades - GRADES:
            raise ApiException(400, "invalid_parameter", "`grade` takes accepted, rejected and/or unmarked.",
                               {"unknown": sorted(grades - GRADES)})
        q = q.filter(or_(*[{"accepted": FitsFile.rejected == 0, "rejected": FitsFile.rejected == 1,
                            "unmarked": FitsFile.rejected.is_(None)}[g] for g in sorted(grades)]))
    if file_format:
        exts = [e for e, fmt in _FORMATS.items() if fmt == file_format]
        q = q.filter(or_(*[FitsFile.path.ilike(f"%{e}") for e in exts]))
    if solved is not None:
        q = q.filter(FitsFile.solved == 1) if solved else q.filter((FitsFile.solved.is_(None)) | (FitsFile.solved != 1))
    if rejected is not None:
        q = q.filter(FitsFile.rejected == 1) if rejected else q.filter((FitsFile.rejected.is_(None)) | (FitsFile.rejected != 1))
    if path_like and path_like.strip():
        q = q.filter(core._norm_path_col().ilike(core._path_pattern(path_like)))
    if path_not_like and path_not_like.strip():
        q = q.filter(~core._norm_path_col().ilike(core._path_pattern(path_not_like)))
    if watched_path_id is not None:
        q = q.filter(FitsFile.watched_path_id == watched_path_id)
    if indexed_since is not None:
        if indexed_since.tzinfo is not None:
            indexed_since = indexed_since.astimezone(timezone.utc).replace(tzinfo=None)
        q = q.filter(FitsFile.indexed_at >= indexed_since)

    # Spatial filters and `where` run through the expression engine (in memory)
    clauses = [f"({where})"] if where and where.strip() else []
    if any(v is not None for v in (near_ra, near_dec, radius_deg)):
        if None in (near_ra, near_dec, radius_deg):
            raise ApiException(400, "invalid_parameter", "Cone search needs near_ra, near_dec and radius_deg together.")
        clauses.append(f"angsep({near_ra!r}, {near_dec!r}) <= {radius_deg!r}")
    if (covers_ra is None) != (covers_dec is None):
        raise ApiException(400, "invalid_parameter", "covers_ra and covers_dec must be given together.")
    if covers_ra is not None:
        clauses.append(f"contains({covers_ra!r}, {covers_dec!r})")

    q = q.order_by(*order)
    offset = (page - 1) * per_page
    if clauses:
        rows = q.all()
        try:
            mask = core._evaluate_candidates(rows, " && ".join(clauses))
        except expr_engine.ExpressionError as e:
            raise ApiException(400, "invalid_expression", f"Expression error: {e}")
        except Exception as e:                       # e.g. comparing a string column to a number
            raise ApiException(400, "invalid_expression", f"Expression could not be evaluated: {e}")
        rows = [r for r, ok in zip(rows, mask) if ok]
        total, items = len(rows), rows[offset:offset + per_page]
    else:
        total = q.count()
        items = q.offset(offset).limit(per_page).all()

    ctx = _Ctx(request, db, sel).load_sequence(db, items)
    pages = max(1, -(-total // per_page))
    links = {"self": str(request.url),
             "next": str(request.url.include_query_params(page=page + 1)) if page < pages else None,
             "prev": str(request.url.include_query_params(page=page - 1)) if page > 1 else None}
    return S.FramePage.model_validate({
        "items": [frame_dict(f, ctx) for f in items],
        "total": total, "page": page, "per_page": per_page, "pages": pages, "links": links,
    })


def _get_frame_or_404(db, frame_id, with_header=False):
    q = db.query(FitsFile)
    if not with_header:
        q = q.options(defer(FitsFile.header_json))
    f = q.filter(FitsFile.id == frame_id).first()
    if not f:
        raise ApiException(404, "not_found", f"Frame {frame_id} not found.")
    return f


@public_app.get("/frames/{frame_id}", response_model=S.Frame, response_model_exclude_unset=True, tags=["Frames"],
                summary="Get one frame", responses={404: _ERR, 400: _ERR})
def get_frame(
    request: Request,
    frame_id: int = Path(..., description="Frame id."),
    fields: Optional[str] = Query(None, description=FIELDS_DOC),
    expand: Optional[str] = Query(None, description=EXPAND_DOC, examples=["header"]),
    db: Session = Depends(get_db),
):
    sel = parse_selection(fields, expand)
    f = _get_frame_or_404(db, frame_id, with_header="header" in sel)
    return S.Frame.model_validate(frame_dict(f, _Ctx(request, db, sel).load_sequence(db, [f])))


@public_app.get("/frames/{frame_id}/thumbnail", tags=["Frames"], summary="Get a frame's JPEG thumbnail",
                response_class=FileResponse,
                responses={200: {"content": {"image/jpeg": {}}, "description": "Auto-stretched JPEG preview (≤512 px)."},
                           404: {**_ERR, "description": "Frame not found or it has no thumbnail"}})
def get_thumbnail(frame_id: int = Path(..., description="Frame id."), db: Session = Depends(get_db)):
    path = _thumb_file(_get_frame_or_404(db, frame_id))
    if not path:
        raise ApiException(404, "not_found", f"Frame {frame_id} has no thumbnail.")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


@public_app.get("/frames/{frame_id}/preview", tags=["Frames"], summary="Get a large JPEG preview for zooming",
                response_class=FileResponse,
                description="Auto-stretched JPEG up to `size` pixels on the long side (capped by the server's preview "
                            "limit, 2048 px by default). Rendered from the source file on first request, then cached, "
                            "so the first call can take a few seconds and needs the file to be online.",
                responses={200: {"content": {"image/jpeg": {}}, "description": "Stretched JPEG preview."},
                           404: {**_ERR, "description": "Frame not found (`not_found`) or its file is offline "
                                                        "(`source_unavailable`)"},
                           500: {**_ERR, "description": "The file could not be rendered (`render_failed`)"}})
def get_preview(frame_id: int = Path(..., description="Frame id."),
                size: int = Query(2048, ge=256, le=8192, description="Longest side, pixels."),
                db: Session = Depends(get_db)):
    core = _core()
    try:
        path = core.cached_preview(frame_id, size, db)
    except core.PreviewError as e:
        message = f"Frame {frame_id} not found." if e.code == "not_found" else f"Frame {frame_id}: {e.message}."
        raise ApiException(e.status, e.code, message)
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@public_app.get("/frames/{frame_id}/diagnostics", response_model=S.FrameDiagnostics, tags=["Frames"],
                summary="Get a frame's analysis diagnostics",
                description="Per-star classification and the screening grid recorded when the frame was analyzed — the "
                            "data behind `quality` (and the catalog UI's diagnostics overlay). Coordinates are original "
                            "frame pixels, so they overlay the preview after scaling.",
                responses={404: {**_ERR, "description": "Frame not found (`not_found`) or not analyzed yet "
                                                        "(`not_analyzed`)"}})
def get_frame_diagnostics(frame_id: int = Path(..., description="Frame id."), db: Session = Depends(get_db)):
    row = db.query(FitsFile.diagnostics_json).filter(FitsFile.id == frame_id).first()
    if row is None:
        raise ApiException(404, "not_found", f"Frame {frame_id} not found.")
    if not row[0]:
        raise ApiException(404, "not_analyzed", f"Frame {frame_id} has no diagnostics yet — it needs to be (re-)analyzed.")
    return json.loads(row[0])


@public_app.get("/fields", response_model=S.FieldCatalog, tags=["Catalog"], summary="List selectable frame fields")
def list_fields():
    return field_catalog()


# ── Routes: Targets / sessions ────────────────────────────────────────────────
@public_app.get("/targets", response_model=S.TargetList, tags=["Targets"], summary="Integration per OBJECT header",
                description="One rollup per OBJECT value over indexed light frames, sorted by integration time (desc).")
def list_targets(db: Session = Depends(get_db)):
    return _core().list_targets(db)


@public_app.get("/targets/{object_name}", response_model=S.TargetDetail, tags=["Targets"],
                summary="Per-night breakdown for one OBJECT", responses={404: _ERR})
def get_target(object_name: str = Path(..., description="Exact OBJECT value (URL-encoded).", examples=["M 31"]),
               db: Session = Depends(get_db)):
    detail = _core().target_detail(object_name, db)
    if not detail["frames"]:
        raise ApiException(404, "not_found", f"No light frames with OBJECT '{object_name}'.")
    return detail


@public_app.get("/user-targets", response_model=S.UserTargetList, response_model_exclude_unset=True, tags=["Targets"],
                summary="User-defined targets",
                description="Targets defined in the catalog UI. Frames match spatially (field of view covers the position), "
                            "so one frame can count toward several targets.")
def list_user_targets(rollup: bool = Query(True, description="Include the matched-frame rollup (slower on big catalogs)."),
                      db: Session = Depends(get_db)):
    return _core().list_user_targets(rollup=rollup, db=db)


@public_app.get("/user-targets/{target_id}", response_model=S.UserTargetDetail, tags=["Targets"],
                summary="Per-night breakdown for a user-defined target", responses={404: _ERR})
def get_user_target(target_id: int = Path(...), db: Session = Depends(get_db)):
    return _core().user_target_detail(target_id, db)


@public_app.get("/sessions", response_model=S.SessionList, tags=["Sessions"], summary="Imaging sessions",
                description="Light frames grouped by object and night (UTC date of DATE-OBS), newest first.")
def list_sessions(db: Session = Depends(get_db)):
    return _core().list_sessions(db)


@public_app.get("/calibration", response_model=S.CalibrationReport, tags=["Calibration"],
                summary="Calibration coverage",
                description="Groups light frames into setups (camera, binning, gain, offset, exposure, filter, sensor "
                            "temperature) and counts the darks, flats and bias in the catalog that match each — the "
                            "same matching WBPP does. Setups with missing or stale calibration come first.")
def calibration_coverage(db: Session = Depends(get_db)):
    return _core().calibration_coverage(db)


# ── Routes: Catalog ───────────────────────────────────────────────────────────
@public_app.get("/catalog", response_model=S.CatalogSummary, tags=["Catalog"], summary="Catalog summary")
def catalog_summary(db: Session = Depends(get_db)):
    return _core().get_stats(db)


def _saved_or_404(db, query_id):
    from database import SavedQuery
    row = db.query(SavedQuery).filter(SavedQuery.id == query_id).first()
    if not row:
        raise ApiException(404, "not_found", f"Saved query {query_id} not found.")
    return _core()._saved_to_dict(row)


@public_app.get("/queries", response_model=list[S.SavedQueryInfo], tags=["Catalog"], summary="Saved Selector queries")
def list_saved_queries(db: Session = Depends(get_db)):
    return _core().list_queries(db)


@public_app.get("/queries/{query_id}", response_model=S.SavedQueryInfo, tags=["Catalog"],
                summary="Get a saved Selector query", responses={404: _ERR})
def get_saved_query(query_id: int = Path(...), db: Session = Depends(get_db)):
    return _saved_or_404(db, query_id)


@public_app.get("/queries/{query_id}/frames", response_model=S.FramePage, response_model_exclude_unset=True,
                tags=["Catalog", "Frames"], summary="Frames approved by a saved query",
                description="Runs a saved Selector query (base filters + expression) exactly as the UI does and returns "
                            "the approved frames. Manually rejected frames are left out unless `include_rejected=true`, "
                            "matching the Selector's exports.",
                responses={404: _ERR, 400: _ERR})
def saved_query_frames(
    request: Request,
    query_id: int = Path(...),
    fields: Optional[str] = Query(None, description=FIELDS_DOC),
    expand: Optional[str] = Query(None, description=EXPAND_DOC),
    sort: Optional[str] = Query(None, description="As on GET /frames."),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=500),
    include_rejected: bool = Query(False),
    db: Session = Depends(get_db),
):
    core = _core()
    state = _saved_or_404(db, query_id)["query"]
    sel = parse_selection(fields, expand)
    order = parse_sort(sort)
    body = core.QueryRequest(
        expression=state.get("expression") or "",
        imagetyp=state.get("imagetyp", "Light Frame") or None,
        **{k: state.get(k) or None for k in ("object", "filter", "telescop", "instrume", "date_from", "date_to")},
    )
    q = core._candidate_query(db, body).filter(FitsFile.index_error.is_(None))
    if "header" not in sel:
        q = q.options(defer(FitsFile.header_json))
    rows = q.order_by(*order).all()
    try:
        mask = core._evaluate_candidates(rows, body.expression)
    except expr_engine.ExpressionError as e:
        raise ApiException(400, "invalid_expression", f"Saved expression error: {e}")
    rows = [r for r, ok in zip(rows, mask) if ok and (include_rejected or not r.rejected)]

    total, pages = len(rows), max(1, -(-len(rows) // per_page))
    start = (page - 1) * per_page
    page_rows = rows[start:start + per_page]
    ctx = _Ctx(request, db, sel).load_sequence(db, page_rows)
    return S.FramePage.model_validate({
        "items": [frame_dict(f, ctx) for f in page_rows],
        "total": total, "page": page, "per_page": per_page, "pages": pages,
        "links": {"self": str(request.url),
                  "next": str(request.url.include_query_params(page=page + 1)) if page < pages else None,
                  "prev": str(request.url.include_query_params(page=page - 1)) if page > 1 else None},
    })
